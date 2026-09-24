#!/usr/bin/env bash
# =============================================================================
# Replay / verify walkthrough — record a live run, replay it offline, verify it
#
#   1. LIVE    fetch three pages from a local fixture server; every HTTP call is
#              recorded in the Landscape audit trail.
#   2. REPLAY  stop the server, then rerun with run_mode: replay. The recorded
#              responses stand in for the network; the server's access log
#              must not grow.
#   3. VERIFY  restart the server, then rerun with run_mode: verify. Every live
#              response is compared with the recording; the verdicts land in
#              the call_verifications table.
#
# Then three negative arms, each asserted on its REASON, not only its exit code:
#
#   a. replay_from names a run that does not exist        -> refused, exit 1
#   b. an execution setting differs from the recorded run  -> refused, exit 4
#   c. a page changes after the recording, then verify     -> mismatch, exit 4
#
# The script exits 0 only when every step behaved as described. Replay and
# verify never write the configured sinks, so output/pages.jsonl must still be
# the live run's file, byte for byte, at the end.
#
# Usage:
#   ./examples/replay_verify/run.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

EXAMPLE=examples/replay_verify
RUNS="$EXAMPLE/runs"
OUTPUT="$EXAMPLE/output/pages.jsonl"
DB="$RUNS/audit.db"
ACCESS_LOG="$RUNS/access.log"
PORT=8204
ELSPETH=.venv/bin/elspeth
PYTHON=.venv/bin/python
SERVER_PID=""

stop_server() {
    if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    SERVER_PID=""
}
trap stop_server EXIT

start_server() {  # pages_dir
    "$PYTHON" "$EXAMPLE/serve_pages.py" --port "$PORT" --access-log "$ACCESS_LOG" --pages-dir "$1" &
    SERVER_PID=$!
    for _ in $(seq 1 30); do
        if curl -sf "http://127.0.0.1:$PORT/returns.html" > /dev/null 2>&1; then
            # The readiness probe is a request too; start each step's count clean.
            : > "$ACCESS_LOG"
            return 0
        fi
        if ! kill -0 "$SERVER_PID" 2>/dev/null; then
            echo "ERROR: fixture server failed to start (is port $PORT in use?)." >&2
            exit 1
        fi
        sleep 0.2
    done
    echo "ERROR: fixture server not responding on port $PORT." >&2
    exit 1
}

requests_served() {
    if [ -f "$ACCESS_LOG" ]; then wc -l < "$ACCESS_LOG" | tr -d ' '; else echo 0; fi
}

fail() {
    echo "VERIFICATION FAILED: $*" >&2
    exit 1
}

# Write a copy of settings.yaml with a different run_mode and a literal
# replay_from. Both must be literal YAML: the CLI refuses ELSPETH_REPLAY_FROM
# from the environment. The run ID is quoted so YAML cannot read it as a number.
write_mode_settings() {  # mode  run_id  destination
    {
        printf 'run_mode: %s\nreplay_from: "%s"\n' "$1" "$2"
        grep -v '^run_mode: ' "$EXAMPLE/settings.yaml"
    } > "$3"
}

# Run the pipeline with JSON progress events on stdout. Sets RC and RUN_ID.
run_pipeline() {  # settings  label
    local settings="$1" label="$2"
    RC=0
    "$ELSPETH" run --settings "$settings" --execute --format json \
        > "$RUNS/$label.out" 2> "$RUNS/$label.err" || RC=$?
    RUN_ID="$("$PYTHON" - "$RUNS/$label.out" <<'PY'
import json, sys
run_id = ""
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if line.startswith("{"):
        event = json.loads(line)
        if "run_id" in event:
            run_id = event["run_id"]
print(run_id)
PY
)"
    echo "  exit code: $RC   run_id: ${RUN_ID:-<none>}"
}

# With --format json a fatal error is a JSON event on stderr; print its reason.
fatal_reason() {  # label
    "$PYTHON" - "$RUNS/$1.err" <<'PY'
import json, sys
for line in open(sys.argv[1], encoding="utf-8"):
    if line.startswith('{"event": "fatal"'):
        event = json.loads(line)
        print(f"{event['error_type']}: {event['error']}")
        break
PY
}

query() {  # sql  [params...]
    "$PYTHON" - "$DB" "$@" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
cursor = conn.execute(sys.argv[2], sys.argv[3:])
names = [d[0] for d in cursor.description]
rows = cursor.fetchall()
widths = [max(len(str(v)) for v in [n, *(r[i] for r in rows)]) for i, n in enumerate(names)]
print("  " + "  ".join(n.ljust(w) for n, w in zip(names, widths)))
for row in rows:
    print("  " + "  ".join(str(v).ljust(w) for v, w in zip(row, widths)))
PY
}

scalar() {  # sql  [params...]
    "$PYTHON" - "$DB" "$@" <<'PY'
import sqlite3, sys
print(sqlite3.connect(sys.argv[1]).execute(sys.argv[2], sys.argv[3:]).fetchone()[0])
PY
}

