#!/usr/bin/env bash
# =============================================================================
# join_refused — the admission fence, shown returning a NEGATIVE
#
# Every other multi-worker example shows `elspeth join` succeeding. This one
# shows it refusing, which is the arm that protects a live run: a follower whose
# resolved settings do not hash to the leader's `runs.config_hash` is refused
# atomically, with ZERO durable mutation, while a matching follower admitted
# against the same run keeps working.
#
# Both arms run against ONE live run, concurrently:
#
#   ARM 1 (positive control)  same settings.yaml as the leader -> admitted,
#                             shares work, exits 0
#   ARM 2 (the refusal)       settings_mismatched.yaml, which differs from the
#                             leader's config by ONE scalar (temperature
#                             0.0 -> 0.1) -> JoinRefusedError, exit 1
#
# The assertion is on the REASON, not the exit code. Exit 1 is JoinRefusedError
# generically, and the admission path has four refusal arms (filesystem
# preflight, run not RUNNING, config-hash mismatch, no live leader seat), so an
# exit-1-only check would pass for the wrong reason — including for a run that
# had already finished.
#
# Usage:
#   ./examples/join_refused/run.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"
source "$PROJECT_ROOT/examples/chaosllm_env.sh"

SETTINGS="examples/join_refused/settings.yaml"
MISMATCHED="examples/join_refused/settings_mismatched.yaml"
CHAOS_CONFIG="examples/join_refused/chaos_config.yaml"
DB="examples/join_refused/runs/audit.db"
CHAOS_PORT=8199
CHAOS_PID=""
LEADER_PID=""
FOLLOWER_PID=""

