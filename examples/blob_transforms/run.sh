#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
EXAMPLE_DIR="$PROJECT_ROOT/examples/blob_transforms"

PYTHON_BIN="${ELSPETH_BLOB_TRANSFORMS_PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
ELSPETH_BIN="${ELSPETH_BLOB_TRANSFORMS_CLI_BIN:-$PROJECT_ROOT/.venv/bin/elspeth}"

cd "$PROJECT_ROOT"

mkdir -p "$EXAMPLE_DIR/input" "$EXAMPLE_DIR/output" "$EXAMPLE_DIR/payloads/offline" "$EXAMPLE_DIR/runs"
# FilesystemPayloadStore rejects group/world-writable roots (see
# elspeth.core.payload_store._validate_store_directory). `mkdir -p` applies
# the umask, which on hosts with a permissive umask (e.g. 0002) leaves this
# directory group-writable and self-healing only fires when the store
# creates the directory itself, not when it already exists. Pin the mode
# explicitly so the launcher works regardless of the operator's umask.
chmod 700 "$EXAMPLE_DIR/payloads/offline"

# Clear ONLY this example's generated offline artifacts. The list stays
# explicit rather than a glob so the opt-in hosted-fetch outputs, and anything
# an operator left in output/, survive a rerun.
rm -f \
  "$EXAMPLE_DIR/input/csv_blob_manifest.csv" \
  "$EXAMPLE_DIR/input/json_blob_manifest.csv" \
  "$EXAMPLE_DIR/input/text_blob_manifest.csv" \
  "$EXAMPLE_DIR/output/expanded_csv_rows.csv" \
  "$EXAMPLE_DIR/output/expansion_failures.jsonl" \
  "$EXAMPLE_DIR/output/expanded_json_rows.jsonl" \
  "$EXAMPLE_DIR/output/json_expansion_failures.jsonl" \
  "$EXAMPLE_DIR/output/expanded_text_lines.jsonl" \
  "$EXAMPLE_DIR/output/text_expansion_failures.jsonl" \
  "$EXAMPLE_DIR/output/expanded_inline_csv_rows.csv" \
  "$EXAMPLE_DIR/output/inline_csv_failures.jsonl" \
  "$EXAMPLE_DIR/output/expanded_inline_json_rows.jsonl" \
  "$EXAMPLE_DIR/output/inline_json_failures.jsonl"
for db in offline_audit json_blob_audit text_blob_audit inline_csv_audit inline_json_audit; do
  rm -f "$EXAMPLE_DIR/runs/$db.db" "$EXAMPLE_DIR/runs/$db.db-shm" "$EXAMPLE_DIR/runs/$db.db-wal"
done
find "$EXAMPLE_DIR/payloads/offline" -mindepth 1 -type f ! -name .gitkeep -delete
find "$EXAMPLE_DIR/payloads/offline" -mindepth 1 -depth -type d -empty -delete

echo "Preparing offline CSV blob manifest..."
"$PYTHON_BIN" "$EXAMPLE_DIR/scripts/prepare_csv_blob_manifest.py"
echo "Preparing offline JSON and text blob manifests..."
"$PYTHON_BIN" "$EXAMPLE_DIR/scripts/prepare_expander_blobs.py"

# Expected exit code per config, never a blanket `-eq 0` — see the
# "Exit 0 is not the corpus gate" section of examples/AGENTS.md. All five
# offline configs are clean fixtures and are expected to end COMPLETED/0; a
# non-zero exit from any of them is a real defect, not a by-design partial.
run_config() {
  local settings="$1" expected_exit="$2" label="$3"
  echo
  echo "Executing $label..."
  local actual_exit=0
  "$ELSPETH_BIN" run --settings "$settings" --execute || actual_exit=$?
  if [ "$actual_exit" -ne "$expected_exit" ]; then
    echo "FAIL: $label exited $actual_exit, expected $expected_exit" >&2
    return 1
  fi
  echo "OK: $label exited $actual_exit as expected"
}

run_config examples/blob_transforms/settings_expand_csv_blobs.yaml 0 "CSV blob expansion (payload store)"
run_config examples/blob_transforms/settings_expand_json_blobs.yaml 0 "JSON blob expansion (payload store)"
run_config examples/blob_transforms/settings_expand_text_blobs.yaml 0 "text blob expansion (payload store)"
run_config examples/blob_transforms/settings_expand_inline_csv.yaml 0 "inline CSV expansion (row field, no payload store)"
run_config examples/blob_transforms/settings_expand_inline_json.yaml 0 "inline JSON expansion (row field, no payload store)"

echo
echo "Outputs:"
echo "  examples/blob_transforms/output/expanded_csv_rows.csv           (200 rows)"
echo "  examples/blob_transforms/output/expanded_json_rows.jsonl        (3 rows)"
echo "  examples/blob_transforms/output/expanded_text_lines.jsonl       (5 rows)"
echo "  examples/blob_transforms/output/expanded_inline_csv_rows.csv    (5 rows)"
echo "  examples/blob_transforms/output/expanded_inline_json_rows.jsonl (3 rows)"
echo "Audit databases: examples/blob_transforms/runs/*.db"
