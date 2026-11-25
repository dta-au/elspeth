#!/usr/bin/env bash
# examples-gate.sh — run every offline shipped example the way its documentation
# says to run it, and check each against its documented result.
#
# The examples gate runs the non-network examples to catch documented result
# drift that config validation alone cannot see. Before this script, the
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
#      directory, top-level pipeline config and *.sh launcher in that tree
#      (examples_gate.py validate). A new example cannot be silently left out.
#   3. For each case, in order, serially:
#        - a fresh copy of the tree (tests/, docs/, website/, .claude/ left out; .venv is a
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
# --execute requires bwrap (bubblewrap) and assumes the bwrap on PATH is the
# system's own. Each case runs in a sandbox whose filesystem is an allowlist:
# /usr, the venv and a fixed list of /etc entries read-only, the case directory
# read-write, and the case's declared ~/.cache prerequisites read-only in its
# otherwise empty HOME. The sandbox root is read-only. The case's server and
# command share one private network with loopback only, in private PID and IPC
# namespaces and a new session; /tmp and /run start empty; stdin is /dev/null
# and no file descriptor above 2 is inherited. The case environment has no
# inherited credentials. The shared venv is outside the exported-tree digest.
#
# Checked before the first case, with the shared binds: every bind source is
# inside its allowed root (an /etc source is one of the listed entries), and no
# mount point, socket or FIFO is beneath it. Checked before each case: the
# same for the shared binds' sources and mount points, and the full walk for
# the case's own binds. The gate assumes the shared bind sources do not change
# during the run.
#
# Self-test, before the first case, inside the sandbox: network interfaces are
# loopback only and an outbound connect fails; an abstract socket and a path
# socket held outside are unreachable, and a host path that is not bound is
# absent; /run is empty and no SysV IPC object is visible; no descriptor above
# 2 is open; every mount is read-only except a listed writable set (the
# private /dev, /proc, /tmp, /run) and its own directory; writes into the venv,
# into /usr and into a read-only probe bind, and a mkdir at /, fail read-only;
# /etc holds only the listed entries. Each case, before its command, checks the
# mounts the same way with its own directory as the writable one. It must
# print this run's nonce, which shows the output came from this run. The text
# guard remains an early refusal.
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

