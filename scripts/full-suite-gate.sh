#!/usr/bin/env bash
# full-suite-gate.sh — the pre-merge gate, run the way CI runs it, with the
# exit code of every stage written down.
#
# Canonical form of the suite invocations this repo's agents typed by hand,
# measured from the session transcripts on 2026-09-09 (54,325 Bash calls):
#   python -m pytest 4146 · `pytest ... | tail` 2828 (masks the exit code — the
#   form AGENTS.md bans, and it outnumbered the correct `> log; echo exit=$?`
#   form 1001 nearly 3:1) · ruff check 926 · mypy 756 · setsid nohup 373 ·
#   .done marker 1137 · elspeth-lints --rules all 242 · -m testcontainer -n 0 116
#
# DRY-RUN BY DEFAULT: prints the preflight facts and the exact commands, runs
# nothing. Both execution forms supervise the foreground driver, including
# execution preflight. --detach starts a supervisor-owning launcher in its own
# session and returns once launched; that return is not a gate result.
#
# Stages (--stages, comma-separated; default: ruff,pytest):
#   ruff          ruff check + ruff format --check over src/ tests/ scripts/ examples/
#                 elspeth-lints/src/   (CI "Static analysis", pre-push hygiene)
#   mypy          mypy src/ elspeth-lints/src/
#   contracts     scripts/check_contracts.py + generate_skill_inventory.py --check
#   lints         elspeth-lints check --rules all --root src/elspeth, keyless
#                 (shape-only verification). The corpus is EXPECTED to be non-zero
#                 while signing is suspended (AGENTS.md), so complete, valid
#                 finding corpora are advisory unless --lints-strict. Invalid
#                 output or a failed scan makes the gate incomplete/nonzero.
#                 Compare keyless finding sets before and after a change, not counts
#                 or zero; this stage does not establish signature clearance.
#   pytest        pytest tests/ — the default selection = CI's "Test" job. The
#                 marker expression comes from pyproject addopts; this script never
#                 passes -m for it.
#   testcontainer pytest tests/ -m testcontainer -n 0 — CI's required PostgreSQL job.
#                 Needs Docker; serial by design. Run it if you touched schema, SQL,
#                 session/Landscape persistence, or a lock. Score against the known
#                 flake list, not against zero.
#
# What the preflight enforces (each line is a past incident):
#   - refuses when ELSPETH_JUDGE_METADATA_HMAC_KEY is in the environment ([O1]):
#     18 ids fail on every tree and lint verification silently becomes full HMAC
#   - refuses when the tree has no .venv (subprocess tests fail as assertion errors)
#   - proves the tree under test: elspeth.__file__ and elspeth_lints.__file__ must
#     resolve inside this tree. Both `-o pythonpath=` (in-process; the ini setting
#     beats PYTHONPATH) AND an exported PYTHONPATH (for spawned interpreters) are
#     set, because each alone leaves a class of test importing the MAIN checkout
#   - with workers omitted, counts sibling pytest processes from /proc and
#     selects 8 if any match, otherwise 12; explicit --workers (including 0)
#     bypasses sibling observation entirely (caller-established capacity)
#   - records HEAD + a hash of the working-tree state before the run, samples
#     it every 5 s while each stage runs, and checks it after: a run during
#     which the tree moved is NOT evidence and is marked frozen=NO even if the
#     change was reverted before the end (a change shorter than one sampling
#     interval can still slip through; the digest covers tracked content and
#     untracked NAMES, not ignored files)
#
# Output: one directory per run under --log-dir (default
# $ELSPETH_GATE_LOG_DIR or $ROOT/.claude/lanes/full-suite-gates), containing
#   preflight.txt · <stage>.log · <stage>-<ordinal>.exit (observed stage rc)
#   lints.json (raw JSON stdout; lints.log retains stderr/validation errors)
#   pytest-junit.xml · summary.txt · driver.exit · .done (last, driver terminal
#   receipt only; read stage exit and frozen status before qualifying a run)
#   supervisor.json · supervisor.exit (actual supervisor CLI exit, published
#   only after it exits) · supervisor.pid · supervisor.start-stat · launcher.pid
#   launcher.start-stat · launcher.exit (cancellation-aware outer completion)
#   supervisor.argv.json (literal CLI/driver argv, deadline and grace)
# The supervisor owns ordinary descendants in the driver's process group;
# deliberate session/group escapes and external resources are outside it.
# Its heartbeat is liveness, not test progress. An outer failure cannot qualify
# a driver's PASS/.done. Require launcher.exit and supervisor.exit together;
# a missing terminal receipt is incomplete evidence.
# Nothing is ever piped through tail; read summary.txt, then the logs.
#
# Usage:
#   scripts/full-suite-gate.sh                                   # plan only
#   scripts/full-suite-gate.sh --execute                         # foreground
#   scripts/full-suite-gate.sh --execute --detach                # background; prints the wait loop
#   scripts/full-suite-gate.sh --execute --stages ruff,mypy,contracts,lints,pytest
#   scripts/full-suite-gate.sh --execute --stages testcontainer
#   scripts/full-suite-gate.sh --execute --root .claude/worktrees/<name> --log-dir "$S/gate"
#
# Options:
#   --execute            run the stages (default: print the plan)
#   --detach             with --execute: run under setsid nohup and return immediately
#   --stages LIST        see above (default: ruff,pytest)
#   --workers N          explicit pytest workers; no sibling observation (0 = serial)
#                        omitted: automatic discovery selects 12, or 8 with siblings
#   --root DIR           tree to gate (default: the git toplevel of the cwd)
#   --log-dir DIR        parent directory for run directories
#   --lints-strict       make a non-zero elspeth-lints exit fatal
#   --pytest-args "..."  extra arguments appended to the pytest stage
#   --run-timeout-seconds N  finite positive aggregate deadline (default 10800)
#                       separate from per-item pytest timeout; cleanup grace 10 s
#   -h, --help
#
# Exit codes:  0 fatal stages passed, tree frozen and outer cleanup complete
#              1 a fatal stage failed, a scan was incomplete, or the tree moved
#              2 preflight refused, or usage error
#              124 aggregate deadline; 125 abandoned/incomplete group cleanup
#              128+signal cancellation (or a signalled supervisor CLI)
#              (with --detach the parent exits 0 once launch is accepted;
#               terminal outer status is launcher.exit, not summary alone)