cleanup() {
    for pid in "$FOLLOWER_PID" "$LEADER_PID" "$CHAOS_PID"; do
        [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
}
trap cleanup EXIT

rm -f "$DB" "$DB-wal" "$DB-shm"
rm -f examples/join_refused/output/results.json examples/join_refused/output/quarantined.json
mkdir -p examples/join_refused/runs examples/join_refused/output

ro_query() {  # one read-only scalar query against the live audit DB
    sqlite3 "file:${DB}?mode=ro" "PRAGMA query_only=ON; $1" 2>/dev/null || echo ""
}

echo "=== join_refused — the admission fence returns a negative ==="
echo ""

echo "Starting ChaosLLM server on port $CHAOS_PORT..."
.venv/bin/chaosllm serve --config "$CHAOS_CONFIG" --port "$CHAOS_PORT" --workers 1 &
CHAOS_PID=$!
for _ in $(seq 1 60); do
    curl -sf "http://127.0.0.1:$CHAOS_PORT/health" > /dev/null 2>&1 && break
    if ! kill -0 "$CHAOS_PID" 2>/dev/null; then echo "ERROR: ChaosLLM failed to start." >&2; exit 1; fi
    sleep 0.5
done
curl -sf "http://127.0.0.1:$CHAOS_PORT/health" > /dev/null 2>&1 || {
    echo "ERROR: ChaosLLM not responding after 30 seconds." >&2; exit 1; }
echo "ChaosLLM is ready."
echo ""

echo "Launching leader: elspeth run --execute ..."
.venv/bin/elspeth run --settings "$SETTINGS" --execute &
LEADER_PID=$!

# Gate on state that PERSISTS (the multi_worker idiom): RUNNING plus at least
# one leased work item, so the leader is demonstrably processing. Never a sleep.
RUN_ID=""
for _ in $(seq 1 240); do
    RUN_ID="$(ro_query "SELECT run_id FROM runs WHERE status='running' LIMIT 1;")"
    if [ -n "$RUN_ID" ]; then
        LEASED="$(ro_query "SELECT COUNT(*) FROM token_work_items WHERE run_id='$RUN_ID' AND status='leased';")"
        [ "${LEASED:-0}" -ge 1 ] && break
    fi
    RUN_ID=""
    if ! kill -0 "$LEADER_PID" 2>/dev/null; then
        echo "ERROR: leader exited before reaching RUNNING with leased work." >&2; exit 1
    fi
    sleep 0.5
done
[ -n "$RUN_ID" ] || { echo "ERROR: leader never reached RUNNING with leased work." >&2; exit 1; }
echo "Run is live: $RUN_ID"
echo ""

# --- ARM 1: the positive control, admitted and sharing work -------------------
echo "ARM 1 (positive control): joining with the leader's OWN settings..."
.venv/bin/elspeth join "$RUN_ID" --settings "$SETTINGS" --database "$DB" &
FOLLOWER_PID=$!
for _ in $(seq 1 120); do
    ACTIVE="$(ro_query "SELECT COUNT(*) FROM run_workers WHERE run_id='$RUN_ID' AND role='follower' AND status='active';")"
    [ "${ACTIVE:-0}" -ge 1 ] && break
    sleep 0.5
done
[ "${ACTIVE:-0}" -ge 1 ] || { echo "ERROR: the matching follower was never admitted." >&2; exit 1; }
echo "  admitted (active follower rows: $ACTIVE)"
echo ""

# --- ARM 2: the refusal, against the SAME live run ---------------------------
# run_workers rows are single-use and never deleted, so COUNT(*) is a stable
# zero-mutation probe: an admitted joiner INSERTs a row, a refused one must not.
WORKERS_BEFORE="$(ro_query "SELECT COUNT(*) FROM run_workers WHERE run_id='$RUN_ID';")"
echo "ARM 2 (the refusal): joining with settings_mismatched.yaml..."
REFUSAL_OUTPUT="$(mktemp)"
REFUSAL_RC=0
# Guarded: exit 1 here is the POINT of the example, and `set -e` would abort.
.venv/bin/elspeth join "$RUN_ID" --settings "$MISMATCHED" --database "$DB" \
    > "$REFUSAL_OUTPUT" 2>&1 || REFUSAL_RC=$?
WORKERS_AFTER="$(ro_query "SELECT COUNT(*) FROM run_workers WHERE run_id='$RUN_ID';")"

echo "  exit code: $REFUSAL_RC"
echo "  reason:"
sed 's/^/    /' "$REFUSAL_OUTPUT"
echo "  run_workers rows: $WORKERS_BEFORE before, $WORKERS_AFTER after"
echo ""

FAILURES=0
if [ "$REFUSAL_RC" -ne 1 ]; then
    echo "✗ FAIL: mismatched join exited $REFUSAL_RC, expected 1 (JoinRefusedError)." >&2
    FAILURES=$((FAILURES + 1))
fi
# The reason is the real assertion: it distinguishes THIS refusal arm from the
# other three, all of which also exit 1.
if ! grep -q "does not match the run's config_hash" "$REFUSAL_OUTPUT"; then
    echo "✗ FAIL: refusal did not name a config_hash mismatch — it may have been refused" >&2
    echo "        for a different reason (run not RUNNING, no live leader, preflight)." >&2
    FAILURES=$((FAILURES + 1))
fi
if [ "$WORKERS_BEFORE" != "$WORKERS_AFTER" ]; then
    echo "✗ FAIL: refused join mutated run_workers ($WORKERS_BEFORE -> $WORKERS_AFTER); admission must be atomic." >&2
    FAILURES=$((FAILURES + 1))
fi
rm -f "$REFUSAL_OUTPUT"

# --- Let the pack finish ------------------------------------------------------
echo "Waiting for the leader and the admitted follower to finish..."
LEADER_RC=0; wait "$LEADER_PID" || LEADER_RC=$?; LEADER_PID=""
FOLLOWER_RC=0; wait "$FOLLOWER_PID" || FOLLOWER_RC=$?; FOLLOWER_PID=""
echo "  leader exit=$LEADER_RC, admitted follower exit=$FOLLOWER_RC"
echo ""

[ "$LEADER_RC" -eq 0 ] || { echo "✗ FAIL: leader exited $LEADER_RC, expected 0." >&2; FAILURES=$((FAILURES + 1)); }
[ "$FOLLOWER_RC" -eq 0 ] || { echo "✗ FAIL: admitted follower exited $FOLLOWER_RC, expected 0." >&2; FAILURES=$((FAILURES + 1)); }

# The positive control must have done REAL work, not merely been admitted.
# Attribution comes from scheduler_events.from_lease_owner on mark_pending_sink
# (see examples/multi_worker/README.md § Attribution mechanism).
echo "Per-worker attribution (scheduler_events grouped by from_lease_owner):"
sqlite3 "file:${DB}?mode=ro" <<SQL
PRAGMA query_only = ON;
SELECT se.from_lease_owner, w.role, COUNT(*) AS completed_rows
FROM scheduler_events se
LEFT JOIN run_workers w ON w.worker_id = se.from_lease_owner AND w.run_id = se.run_id
WHERE se.run_id = '$RUN_ID'
  AND se.event_type IN ('mark_pending_sink', 'mark_failed')
  AND se.from_lease_owner IS NOT NULL
GROUP BY se.from_lease_owner, w.role
ORDER BY w.role DESC, completed_rows DESC;
SQL
WORKER_COUNT="$(ro_query "SELECT COUNT(*) FROM (SELECT from_lease_owner FROM scheduler_events WHERE run_id='$RUN_ID' AND event_type IN ('mark_pending_sink','mark_failed') AND from_lease_owner IS NOT NULL GROUP BY from_lease_owner);")"
REFUSED_REGISTERED="$(ro_query "SELECT COUNT(*) FROM run_workers WHERE run_id='$RUN_ID';")"
echo ""

if [ "${WORKER_COUNT:-0}" -lt 2 ]; then
    echo "✗ FAIL: only ${WORKER_COUNT:-0} worker(s) completed rows; the positive control never shared work" >&2
    echo "        (join-window race — raise the item count in input.jsonl and re-run)." >&2
    FAILURES=$((FAILURES + 1))
fi

if [ "$FAILURES" -eq 0 ]; then
    echo "VERIFIED: the run admitted 2 workers and refused 1; the refusal named the"
    echo "          config_hash mismatch, exited 1, and left run_workers at"
    echo "          $REFUSED_REGISTERED rows — the joiner that lost is not in the registry."
    echo ""
    echo "✓ PASS: admission fence refused a mismatched joiner without disturbing the live run"
    exit 0
fi
echo "✗ FAIL: $FAILURES assertion(s) failed" >&2
exit 1
