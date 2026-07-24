#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
EXAMPLE_DIR="$PROJECT_ROOT/examples/blob_transforms"

PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
ELSPETH_BIN="${ELSPETH_BIN:-$PROJECT_ROOT/.venv/bin/elspeth}"

cd "$PROJECT_ROOT"

mkdir -p "$EXAMPLE_DIR/input" "$EXAMPLE_DIR/output" "$EXAMPLE_DIR/payloads" "$EXAMPLE_DIR/runs"
rm -f \
  "$EXAMPLE_DIR/input/csv_blob_manifest.csv" \
  "$EXAMPLE_DIR/output/expanded_csv_rows.csv" \
  "$EXAMPLE_DIR/output/expansion_failures.jsonl" \
  "$EXAMPLE_DIR/runs/audit.db" \
  "$EXAMPLE_DIR/runs/audit.db-shm" \
  "$EXAMPLE_DIR/runs/audit.db-wal"
find "$EXAMPLE_DIR/payloads" -mindepth 1 -type f ! -name .gitkeep -delete
find "$EXAMPLE_DIR/payloads" -mindepth 1 -depth -type d -empty -delete

echo "Preparing offline CSV blob manifest..."
"$PYTHON_BIN" "$EXAMPLE_DIR/scripts/prepare_csv_blob_manifest.py"

echo "Executing offline CSV blob expansion..."
"$ELSPETH_BIN" run --settings examples/blob_transforms/settings_expand_csv_blobs.yaml --execute

echo "Output: examples/blob_transforms/output/expanded_csv_rows.csv"
echo "Audit: examples/blob_transforms/runs/audit.db"