set -euo pipefail

EXECUTE=0; DETACH=0; STAGES="ruff,pytest"; WORKERS=""; ROOT=""; LOG_DIR=""; LINTS_STRICT=0; PYTEST_ARGS=""; RUN_DIR=""
DRIVER_ONLY=0; LAUNCHER_ONLY=0; RUN_TIMEOUT="10800"
usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }
# Absolute path to this script: --detach re-executes it AFTER `cd "$ROOT"`, and a
# relative $0 then points at a file the target tree may not have (it did not).
SELF="$(cd "$(dirname "$0")" && pwd -P)/$(basename "$0")"
while [ $# -gt 0 ]; do
    case "$1" in
        --execute) EXECUTE=1 ;;
        --detach) DETACH=1 ;;
        --stages) STAGES="${2:?}"; shift ;;
        --workers) WORKERS="${2:?}"; shift ;;
        --root) ROOT="${2:?}"; shift ;;
        --log-dir) LOG_DIR="${2:?}"; shift ;;
        --lints-strict) LINTS_STRICT=1 ;;
        --pytest-args) PYTEST_ARGS="${2:?}"; shift ;;
        --run-timeout-seconds) RUN_TIMEOUT="${2:?}"; shift ;;
        --run-dir) RUN_DIR="${2:?}"; shift ;;      # internal: the detached child reuses the parent's dir
        --driver-only) DRIVER_ONLY=1 ;;            # internal recursion control, not a security boundary
        --launcher-only) LAUNCHER_ONLY=1 ;;        # internal detached owner, never the driver
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac; shift
done

# --- resolve the tree --------------------------------------------------------
if [ -z "$ROOT" ]; then ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "not in a git tree; pass --root" >&2; exit 2; }; fi
ROOT="$(cd "$ROOT" && pwd -P)"
cd "$ROOT"
[ -f pyproject.toml ] && [ -d src/elspeth ] || { echo "$ROOT is not an ELSPETH tree" >&2; exit 2; }
PY="$ROOT/.venv/bin/python"
TREE_NAME="$(basename "$ROOT")"
LOG_DIR="${LOG_DIR:-${ELSPETH_GATE_LOG_DIR:-$ROOT/.claude/lanes/full-suite-gates}}"

# Argument validation is isolated from project imports. The supervisor starts
# before the execution driver's provenance imports and source-state sampling.
if [ ! -x "$PY" ]; then echo "REFUSED: no executable $PY" >&2; exit 2; fi
if ! "$PY" -I -S -B -c 'import math, sys; n = float(sys.argv[1]); sys.exit(0 if math.isfinite(n) and n > 0 else 2)' "$RUN_TIMEOUT"; then
    echo "--run-timeout-seconds must be finite and greater than zero" >&2
    exit 2
fi

IFS=',' read -r -a STAGE_LIST <<<"$STAGES"
for s in "${STAGE_LIST[@]}"; do
    case "$s" in ruff|mypy|contracts|lints|pytest|testcontainer) ;; *) echo "unknown stage: $s" >&2; exit 2 ;; esac
done
has_stage() { local s; for s in "${STAGE_LIST[@]}"; do [ "$s" = "$1" ] && return 0; done; return 1; }

