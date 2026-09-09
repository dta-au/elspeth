#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
EXAMPLE_DIR="$PROJECT_ROOT/examples/blob_transforms"

ELSPETH_BIN="${ELSPETH_BLOB_TRANSFORMS_CLI_BIN:-$PROJECT_ROOT/.venv/bin/elspeth}"

cd "$PROJECT_ROOT"

# The payload store rejects group/world-writable roots. Hosts commonly use a
# collaborative umask such as 0002, so make the example safe before startup.
mkdir -p "$EXAMPLE_DIR/output" "$EXAMPLE_DIR/payloads" "$EXAMPLE_DIR/runs"
chmod 700 "$EXAMPLE_DIR/payloads"

echo "Fetching hosted tutorial HTML into the payload store..."
"$ELSPETH_BIN" run --settings examples/blob_transforms/settings_fetch_tutorial_html.yaml --execute

echo "Output: examples/blob_transforms/output/tutorial_html_blobs.jsonl"
echo "Audit: examples/blob_transforms/runs/audit.db"
