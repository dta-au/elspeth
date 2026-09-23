#!/usr/bin/env bash
# =============================================================================
# Document Review Panel — the stack unrolls: token -> page -> document -> run
#
# Runs three configs against one local ChaosLLM server:
#
#   settings.yaml              clean corpus            COMPLETED, exit 0
#   settings_incomplete.yaml   ONE page missing ONE    PARTIAL,   exit 1
#                              field                   corpus summary over 3 of 4
#   settings_run_as_row.yaml   the same loss, with     PARTIAL,   exit 1
#                              the run encapsulated    NOTHING published at all
#                              as a single row
#
# The three together are the point: the same lost token costs a page, then a
# document, and then either three-quarters of a published number or the whole
# thing, depending on where the row boundary sits.
#
# Fault injection is zeroed and the mock's scoring is deterministic, so every
# count below is fixed. Each config's expected exit code is asserted.
#
# Usage:
#   ./examples/document_review_panel/run.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"
source "$PROJECT_ROOT/examples/chaosllm_env.sh"

CHAOS_CONFIG="examples/document_review_panel/chaos_config.yaml"
CHAOS_PORT=8199
CHAOS_PID=""

cleanup() {
    if [ -n "$CHAOS_PID" ] && kill -0 "$CHAOS_PID" 2>/dev/null; then
        echo ""
        echo "Stopping ChaosLLM server (PID $CHAOS_PID)..."
        kill "$CHAOS_PID" 2>/dev/null || true
        wait "$CHAOS_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

rm -f examples/document_review_panel/runs/*.db examples/document_review_panel/runs/*.db-wal examples/document_review_panel/runs/*.db-shm
rm -f examples/document_review_panel/output/*.jsonl

echo "=== Document Review Panel: token -> page -> document -> run ==="
echo ""

echo "Starting ChaosLLM server on port $CHAOS_PORT..."
.venv/bin/chaosllm serve --config "$CHAOS_CONFIG" --port "$CHAOS_PORT" --workers 1 &
CHAOS_PID=$!

echo "Waiting for ChaosLLM to be ready..."
for i in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:$CHAOS_PORT/health" > /dev/null 2>&1; then
        echo "ChaosLLM is ready."
        echo ""
        break
    fi
    if ! kill -0 "$CHAOS_PID" 2>/dev/null; then
        echo "ERROR: ChaosLLM failed to start."
        exit 1
    fi
    sleep 0.5
done

if ! curl -sf "http://127.0.0.1:$CHAOS_PORT/health" > /dev/null 2>&1; then
    echo "ERROR: ChaosLLM not responding after 15 seconds."
    exit 1
fi

run_case() {  # settings  label  output  audit_db  expected_exit  expected_docs  expected_row_llm  expected_preflight_llm  expected_losses
    local settings="$1" label="$2" out="$3" db="$4"
    local expect_rc="$5" docs="$6" row_llm="$7" preflight_llm="$8" losses="$9"
    echo "--- $label ---"
    local rc=0
    .venv/bin/elspeth run --settings "$settings" --execute || rc=$?
    if [ "$rc" -ne "$expect_rc" ]; then
        echo "ERROR: $settings exited $rc, expected $expect_rc." >&2
        return 1
    fi
    echo ""
    .venv/bin/python - "$out" "$db" "$docs" "$row_llm" "$preflight_llm" "$losses" <<'PYCHECK'
import json, os, sqlite3, sys

out_path, db_path = sys.argv[1], sys.argv[2]
expect_docs, expect_row_llm, expect_preflight_llm, expect_losses = map(int, sys.argv[3:7])
conn = sqlite3.connect(db_path)
failures = []

# What actually reached the sink. `expect_docs == 0` means the run was supposed
# to publish NOTHING — an absent or empty file is the pass, not a broken run.
lines = []
if os.path.exists(out_path):
    lines = [l for l in open(out_path).read().splitlines() if l.strip()]
if expect_docs == 0:
    if lines:
        failures.append(f"expected NO published rows, found {len(lines)} in {out_path}")
else:
    if not lines:
        failures.append(f"no rows published to {out_path}")
    else:
        report = json.loads(lines[-1])
        if report["count"] != expect_docs:
            failures.append(f"published count {report['count']} != {expect_docs} documents")

# Row-level provider calls prove the reviews happened. Runtime preflight calls
# are audited separately and do not review a page.
row_llm_calls = conn.execute(
    "SELECT COUNT(*) FROM calls WHERE call_type = 'llm' AND state_id IS NOT NULL"
).fetchone()[0]
preflight_llm_calls = conn.execute(
    "SELECT COUNT(*) FROM calls AS c JOIN operations AS o ON c.operation_id = o.operation_id "
    "WHERE c.call_type = 'llm' AND o.operation_type = 'runtime_preflight'"
).fetchone()[0]
total_llm_calls = conn.execute("SELECT COUNT(*) FROM calls WHERE call_type = 'llm'").fetchone()[0]
if row_llm_calls != expect_row_llm:
    failures.append(f"{row_llm_calls} audited row llm calls != {expect_row_llm}")
if preflight_llm_calls != expect_preflight_llm:
    failures.append(f"{preflight_llm_calls} audited preflight llm calls != {expect_preflight_llm}")
if total_llm_calls != row_llm_calls + preflight_llm_calls:
    failures.append(f"{total_llm_calls} total llm calls include unexpected operation calls")
losses = conn.execute("SELECT COUNT(*) FROM group_losses").fetchone()[0]
if losses != expect_losses:
    failures.append(f"{losses} group_losses rows != {expect_losses}")

if failures:
    print("VERIFICATION FAILED:", file=sys.stderr)
    for f in failures:
        print(f"  - {f}", file=sys.stderr)
    raise SystemExit(1)

ledger = conn.execute(
    "SELECT closer_name, reason, COUNT(*) FROM group_losses GROUP BY 1, 2 ORDER BY 1"
).fetchall()
where = "; ".join(f"{c} ({r}) x{n}" for c, r, n in ledger) or "no losses"
published = f"{expect_docs} document(s) in the published verdict" if expect_docs else "NOTHING published"
print(f"VERIFIED: {row_llm_calls} row llm calls + {preflight_llm_calls} preflight llm calls, {published}; ledger: {where}")
PYCHECK
    echo ""
}

run_case examples/document_review_panel/settings.yaml \
    "CLEAN — 4 documents, 12 pages, every page reviewed twice" \
    examples/document_review_panel/output/corpus_summary.jsonl \
    examples/document_review_panel/runs/clean.db \
    0 4 24 2 0

run_case examples/document_review_panel/settings_incomplete.yaml \
    "CASCADE — one page missing one field; page, then document, then the number" \
    examples/document_review_panel/output/corpus_summary_incomplete.jsonl \
    examples/document_review_panel/runs/incomplete.db \
    1 3 23 2 2

run_case examples/document_review_panel/settings_run_as_row.yaml \
    "RUN AS ONE ROW — the same loss, and now nothing is published at all" \
    examples/document_review_panel/output/corpus_verdict.jsonl \
    examples/document_review_panel/runs/run_as_row.db \
    1 0 3 1 1

echo "Done. Audit trails under examples/document_review_panel/runs/."
echo "Inspect the unroll with:"
echo "  sqlite3 examples/document_review_panel/runs/incomplete.db \\"
echo "    \"SELECT closer_name, reason, COUNT(*) FROM group_losses GROUP BY 1,2\""