if [ "$EXECUTE" = 1 ] && [ "$DRIVER_ONLY" = 0 ]; then
    if [ -n "${ELSPETH_JUDGE_METADATA_HMAC_KEY:-}" ]; then
        echo "REFUSED: ELSPETH_JUDGE_METADATA_HMAC_KEY is set ([O1])" >&2
        exit 2
    fi
    if [ "$LAUNCHER_ONLY" = 0 ]; then
        [ -z "$RUN_DIR" ] || { echo "--run-dir is internal" >&2; exit 2; }
        RUN_DIR="$LOG_DIR/$(date -u +%Y%m%dT%H%M%SZ)-$TREE_NAME-$$"
        mkdir -p "$RUN_DIR"
        RUN_DIR="$(cd "$RUN_DIR" && pwd -P)"
    else
        [ -n "$RUN_DIR" ] && [ "$DETACH" = 0 ] || { echo "invalid launcher re-entry" >&2; exit 2; }
    fi
    DRIVER_ARGS=(--execute --root "$ROOT" --run-dir "$RUN_DIR"
        --stages "$STAGES" --log-dir "$LOG_DIR" --run-timeout-seconds "$RUN_TIMEOUT")
    [ -z "$WORKERS" ] || DRIVER_ARGS+=(--workers "$WORKERS")
    [ "$LINTS_STRICT" = 0 ] || DRIVER_ARGS+=(--lints-strict)
    [ -z "$PYTEST_ARGS" ] || DRIVER_ARGS+=(--pytest-args "$PYTEST_ARGS")
    if [ "$DETACH" = 1 ]; then
        setsid nohup "$SELF" --launcher-only "${DRIVER_ARGS[@]}" >"$RUN_DIR/driver.log" 2>&1 </dev/null &
        echo "detached launcher requested: $!; actual owner: $RUN_DIR/launcher.pid"
        echo "run_dir=$RUN_DIR"
        echo "wait for launcher.exit, then inspect supervisor.exit, supervisor.json, summary.txt and driver.exit"
        echo "cancel ONLY verified supervisor.pid (identity in supervisor.start-stat) with TERM"
        echo "driver .done is driver-only; absent terminal receipts after owner exit mean INCOMPLETE"
        exit 0
    fi
    echo "run_dir=$RUN_DIR"
    echo "aggregate deadline=$RUN_TIMEOUT seconds; cleanup grace=10 seconds"
    echo "cancel ONLY verified $RUN_DIR/supervisor.pid (supervisor.start-stat) with TERM"
    echo "terminal receipt: $RUN_DIR/launcher.exit; also inspect supervisor.exit; driver .done is driver-only"
    # This glue owns only CLI identity, signal forwarding, wait and publication.
    # All deadlines, process-group signalling and reaping remain in the existing
    # supervisor. A separate session prevents terminal signals reaching it twice.
    exec "$PY" -I -S -B -c '
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

run_dir = Path(sys.argv[1])
deadline, supervisor_script, python = sys.argv[2:5]
command = [python, supervisor_script, "--nodeid", "full-suite-gate:" + run_dir.name,
           "--timeout-seconds", deadline, "--grace-seconds", "10",
           "--report", str(run_dir / "supervisor.json"), "--", *sys.argv[5:]]
child = None
pending = None
forwarded = False
publishing = False

def forward():
    global forwarded
    if not forwarded and pending is not None and child is not None and child.poll() is None:
        forwarded = True
        child.send_signal(pending)

def cancel(signum, _frame):
    global pending
    if pending is None:
        pending = signum
    if publishing:
        # Actual helper status remains honest; accepted outer cancellation
        # cannot leave a qualifying launcher-success receipt behind.
        (run_dir / "launcher.exit").unlink(missing_ok=True)
        raise SystemExit(128 + pending)
    forward()

signal.signal(signal.SIGTERM, cancel)
signal.signal(signal.SIGINT, cancel)
try:
    (run_dir / "launcher.pid").write_text(str(os.getpid()) + "\n")
    launcher_stat = Path("/proc/self/stat").read_text()
    (run_dir / "launcher.start-stat").write_text(launcher_stat)
    environment = os.environ.copy()
    environment["_ELSPETH_GATE_LAUNCHER_IDENTITY"] = json.dumps(
        {"pid": os.getpid(), "start": launcher_stat.rpartition(")")[2].split()[19]})
    (run_dir / "supervisor.argv.json").write_text(json.dumps(command) + "\n")
    child = subprocess.Popen(command, start_new_session=True, env=environment)
    (run_dir / "supervisor.pid").write_text(str(child.pid) + "\n")
    (run_dir / "pid").write_text(str(child.pid) + "\n")
    try:
        (run_dir / "supervisor.start-stat").write_text(Path(f"/proc/{child.pid}/stat").read_text())
    except FileNotFoundError:
        pass
    forward()
    result = child.wait()
    status = result if result >= 0 else 128 - result
    # CLI finalization has finished; its JSON alone is not this terminal signal.
    publishing = True
    launcher_status = status if pending is None else 128 + pending
    for name, value in (("supervisor.exit", status), ("launcher.exit", launcher_status)):
        temporary = run_dir / ("." + name + ".pending")
        with temporary.open("x") as stream:
            stream.write(str(value) + "\n")
        os.replace(temporary, run_dir / name)
except BaseException:
    if child is not None and child.poll() is None:
        child.send_signal(signal.SIGTERM)
        child.wait()
    raise