main() {
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
        -h|--help) usage; return 0 ;;
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
ROOT="$(cd -P "$ROOT" && pwd -P)"
case "$TMP_ROOT/" in "$(realpath "$ROOT")"/*) die "--tmp-root must not be inside the checkout ($ROOT)" ;; esac
case "$TMP_ROOT" in /tmp|/tmp/*) die "--tmp-root must not be under /tmp" ;; esac
[ -n "$VENV" ] || VENV="$ROOT/.venv"
VENV="$(cd "$VENV" && pwd -P)" || die "--venv $VENV does not exist"
cases_dir="$(cd -P "$(dirname "$CASES")" && pwd -P)" || die "case data directory for $CASES does not exist"
CASES="$cases_dir/$(basename "$CASES")"
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
    case "$output_path" in /tmp|/tmp/*) die "$output_path must not be under /tmp" ;; esac
done
PY="$VENV/bin/python"
[ -x "$PY" ] || die "no interpreter at $PY"
HELPER=("$PY" -I "$SCRIPT_DIR/examples_gate.py")
SEP=$'\x1f'

# ------------------------------------------------------------------ the tree
if [ -n "$TREE" ]; then
    TREE="$(cd -P "$TREE" && pwd -P)"
    case "$TMP_ROOT/" in "$TREE"/*) die "--tmp-root must not be inside --tree ($TREE)" ;; esac
    for output_path in "$LOG_DIR" "$STOP_FILE"; do
        case "$output_path/" in "$TREE"/*) die "$output_path must not be inside --tree" ;; esac
    done
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
    echo "  only       : ${ONLY:-ALL}"
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
    "${HELPER[@]}" list --cases "$CASES" ${ONLY:+--only "$ONLY"} | while IFS="$SEP" read -r id cmd scmd sport ports envx fpk homero skip; do
        if [ -n "$skip" ]; then echo "  SKIP $id -- $skip"; continue; fi
        echo "  $id: ${envx:+$envx }$cmd${scmd:+   [server: $scmd]}${ports:+   [ports: $ports]}"
    done
    echo "dry run: nothing executed (pass --execute)"
    return 0
fi

# ---------------------------------------------------------------- preflight
[ -e "$LOG_DIR/summary.txt" ] && die "log dir $LOG_DIR already holds a run; pass a fresh --log-dir"
mkdir -p "$LOG_DIR/logs" "$LOG_DIR/evidence"
SUMMARY="$LOG_DIR/summary.txt"
PREFLIGHT="$LOG_DIR/preflight.txt"
: > "$SUMMARY"
plan > "$PREFLIGHT"
echo "overall=running" > "$LOG_DIR/overall.exit"
on_exit() {
    local rc=$?
    if [ -f "$LOG_DIR/overall.exit" ] && [ "$(cat "$LOG_DIR/overall.exit")" = overall=running ]; then
        printf 'overall=%s (aborted)\n' "$rc" > "$LOG_DIR/overall.exit"
    fi
}
trap on_exit EXIT

refuse() { echo "REFUSED: $*" | tee -a "$PREFLIGHT" >&2; echo 2 > "$LOG_DIR/overall.exit"; exit 2; }

cp -- "$CASES" "$LOG_DIR/cases.yaml" || refuse "could not snapshot case data"
cp -- "$SCRIPT_DIR/examples_gate.py" "$LOG_DIR/examples_gate.py" || refuse "could not snapshot helper"
CASES="$LOG_DIR/cases.yaml"
HELPER=("$PY" -I "$LOG_DIR/examples_gate.py")
echo "cases_sha256=$(sha256sum "$CASES" | cut -d' ' -f1)" >> "$PREFLIGHT"
echo "helper_sha256=$(sha256sum "$LOG_DIR/examples_gate.py" | cut -d' ' -f1)" >> "$PREFLIGHT"
echo "driver_sha256=$(sha256sum "${BASH_SOURCE[0]}" | cut -d' ' -f1)" >> "$PREFLIGHT"

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
total_count="$("${HELPER[@]}" count --cases "$CASES")"
echo "selected=$count total=$total_count only=${ONLY:-ALL}" >> "$PREFLIGHT"

BWRAP="$(command -v bwrap || true)"
[ -n "$BWRAP" ] || refuse "bwrap (bubblewrap) is required for kernel-enforced offline execution"
BWRAP="$(realpath "$BWRAP")"
# The self-test below assumes this bwrap is the system's own. Record which binary ran.
echo "bwrap=$BWRAP owner=$(stat -c %U "$BWRAP") sha256=$(sha256sum "$BWRAP" | cut -d' ' -f1)" >> "$PREFLIGHT"
pin() { if [ -n "$CPUS" ]; then echo taskset -c "$CPUS"; fi; }
# The sandbox filesystem is an allowlist: what is not bound here, or per case below, does not
# exist inside. Read-only: /usr (and the top-level links or directories that lead into it), a
# short list of /etc entries, and the venv.
BWRAP_ARGS=(--ro-bind /usr /usr)
for top in bin sbin lib lib32 lib64 libx32; do
    if [ -L "/$top" ]; then
        BWRAP_ARGS+=(--symlink "$(readlink "/$top")" "/$top")
    elif [ -d "/$top" ]; then
        BWRAP_ARGS+=(--ro-bind "/$top" "/$top")
    fi
done
# The helper refuses any /etc source not in its own copy of this list (ETC_BIND_SOURCES).
ETC_ENTRIES=(passwd group nsswitch.conf hosts resolv.conf ld.so.cache localtime protocols services
             mime.types alternatives ssl/certs ssl/openssl.cnf)
ETC_TOPS="$(printf '%s\n' "${ETC_ENTRIES[@]%%/*}" | sort -u | paste -sd, -)"
for etc_entry in "${ETC_ENTRIES[@]}"; do
    BWRAP_ARGS+=(--ro-bind-try "/etc/$etc_entry" "/etc/$etc_entry")
done
BWRAP_ARGS+=(--ro-bind "$VENV" "$VENV" --dev /dev --proc /proc --tmpfs /tmp --tmpfs /run
             --unshare-net --unshare-pid --unshare-ipc --new-session --die-with-parent)

# bind_check LABEL [--allow ROOT]... [--shared-count N] -- BWRAP-OPTIONS...
# Refuses, naming the bind, when a bind source is outside the allowed roots or a socket file or
# mount point is beneath one; a check that ends without its completion line says how it ended.
bind_check() {
    local label="$1" out rc how
    shift
    set +e
    out="$("${HELPER[@]}" socket-walk "$@" 2>&1)"
    rc=$?
    set -e
    printf '%s\n' "$out" >> "$PREFLIGHT"
    if [ "$rc" -eq 0 ] && grep -q '^socket-walk complete: ' <<< "$out"; then
        return 0
    fi
    if [ "$rc" -eq 1 ] && grep -q '^REFUSE ' <<< "$out"; then
        refuse "$label sandbox bind check failed: $(grep '^REFUSE ' <<< "$out" | tr '\n' ' ')"
    fi
    if [ "$rc" -gt 128 ]; then how="signal $((rc - 128))"; else how="exit $rc"; fi
    refuse "$label sandbox bind check ended without a result ($how): $(tr '\n' ' ' <<< "$out")"
}
bind_check "shared" --allow "$VENV" -- "${BWRAP_ARGS[@]}"

# in_sandbox ENV... -- BINDS... -- COMMAND...
# Runs COMMAND under env -i ENV in the sandbox with the shared binds plus BINDS and a read-only
# root; stdin is /dev/null and no file descriptor above 2 is inherited.
in_sandbox() {
    local -a envv=() binds=()
    while [ "$1" != -- ]; do envv+=("$1"); shift; done
    shift
    while [ "$1" != -- ]; do binds+=("$1"); shift; done
    shift
    (
        exec < /dev/null
        for fd in /proc/"$BASHPID"/fd/*; do
            fd="${fd##*/}"
            if [ "$fd" -gt 2 ]; then exec {fd}>&-; fi
        done
        exec env -i "${envv[@]}" $(pin) "$BWRAP" "${BWRAP_ARGS[@]}" "${binds[@]}" --remount-ro / -- "$@"
    )
}

