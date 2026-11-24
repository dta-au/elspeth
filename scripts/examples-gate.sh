#!/usr/bin/env bash
# examples-gate.sh — run every offline shipped example the way its documentation
# says to run it, and check each against its documented result.
#
# The examples gate runs the non-network examples. Before this script, the
# test suite validates every config and executes only a handful
# (tests/e2e/examples/test_shipped_examples.py).
#
# DRY-RUN BY DEFAULT: validates the case data file against the tree and prints
# the plan. --execute runs it.
#
# What it does with --execute
#   1. Exports the tree under test with `git archive <ref>` (or takes --tree, an
#      already-exported directory). It never runs anything inside --root and
#      never writes there, so a shared landing worktree is safe to gate.
#   2. Refuses unless scripts/examples-gate-cases.yaml classifies EVERY example
#      directory, top-level pipeline config and launcher in that tree
#      (examples_gate.py validate). A new example cannot be silently left out.
#   3. For each case, in order, serially:
#        - a fresh copy of the tree (tests/, docs/, website/ left out; .venv is a
#          symlink to --venv), so audit DBs, outputs and .elspeth/ never collide
#          and every launcher sees a clean checkout;
#        - proves elspeth.__file__ and elspeth_lints.__file__ resolve inside that
#          copy (the console scripts' shebang is --venv's interpreter; only the
#          exported PYTHONPATH redirects them);
#        - an offline guard over every config the case runs and its input
#          fixtures: any non-loopback URL, remote-service plugin, cloud LLM
#          provider or provider without a loopback base_url refuses the case;
#        - starts the fixture server the documentation tells you to start
#          (only chaosllm/chaosweb on loopback), runs the documented command
#          under `env -i` with an allowlisted environment, records its exit
#          code, and tears down the private PID namespace;
#        - checks the result: accepted exit codes, run status, source row count,
#          per-sink completed outcomes, launcher self-verification markers,
#          conservation for stochastic fault-injection cases (examples_gate.py check).
#   4. Hashes the exported tree before and after; a tree that moved is not evidence.
#
# --execute requires bwrap (bubblewrap): each case's server and command share
# one private network with loopback, and the kernel blocks outbound traffic.
# The text guard remains an early, explanatory refusal. The case environment
# has no inherited credentials, and each case gets an empty HOME.
#
# Verdicts (summary.txt, one line per case, then one SKIP line per excluded config)
#   PASS     ran and matched every documented expectation
#   FAIL     ran and did not match (evidence on the line; full log in logs/)
#   NOTRUN   could not be run here (stop file, missing prerequisite,
#            provenance or offline-guard refusal) -- counts against the gate
#   SKIP     classified as not runnable offline in the data file (network,
#            container-only, not a pipeline); never counts against the gate
# overall.exit: 0 only when every case PASSed and the tree stayed frozen.
#
# No timeouts: a hung case hangs the gate; current-case.txt names it and its
# start time. Create the stop file to stop between cases (the running case is
# allowed to finish).
#
# Usage:
#   scripts/examples-gate.sh                                   # plan
#   scripts/examples-gate.sh --execute                         # run every case
#   scripts/examples-gate.sh --execute --only threshold_gate,deep_routing
#   scripts/examples-gate.sh --execute --root <worktree> --ref HEAD --tmp-root <scratch-dir>
#
# Options:
#   --execute          run the cases (default: plan only)
#   --root DIR         git checkout whose --ref is exported (default: git toplevel of cwd)
#   --ref REF          commit to export (default: HEAD)
#   --tree DIR         use this already-exported tree instead of git archive (controls)
#   --venv DIR         virtualenv with elspeth's dependencies (default: <root>/.venv)
#   --cases FILE       case data file (default: next to this script)
#   --only IDS         comma-separated case ids
#   --cpus LIST        taskset CPU list for every case and fixture server (default: no pinning)
#   --tmp-root DIR     tree copies and per-case TMPDIR (default:
#                      ${XDG_CACHE_HOME:-$HOME/.cache}/elspeth-examples-gate; a full run
#                      copies the tree once per case, several GB); never under /tmp and
#                      never inside the checkout under test
#   --log-dir DIR      summary, logs, evidence (default: <tmp-root>/runs/<timestamp>)
#   --stop-file FILE   checked between cases (default: <tmp-root>/STOP)
#   --min-mem-gb N     refuse to start below this MemAvailable (default: 4)
#   --keep-passing     keep the tree copy of a PASSing case (default: delete it once its
#                      evidence json and log are written; FAIL/NOTRUN copies are always kept)
#   -h, --help
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXECUTE=0
ROOT=""
REF="HEAD"
TREE=""
VENV=""
CASES="$SCRIPT_DIR/examples-gate-cases.yaml"
ONLY=""
CPUS=""
TMP_ROOT=""
LOG_DIR=""
STOP_FILE=""
MIN_MEM_GB=4
KEEP_PASSING=0