raise SystemExit(launcher_status)
' "$RUN_DIR" "$RUN_TIMEOUT" "$(dirname "$SELF")/ci_proof_progress.py" "$PY" "$SELF" --driver-only "${DRIVER_ARGS[@]}"
fi

[ "$DRIVER_ONLY" = 0 ] || { [ "$EXECUTE" = 1 ] && [ "$DETACH" = 0 ] && [ -n "$RUN_DIR" ]; } || {
    echo "invalid driver re-entry" >&2; exit 2;
}

# --- preflight ---------------------------------------------------------------
REFUSE=0
say() { printf '%s\n' "$*"; }
PRE=""
pre() { PRE+="$*"$'\n'; say "$*"; }

pre "tree        : $ROOT"
pre "HEAD        : $(git rev-parse HEAD)  ($(git branch --show-current || true))"
pre "stages      : $STAGES"
pre "log dir     : $LOG_DIR"
pre "run timeout : $RUN_TIMEOUT seconds; supervisor cleanup grace 10 seconds"

if [ -n "${ELSPETH_JUDGE_METADATA_HMAC_KEY:-}" ]; then
    pre "REFUSED     : ELSPETH_JUDGE_METADATA_HMAC_KEY is set in this shell ([O1]); results here are not the keyless corpus"
    REFUSE=1
fi
if [ ! -x "$PY" ]; then
    pre "REFUSED     : no $PY — link the venv: scripts/worktree-cleanup.sh --link-venv --path '$ROOT'"
    REFUSE=1
else
    export PYTHONPATH="$ROOT/src:$ROOT/elspeth-lints/src"
    if PROV="$("$PY" -B -c 'import elspeth, elspeth_lints; print(elspeth.__file__); print(elspeth_lints.__file__)' 2>&1)"; then
        E_FILE="$(sed -n 1p <<<"$PROV")"; L_FILE="$(sed -n 2p <<<"$PROV")"
        if [[ "$E_FILE" == "$ROOT/src/"* && "$L_FILE" == "$ROOT/elspeth-lints/src/"* ]]; then
            pre "provenance  : elspeth -> $E_FILE"
            pre "              elspeth_lints -> $L_FILE"
        else
            pre "REFUSED     : imports resolve outside this tree: $(tr '\n' ' ' <<<"$PROV")"
            REFUSE=1
        fi
    else
        PROV_STATUS=$?
        pre "REFUSED     : provenance interpreter exited $PROV_STATUS: $(tr '\n' ' ' <<<"$PROV")"
        REFUSE=1
    fi
fi

# Explicit capacity bypasses the WHOLE inventory branch, including wrapper
# exclusions and fixture-input validation. Zero is an explicit serial choice.
SIBLINGS_OBSERVED=0
if [ -n "$WORKERS" ]; then
    pre "capacity    : explicit workers=$WORKERS; caller-established capacity"
    pre "siblings    : unobserved (explicit workers; no sibling discovery)"
else
    SIBLING_ROOT=/proc
    SIBLING_SOURCE="host /proc"
    if [ "${_ELSPETH_GATE_TEST_SIBLING_ROOT+x}" = x ]; then
        # Internal fixture seam for sibling inventory ONLY. It never redirects
        # self/parent identities, supervisor ownership, signals or cleanup.
        SIBLING_ROOT="$_ELSPETH_GATE_TEST_SIBLING_ROOT"
        case "$SIBLING_ROOT" in
            "$ROOT/.pytest_cache/"*) ;;
            *) pre "REFUSED     : invalid sibling fixture root; expected private project cache data"; exit 2 ;;
        esac
        if [ -z "$SIBLING_ROOT" ] || [ -L "$SIBLING_ROOT" ] || [ ! -d "$SIBLING_ROOT" ]; then
            pre "REFUSED     : invalid sibling fixture root"
            exit 2
        fi
        if ! SIBLING_REAL="$(cd "$SIBLING_ROOT" && pwd -P)" || [ "$SIBLING_REAL" != "$SIBLING_ROOT" ]; then
            pre "REFUSED     : sibling fixture root must be a canonical directory"
            exit 2
        fi
        SIBLING_SOURCE="fixture $SIBLING_ROOT"
    fi
    # Sibling suites: python/pytest interpreters whose argv mentions pytest.
    # Shells merely quoting pytest are not counted. Exact owned wrappers are
    # excluded; missing cmdline data retains the disappearing-process skip.
    OWNED_WRAPPERS="$("$PY" -I -S -B -c '
import json, os, sys
def process(pid):
    with open(f"/proc/{pid}/stat") as stream:
        fields = stream.read().rpartition(")")[2].split()
    return int(fields[1]), fields[19]
try:
    identity = json.loads(os.environ.get("_ELSPETH_GATE_LAUNCHER_IDENTITY", "null"))
    if identity is not None:
        supervisor, _ = process(int(sys.argv[1]))
        launcher, _ = process(supervisor)
        if launcher == identity["pid"] and process(launcher)[1] == identity["start"]:
            print(launcher, supervisor)
