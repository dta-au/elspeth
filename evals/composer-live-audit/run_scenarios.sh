#!/usr/bin/env bash
# Run scenarios serially against one running instance, then mine and build the register.
#
#   evals/composer-live-audit/run_scenarios.sh <port> <instance-name> <scenario> [<scenario> ...]
#
# Serial per instance: provider calls are attributed to turns by wall-clock window.
# Run several instances in parallel for throughput.
set -uo pipefail
PORT=$1
NAME=$2
shift 2
ROOT=$(git rev-parse --show-toplevel)
HERE="$ROOT/evals/composer-live-audit"
INST="$HERE/runs/instances/$NAME"
cd "$ROOT"
mkdir -p "$HERE/runs/sessions"
for s in "$@"; do
  .venv/bin/python "$HERE/drive_session.py" --base "http://127.0.0.1:$PORT" --capture "$INST/capture" \
    --creds "$INST/credentials.json" --scenario "$HERE/scenarios/$s.json" --out "$HERE/runs/sessions/$s" \
    > "$HERE/runs/sessions-$s.log" 2>&1
  echo "$s exit=$?"
done
.venv/bin/python "$HERE/mine_sessions.py" "$HERE/runs/sessions" "$HERE/runs/digest.md" "$HERE/runs/digest.json" >/dev/null
# shellcheck disable=SC2046
.venv/bin/python "$HERE/build_register.py" "$HERE/runs/register.json" $(ls "$HERE"/runs/instances/*/capture/llm_calls.jsonl) >/dev/null
.venv/bin/python "$HERE/render_register.py" "$HERE/runs/register.json" "$HERE/runs/register.md"
echo "digest: $HERE/runs/digest.md  register: $HERE/runs/register.md"