usage() { sed -n '2,/^set -euo pipefail/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//'; }
die() { echo "examples-gate: $*" >&2; exit 2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --execute) EXECUTE=1 ;;
        --root) ROOT="$2"; shift ;;
        --ref) REF="$2"; shift ;;
        --tree) TREE="$2"; shift ;;
        --venv) VENV="$2"; shift ;;
        --cases) CASES="$2"; shift ;;
        --only) ONLY="$2"; shift ;;
        --cpus) CPUS="$2"; shift ;;
        --tmp-root) TMP_ROOT="$2"; shift ;;
        --log-dir) LOG_DIR="$2"; shift ;;
        --stop-file) STOP_FILE="$2"; shift ;;
        --min-mem-gb) MIN_MEM_GB="$2"; shift ;;
        --keep-passing) KEEP_PASSING=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option $1 (see --help)" ;;
    esac
    shift
done

# Default outside both /tmp (memory-backed on some hosts) and the checkout (the case
# copies would show in git status, the tree hash and file walkers).
[ -n "$TMP_ROOT" ] || TMP_ROOT="${XDG_CACHE_HOME:-${HOME:?HOME is not set; pass --tmp-root}/.cache}/elspeth-examples-gate"
TMP_ROOT="$(realpath -m "$TMP_ROOT")"
if [ -z "$ROOT" ]; then
    ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || die "not in a git checkout; pass --root"