except (OSError, ValueError, KeyError, TypeError):
    pass
' "$$")"
    SIBLINGS=""
    for d in "$SIBLING_ROOT"/[0-9]*; do
        pid="${d##*/}"; [ "$pid" = "$$" ] && continue
        if [ "$SIBLING_SOURCE" != "host /proc" ]; then
            [ -e "$d" ] || [ -L "$d" ] || continue
            if [ -L "$d" ] || [ ! -d "$d" ] || [ -L "$d/cmdline" ] ||
               { [ -e "$d/cmdline" ] && [ ! -f "$d/cmdline" ]; }; then
                pre "REFUSED     : sibling fixture entries must be ordinary private data"
                exit 2
            fi
        fi
        case " $OWNED_WRAPPERS " in *" $pid "*) continue ;; esac
        cmd="$(cat "$d/cmdline" 2>/dev/null | tr '\0' ' ' || true)"
        [ -n "$cmd" ] || continue
        argv0="$(basename -- "${cmd%% *}")"   # -- : a login shell's argv[0] is "-bash"
        case "$argv0" in python*|pytest) ;; *) continue ;; esac
        case "$cmd" in *pytest*) SIBLINGS+="  pid $pid cwd $(readlink "$d/cwd" 2>/dev/null || echo '?') :: ${cmd:0:90}"$'\n' ;; esac
    done
    N_SIB=0; [ -n "$SIBLINGS" ] && N_SIB="$(grep -c . <<<"$SIBLINGS")"
    if [ "$N_SIB" -gt 0 ]; then WORKERS=8; else WORKERS=12; fi
    SIBLINGS_OBSERVED=1
    pre "capacity    : automatic workers=$WORKERS"
    pre "siblings    : $N_SIB pytest process(es) observed from $SIBLING_SOURCE -> workers=$WORKERS"
    [ -n "$SIBLINGS" ] && pre "$SIBLINGS"
fi

if has_stage testcontainer; then
    if docker info >/dev/null 2>&1; then pre "docker      : reachable"; else pre "REFUSED     : testcontainer stage requested but docker is not reachable"; REFUSE=1; fi
    if [ "$SIBLINGS_OBSERVED" = 0 ]; then
        pre "WARNING     : siblings unobserved; caller must establish serial/exclusive testcontainer capacity"
    elif [ "$N_SIB" -gt 0 ]; then
        pre "WARNING     : a testcontainer run beside another suite records contention as failures; serialise on the box first"
    fi
fi

tree_state() {
    # HEAD plus a digest of the working-tree state: every status line (tracked
    # changes, untracked files listed one by one) and the sha256 of each such
    # file's current content. Ignored files are not covered. No `git diff` is
    # used: `--no-optional-locks` stops `git status` from rewriting the index
    # but does NOT stop `git diff`, which refreshes the stat cache on every call
    # (measured on git 2.43: 10/10 rewrites), and this runs every 5 s inside a
    # tree other lanes may be working in. A path containing a newline or a
    # double quote is only covered by its status line.
    local st head file_hashes digest row code path hash_line
    st="$(git --no-optional-locks status --porcelain -uall)" || return 1
    head="$(git rev-parse HEAD)" || return 1
    file_hashes=""
    if [ -n "$st" ]; then
        while IFS= read -r row; do
            [ -n "$row" ] || continue
            code="${row:0:2}"
            path="$(awk '{print $NF}' <<<"$row")" || return 1
            # These exact porcelain states describe a tracked path deleted
            # from the worktree. Its status line is evidence; no file remains
            # to hash. Every other missing path remains a failed sample.
            case "$code" in
                'D '|' D'|'MD')
                    [ ! -e "$path" ] && [ ! -L "$path" ] || return 1
                    continue ;;
            esac
            hash_line="$(sha256sum -- "$path")" || return 1
            file_hashes+="$hash_line"$'\n'
        done <<<"$st"
    fi
    digest="$( { printf '%s\n' "$st"; printf '%s' "$file_hashes"; } | sha256sum)" || return 1
    printf '%s %s\n' "$head" "${digest:0:16}"
}
if BEFORE="$(tree_state)"; then
    pre "tree state  : $BEFORE  (HEAD + sha256 of status+diff; must match after the run)"
else
    BEFORE="UNKNOWN"
    pre "REFUSED     : initial tree-state sample failed; source freeze cannot be proved"
    REFUSE=1
fi
if DIRTY_N="$(git --no-optional-locks status --porcelain | wc -l)"; then
    [ "$DIRTY_N" -gt 0 ] && pre "note        : $DIRTY_N uncommitted path(s); the measurement is of the WORKING TREE, not of HEAD"
else
    pre "REFUSED     : working-tree status failed during preflight"
    REFUSE=1
fi