# Every mount inside the sandbox is read-only except these, and the run's own directory (the
# self-test's, or the case's). An entry that is absent is fine; a writable mount not listed refuses.
WRITABLE_MOUNTS=(
    /dev            # bwrap --dev: a private minimal device directory
    /dev/null       # device node bound by --dev
    /dev/zero       # device node bound by --dev
    /dev/full       # device node bound by --dev
    /dev/random     # device node bound by --dev
    /dev/urandom    # device node bound by --dev
    /dev/tty        # device node bound by --dev
    /dev/pts        # --dev: a private devpts instance
    /proc           # --proc: procfs for the private PID namespace
    /tmp            # --tmpfs: empty, private
    /run            # --tmpfs: empty, private
)
# Read /proc/self/mountinfo inside the sandbox; argv: the writable set.
MOUNT_CHECK_PY='
import re
import sys

writable = set(sys.argv[1:])
loose = []
for line in open("/proc/self/mountinfo"):
    fields = line.split()
    point = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), fields[4])
    if "ro" not in fields[5].split(",") and point not in writable:
        loose.append(point)
if loose:
    sys.exit("writable mount outside the writable set: " + ", ".join(loose))
print("mount-check-ok")
'

# Self-test: the properties listed in the header, checked inside the sandbox. The probe must echo
# this run's nonce, which shows its output came from this run.
SELF_TEST_DIR="$CASE_BASE/self-test"
OUTSIDE_DIR="$CASE_BASE/outside"
mkdir -p "$SELF_TEST_DIR/ro" "$OUTSIDE_DIR"
printf '%s' "$MOUNT_CHECK_PY" > "$SELF_TEST_DIR/mount-check.py"
NONCE="$("$PY" -I -c 'import secrets; print(secrets.token_hex(16))')"
# A listener outside every bind (path and abstract name), reachable from here.
"$PY" -I -c '
import os
import socket
import sys
import time
directory, abstract, done = sys.argv[1:4]
os.chdir(directory)  # a relative name: no AF_UNIX path-length limit
with socket.socket(socket.AF_UNIX) as server, socket.socket(socket.AF_UNIX) as named:
    server.bind("listener.sock")
    server.listen(4)
    named.bind("\0" + abstract)
    named.listen(4)
    for address in ("listener.sock", "\0" + abstract):
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(address)
    open("ready", "w").close()
    deadline = time.monotonic() + 600
    while not os.path.exists(done):
        if time.monotonic() > deadline:
            sys.exit("the self-test never finished; the listener stopped without it")
        time.sleep(0.1)
