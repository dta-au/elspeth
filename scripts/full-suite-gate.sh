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
# nothing. --execute runs them; --execute --detach runs them in their own
# session (setsid nohup) and returns at once, for callers whose shell times out
# before a 20-minute suite finishes (the Bash tool caps at 10 minutes).
#
# Stages (--stages, comma-separated; default: ruff,pytest):
#   ruff          ruff check + ruff format --check over src/ tests/ scripts/ examples/
#                 elspeth-lints/src/   (CI "Static analysis", pre-push hygiene)
#   mypy          mypy src/ elspeth-lints/src/
#   contracts     scripts/check_contracts.py + generate_skill_inventory.py --check
#   lints         elspeth-lints check --rules all --root src/elspeth, keyless
#                 (shape-only verification). The corpus is EXPECTED to be non-zero
#                 while the judge-signature stage is open (AGENTS.md), so this stage
#                 records the finding count and is not fatal unless --lints-strict:
#                 compare the count before and after your change, not to zero.
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
#   - counts sibling pytest processes on this box from /proc (never a pgrep
#     pattern) and caps workers at 8 unless --workers is given
#   - records HEAD + a hash of the working-tree state before the run, samples
#     it every 5 s while each stage runs, and checks it after: a run during
#     which the tree moved is NOT evidence and is marked frozen=NO even if the
#     change was reverted before the end (a change shorter than one sampling
#     interval can still slip through; the digest covers tracked content and
#     untracked NAMES, not ignored files)
#
# Output: one directory per run under --log-dir (default
# $ELSPETH_GATE_LOG_DIR or /tmp/elspeth-gates/$USER), containing
#   preflight.txt · <stage>.log · pytest-junit.xml · summary.txt · .done (last)
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
#   --workers N          xdist workers for the pytest stage (default 12; 8 if siblings)
#   --root DIR           tree to gate (default: the git toplevel of the cwd)
#   --log-dir DIR        parent directory for run directories
#   --lints-strict       make a non-zero elspeth-lints exit fatal
#   --pytest-args "..."  extra arguments appended to the pytest stage
#   -h, --help
#
# Exit codes:  0 every fatal stage passed and the tree stayed frozen
#              1 a fatal stage failed, or the tree moved during the run
#              2 preflight refused, or usage error
#              (with --detach the parent exits 0 once the child is launched; the
#               child's code is the RESULT line in summary.txt)

set -euo pipefail

EXECUTE=0; DETACH=0; STAGES="ruff,pytest"; WORKERS=""; ROOT=""; LOG_DIR=""; LINTS_STRICT=0; PYTEST_ARGS=""; RUN_DIR=""
usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }
ORIG_ARGS=("$@")
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
        --run-dir) RUN_DIR="${2:?}"; shift ;;      # internal: the detached child reuses the parent's dir
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
LOG_DIR="${LOG_DIR:-${ELSPETH_GATE_LOG_DIR:-/tmp/elspeth-gates/${USER:-$(id -un)}}}"

IFS=',' read -r -a STAGE_LIST <<<"$STAGES"
for s in "${STAGE_LIST[@]}"; do
    case "$s" in ruff|mypy|contracts|lints|pytest|testcontainer) ;; *) echo "unknown stage: $s" >&2; exit 2 ;; esac
done
has_stage() { local s; for s in "${STAGE_LIST[@]}"; do [ "$s" = "$1" ] && return 0; done; return 1; }

# --- preflight ---------------------------------------------------------------
REFUSE=0
say() { printf '%s\n' "$*"; }
PRE=""
pre() { PRE+="$*"$'\n'; say "$*"; }

pre "tree        : $ROOT"
pre "HEAD        : $(git rev-parse HEAD)  ($(git branch --show-current || true))"
pre "stages      : $STAGES"
pre "log dir     : $LOG_DIR"

if [ -n "${ELSPETH_JUDGE_METADATA_HMAC_KEY:-}" ]; then
    pre "REFUSED     : ELSPETH_JUDGE_METADATA_HMAC_KEY is set in this shell ([O1]); results here are not the keyless corpus"
    REFUSE=1
fi
if [ ! -x "$PY" ]; then
    pre "REFUSED     : no $PY — link the venv: scripts/worktree-cleanup.sh --link-venv --path '$ROOT'"
    REFUSE=1
