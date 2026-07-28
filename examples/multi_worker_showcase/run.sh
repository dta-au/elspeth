#!/usr/bin/env bash
# =============================================================================
# multi_worker_showcase — elspeth join: self-verifying 4-worker swarm
#
# Backgrounds a LEADER (elspeth run), polls the audit DB read-only until the
# run is RUNNING with >=1 claimed work item, then launches WORKERS (default 3)
# FOLLOWERS via `elspeth join <run_id>`. After all processes exit it renders an
# ASCII stats card (workers spawned, total rows, rows/sec, succeeded/failed)
# and fails unless at least two workers processed outcomes. ADR-030
# "One-Host WAL Pack".
#
# Usage:   ./examples/multi_worker_showcase/run.sh           # leader + 3 followers (4-way)
#          WORKERS=1 ./examples/multi_worker_showcase/run.sh  # smaller swarm for quick dev
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"
source "$PROJECT_ROOT/examples/chaosllm_env.sh"

CHAOS_CONFIG="examples/multi_worker_showcase/chaos_config.yaml"
PIPELINE_CONFIG="examples/multi_worker_showcase/settings.yaml"
DB="examples/multi_worker_showcase/runs/audit.db"
CHAOS_PORT=8199
CHAOS_PID=""
LEADER_PID=""
FOLLOWER_PIDS=()
WORKERS="${WORKERS:-3}"

