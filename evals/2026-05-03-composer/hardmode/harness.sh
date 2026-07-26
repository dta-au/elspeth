#!/usr/bin/env bash
# Hard-mode harness driver: one scenario at a time.
#
# Usage:  harness.sh <scenario_id>
# Example: harness.sh p1_t1_happy
#
# Per-scenario flow:
#   1. Read scenario JSON (persona, task, optional CSV).
#   2. Login (fresh JWT each scenario).
#   3. Create composer session, upload CSV blob if provided.
#   4. Driver-LLM (parent) loops:
#        (a) Send (or pass-through) the user message to /messages.
#        (b) Capture composer response, /state, /composer-progress events.
#        (c) Hand the composer response back to the persona-subagent (via stdin file).
#        (d) Persona returns next user msg or 'DONE: <reason>'.
#   5. After persona DONE: run /validate, /execute (if state.is_valid), capture run.
#   6. Write per-scenario ledger JSON: messages, metrics, validate, run, output files.
#
# This script is a SCAFFOLD — the parent agent (Claude in the main thread)
# performs the persona-subagent role-locking via the Agent tool. The shell
# script is responsible for the deterministic HTTP plumbing only.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVALS_SCRIPT_DIR="$ROOT"
# shellcheck source=../../lib/common.sh
source "$ROOT/../../lib/common.sh"

scenario_id="${1:-}"
if [[ -z "$scenario_id" ]]; then evals_die 64 "usage: $0 <scenario_id>"; fi

evals_require_tools
: "${ELSPETH_EVAL_BASE_URL:=https://elspeth.foundryside.dev}"
evals_load_env --require-creds

scen=$(evals_resolve_scenario "$ROOT/scenarios" "$scenario_id")

out="$ROOT/results/$scenario_id"
mkdir -p "$out"
cp "$scen" "$out/scenario.json"
export EVALS_OUT_DIR="$out"
export EVALS_JWT_FILE="$out/jwt.txt"

# --- step 1: login fresh ---
evals_login

# --- step 2: create session ---
title=$(jq -r '.task_summary' "$out/scenario.json")
sid=$(evals_create_session "hardmode/$scenario_id $title")

# --- step 3: upload blob if scenario has one ---
csv_filename=$(jq -r '.csv_filename // empty' "$out/scenario.json")
if [[ -n "$csv_filename" ]]; then
  csv_content_file=$(mktemp)
  trap 'rm -f "$csv_content_file"' EXIT
  jq -r '.csv_content' "$out/scenario.json" > "$csv_content_file"
  evals_upload_blob "$sid" "$csv_filename" "text/csv" "$csv_content_file"
fi

echo "Scaffold ready: $out (sid=$sid)"
echo "Next: parent agent runs the persona-subagent loop, posts /messages, then runs validate-and-execute.sh"