fi
ROOT="$(cd "$ROOT" && pwd)"
case "$TMP_ROOT/" in "$(realpath "$ROOT")"/*) die "--tmp-root must not be inside the checkout ($ROOT)" ;; esac
case "$TMP_ROOT" in /tmp|/tmp/*) die "--tmp-root must not be under /tmp" ;; esac
[ -n "$VENV" ] || VENV="$ROOT/.venv"
VENV="$(cd "$VENV" && pwd -P)" || die "--venv $VENV does not exist"
CASES="$(cd "$(dirname "$CASES")" && pwd)/$(basename "$CASES")"
[ -f "$CASES" ] || die "case data file $CASES not found"
if [ "$EXECUTE" -eq 1 ]; then
    mkdir -p "$TMP_ROOT"
    TMP_ROOT="$(cd "$TMP_ROOT" && pwd)"
fi
[ -n "$STOP_FILE" ] || STOP_FILE="$TMP_ROOT/STOP"
STOP_FILE="$(realpath -m "$STOP_FILE")"
# Second resolution alone let two gates started in the same second share (and
# overwrite) a log dir and case roots; the PID makes the stamp unique per run.
STAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$"
[ -n "$LOG_DIR" ] || LOG_DIR="$TMP_ROOT/runs/$STAMP"
LOG_DIR="$(realpath -m "$LOG_DIR")"
for output_path in "$LOG_DIR" "$STOP_FILE"; do
    case "$output_path/" in "$(realpath "$ROOT")"/*) die "$output_path must not be inside the checkout" ;; esac
done
PY="$VENV/bin/python"
[ -x "$PY" ] || die "no interpreter at $PY"
HELPER=("$PY" -I "$SCRIPT_DIR/examples_gate.py")
SEP=$'\x1f'

# ------------------------------------------------------------------ the tree
if [ -n "$TREE" ]; then
    TREE="$(cd "$TREE" && pwd)"
    case "$TMP_ROOT/" in "$TREE"/*) die "--tmp-root must not be inside --tree ($TREE)" ;; esac
    while IFS= read -r -d '' link; do
        target="$(realpath -m "$link")"
        case "$target/" in "$TREE"/*) ;; *) die "tree symlink $link resolves outside --tree" ;; esac
    done < <(find "$TREE" -type l -print0)
    SOURCE_DESC="tree $TREE (pre-exported; --root/--ref not used)"
    SHA="n/a"
else
    SHA="$(git -C "$ROOT" rev-parse --verify "$REF^{commit}")" || die "cannot resolve $REF in $ROOT"
    SOURCE_DESC="git archive $SHA from $ROOT"
fi

plan() {
    echo "examples-gate plan"
    echo "  source     : $SOURCE_DESC"
    echo "  venv       : $VENV"
    echo "  cases      : $CASES"
    echo "  cpus       : ${CPUS:-unpinned}"
    echo "  tmp-root   : $TMP_ROOT"
    echo "  log-dir    : $LOG_DIR"
    echo "  stop-file  : $STOP_FILE"
    echo "  min-mem-gb : $MIN_MEM_GB"
}

if [ "$EXECUTE" -eq 0 ]; then
    plan
    if [ -n "$TREE" ]; then
        "${HELPER[@]}" validate --cases "$CASES" --tree "$TREE"
    else
        echo "  (validating checkout; --execute validates the exported commit)"
        "${HELPER[@]}" validate --cases "$CASES" --tree "$ROOT"
    fi
    echo "cases:"
    "${HELPER[@]}" list --cases "$CASES" ${ONLY:+--only "$ONLY"} | while IFS="$SEP" read -r id cmd scmd sport ports envx fpk skip; do
        if [ -n "$skip" ]; then echo "  SKIP $id -- $skip"; continue; fi
        echo "  $id: ${envx:+$envx }$cmd${scmd:+   [server: $scmd]}${ports:+   [ports: $ports]}"
    done
    echo "dry run: nothing executed (pass --execute)"
    exit 0
fi

# ---------------------------------------------------------------- preflight
[ -e "$LOG_DIR/summary.txt" ] && die "log dir $LOG_DIR already holds a run; pass a fresh --log-dir"
mkdir -p "$LOG_DIR/logs" "$LOG_DIR/evidence"
SUMMARY="$LOG_DIR/summary.txt"
PREFLIGHT="$LOG_DIR/preflight.txt"
: > "$SUMMARY"
plan > "$PREFLIGHT"
echo "overall=running" > "$LOG_DIR/overall.exit"

refuse() { echo "REFUSED: $*" | tee -a "$PREFLIGHT" >&2; echo 2 > "$LOG_DIR/overall.exit"; exit 2; }

cp -- "$CASES" "$LOG_DIR/cases.yaml" || refuse "could not snapshot case data"
cp -- "$SCRIPT_DIR/examples_gate.py" "$LOG_DIR/examples_gate.py" || refuse "could not snapshot helper"
CASES="$LOG_DIR/cases.yaml"
HELPER=("$PY" -I "$LOG_DIR/examples_gate.py")
echo "cases_sha256=$(sha256sum "$CASES" | cut -d' ' -f1)" >> "$PREFLIGHT"
echo "helper_sha256=$(sha256sum "$LOG_DIR/examples_gate.py" | cut -d' ' -f1)" >> "$PREFLIGHT"

mem_kb="$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)"
echo "MemAvailable_kB=$mem_kb (floor ${MIN_MEM_GB} GB)" >> "$PREFLIGHT"
[ "$mem_kb" -ge $((MIN_MEM_GB * 1024 * 1024)) ] || refuse "MemAvailable ${mem_kb} kB is below ${MIN_MEM_GB} GB"
[ -f "$STOP_FILE" ] && refuse "stop file $STOP_FILE exists"
for tool in sqlite3 tar cp; do command -v "$tool" >/dev/null || refuse "$tool not on PATH"; done
[ -z "$CPUS" ] || command -v taskset >/dev/null || refuse "taskset not on PATH (--cpus given)"
for bin in elspeth chaosllm chaosweb; do [ -x "$VENV/bin/$bin" ] || refuse "$VENV/bin/$bin missing (install the package and errorworks)"; done

CASE_BASE="$TMP_ROOT/cases/$STAMP"
[ -e "$CASE_BASE" ] && refuse "case directory $CASE_BASE already exists"
mkdir -p "$CASE_BASE"
if [ -z "$TREE" ]; then
    TREE="$TMP_ROOT/trees/$SHA-$STAMP"
    mkdir -p "$TREE"
    git -C "$ROOT" archive "$SHA" | tar -x -C "$TREE"
    echo "exported $SHA to $TREE" >> "$PREFLIGHT"
fi
echo "source_sha=$SHA" >> "$PREFLIGHT"

while IFS= read -r -d '' link; do
    target="$(realpath -m "$link")"
    case "$target/" in "$TREE"/*) ;; *) refuse "tree symlink $link resolves outside --tree" ;; esac
done < <(find "$TREE" -type l -print0)

[ -z "$(find "$TREE" -name .env -print -quit)" ] || refuse "exported tree contains .env"

set +e
"${HELPER[@]}" validate --cases "$CASES" --tree "$TREE" >> "$PREFLIGHT" 2>&1
rc=$?
set -e
[ "$rc" -eq 0 ] || refuse "case data file does not cover the tree (see $PREFLIGHT)"

set +e
"${HELPER[@]}" list --cases "$CASES" ${ONLY:+--only "$ONLY"} > "$LOG_DIR/cases.list" 2>> "$PREFLIGHT"
list_rc=$?
count="$("${HELPER[@]}" count --cases "$CASES" ${ONLY:+--only "$ONLY"} 2>> "$PREFLIGHT")"
count_rc=$?
set -e
[ "$list_rc" -eq 0 ] || refuse "case list failed (exit $list_rc; see $PREFLIGHT)"
[ "$count_rc" -eq 0 ] && [[ "$count" =~ ^[0-9]+$ ]] || refuse "case list count failed"
mapfile -t CASE_LINES < "$LOG_DIR/cases.list"
[ "${#CASE_LINES[@]}" -eq "$count" ] || refuse "case list has ${#CASE_LINES[@]} rows, selection reports $count"
[ "$count" -gt 0 ] || refuse "no cases selected"

BWRAP="$(command -v bwrap || true)"
[ -n "$BWRAP" ] || refuse "bwrap (bubblewrap) is required for kernel-enforced offline execution"
BWRAP="$(realpath "$BWRAP")"
pin() { if [ -n "$CPUS" ]; then echo taskset -c "$CPUS"; fi; }
if ! env -i HOME="$LOG_DIR" PATH="/usr/local/bin:/usr/bin:/bin" TMPDIR="$LOG_DIR" LANG=C.UTF-8 \
    $(pin) "$BWRAP" --dev-bind / / --unshare-net --unshare-pid --proc /proc --die-with-parent -- "$PY" -c '
import socket
interfaces = {line.split(":", 1)[0].strip() for line in open("/proc/net/dev").read().splitlines() if ":" in line}
assert interfaces == {"lo"}, interfaces
with socket.socket() as outside:
    try:
        outside.connect(("1.1.1.1", 53))
    except OSError:
        pass
    else:
        raise AssertionError("outbound connect succeeded")
with socket.socket() as server:
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    with socket.socket() as client:
        client.connect(server.getsockname())
        peer, _ = server.accept()
        peer.close()
' >> "$PREFLIGHT" 2>&1; then
    refuse "bwrap private-network self-test failed (see $PREFLIGHT)"
fi

tree_digest() {
    "${HELPER[@]}" digest --tree "$1" --manifest "$2"
}
DIGEST_BEFORE="$(tree_digest "$TREE" "$LOG_DIR/tree-before.sha256")"
echo "tree_digest_before=$DIGEST_BEFORE" >> "$PREFLIGHT"

FINGERPRINT_KEY="$("$PY" -c 'import secrets; print(secrets.token_hex(32))')"

make_case_root() {
    local root="$1" entry
    mkdir -p "$root"
    for entry in "$TREE"/* "$TREE"/.[!.]*; do
        [ -e "$entry" ] || [ -L "$entry" ] || continue
        case "$(basename "$entry")" in
            tests|docs|website|.venv|.git) ;;
            *) cp -a "$entry" "$root/" ;;
        esac
    done
    ln -s "$VENV" "$root/.venv"
}

# ---------------------------------------------------------------- the cases
PASS=0; FAIL=0; NOTRUN=0; SKIP=0
GATE_START=$(date +%s)
STOPPED=0

record() { echo "$1" >> "$SUMMARY"; echo "$1"; }

for line in "${CASE_LINES[@]}"; do
    IFS="$SEP" read -r id cmd scmd sport ports envx fpk skip <<< "$line"
    if [ -n "$skip" ]; then record "SKIP($skip) $id"; SKIP=$((SKIP + 1)); continue; fi
    if [ "$STOPPED" -eq 1 ] || [ -f "$STOP_FILE" ]; then
        STOPPED=1; record "NOTRUN $id -- stop file present"; NOTRUN=$((NOTRUN + 1)); continue
    fi
    case_dir="$CASE_BASE/$id"; root="$case_dir/root"; log="$LOG_DIR/logs/$id.log"
    make_case_root "$root"
    mkdir -p "$case_dir/tmp" "$case_dir/home"
    dir="$root"
    while :; do
        { [ -e "$dir/.env" ] || [ -L "$dir/.env" ]; } && refuse "$dir/.env exists; the elspeth CLI loads the nearest .env above its cwd"
        [ "$dir" = "/" ] && break
        dir="$(dirname "$dir")"
    done
    printf '%s started %s\n' "$id" "$(date -u +%FT%TZ)" > "$LOG_DIR/current-case.txt"

    ENVV=(HOME="$case_dir/home" USER="${USER:-$(id -un)}" LOGNAME="${LOGNAME:-$(id -un)}" LANG=C.UTF-8
          PATH="$root/.venv/bin:/usr/local/bin:/usr/bin:/bin" TMPDIR="$case_dir/tmp"
          PYTHONPATH="$root/src:$root/elspeth-lints/src"
          ANONYMIZED_TELEMETRY=False HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 LITELLM_LOCAL_MODEL_COST_MAP=True
          HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9
          http_proxy=http://127.0.0.1:9 https_proxy=http://127.0.0.1:9
          NO_PROXY=127.0.0.1,localhost,::1 no_proxy=127.0.0.1,localhost,::1)
    [ "$fpk" = 1 ] && ENVV+=(ELSPETH_FINGERPRINT_KEY="$FINGERPRINT_KEY")
    for kv in $envx; do ENVV+=("$kv"); done
    for kv in "${ENVV[@]}"; do
        name="${kv%%=*}"
        if [[ "$name" =~ (KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|^AWS_|^AZURE_|OPENROUTER|OPENAI|ANTHROPIC) ]] \
           && [ "$name" != ELSPETH_FINGERPRINT_KEY ]; then
            refuse "case $id environment would carry $name"
        fi
    done

    # Provenance: the copy's elspeth and elspeth_lints, not the venv's editable install.
    set +e
    prov="$(cd "$root" && env -i "${ENVV[@]}" python -c 'import elspeth, elspeth_lints; print(elspeth.__file__); print(elspeth_lints.__file__)' 2>&1)"
    rc=$?
    set -e
    bad_prov=0
    [ "$rc" -eq 0 ] || bad_prov=1
    while read -r f; do case "$f" in "$root"/*) ;; *) bad_prov=1 ;; esac; done <<< "$prov"
    if [ "$bad_prov" -eq 1 ]; then
        record "NOTRUN $id -- provenance: $(echo "$prov" | tr '\n' ' ')"; NOTRUN=$((NOTRUN + 1)); continue
    fi

    set +e
    pre="$("${HELPER[@]}" preflight --cases "$CASES" --tree "$root" --case "$id" 2>&1)"
    rc=$?
    set -e
    if [ "$rc" -ne 0 ]; then
        record "NOTRUN $id -- $(echo "$pre" | tr '\n' ' ')"; NOTRUN=$((NOTRUN + 1)); continue
    fi
    cat > "$case_dir/inner.sh" <<'INNER'
#!/usr/bin/env bash
set -u
root="$1"; cmd="$2"; scmd="$3"; sport="$4"; log="$5"; server_log="$6"; rc_file="$7"; server_marker="$8"
cd "$root" || exit 2
port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
if [ -n "$scmd" ]; then
    bash -c "$scmd" > "$server_log" 2>&1 &
    for _ in $(seq 1 60); do port_busy "$sport" && break; sleep 0.5; done
    if ! port_busy "$sport"; then
        : > "$server_marker"
        exit 1
    fi
fi
bash -c "$cmd" > "$log" 2>&1
rc=$?
printf '%s\n' "$rc" > "$rc_file"
exit "$rc"
INNER
    t0=$(date +%s)
    set +e
    env -i "${ENVV[@]}" $(pin) "$BWRAP" --dev-bind / / --unshare-net --unshare-pid --proc /proc --die-with-parent -- \
        bash "$case_dir/inner.sh" "$root" "$cmd" "$scmd" "$sport" "$log" "$LOG_DIR/logs/$id.server.log" \
        "$case_dir/command.exit" "$case_dir/server.failed" > "$LOG_DIR/logs/$id.sandbox.log" 2>&1
    sandbox_rc=$?
    set -e
    secs=$(( $(date +%s) - t0 ))
    if [ -e "$case_dir/server.failed" ]; then
        record "FAIL $id -- fixture server ($scmd) did not listen on $sport; see logs/$id.server.log"
        FAIL=$((FAIL + 1)); continue
    fi
    if [ ! -f "$case_dir/command.exit" ]; then
        record "FAIL $id -- sandbox error: command exit file missing (bwrap exit $sandbox_rc; see logs/$id.sandbox.log)"
        FAIL=$((FAIL + 1)); continue
    fi
    rc="$(cat "$case_dir/command.exit")"
    if ! [[ "$rc" =~ ^(0|[1-9][0-9]{0,2})$ ]] || [ "$rc" -gt 255 ] || [ "$rc" -ne "$sandbox_rc" ]; then
        record "FAIL $id -- sandbox error: invalid command exit file or bwrap exit mismatch ($sandbox_rc; see logs/$id.sandbox.log)"
        FAIL=$((FAIL + 1)); continue
    fi

    set +e
    verdict="$("${HELPER[@]}" check --cases "$CASES" --root "$root" --case "$id" --exit "$rc" \
        --log "$log" --evidence "$LOG_DIR/evidence/$id.json" 2>&1)"
    crc=$?
    set -e
    extra=" [${secs}s]"
    if [ "$crc" -eq 0 ]; then
        PASS=$((PASS + 1)); record "$verdict$extra"
        # Remove successful case copies unless explicitly requested for inspection.
        [ "$KEEP_PASSING" -eq 1 ] || rm -rf -- "$case_dir"
    else
        case "$verdict" in FAIL*) ;; *) verdict="FAIL $id -- checker error: $(echo "$verdict" | tr '\n' ' ')" ;; esac
        FAIL=$((FAIL + 1)); record "$verdict$extra"
    fi
done

rm -f "$LOG_DIR/current-case.txt"
"${HELPER[@]}" excluded --cases "$CASES" >> "$SUMMARY"
DIGEST_AFTER="$(tree_digest "$TREE" "$LOG_DIR/tree-after.sha256")"
FROZEN=YES
[ "$DIGEST_BEFORE" = "$DIGEST_AFTER" ] || FROZEN=NO
WALL=$(( $(date +%s) - GATE_START ))
{
    echo "----"
    echo "source=$SOURCE_DESC"
    echo "totals: PASS=$PASS FAIL=$FAIL NOTRUN=$NOTRUN SKIP=$SKIP (cases) frozen=$FROZEN wall=${WALL}s"
} >> "$SUMMARY"
overall=0
{ [ "$FAIL" -eq 0 ] && [ "$NOTRUN" -eq 0 ] && [ "$FROZEN" = YES ]; } || overall=1
echo "$overall" > "$LOG_DIR/overall.exit"
echo "summary: $SUMMARY"
echo "totals: PASS=$PASS FAIL=$FAIL NOTRUN=$NOTRUN SKIP=$SKIP frozen=$FROZEN wall=${WALL}s overall=$overall"
exit "$overall"
