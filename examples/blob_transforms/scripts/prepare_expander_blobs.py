"""Stage the JSON and text blobs for the blob_json_expand / blob_text_expand examples.

Deliberately separate from ``prepare_csv_blob_manifest.py``: that script stages
the original CSV example and is left alone so its green path cannot be disturbed
by work on the newer expanders. Both write into the same isolated offline store.

The JSON manifest carries a ``blob_content_type`` column because
``blob_json_expand`` infers its parse format from the stored content type,
fail-closed. The text manifest has no such column — ``blob_text_expand`` reads
bytes as text and has no format to choose.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from elspeth.core.payload_store import FilesystemPayloadStore

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_DIR = ROOT / "examples" / "blob_transforms"
PAYLOAD_DIR = EXAMPLE_DIR / "payloads" / "offline"
INPUT_DIR = EXAMPLE_DIR / "input"

JSON_MANIFEST = INPUT_DIR / "json_blob_manifest.csv"
TEXT_MANIFEST = INPUT_DIR / "text_blob_manifest.csv"

JSON_INPUTS = (
    ("catalog_a", INPUT_DIR / "catalog_a.json"),
    ("catalog_b", INPUT_DIR / "catalog_b.json"),
)
TEXT_INPUTS = (
    ("prose_notes", INPUT_DIR / "prose_notes.txt"),
    ("release_notes", INPUT_DIR / "release_notes.txt"),
)


def _stage(
    store: FilesystemPayloadStore,
    manifest_path: Path,
    inputs: tuple[tuple[str, Path], ...],
    *,
    content_type: str | None,
) -> None:
    fieldnames = ["manifest_index", "source_name", "source_url", "blob_ref"]
    if content_type is not None:
        fieldnames.append("blob_content_type")

    with manifest_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for manifest_index, (source_name, path) in enumerate(inputs):
            row = {
                "manifest_index": manifest_index,
                "source_name": source_name,
                # Repo-relative, so the manifest and the audit records it
                # feeds do not embed the operator's checkout path.
                "source_url": path.relative_to(ROOT).as_posix(),
                "blob_ref": store.store(path.read_bytes()),
            }
            if content_type is not None:
                row["blob_content_type"] = content_type
            writer.writerow(row)

    sys.stdout.write(f"Wrote {manifest_path.relative_to(ROOT)} ({len(inputs)} blobs)\n")


def main() -> None:
    store = FilesystemPayloadStore(PAYLOAD_DIR)
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    _stage(store, JSON_MANIFEST, JSON_INPUTS, content_type="application/json")
    _stage(store, TEXT_MANIFEST, TEXT_INPUTS, content_type=None)
    sys.stdout.write(f"Stored blobs in {PAYLOAD_DIR.relative_to(ROOT)}\n")


if __name__ == "__main__":
    main()