if [[ ! "$WORKERS" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: WORKERS must be a positive integer." >&2
    exit 2
fi

START=$(date +%s)

cleanup() {
    # Kill any still-running followers and the leader, then the ChaosLLM server.
    # Array-length-guarded so an empty FOLLOWER_PIDS under `set -u` does not
    # expand to one empty-string element (which would run `kill -0 ""`).
    if [ ${#FOLLOWER_PIDS[@]} -gt 0 ]; then
        for pid in "${FOLLOWER_PIDS[@]}"; do
            kill -0 "$pid" 2>/dev/null && kill "$pid" 2>/dev/null || true
        done
    fi
    [ -n "$LEADER_PID" ] && kill -0 "$LEADER_PID" 2>/dev/null && kill "$LEADER_PID" 2>/dev/null || true
    if [ -n "$CHAOS_PID" ] && kill -0 "$CHAOS_PID" 2>/dev/null; then
        echo ""
        echo "Stopping ChaosLLM server (PID $CHAOS_PID)..."
        kill "$CHAOS_PID" 2>/dev/null || true
        wait "$CHAOS_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# Clean previous run artifacts
rm -f examples/multi_worker_showcase/runs/audit.db \
      examples/multi_worker_showcase/runs/audit.db-wal \
      examples/multi_worker_showcase/runs/audit.db-shm
rm -f examples/multi_worker_showcase/output/results.json \
      examples/multi_worker_showcase/output/quarantined.json
mkdir -p examples/multi_worker_showcase/runs examples/multi_worker_showcase/output

echo "=== multi_worker_showcase — self-verifying 4-worker swarm (200 outcomes) ==="
echo "    leader + $WORKERS follower(s) = $((WORKERS+1))-way pack"
echo ""

# --- Start ChaosLLM (errorworks bug: must use --workers 1) ---
echo "Starting ChaosLLM server on port $CHAOS_PORT..."
.venv/bin/chaosllm serve --config "$CHAOS_CONFIG" --port "$CHAOS_PORT" --workers 1 &
CHAOS_PID=$!
echo "Waiting for ChaosLLM to be ready..."
for i in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:$CHAOS_PORT/health" > /dev/null 2>&1; then
        echo "ChaosLLM is ready."; echo ""; break
    fi
    if ! kill -0 "$CHAOS_PID" 2>/dev/null; then echo "ERROR: ChaosLLM failed to start."; exit 1; fi
    sleep 0.5
done
if ! curl -sf "http://127.0.0.1:$CHAOS_PORT/health" > /dev/null 2>&1; then
    echo "ERROR: ChaosLLM not responding after 15 seconds."; exit 1
fi

# --- Launch LEADER (background) ---
echo "Launching leader: elspeth run --execute ..."
.venv/bin/elspeth run --settings "$PIPELINE_CONFIG" --execute &
LEADER_PID=$!

# --- Poll audit DB (read-only) for RUNNING run with >=1 claimed work item ---
# Readiness criterion (design D4): RUNNING is not enough; require >=1 'leased'
# token_work_item so the leader has demonstrably begun processing before a
# follower attaches. Bounded retry; no artificial sleep widens the join window.
RUN_ID=""
for attempt in $(seq 1 60); do
    RUN_ID="$(sqlite3 "file:${DB}?mode=ro" \
        "PRAGMA query_only=ON; SELECT run_id FROM runs WHERE status='running' LIMIT 1;" 2>/dev/null || true)"
    if [ -n "$RUN_ID" ]; then
        CLAIMED="$(sqlite3 "file:${DB}?mode=ro" \
            "PRAGMA query_only=ON; SELECT COUNT(*) FROM token_work_items WHERE run_id='$RUN_ID' AND status='leased';" 2>/dev/null || echo 0)"
        if [ "${CLAIMED:-0}" -ge 1 ]; then
            echo "Leader RUNNING (run_id=$RUN_ID) with $CLAIMED claimed item(s); joining."
            break
        fi
    fi
    # Guard: leader may have already finished (degenerate fast-drain race)
    if ! kill -0 "$LEADER_PID" 2>/dev/null; then
        echo "WARNING: leader exited before followers could join (fast-drain race)." >&2
        break
    fi
    sleep 0.5
done
if [ -z "$RUN_ID" ]; then
    echo "ERROR: leader never reached RUNNING within the poll window." >&2
fi

# --- Launch FOLLOWERS (same --settings => identical config_hash; NO --execute) ---
# elspeth join executes unconditionally — there is no --execute flag on join.
if [ -n "$RUN_ID" ]; then
    for i in $(seq 1 "$WORKERS"); do
        echo "Launching follower $i: elspeth join $RUN_ID ..."
        .venv/bin/elspeth join "$RUN_ID" --settings "$PIPELINE_CONFIG" &
        FOLLOWER_PIDS+=("$!")
    done
fi

# --- Optionally tail combined progress every ~2s while leader is alive ---
echo ""
echo "--- Live progress (token_work_items status counts) ---"
while kill -0 "$LEADER_PID" 2>/dev/null && [ -n "$RUN_ID" ]; do
    COUNTS="$(sqlite3 "file:${DB}?mode=ro" \
        "PRAGMA query_only=ON; SELECT status, COUNT(*) FROM token_work_items WHERE run_id='$RUN_ID' GROUP BY status;" 2>/dev/null || true)"
    printf "\r  %s" "$(echo "$COUNTS" | tr '\n' '  ')"
    sleep 2
done
echo ""
echo "--- Leader finished ---"
echo ""

# --- Reap leader + followers; every non-zero child exit fails the showcase ---
WORKER_FAILED=0
if wait "$LEADER_PID"; then
    LEADER_EXIT=0
else
    LEADER_EXIT=$?
    WORKER_FAILED=1
fi
echo "Leader exited ($LEADER_EXIT)."
if [ ${#FOLLOWER_PIDS[@]} -gt 0 ]; then
    for pid in "${FOLLOWER_PIDS[@]}"; do
        if wait "$pid"; then
            echo "Follower $pid exited cleanly (0)."
        else
            follower_exit=$?
            echo "Follower $pid exited non-zero ($follower_exit) (see exit-code semantics in README)." >&2
            WORKER_FAILED=1
        fi
    done
fi

END=$(date +%s)
ELAPSED=$((END - START))
[ "$ELAPSED" -lt 1 ] && ELAPSED=1

# --- ASCII stats card (read-only queries) ---
echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║        multi_worker_showcase — run complete          ║"
echo "╚══════════════════════════════════════════════════════╝"

CONTRIBUTING_WORKERS=0
if [ -n "$RUN_ID" ]; then
    WORKERS_SPAWNED="$(sqlite3 "file:${DB}?mode=ro" \
        "PRAGMA query_only=ON; SELECT COUNT(*) FROM run_workers WHERE run_id='$RUN_ID';" 2>/dev/null || echo 0)"
    CONTRIBUTING_WORKERS="$(sqlite3 "file:${DB}?mode=ro" \
        "PRAGMA query_only=ON; SELECT COUNT(*) FROM (SELECT from_lease_owner FROM scheduler_events WHERE run_id='$RUN_ID' AND event_type IN ('mark_pending_sink','mark_failed') AND from_lease_owner IS NOT NULL GROUP BY from_lease_owner HAVING COUNT(*) >= 1);" 2>/dev/null || echo 0)"
    OUTCOME_COUNTS="$(bash "$SCRIPT_DIR/outcome_stats.sh" "$DB" "$RUN_ID")"
    IFS='|' read -r TOTAL_ROWS SUCCEEDED FAILED EXTRA_COUNT <<< "$OUTCOME_COUNTS"
    if [ -n "${EXTRA_COUNT:-}" ] \
        || [[ ! "$TOTAL_ROWS" =~ ^[0-9]+$ ]] \
        || [[ ! "$SUCCEEDED" =~ ^[0-9]+$ ]] \
        || [[ ! "$FAILED" =~ ^[0-9]+$ ]]; then
        echo "ERROR: invalid outcome counts: $OUTCOME_COUNTS" >&2
        exit 1
    fi
    if [[ ! "$WORKERS_SPAWNED" =~ ^[0-9]+$ ]] \
        || [[ ! "$CONTRIBUTING_WORKERS" =~ ^[0-9]+$ ]]; then
        echo "ERROR: invalid worker counts: spawned=$WORKERS_SPAWNED contributing=$CONTRIBUTING_WORKERS" >&2
        exit 1
    fi
    ROWS_PER_SEC=$((TOTAL_ROWS / ELAPSED))

    echo ""
    printf "  Workers spawned:   %s\n" "$WORKERS_SPAWNED"
    printf "  Workers with work: %s\n" "$CONTRIBUTING_WORKERS"
    printf "  Total rows done:   %s\n" "$TOTAL_ROWS"
    printf "  Succeeded:         %s\n" "$SUCCEEDED"
    printf "  Failed outcomes:   %s\n" "$FAILED"
    printf "  Wall-clock:        %ss\n" "$ELAPSED"
    printf "  Aggregate rows/s:  %s\n" "$ROWS_PER_SEC"
    echo ""
    echo "  Per-worker attribution (scheduler_events / from_lease_owner):"
    # NOTE: token_work_items.lease_owner is NULLed on terminal/failed; in
    # multi-worker mode the leader also drains follower PENDING_SINK rows under
    # its own lease_owner, so mark_pending_sink_terminal always shows the leader.
    # We attribute via mark_pending_sink (LEASED→PENDING_SINK) and mark_failed,
    # which record from_lease_owner = the worker that processed the row.
    sqlite3 "file:${DB}?mode=ro" <<SQL
PRAGMA query_only = ON;
SELECT
  '  ' || COALESCE(w.role, 'unknown') || '  ' || se.from_lease_owner ||
  '  completed=' || COUNT(*)
FROM scheduler_events se
LEFT JOIN run_workers w ON w.worker_id = se.from_lease_owner AND w.run_id = se.run_id
WHERE se.run_id = '$RUN_ID'
  AND se.event_type IN ('mark_pending_sink', 'mark_failed')
  AND se.from_lease_owner IS NOT NULL
GROUP BY se.from_lease_owner, w.role
ORDER BY w.role DESC, COUNT(*) DESC;
SQL
else
    echo ""
    echo "  (run_id not captured — leader may have finished before poll window)"
fi

echo ""
if [ "$WORKER_FAILED" -ne 0 ]; then
    echo "✗ FAIL: a worker process exited non-zero; the showcase did not complete cleanly." >&2
    exit 1
elif [ -z "$RUN_ID" ]; then
    echo "✗ FAIL: no running run was captured." >&2
    exit 1
elif [ "$CONTRIBUTING_WORKERS" -lt 2 ]; then
    echo "✗ FAIL: only $CONTRIBUTING_WORKERS worker(s) processed outcomes; expected at least 2." >&2
    exit 1
fi

echo "✓ PASS: $CONTRIBUTING_WORKERS workers shared $TOTAL_ROWS completed outcomes."
