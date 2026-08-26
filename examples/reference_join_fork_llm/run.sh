#!/usr/bin/env bash
# =============================================================================
# reference_join + fork + two LLM calls + coalesce
#
# Starts TWO ChaosLLM servers with canned responses and ZERO fault injection
# (one per fork branch), then runs the pipeline against them.
#
# Usage:
#   ./examples/reference_join_fork_llm/run.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"
source "$PROJECT_ROOT/examples/chaosllm_env.sh"

EXAMPLE_DIR="examples/reference_join_fork_llm"
TRIAGE_PORT=8201
REPLY_PORT=8202
PIDS=()

cleanup() {
    for pid in "${PIDS[@]:-}"; do
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
        fi
    done
}
trap cleanup EXIT

start_server() {
    local name="$1" port="$2"
    echo "Starting ChaosLLM ($name) on port $port..."
    .venv/bin/chaosllm serve --config "$EXAMPLE_DIR/chaos_${name}.yaml" --port "$port" --workers 1 &
    PIDS+=("$!")
    for _ in $(seq 1 30); do
        if curl -sf "http://127.0.0.1:$port/health" > /dev/null 2>&1; then
            echo "  ready."
            return 0
        fi
        sleep 0.5
    done
    echo "ERROR: ChaosLLM ($name) not responding on port $port after 15 seconds." >&2
    exit 1
}

rm -f "$EXAMPLE_DIR"/runs/audit.db "$EXAMPLE_DIR"/runs/audit.db-wal "$EXAMPLE_DIR"/runs/audit.db-shm
rm -f "$EXAMPLE_DIR"/output/handled_tickets.json "$EXAMPLE_DIR"/output/quarantine.json

echo "=== reference_join + fork + two LLM calls + coalesce ==="
echo ""
start_server triage "$TRIAGE_PORT"
start_server reply "$REPLY_PORT"
echo ""

.venv/bin/elspeth run --settings "$EXAMPLE_DIR/settings.yaml" --execute

echo ""
if [ -f "$EXAMPLE_DIR/output/handled_tickets.json" ]; then
    echo "Output ($EXAMPLE_DIR/output/handled_tickets.json):"
    head -1 "$EXAMPLE_DIR/output/handled_tickets.json" | python3 -m json.tool
    echo "($(wc -l < "$EXAMPLE_DIR/output/handled_tickets.json") rows written)"
fi
echo ""
echo "Done. Audit trail: $EXAMPLE_DIR/runs/audit.db"