# --- the commands ------------------------------------------------------------
RUFF_PATHS=(src/ tests/ scripts/ examples/ elspeth-lints/src/)
declare -A CMD_OF=()
CMD_OF[ruff]="$ROOT/.venv/bin/ruff check ${RUFF_PATHS[*]} && $ROOT/.venv/bin/ruff format --check ${RUFF_PATHS[*]}"
CMD_OF[mypy]="$ROOT/.venv/bin/mypy src/ elspeth-lints/src/"
CMD_OF[contracts]="$PY scripts/check_contracts.py && $PY scripts/cicd/generate_skill_inventory.py --check"
CMD_OF[lints]="ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing $PY -m elspeth_lints.core.cli check --rules all --root src/elspeth --format json"
CMD_OF[pytest]="$PY -m pytest tests/ -n $WORKERS -o 'pythonpath=$ROOT/src $ROOT/elspeth-lints/src' -v --junitxml=<run-dir>/pytest-junit.xml $PYTEST_ARGS"
CMD_OF[testcontainer]="$PY -m pytest tests/ -m testcontainer -n 0 -o 'pythonpath=$ROOT/src $ROOT/elspeth-lints/src' -v --junitxml=<run-dir>/testcontainer-junit.xml"

say
say "commands (PYTHONPATH=$ROOT/src:$ROOT/elspeth-lints/src exported for every stage):"
for s in "${STAGE_LIST[@]}"; do say "  [$s] ${CMD_OF[$s]}"; done
say

if [ "$REFUSE" = 1 ]; then say "preflight REFUSED; nothing run"; exit 2; fi
if [ "$EXECUTE" = 0 ]; then
    say "dry-run: nothing run. Re-run with --execute (add --detach from a shell that times out)."
    exit 0
fi

# --- run directory -----------------------------------------------------------
if [ -z "$RUN_DIR" ]; then
    RUN_DIR="$LOG_DIR/$(date -u +%Y%m%dT%H%M%SZ)-$TREE_NAME-$$"
    mkdir -p "$RUN_DIR"
fi
printf '%s' "$PRE" >"$RUN_DIR/preflight.txt"
SUMMARY="$RUN_DIR/summary.txt"

# --- execute -----------------------------------------------------------------
note() { printf '%s\n' "$*" | tee -a "$SUMMARY"; }
RESULT=0
MONITOR_FAILED=0
LINTS_INCOMPLETE=0
STAGE_SEQ=0
CURRENT_STAGE="none"
CURRENT_PHASE="setup"
OBSERVED_STAGE_RC="UNKNOWN"
TERMINAL_REASON="EXIT"
CURRENT_WATCH_PID=""
CURRENT_WATCH_STOP=""
CURRENT_MONITOR="UNKNOWN"

record_cleanup_pending() {
    # A nonterminal observation before any watcher wait. It is not a child
    # completion, a clean freeze, or a substitute for driver.exit.
    printf 'cleanup_pending=1 phase=%s stage=%s stage_exit=%s watcher_pid=%s\n' \
        "$CURRENT_PHASE" "$CURRENT_STAGE" "$OBSERVED_STAGE_RC" "$CURRENT_WATCH_PID" \
        >"$RUN_DIR/driver.cleanup-pending" || true
}