' "$OUTSIDE_DIR" "examples-gate-$NONCE" "$OUTSIDE_DIR/done" > "$LOG_DIR/self-test-listener.log" 2>&1 &
listener_pid=$!
for _ in $(seq 1 100); do
    [ -e "$OUTSIDE_DIR/ready" ] && break
    kill -0 "$listener_pid" 2>/dev/null || break
    sleep 0.1
done
[ -e "$OUTSIDE_DIR/ready" ] || refuse "self-test listener did not start (see $LOG_DIR/self-test-listener.log)"
set +e
in_sandbox HOME="$SELF_TEST_DIR" PATH="/usr/local/bin:/usr/bin:/bin" TMPDIR="$SELF_TEST_DIR" LANG=C.UTF-8 -- \
    --bind "$SELF_TEST_DIR" "$SELF_TEST_DIR" --ro-bind "$SELF_TEST_DIR/ro" "$SELF_TEST_DIR/ro" -- \
    "$PY" -I -c '
import errno
import os
import socket
import subprocess
import sys
nonce, listener, abstract, read_only_target, absent, venv, etc_tops, mount_check = sys.argv[1:9]
writable = sys.argv[9:]


def write_refused(path):
    # O_EXCL: a file this probe did not make is never opened, and one it made by mistake is removed.
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as error:
        assert error.errno == errno.EROFS, (path, error)
    else:
        os.close(descriptor)
        os.unlink(path)
        raise AssertionError(f"write {path} succeeded")


def refused(code, action, *args):
    try:
        action(*args)
    except OSError as error:
        assert error.errno == code, (action, args, error)
    else:
        raise AssertionError(f"{action} {args} succeeded")


interfaces = {line.split(":", 1)[0].strip() for line in open("/proc/net/dev").read().splitlines() if ":" in line}
assert interfaces == {"lo"}, interfaces
assert os.listdir("/run") == [], os.listdir("/run")
inherited = [fd for fd in os.listdir("/proc/self/fd") if int(fd) > 2 and os.path.exists(f"/proc/self/fd/{fd}")]
assert inherited == [], {fd: os.readlink(f"/proc/self/fd/{fd}") for fd in inherited}
for kind in ("shm", "msg", "sem"):
    rows = [row for row in open(f"/proc/sysvipc/{kind}").read().splitlines()[1:] if row.strip()]
    assert rows == [], (kind, rows)