else
    export PYTHONPATH="$ROOT/src:$ROOT/elspeth-lints/src"
    PROV="$("$PY" -c 'import elspeth, elspeth_lints; print(elspeth.__file__); print(elspeth_lints.__file__)' 2>&1 || true)"
    E_FILE="$(sed -n 1p <<<"$PROV")"; L_FILE="$(sed -n 2p <<<"$PROV")"
    if [[ "$E_FILE" == "$ROOT/src/"* && "$L_FILE" == "$ROOT/elspeth-lints/src/"* ]]; then
        pre "provenance  : elspeth -> $E_FILE"
        pre "              elspeth_lints -> $L_FILE"
    else
        pre "REFUSED     : imports resolve outside this tree: $(tr '\n' ' ' <<<"$PROV")"
        REFUSE=1
    fi
fi

# Sibling suites: python INTERPRETERS on the box running pytest (argv[0] is a
# python or pytest binary and the argv mentions pytest), with cwd read from /proc.
# Wrapper shells and sandboxes whose argv merely quotes a pytest command are not
# counted. Our own run has not started, so every hit is a sibling. A process that
# exits mid-scan is skipped silently (cat's stderr is dropped, not the shell's).
SIBLINGS=""
for d in /proc/[0-9]*; do
    pid="${d#/proc/}"; [ "$pid" = "$$" ] && continue
    cmd="$(cat "$d/cmdline" 2>/dev/null | tr '\0' ' ' || true)"
    [ -n "$cmd" ] || continue
    argv0="$(basename -- "${cmd%% *}")"   # -- : a login shell's argv[0] is "-bash"
    case "$argv0" in python*|pytest) ;; *) continue ;; esac
    case "$cmd" in *pytest*) SIBLINGS+="  pid $pid cwd $(readlink "$d/cwd" 2>/dev/null || echo '?') :: ${cmd:0:90}"$'\n' ;; esac
done
N_SIB=0; [ -n "$SIBLINGS" ] && N_SIB="$(grep -c . <<<"$SIBLINGS")"
if [ -z "$WORKERS" ]; then
    if [ "$N_SIB" -gt 0 ]; then WORKERS=8; else WORKERS=12; fi
fi
pre "siblings    : $N_SIB pytest process(es) already on this box -> workers=$WORKERS"
[ -n "$SIBLINGS" ] && pre "$SIBLINGS"

if has_stage testcontainer; then
    if docker info >/dev/null 2>&1; then pre "docker      : reachable"; else pre "REFUSED     : testcontainer stage requested but docker is not reachable"; REFUSE=1; fi
    [ "$N_SIB" -gt 0 ] && pre "WARNING     : a testcontainer run beside another suite records contention as failures; serialise on the box first"
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
    local st; st="$(git --no-optional-locks status --porcelain -uall)"
    printf '%s %s\n' "$(git rev-parse HEAD)" "$( { printf '%s\n' "$st"; awk '{print $NF}' <<<"$st" | grep -v '^$' | tr '\n' '\0' | xargs -0 -r sha256sum 2>/dev/null; } | sha256sum | cut -c1-16)"
}
BEFORE="$(tree_state)"
pre "tree state  : $BEFORE  (HEAD + sha256 of status+diff; must match after the run)"
DIRTY_N="$(git --no-optional-locks status --porcelain | wc -l)"
[ "$DIRTY_N" -gt 0 ] && pre "note        : $DIRTY_N uncommitted path(s); the measurement is of the WORKING TREE, not of HEAD"

# --- the commands ------------------------------------------------------------
RUFF_PATHS=(src/ tests/ scripts/ examples/ elspeth-lints/src/)
declare -A CMD_OF=()
CMD_OF[ruff]="$ROOT/.venv/bin/ruff check ${RUFF_PATHS[*]} && $ROOT/.venv/bin/ruff format --check ${RUFF_PATHS[*]}"
CMD_OF[mypy]="$ROOT/.venv/bin/mypy src/ elspeth-lints/src/"
CMD_OF[contracts]="$PY scripts/check_contracts.py && $PY scripts/cicd/generate_skill_inventory.py --check"
CMD_OF[lints]="ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing $PY -m elspeth_lints.core.cli check --rules all --root src/elspeth"
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
    printf '%s' "$PRE" >"$RUN_DIR/preflight.txt"