record_driver_exit() {
    local observed_rc="$1" driver_rc="$1" line summary_ok=0 marker_ok=0 watch_rc
    trap - EXIT ERR INT TERM
    record_cleanup_pending
    # Cooperative stop only: a shell-only signal during a nested source scan
    # would not prove that its descendants were joined.
    if [ -n "$CURRENT_WATCH_PID" ]; then
        if ! : >"$CURRENT_WATCH_STOP"; then
            CURRENT_MONITOR="UNKNOWN"
            printf 'could not request watcher stop; cleanup remains pending\n' >&2
        fi
        if wait "$CURRENT_WATCH_PID"; then watch_rc=0; else watch_rc=$?; fi
        [ "$watch_rc" = 0 ] || CURRENT_MONITOR="UNKNOWN"
    fi
    line="driver_exit=$observed_rc reason=$TERMINAL_REASON phase=$CURRENT_PHASE stage=$CURRENT_STAGE stage_exit=$OBSERVED_STAGE_RC freeze_monitor=$CURRENT_MONITOR"
    if printf '%s\n' "$line" >>"$SUMMARY"; then
        summary_ok=1
    else
        driver_rc=1
        printf 'could not append terminal summary: %s\n' "$line" >&2
    fi
    line="driver_exit=UNKNOWN observed_exit=$observed_rc summary_write=$summary_ok marker_write=pending reason=$TERMINAL_REASON phase=$CURRENT_PHASE stage=$CURRENT_STAGE stage_exit=$OBSERVED_STAGE_RC freeze_monitor=$CURRENT_MONITOR"
    if printf '%s\n' "$line" >"$RUN_DIR/driver.exit"; then
        # Prepare a regular marker privately, then publish it only after the
        # final independent driver receipt has been written successfully.
        if [ ! -e "$RUN_DIR/.done.pending" ] && [ ! -L "$RUN_DIR/.done.pending" ] &&
           printf 'driver_receipt_complete=1\n' >"$RUN_DIR/.done.pending" &&
           [ -f "$RUN_DIR/.done.pending" ] && [ ! -L "$RUN_DIR/.done.pending" ]; then
            line="driver_exit=$driver_rc observed_exit=$observed_rc summary_write=$summary_ok marker_write=1 reason=$TERMINAL_REASON phase=$CURRENT_PHASE stage=$CURRENT_STAGE stage_exit=$OBSERVED_STAGE_RC freeze_monitor=$CURRENT_MONITOR"
            if printf '%s\n' "$line" >"$RUN_DIR/driver.exit" &&
               [ ! -e "$RUN_DIR/.done" ] && [ ! -L "$RUN_DIR/.done" ] &&
               mv -T -- "$RUN_DIR/.done.pending" "$RUN_DIR/.done" &&
               [ -f "$RUN_DIR/.done" ] && [ ! -L "$RUN_DIR/.done" ]; then
                marker_ok=1
            fi
        fi
        if [ "$marker_ok" = 0 ]; then
            driver_rc=1
            line="driver_exit=1 observed_exit=$observed_rc summary_write=$summary_ok marker_write=0 reason=$TERMINAL_REASON phase=$CURRENT_PHASE stage=$CURRENT_STAGE stage_exit=$OBSERVED_STAGE_RC freeze_monitor=$CURRENT_MONITOR"
            printf '%s\n' "$line" >"$RUN_DIR/driver.exit" || true
            [ "$summary_ok" = 0 ] || printf 'driver_marker_failed=1 actual_exit=1\n' >>"$SUMMARY" || true
            rm -f -- "$RUN_DIR/.done.pending" 2>/dev/null || true
            printf 'could not publish complete driver marker: %s\n' "$line" >&2
        fi
    else
        driver_rc=1
        printf 'could not persist independent driver receipt: %s\n' "$line" >&2
        [ "$summary_ok" = 0 ] || printf 'driver_receipt_failed=1 actual_exit=1\n' >>"$SUMMARY" || true
    fi
    # A failed sink changes the real driver exit. The original observed exit
    # stays visible in the independent receipt when that sink remains usable.
    if [ "$driver_rc" != "$observed_rc" ]; then exit "$driver_rc"; fi
}
set -E
trap 'TERMINAL_REASON="ERR"' ERR
trap 'TERMINAL_REASON="SIGINT"; exit 130' INT
trap 'TERMINAL_REASON="SIGTERM"; exit 143' TERM
trap 'record_driver_exit "$?"' EXIT
: >"$SUMMARY"
note "run_dir=$RUN_DIR"
note "tree=$ROOT head=$(git rev-parse HEAD) before=$BEFORE"

validate_lint_result() {
    # Reuse the pure validator beside this runner, without invoking a scan,
    # exporting source, or changing the measured tree's import roots.
    "$PY" -B - "$(dirname "$SELF")" "$ROOT" "$RUN_DIR/lints.json" "$1" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from trust_tier_ratchet import RatchetError, validate_scan_result

try:
    findings = validate_scan_result(
        Path(sys.argv[3]).read_text(), returncode=int(sys.argv[4]), tree_root=Path(sys.argv[2])
    )
except (OSError, UnicodeError, RatchetError) as exc:
    print(f"lint measurement incomplete: {exc}", file=sys.stderr)
    raise SystemExit(1)
print(len(findings))
PY
}