checked = subprocess.run([sys.executable, "-I", mount_check, *writable], capture_output=True, text=True, check=False)
assert checked.returncode == 0 and checked.stdout.strip() == "mount-check-ok", (checked.stdout, checked.stderr)
for target in (read_only_target, os.path.join(venv, "examples-gate-probe"), "/usr/examples-gate-probe"):
    write_refused(target)
refused(errno.EROFS, os.mkdir, "/examples-gate-probe")
unlisted = set(os.listdir("/etc")) - set(etc_tops.split(","))
assert not unlisted, ("/etc", sorted(unlisted))
refused(errno.ENOENT, os.stat, absent)
refused(errno.ENOENT, os.stat, listener)
refused(errno.ENOENT, os.chdir, os.path.dirname(listener))
with socket.socket(socket.AF_UNIX) as client:
    refused(errno.ECONNREFUSED, client.connect, "\0" + abstract)
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
print(f"self-test-ok {nonce}")
' "$NONCE" "$OUTSIDE_DIR/listener.sock" "examples-gate-$NONCE" "$SELF_TEST_DIR/ro/write-probe" "$LOG_DIR" "$VENV" "$ETC_TOPS" \
    "$SELF_TEST_DIR/mount-check.py" "${WRITABLE_MOUNTS[@]}" "$SELF_TEST_DIR" \
    > "$LOG_DIR/self-test.out" 2>&1
self_test_rc=$?
: > "$OUTSIDE_DIR/done"
wait "$listener_pid"
listener_rc=$?
set -e
cat "$LOG_DIR/self-test.out" >> "$PREFLIGHT"
echo "self_test_rc=$self_test_rc listener_rc=$listener_rc" >> "$PREFLIGHT"
if [ "$self_test_rc" -ne 0 ] || [ "$listener_rc" -ne 0 ] || ! grep -Fxq "self-test-ok $NONCE" "$LOG_DIR/self-test.out"; then
    refuse "bwrap sandbox self-test failed (see $PREFLIGHT)"
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
            tests|docs|website|.venv|.git|.claude) ;;
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
    IFS="$SEP" read -r id cmd scmd sport ports envx fpk homero skip <<< "$line"
    if [ -n "$skip" ]; then record "SKIP($skip) $id"; SKIP=$((SKIP + 1)); continue; fi
    if [ "$STOPPED" -eq 1 ] || [ -f "$STOP_FILE" ]; then
        STOPPED=1; record "NOTRUN $id -- stop file present"; NOTRUN=$((NOTRUN + 1)); continue
    fi
    case_dir="$CASE_BASE/$id"; root="$case_dir/root"; log="$LOG_DIR/logs/$id.log"
    make_case_root "$root"
    mkdir -p "$case_dir/tmp" "$case_dir/home"
    "${HELPER[@]}" reset-audit --cases "$CASES" --root "$root" --case "$id" > "$case_dir/reset-audit.log"
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
    if [ -n "$envx" ]; then
        read -r -a extra_env <<< "$envx"
        ENVV+=("${extra_env[@]}")
    fi
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
    # The case's own binds, one list for the check and the launch. A home_ro directory is bound
    # from the physical ~/.cache and judged against that root, so a link out of it is refused.
    case_binds=(--bind "$case_dir" "$case_dir")
    home_allow=()
    if [ -n "$homero" ]; then
        home_cache="$(realpath -m "$HOME/.cache")"
        home_allow=(--allow "$home_cache")
        IFS=, read -r -a home_dirs <<< "$homero"
        for rel in "${home_dirs[@]}"; do
            mkdir -p "$case_dir/home/$rel"
            case_binds+=(--ro-bind "$home_cache/${rel#.cache/}" "$case_dir/home/$rel")
        done
    fi
    # Every bind of this launch: sources and mount points of the shared binds again, sockets for
    # the case's own binds (the shared binds' sockets were walked once, before the first case).
    bind_check "case $id" --allow "$VENV" --allow "$CASE_BASE" "${home_allow[@]}" --shared-count "${#BWRAP_ARGS[@]}" -- \
        "${BWRAP_ARGS[@]}" "${case_binds[@]}"
    cat > "$case_dir/inner.sh" <<'INNER'
