#!/usr/bin/env bash
# =============================================================================
# A/B Experiment — fork one case study to two LLM arms, union the pair
#
# Starts a ChaosLLM server whose responses are model- and prompt-aware, then
# runs BOTH configs against it in turn:
#
#   settings.yaml         one model, TWO PROMPTS   (control vs treatment)
#   settings_models.yaml  one prompt, TWO MODELS   (analyst-v1 vs analyst-v2)
#
#   settings_arm_loss.yaml  24 cases, 3 of which LOSE ONE ARM
#
# Fault injection is zeroed, so every run is deterministic. The first two must
# end COMPLETED (exit 0); the third must end PARTIAL (exit 1) with 21 whole
# pairs out of 24 cases. Each expected exit code is asserted per config — an
# unexpected one is a real defect, not fixture noise.
#
# Usage:
#   ./examples/ab_llm_experiment/run.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"
source "$PROJECT_ROOT/examples/chaosllm_env.sh"

CHAOS_CONFIG="examples/ab_llm_experiment/chaos_config.yaml"
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

# Clean previous run artifacts. Each config owns its own audit trail so the
# two experiments never share a Landscape database.
rm -f examples/ab_llm_experiment/runs/audit.db examples/ab_llm_experiment/runs/audit.db-wal examples/ab_llm_experiment/runs/audit.db-shm
rm -f examples/ab_llm_experiment/runs/audit_models.db examples/ab_llm_experiment/runs/audit_models.db-wal examples/ab_llm_experiment/runs/audit_models.db-shm
rm -f examples/ab_llm_experiment/runs/audit_arm_loss.db examples/ab_llm_experiment/runs/audit_arm_loss.db-wal examples/ab_llm_experiment/runs/audit_arm_loss.db-shm
rm -f examples/ab_llm_experiment/output/prompt_experiment.json
rm -f examples/ab_llm_experiment/output/model_experiment.json
rm -f examples/ab_llm_experiment/output/arm_loss_experiment.json

echo "=== A/B Experiment: fork -> two LLM arms -> row_union ==="
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

run_experiment() {  # settings  label  output  audit_db  variant_field  expected_exit  expected_pairs  expected_llm_calls
    local settings="$1" label="$2" out="$3" db="$4" field="$5"
    local expect_rc="$6" pairs="$7" llm_calls="$8"
    echo "--- $label ---"
    local rc=0
    .venv/bin/elspeth run --settings "$settings" --execute || rc=$?
    if [ "$rc" -ne "$expect_rc" ]; then
        echo "ERROR: $settings exited $rc, expected $expect_rc." >&2
        return 1
    fi
    echo ""
    if [ ! -f "$out" ]; then
        echo "ERROR: expected output $out was not written." >&2
        return 1
    fi
    echo "Comparison ($out):"
    python3 -m json.tool < "$out" 2>/dev/null || cat "$out"
    echo ""
    # Self-verification. Exit 0 alone is a weak oracle: a pipeline that scored
    # nothing also exits 0. These three facts are what prove the mechanics ran.
    .venv/bin/python - "$out" "$db" "$field" "$pairs" "$llm_calls" <<'PYCHECK'
import json, sqlite3, sys

out_path, db_path, variant_field = sys.argv[1], sys.argv[2], sys.argv[3]
pairs, expected_llm = int(sys.argv[4]), int(sys.argv[5])
report = json.loads(open(out_path).read().strip().splitlines()[-1])

failures = []
# 1. The row_union released only WHOLE pairs, and exactly this many of them.
if report["batch_size"] != pairs * 2:
    failures.append(f"batch_size {report['batch_size']} != {pairs * 2} ({pairs} whole pairs)")
if report["baseline_count"] != pairs or report["variant_count"] != pairs:
    failures.append(f"arm counts {report['baseline_count']}/{report['variant_count']} != {pairs}/{pairs}")
# 2. The comparison ran over the intended discriminator.
if report["variant_field"] != variant_field:
    failures.append(f"variant_field {report['variant_field']!r} != {variant_field!r}")
# 3. Provider calls actually made. Where an arm is lost this DIFFERS from
#    2 x pairs, and the gap is the work paid for and thrown away.
llm_calls = sqlite3.connect(db_path).execute(
    "SELECT COUNT(*) FROM calls WHERE call_type = 'llm'"
).fetchone()[0]
if llm_calls != expected_llm:
    failures.append(f"{llm_calls} audited llm calls != {expected_llm}")

if failures:
    print("VERIFICATION FAILED:", file=sys.stderr)
    for f in failures:
        print(f"  - {f}", file=sys.stderr)
    raise SystemExit(1)
wasted = expected_llm - pairs * 2
note = f", {wasted} llm calls discarded with their invalidated rows" if wasted else ""
print(
    f"VERIFIED: {expected_llm} llm calls, {pairs}+{pairs} paired observations "
    f"released as whole groups{note}; "
    f"{report['baseline_variant']} {report['baseline_mean']:.2f} -> "
    f"{report['variant']} {report['variant_mean']:.2f} "
    f"(delta {report['mean_delta']:+.2f}, lift {report['relative_lift']:.1%})"
)
PYCHECK
    echo ""
}

run_experiment examples/ab_llm_experiment/settings.yaml \
    "A/B by PROMPT — one model, terse rubric vs weighted rubric" \
    examples/ab_llm_experiment/output/prompt_experiment.json \
    examples/ab_llm_experiment/runs/audit.db \
    prompt_variant 0 8 16

run_experiment examples/ab_llm_experiment/settings_models.yaml \
    "A/B by MODEL — one prompt, analyst-v1 vs analyst-v2" \
    examples/ab_llm_experiment/output/model_experiment.json \
    examples/ab_llm_experiment/runs/audit_models.db \
    model_variant 0 8 16

# The fail-closed fixture. 24 cases, 3 of them missing the field arm B needs.
# Expected exit is 1, and 21 pairs from 45 llm calls: three arm-A assessments
# completed successfully and were invalidated anyway, because their arm-B
# siblings never arrived.
run_experiment examples/ab_llm_experiment/settings_arm_loss.yaml \
    "ARM LOSS — 24 cases, 3 lose one arm; the whole row is invalidated" \
    examples/ab_llm_experiment/output/arm_loss_experiment.json \
    examples/ab_llm_experiment/runs/audit_arm_loss.db \
    prompt_variant 1 21 45

echo ""
echo "Done. Audit trails:"
echo "  examples/ab_llm_experiment/runs/audit.db           (prompt A/B)"
echo "  examples/ab_llm_experiment/runs/audit_models.db    (model A/B)"
echo "  examples/ab_llm_experiment/runs/audit_arm_loss.db  (arm loss; group_losses has 3 rows)"
