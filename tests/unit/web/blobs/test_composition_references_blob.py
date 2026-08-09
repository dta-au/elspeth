"""Tests for active-run blob reference discovery across composition state."""

from __future__ import annotations

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.blobs.service import _composition_references_blob


def test_composition_references_blob_finds_transform_inline_content_ref() -> None:
    composition_state = {
        "sources": {"primary": {"plugin": "csv", "options": {"path": "data.csv"}}},
        "transforms": [
            {
                "name": "classify",
                "plugin": "llm",
                "options": {
                    "system_prompt": {
                        "blob_ref": "blob-123",
                        "mode": "inline_content",
                        "sha256": "a" * 64,
                    }
                },
            }
        ],
        "sinks": {"output": {"plugin": "csv", "options": {"path": "out.csv"}}},
    }

    assert _composition_references_blob(composition_state, "blob-123", "/unused")


def test_composition_references_blob_preserves_legacy_path_match() -> None:
    composition_state = {
        "sources": {"primary": {"plugin": "csv", "options": {"path": "/blob/storage.csv"}}},
    }

    assert _composition_references_blob(composition_state, "other-blob", "/blob/storage.csv")


def test_composition_references_blob_finds_blob_rows_entry_blob_id() -> None:
    """blob_rows persists custody as blobs[].blob_id — the active-run scanner
    must see it, or a delete racing background run startup (before
    _link_blob_rows_to_run creates the run-link rows) succeeds and admission
    then fails on a missing blob (elspeth-0c6a343921 review)."""
    composition_state = {
        "sources": {
            "documents": {
                "plugin": "blob_rows",
                "options": {
                    "blobs": [
                        {
                            "blob_id": "0a274caf-6d51-44a4-b8ef-3d2f5f77a1f0",
                            "payload_ref": "c" * 64,
                            "filename": "doc.pdf",
                            "mime_type": "application/pdf",
                            "size_bytes": 1234,
                        }
                    ],
                    "schema": {"mode": "observed"},
                },
            }
        },
    }

    assert _composition_references_blob(composition_state, "0a274caf-6d51-44a4-b8ef-3d2f5f77a1f0", "/unused")
    assert not _composition_references_blob(composition_state, "1b385db0-7e62-55b5-c9f0-4e3f6f88b2f1", "/unused")


def test_composition_references_blob_crashes_on_corrupt_source_options() -> None:
    composition_state = {
        "sources": {"primary": {"plugin": "csv", "options": ["not", "a", "dict"]}},
    }

    with pytest.raises(AuditIntegrityError, match=r"sources\['primary'\]\.options"):
        _composition_references_blob(composition_state, "blob-123", "/unused")