#!/usr/bin/env bash
set -u
root="$1"; cmd="$2"; scmd="$3"; sport="$4"; log="$5"; server_log="$6"; rc_file="$7"; server_marker="$8"
python="$9"; mount_check="${10}"; mount_out="${11}"; mount_marker="${12}"
shift 12
if ! "$python" -I "$mount_check" "$@" > "$mount_out" 2>&1; then
    : > "$mount_marker"
    exit 2
fi
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
    printf '%s' "$MOUNT_CHECK_PY" > "$case_dir/mount-check.py"
    t0=$(date +%s)
    set +e
    in_sandbox "${ENVV[@]}" -- "${case_binds[@]}" -- \
        bash "$case_dir/inner.sh" "$root" "$cmd" "$scmd" "$sport" "$case_dir/command.log" "$case_dir/server.log" \
        "$case_dir/command.exit" "$case_dir/server.failed" \
        "$PY" "$case_dir/mount-check.py" "$case_dir/mount-check.out" "$case_dir/mount.failed" \
        "${WRITABLE_MOUNTS[@]}" "$case_dir" > "$LOG_DIR/logs/$id.sandbox.log" 2>&1
    sandbox_rc=$?
    set -e
    if [ -e "$case_dir/mount.failed" ]; then
        refuse "case $id sandbox mount check failed: $(tr '\n' ' ' < "$case_dir/mount-check.out")"
    fi
    [ ! -f "$case_dir/command.log" ] || cp -- "$case_dir/command.log" "$log"
    [ ! -f "$case_dir/server.log" ] || cp -- "$case_dir/server.log" "$LOG_DIR/logs/$id.server.log"
    secs=$(( $(date +%s) - t0 ))
    set +e
    rc="$("${HELPER[@]}" classify --server-marker "$case_dir/server.failed" --exit-file "$case_dir/command.exit" --sandbox-rc "$sandbox_rc")"
    classified=$?
    set -e
    if [ "$classified" -ne 0 ]; then
        record "FAIL $id -- $rc; see logs/$id.sandbox.log"
        FAIL=$((FAIL + 1)); continue
    fi

    set +e
    verdict="$("${HELPER[@]}" check --cases "$CASES" --root "$root" --case "$id" --exit "$rc" \
        --log "$log" --evidence "$LOG_DIR/evidence/$id.json" 2> "$LOG_DIR/logs/$id.check.stderr.log")"
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
[ "$(sha256sum "$CASES" | cut -d' ' -f1)" = "$(awk -F= '/^cases_sha256=/{print $2}' "$PREFLIGHT")" ] || FROZEN=NO
[ "$(sha256sum "$LOG_DIR/examples_gate.py" | cut -d' ' -f1)" = "$(awk -F= '/^helper_sha256=/{print $2}' "$PREFLIGHT")" ] || FROZEN=NO
WALL=$(( $(date +%s) - GATE_START ))
totals="$("${HELPER[@]}" totals --pass-count "$PASS" --fail-count "$FAIL" --notrun-count "$NOTRUN" \
    --skip-count "$SKIP" --selected "$count" --total "$total_count" --frozen "$FROZEN" --wall "$WALL")"
{
    echo "----"
    echo "source=$SOURCE_DESC"
    echo "only=${ONLY:-ALL}"
    echo "$totals"
} >> "$SUMMARY"
overall="$("${HELPER[@]}" overall --pass-count "$PASS" --fail-count "$FAIL" --notrun-count "$NOTRUN" --frozen "$FROZEN")"
echo "$overall" > "$LOG_DIR/overall.exit"
echo "summary: $SUMMARY"
echo "$totals overall=$overall"
return "$overall"
}

main "$@"; exit $?
