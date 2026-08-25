#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
EXAMPLE_DIR="$PROJECT_ROOT/examples/pdf_rasterize"

PYTHON_BIN="${ELSPETH_PDF_RASTERIZE_PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
ELSPETH_BIN="${ELSPETH_PDF_RASTERIZE_CLI_BIN:-$PROJECT_ROOT/.venv/bin/elspeth}"

cd "$PROJECT_ROOT"

mkdir -p "$EXAMPLE_DIR/input" "$EXAMPLE_DIR/output" "$EXAMPLE_DIR/payloads/offline" "$EXAMPLE_DIR/runs"
# FilesystemPayloadStore rejects group/world-writable roots (see
# elspeth.core.payload_store._validate_store_directory). `mkdir -p` applies
# the umask, which on hosts with a permissive umask (e.g. 0002) leaves this
# directory group-writable and self-healing only fires when the store
# creates the directory itself, not when it already exists. Pin the mode
# explicitly so the launcher works regardless of the operator's umask.
chmod 700 "$EXAMPLE_DIR/payloads/offline"
rm -f \
  "$EXAMPLE_DIR/input/pdf_manifest.csv" \
  "$EXAMPLE_DIR/output/pages.jsonl" \
  "$EXAMPLE_DIR/output/quarantine.jsonl" \
  "$EXAMPLE_DIR/runs/audit.db" \
  "$EXAMPLE_DIR/runs/audit.db-shm" \
  "$EXAMPLE_DIR/runs/audit.db-wal"
find "$EXAMPLE_DIR/payloads/offline" -mindepth 1 -type f ! -name .gitkeep -delete
find "$EXAMPLE_DIR/payloads/offline" -mindepth 1 -depth -type d -empty -delete

echo "Staging mock PDFs into the payload store..."
"$PYTHON_BIN" "$EXAMPLE_DIR/scripts/prepare_pdf_manifest.py"

echo "Executing pdf_rasterize pipeline..."
"$ELSPETH_BIN" run --settings examples/pdf_rasterize/settings.yaml --execute

echo "Pages: examples/pdf_rasterize/output/pages.jsonl"
echo "Quarantine: examples/pdf_rasterize/output/quarantine.jsonl"
echo "Audit: examples/pdf_rasterize/runs/audit.db"
