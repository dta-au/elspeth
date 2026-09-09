"""Stage the shipped mock PDFs for the pdf_rasterize example."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from elspeth.core.payload_store import FilesystemPayloadStore

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_DIR = ROOT / "examples" / "pdf_rasterize"
PAYLOAD_DIR = EXAMPLE_DIR / "payloads" / "offline"
MANIFEST_PATH = EXAMPLE_DIR / "input" / "pdf_manifest.csv"

INPUTS = (
    ("report", EXAMPLE_DIR / "input" / "report.pdf"),
    ("broken", EXAMPLE_DIR / "input" / "broken.pdf"),
)


def main() -> None:
    store = FilesystemPayloadStore(PAYLOAD_DIR)
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)

    with MANIFEST_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=("manifest_index", "document_name", "blob_ref"))
        writer.writeheader()
        for manifest_index, (document_name, path) in enumerate(INPUTS):
            content = path.read_bytes()
            blob_ref = store.store(content)
            writer.writerow(
                {
                    "manifest_index": manifest_index,
                    "document_name": document_name,
                    "blob_ref": blob_ref,
                }
            )

    sys.stdout.write(f"Wrote {MANIFEST_PATH.relative_to(ROOT)}\n")
    sys.stdout.write(f"Staged {len(INPUTS)} document(s) in {PAYLOAD_DIR.relative_to(ROOT)}\n")


if __name__ == "__main__":
    main()