fi
SUMMARY="$RUN_DIR/summary.txt"

# --- detach: re-exec ourselves in a fresh session and return ------------------
if [ "$DETACH" = 1 ]; then
    CHILD_ARGS=()
    for a in "${ORIG_ARGS[@]}"; do [ "$a" = "--detach" ] || CHILD_ARGS+=("$a"); done
    # A harness teardown kills the caller's process group; setsid puts the run in
    # its own so it survives. nohup + redirected stdio so nothing holds the tty.
    setsid nohup "$SELF" "${CHILD_ARGS[@]}" --run-dir "$RUN_DIR" --workers "$WORKERS" \
        >"$RUN_DIR/driver.log" 2>&1 </dev/null &
    echo $! >"$RUN_DIR/pid"
    say "detached    : pid $(cat "$RUN_DIR/pid")  run dir $RUN_DIR"
    say "wait with   : until [ -f $RUN_DIR/.done ]; do sleep 30; done; cat $RUN_DIR/summary.txt"
    say "kill ONLY   : the pid in $RUN_DIR/pid, after checking /proc/<pid>/cwd is $ROOT"
    exit 0
fi

# --- execute -----------------------------------------------------------------
: >"$SUMMARY"
note() { printf '%s\n' "$*" | tee -a "$SUMMARY"; }
note "run_dir=$RUN_DIR"
note "tree=$ROOT head=$(git rev-parse HEAD) before=$BEFORE"
RESULT=0

run_stage() { # run_stage <name> <fatal 0|1> <command string>
    local name="$1" fatal="$2" cmd="$3" log="$RUN_DIR/$1.log" t0 t1 rc
    t0="$(date +%s)"
    say ">>> [$name] $(date -u +%H:%M:%SZ)  log: $log"
    # Continuous freeze watch: an endpoint-only comparison misses a change that
    # is made and reverted mid-stage. Every deviation is appended with a time.
    ( while :; do s="$(tree_state)"; [ "$s" = "$BEFORE" ] || echo "$(date -u +%FT%TZ) stage=$name $s" >>"$RUN_DIR/tree-moved.log"; sleep 5; done ) &
    local watch=$!
    set +e
    bash -c "$cmd" >"$log" 2>&1
    rc=$?
    set -e
    kill "$watch" 2>/dev/null; wait "$watch" 2>/dev/null || true
    t1="$(date +%s)"
    local extra=""
    case "$name" in
        lints)  extra=" findings=$(grep -cE '^[^ ].*:[0-9]+:' "$log" || true)" ;;
        pytest|testcontainer) extra=" $(grep -E '^(=+ .*(passed|failed|error).* =+)$' "$log" | tail -n 1 | tr -d '=' | sed 's/^ *//; s/ *$//' || true)" ;;
    esac
    note "stage=$name exit=$rc seconds=$((t1 - t0)) fatal=$fatal$extra"
    if [ "$rc" -ne 0 ] && [ "$fatal" = 1 ]; then RESULT=1; fi
}

for s in "${STAGE_LIST[@]}"; do
    fatal=1; [ "$s" = lints ] && [ "$LINTS_STRICT" = 0 ] && fatal=0
    cmd="${CMD_OF[$s]//<run-dir>/$RUN_DIR}"
    run_stage "$s" "$fatal" "$cmd"
done

AFTER="$(tree_state)"
if [ "$AFTER" = "$BEFORE" ] && [ ! -s "$RUN_DIR/tree-moved.log" ]; then
    note "after=$AFTER frozen=yes"
elif [ "$AFTER" = "$BEFORE" ]; then
    note "after=$AFTER frozen=NO — the tree moved mid-run and was restored ($(grep -c . "$RUN_DIR/tree-moved.log") sample(s) in tree-moved.log); this run is not evidence"
    RESULT=1
else
    note "after=$AFTER frozen=NO — the tree moved during the run; this run is not evidence"
    RESULT=1
fi
note "RESULT=$([ "$RESULT" = 0 ] && echo PASS || echo FAIL)"
touch "$RUN_DIR/.done"
exit "$RESULT"
