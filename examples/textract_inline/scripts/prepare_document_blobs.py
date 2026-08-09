"""Stage local documents for the aws_textract_inline_analysis example.

Reads every document in ``examples/textract_inline/input/``, verifies each
one's byte signature against the closed jpeg/png/pdf vocabulary, requires the
set to be format-homogeneous (one transform instance analyzes one declared
format), stores the bytes in the example's isolated payload store, and
generates ``settings.generated.yaml`` with the authoritative ``blob_rows``
entries.

The generated pipeline calls the REAL Amazon Textract AnalyzeDocument API:
running it requires AWS credentials (default chain) with Textract access in
the chosen region, and every run is billable.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import uuid
from pathlib import Path
from typing import Any

import yaml

from elspeth.contracts.binary_documents import (
    BINARY_DOCUMENT_MAX_BYTES,
    binary_document_signature_matches,
)
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.plugins.transforms.aws.textract_regions import TEXTRACT_REGIONS

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_DIR = ROOT / "examples" / "textract_inline"
INPUT_DIR = EXAMPLE_DIR / "input"
PAYLOAD_DIR = EXAMPLE_DIR / "payloads" / "offline"
SETTINGS_PATH = EXAMPLE_DIR / "settings.generated.yaml"

_FORMATS = ("jpeg", "png", "pdf")
_MIME_BY_FORMAT = {"jpeg": "image/jpeg", "png": "image/png", "pdf": "application/pdf"}


def _detect_format(content: bytes, *, name: str) -> str:
    for document_format in _FORMATS:
        if binary_document_signature_matches(document_format, content):  # type: ignore[arg-type]
            return document_format
    raise SystemExit(f"{name}: not a recognized JPEG, PNG, or PDF document (byte signature mismatch)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="ap-southeast-2", choices=sorted(TEXTRACT_REGIONS))
    args = parser.parse_args()

    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    documents = sorted(path for path in INPUT_DIR.iterdir() if path.is_file() and path.name != "README.md")
    if not documents:
        raise SystemExit(f"No documents found. Copy JPEG, PNG, or single-page PDF files into {INPUT_DIR.relative_to(ROOT)} first.")

    store = FilesystemPayloadStore(PAYLOAD_DIR)
    entries: list[dict[str, Any]] = []
    formats: set[str] = set()
    for path in documents:
        content = path.read_bytes()
        if not content:
            raise SystemExit(f"{path.name}: empty file")
        if len(content) > BINARY_DOCUMENT_MAX_BYTES:
            raise SystemExit(f"{path.name}: exceeds the 5 MiB synchronous Textract bound")
        document_format = _detect_format(content, name=path.name)
        formats.add(document_format)
        payload_ref = store.store(content)
        assert payload_ref == hashlib.sha256(content).hexdigest()
        entries.append(
            {
                # Outside the web service the trusted CLI operator supplies
                # the entries; blob_id is provenance, not session custody.
                "blob_id": str(uuid.uuid4()),
                "payload_ref": payload_ref,
                "filename": path.name,
                "mime_type": _MIME_BY_FORMAT[document_format],
                "size_bytes": len(content),
            }
        )

    if len(formats) != 1:
        raise SystemExit(
            f"Mixed document formats {sorted(formats)}: one transform instance analyzes one declared format. "
            "Stage one format at a time (or build branches with one instance per format)."
        )
    document_format = formats.pop()

    settings: dict[str, Any] = {
        "sources": {
            "documents": {
                "plugin": "blob_rows",
                "on_success": "documents",
                "options": {
                    "blobs": entries,
                    "schema": {"mode": "observed"},
                    "on_validation_failure": "discard",
                },
            }
        },
        "transforms": [
            {
                "name": "analyze",
                "plugin": "aws_textract_inline_analysis",
                "input": "documents",
                "on_success": "results",
                "on_error": "failures",
                "options": {
                    "region": args.region,
                    "auth_mode": "default_chain",
                    "document_format": document_format,
                    "feature_types": ["TABLES", "FORMS"],
                    "text_field": "textract_text",
                    "page_count_field": "textract_page_count",
                    "metadata_field": "textract_metadata",
                    "schema": {"mode": "observed"},
                },
            }
        ],
        "sinks": {
            "results": {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {
                    "path": "examples/textract_inline/output/textract_results.jsonl",
                    "format": "jsonl",
                    "schema": {"mode": "observed"},
                },
            },
            "failures": {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {
                    "path": "examples/textract_inline/output/textract_failures.jsonl",
                    "format": "jsonl",
                    "schema": {"mode": "observed"},
                },
            },
        },
        "landscape": {"url": "sqlite:///examples/textract_inline/runs/audit.db"},
        "payload_store": {
            "backend": "filesystem",
            "base_path": "examples/textract_inline/payloads/offline",
        },
    }

    SETTINGS_PATH.write_text(yaml.safe_dump(settings, sort_keys=False), encoding="utf-8")
    sys.stdout.write(f"Staged {len(entries)} {document_format} document(s) in {PAYLOAD_DIR.relative_to(ROOT)}\n")
    sys.stdout.write(f"Wrote {SETTINGS_PATH.relative_to(ROOT)}\n")
    sys.stdout.write("Run (billable AWS call):\n")
    sys.stdout.write("  elspeth run --settings examples/textract_inline/settings.generated.yaml --execute\n")


if __name__ == "__main__":
    main()