run_stage() { # run_stage <name> <fatal 0|1> <command string>
    local name="$1" fatal="$2" cmd="$3" log="$RUN_DIR/$1.log" t0 t1 rc watch_rc monitor attempt
    local watch_ready watch_stop watch_done stage_exit_file
    STAGE_SEQ=$((STAGE_SEQ + 1))
    watch_ready="$RUN_DIR/.watch-$name-$STAGE_SEQ.ready"
    watch_stop="$RUN_DIR/.watch-$name-$STAGE_SEQ.stop"
    watch_done="$RUN_DIR/.watch-$name-$STAGE_SEQ.done"
    stage_exit_file="$RUN_DIR/$name-$STAGE_SEQ.exit"
    CURRENT_STAGE="$name"
    CURRENT_PHASE="stage"
    OBSERVED_STAGE_RC="UNKNOWN"
    CURRENT_MONITOR="UNKNOWN"
    t0="$(date +%s)"
    say ">>> [$name] $(date -u +%H:%M:%SZ)  log: $log"
    # Continuous freeze watch: an endpoint-only comparison misses a change that
    # is made and reverted mid-stage. Normal stop is acknowledged by the
    # watcher; a vanished or failed watcher cannot qualify a frozen run.
    ( while :; do
        if s="$(tree_state)"; then :; else
            printf 'source_sample_failed=1 stage=%s\n' "$name" >"$RUN_DIR/.watch-$name-$STAGE_SEQ.error" || true
            exit 4
        fi
        [ "$s" = "$BEFORE" ] || echo "$(date -u +%FT%TZ) stage=$name $s" >>"$RUN_DIR/tree-moved.log"
        : >"$watch_ready"
        [ ! -e "$watch_stop" ] || break
        sleep 5
      done
      : >"$watch_done"
    ) &
    local watch=$!
    CURRENT_WATCH_PID="$watch"
    CURRENT_WATCH_STOP="$watch_stop"
    CURRENT_PHASE="watcher_start"
    for ((attempt = 0; attempt < 100; attempt++)); do
        [ ! -f "$watch_ready" ] || break
        kill -0 "$watch" 2>/dev/null || break
        sleep 0.05
    done
    if [ ! -f "$watch_ready" ]; then
        MONITOR_FAILED=1
        RESULT=1
        CURRENT_MONITOR="UNKNOWN"
        record_cleanup_pending
        : >"$watch_stop"
        if wait "$watch"; then watch_rc=0; else watch_rc=$?; fi
        CURRENT_WATCH_PID=""
        note "freeze_monitor stage=$name status=UNKNOWN reason=no_initial_sample_or_startup_deadline watcher_exit=$watch_rc"
        return 1
    fi
    CURRENT_PHASE="stage"
    if [ "$name" = lints ]; then
        if bash -c "$cmd" >"$RUN_DIR/lints.json" 2>"$log"; then rc=0; else rc=$?; fi
    else
        if bash -c "$cmd" >"$log" 2>&1; then rc=0; else rc=$?; fi
    fi
    OBSERVED_STAGE_RC="$rc"
    # Preserve the actual child result before fallible watcher/log cleanup.
    printf '%s\n' "$rc" >"$stage_exit_file"
    t1="$(date +%s)"
    local extra="" lint_count
    case "$name" in
        lints)
            CURRENT_PHASE="lint_validation"
            if lint_count="$(validate_lint_result "$rc" 2>>"$log")"; then
                extra=" lint_status=complete findings=$lint_count"
            else
                LINTS_INCOMPLETE=1
                RESULT=1
                extra=" lint_status=INCOMPLETE"
            fi ;;
        pytest|testcontainer) extra=" $(grep -E '^(=+ .*(passed|failed|error).* =+)$' "$log" | tail -n 1 | tr -d '=' | sed 's/^ *//; s/ *$//' || true)" ;;
    esac
    note "stage=$name exit=$rc seconds=$((t1 - t0)) fatal=$fatal$extra"
    CURRENT_PHASE="watcher_cleanup"
    record_cleanup_pending
    : >"$watch_stop"
    if wait "$watch"; then watch_rc=0; else watch_rc=$?; fi
    CURRENT_WATCH_PID=""
    if [ "$watch_rc" = 0 ] && [ -f "$watch_ready" ] && [ -f "$watch_done" ]; then
        monitor=healthy
    else
        monitor=UNKNOWN
        MONITOR_FAILED=1
        RESULT=1
    fi
    CURRENT_MONITOR="$monitor"
    note "freeze_monitor stage=$name status=$monitor watcher_exit=$watch_rc"
    if [ "$rc" -ne 0 ] && [ "$fatal" = 1 ]; then RESULT=1; fi
    CURRENT_STAGE="none"
    CURRENT_PHASE="between_stages"
    OBSERVED_STAGE_RC="UNKNOWN"
}

for s in "${STAGE_LIST[@]}"; do
    fatal=1; [ "$s" = lints ] && [ "$LINTS_STRICT" = 0 ] && fatal=0
    cmd="${CMD_OF[$s]//<run-dir>/$RUN_DIR}"
    run_stage "$s" "$fatal" "$cmd"
done

CURRENT_PHASE="final_freeze"
if AFTER="$(tree_state)"; then
    FINAL_SAMPLE_OK=1
else
    AFTER="UNKNOWN"
    FINAL_SAMPLE_OK=0
    MONITOR_FAILED=1
    RESULT=1
fi
if [ "$FINAL_SAMPLE_OK" = 0 ]; then
    note "after=UNKNOWN frozen=UNKNOWN — final source-state sample failed; this run is not evidence"
elif [ "$AFTER" = "$BEFORE" ] && [ -s "$RUN_DIR/tree-moved.log" ]; then
    note "after=$AFTER frozen=NO — the tree moved mid-run and was restored ($(grep -c . "$RUN_DIR/tree-moved.log") sample(s) in tree-moved.log); this run is not evidence"
    RESULT=1
elif [ "$AFTER" != "$BEFORE" ]; then
    note "after=$AFTER frozen=NO — the tree moved during the run; this run is not evidence"
    RESULT=1
elif [ "$MONITOR_FAILED" = 1 ]; then
    note "after=$AFTER frozen=UNKNOWN — a stage watcher did not prove its full lifetime"
    RESULT=1
else
    note "after=$AFTER frozen=yes"
fi
if [ "$MONITOR_FAILED" = 1 ] || [ "$LINTS_INCOMPLETE" = 1 ]; then
    note "RESULT=INCOMPLETE"
else
    note "RESULT=$([ "$RESULT" = 0 ] && echo PASS || echo FAIL)"
fi
CURRENT_PHASE="complete"
TERMINAL_REASON="completed"
exit "$RESULT"