# Clean previous run artifacts.
rm -f "$DB" "$DB-wal" "$DB-shm" "$ACCESS_LOG" "$RUNS"/*.yaml "$RUNS"/*.out "$RUNS"/*.err
rm -rf "$RUNS/payloads" "$RUNS/pages_changed"
rm -f "$OUTPUT" "$EXAMPLE/output/fetch_failures.jsonl"

echo "=== Replay / verify walkthrough ==="
echo ""

# --- Step 1: LIVE -------------------------------------------------------------
echo "--- Step 1: LIVE run (fixture server up on port $PORT) ---"
start_server "$EXAMPLE/pages"
run_pipeline "$EXAMPLE/settings.yaml" live
LIVE_RUN="$RUN_ID"
[ "$RC" -eq 0 ] || fail "live run exited $RC (see $RUNS/live.err)"
LIVE_REQUESTS="$(requests_served)"
LIVE_ROWS="$(wc -l < "$OUTPUT" | tr -d ' ')"
LIVE_SHA="$(sha256sum "$OUTPUT" | cut -d' ' -f1)"
echo "  requests served: $LIVE_REQUESTS   rows written: $LIVE_ROWS   sha256: $LIVE_SHA"
[ "$LIVE_REQUESTS" -eq 3 ] || fail "live run made $LIVE_REQUESTS requests, expected 3"
[ "$LIVE_ROWS" -eq 3 ] || fail "live run wrote $LIVE_ROWS rows, expected 3"
query "SELECT c.call_type, c.status, substr(c.call_id, 1, 12) AS call_id FROM calls c
       JOIN node_states n ON n.state_id = c.state_id WHERE n.run_id = ? ORDER BY c.created_at" "$LIVE_RUN"
echo ""

# --- Step 2: REPLAY -----------------------------------------------------------
echo "--- Step 2: REPLAY run (fixture server STOPPED) ---"
stop_server
if curl -sf "http://127.0.0.1:$PORT/returns.html" > /dev/null 2>&1; then
    fail "something is still answering on port $PORT; replay would not prove anything"
fi
echo "  port $PORT refuses connections"
: > "$ACCESS_LOG"
write_mode_settings replay "$LIVE_RUN" "$RUNS/settings_replay.yaml"
run_pipeline "$RUNS/settings_replay.yaml" replay
REPLAY_RUN="$RUN_ID"
[ "$RC" -eq 0 ] || fail "replay exited $RC (see $RUNS/replay.err)"
[ "$(requests_served)" -eq 0 ] || fail "replay reached the fixture server"
[ "$(sha256sum "$OUTPUT" | cut -d' ' -f1)" = "$LIVE_SHA" ] || fail "replay changed $OUTPUT"
REPLAYED="$(scalar "SELECT COUNT(*) FROM calls c JOIN node_states n ON n.state_id = c.state_id
                    WHERE n.run_id = ? AND c.source_call_id IS NOT NULL" "$REPLAY_RUN")"
[ "$REPLAYED" -eq 3 ] || fail "replay recorded $REPLAYED calls bound to a source call, expected 3"
echo "  Each replayed call names the recorded call it was served from:"
query "SELECT c.call_type, substr(c.call_id, 1, 12) AS replay_call, substr(c.source_call_id, 1, 12) AS source_call
       FROM calls c JOIN node_states n ON n.state_id = c.state_id WHERE n.run_id = ? ORDER BY c.created_at" "$REPLAY_RUN"
echo "  The sink boundary was compared, not published (virtual effect, same payload hash):"
query "SELECT r.run_mode, e.publication_performed AS published, e.publication_evidence_kind AS evidence,
              substr(e.group_payload_hash, 1, 16) AS payload_hash
       FROM sink_effects e JOIN runs r ON r.run_id = e.run_id WHERE e.run_id IN (?, ?) ORDER BY r.started_at" \
    "$LIVE_RUN" "$REPLAY_RUN"
echo ""

# --- Step 3: VERIFY -----------------------------------------------------------
echo "--- Step 3: VERIFY run (fixture server restarted, same pages) ---"
start_server "$EXAMPLE/pages"
write_mode_settings verify "$LIVE_RUN" "$RUNS/settings_verify.yaml"
run_pipeline "$RUNS/settings_verify.yaml" verify
VERIFY_RUN="$RUN_ID"
[ "$RC" -eq 0 ] || fail "verify exited $RC (see $RUNS/verify.err)"
[ "$(requests_served)" -eq 3 ] || fail "verify made $(requests_served) requests, expected 3"
[ "$(sha256sum "$OUTPUT" | cut -d' ' -f1)" = "$LIVE_SHA" ] || fail "verify changed $OUTPUT"
MATCHES="$(scalar "SELECT COUNT(*) FROM call_verifications WHERE current_run_id = ? AND is_match = 1" "$VERIFY_RUN")"
VERDICTS="$(scalar "SELECT COUNT(*) FROM call_verifications WHERE current_run_id = ?" "$VERIFY_RUN")"
[ "$VERDICTS" -eq 3 ] && [ "$MATCHES" -eq 3 ] || fail "verify recorded $MATCHES matches out of $VERDICTS verdicts, expected 3/3"
echo "  Verdicts (call_verifications):"
query "SELECT substr(current_call_id, 1, 12) AS verify_call, substr(source_call_id, 1, 12) AS source_call,
              is_match, differences_json FROM call_verifications WHERE current_run_id = ?" "$VERIFY_RUN"
echo ""

# --- Refusal a: nonexistent source run ------------------------------------------
echo "--- Refusal a: replay_from names a run that does not exist ---"
write_mode_settings replay "no-such-run" "$RUNS/settings_missing_source.yaml"
run_pipeline "$RUNS/settings_missing_source.yaml" refuse_missing_source
[ "$RC" -ne 0 ] || fail "replay of a missing source run exited 0"
grep -q "source run 'no-such-run' does not exist" "$RUNS/refuse_missing_source.err" \
    || fail "missing-source refusal gave an unexpected reason (see $RUNS/refuse_missing_source.err)"
echo "  $(grep -m1 "does not exist" "$RUNS/refuse_missing_source.err")"
echo ""

# --- Refusal b: settings drift --------------------------------------------------
echo "--- Refusal b: an execution setting differs from the recorded run ---"
write_mode_settings replay "$LIVE_RUN" "$RUNS/settings_drifted.yaml"
sed -i.bak 's/timeout: 10$/timeout: 20/' "$RUNS/settings_drifted.yaml" && rm -f "$RUNS/settings_drifted.yaml.bak"
grep -q 'timeout: 20$' "$RUNS/settings_drifted.yaml" || fail "could not introduce the drift"
run_pipeline "$RUNS/settings_drifted.yaml" refuse_drift
[ "$RC" -ne 0 ] || fail "replay with drifted settings exited 0"
DRIFT_REASON="$(fatal_reason refuse_drift)"
[ "$DRIFT_REASON" = "AuditIntegrityError: Replay execution settings differ from the source run" ] \
    || fail "drift refusal gave an unexpected reason: '$DRIFT_REASON' (see $RUNS/refuse_drift.err)"
echo "  $DRIFT_REASON"
echo ""

# --- Mismatch c: the world changed after the recording -------------------------
echo "--- Mismatch c: a page changed since the recording; verify must fail ---"
stop_server
mkdir -p "$RUNS/pages_changed"
cp "$EXAMPLE"/pages/*.html "$RUNS/pages_changed/"
sed -i.bak 's/two-year warranty/three-year warranty/' "$RUNS/pages_changed/warranty.html" && rm -f "$RUNS/pages_changed/warranty.html.bak"
grep -q "three-year warranty" "$RUNS/pages_changed/warranty.html" || fail "could not change the page"
start_server "$RUNS/pages_changed"
run_pipeline "$RUNS/settings_verify.yaml" verify_changed_page
CHANGED_RUN="$RUN_ID"
[ "$RC" -ne 0 ] || fail "verify against a changed page exited 0"
[ -n "$CHANGED_RUN" ] || fail "verify against a changed page recorded no run"
[ "$(sha256sum "$OUTPUT" | cut -d' ' -f1)" = "$LIVE_SHA" ] || fail "failed verify changed $OUTPUT"
MISMATCHES="$(scalar "SELECT COUNT(*) FROM call_verifications
                      WHERE current_run_id = ? AND is_match = 0 AND source_call_id IS NOT NULL" "$CHANGED_RUN")"
[ "$MISMATCHES" -ge 1 ] || fail "verify against a changed page recorded no mismatch verdict"
CHANGED_REASON="$(fatal_reason verify_changed_page)"
case "$CHANGED_REASON" in
    *"differs from the source run"*) echo "  $CHANGED_REASON" ;;
    *) fail "changed-page verify gave an unexpected reason: '$CHANGED_REASON'" ;;
esac
[ "$(scalar "SELECT status FROM runs WHERE run_id = ?" "$CHANGED_RUN")" = "failed" ] \
    || fail "changed-page verify run is not recorded as failed"
query "SELECT substr(current_call_id, 1, 12) AS verify_call, is_match, substr(differences_json, 1, 60) AS differences
       FROM call_verifications WHERE current_run_id = ?" "$CHANGED_RUN"
echo ""

echo "--- Runs recorded in $DB ---"
query "SELECT run_id, run_mode, replay_from_run_id, status FROM runs ORDER BY started_at"
echo ""
echo "VERIFIED: live -> replay (0 requests, same sink payload) -> verify (3/3 matches);"
echo "          missing source and drifted settings refused; a changed page failed verify."
echo "Output (written by the live run only): $OUTPUT"
