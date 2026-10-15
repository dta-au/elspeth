#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"
source "$PROJECT_ROOT/examples/chaosllm_env.sh"

EXAMPLE=examples/llm_ordered_chain
SERVER_PID=""
stop_server() {
    if [ -n "$SERVER_PID" ]; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
}
trap stop_server EXIT

for artifact in "$EXAMPLE/runs/audit.db" "$EXAMPLE/output/assessed_expenses.jsonl" "$EXAMPLE/output/quarantine.jsonl"; do
    if [ -e "$artifact" ]; then
        echo "Refusing to overwrite $artifact; use a fresh example copy." >&2
        exit 2
    fi
done
if { true > /dev/tcp/127.0.0.1/8199; } 2>/dev/null; then
    echo "Port 8199 is occupied; refusing to attach to an unowned service." >&2
    exit 2
fi
mkdir -p "$EXAMPLE/runs" "$EXAMPLE/output"
.venv/bin/chaosllm serve --config "$EXAMPLE/chaos_config.yaml" --port 8199 --workers 1 \
    > "$EXAMPLE/runs/server.log" 2>&1 &
SERVER_PID=$!
ready=false
for _ in $(seq 1 50); do
    if curl -fsS http://127.0.0.1:8199/health > /dev/null 2>&1; then
        ready=true
        break
    fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        break
    fi
    sleep 0.2
done
if [ "$ready" != true ]; then
    cat "$EXAMPLE/runs/server.log" >&2
    echo "Local fixture did not become ready." >&2
    exit 1
fi

rc=0
.venv/bin/elspeth --json-logs run --settings "$EXAMPLE/settings.yaml" --execute --format json \
    > "$EXAMPLE/runs/pipeline.log" 2>&1 || rc=$?
cat "$EXAMPLE/runs/pipeline.log"
if [ "$rc" -ne 1 ]; then
    echo "Expected PARTIAL/exit 1 for the unreadable receipt; observed $rc." >&2
    exit 1
fi
.venv/bin/python "$EXAMPLE/verify.py"
