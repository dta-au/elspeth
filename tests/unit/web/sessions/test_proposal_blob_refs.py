"""Tests for the closed proposal-to-blob custody contract."""

from __future__ import annotations

import pytest

from elspeth.web.sessions.proposal_blob_refs import proposal_blob_reference_ids


def test_guided_set_pipeline_extracts_plural_source_blob_authority() -> None:
    arguments = {
        "sources": {
            "orders": {"options": {"blob_ref": "blob-one", "path": "blob:blob-one"}},
            "customers": {"options": {"file": "blob:blob-two"}},
            "manual": {"options": {"path": "/operator/input.csv"}},
        }
    }

    assert proposal_blob_reference_ids("set_pipeline", arguments) == ("blob-one", "blob-two")


def test_guided_set_pipeline_deduplicates_shared_blob_authority() -> None:
    arguments = {
        "sources": {
            "first": {"options": {"blob_ref": "shared"}},
            "second": {"options": {"path": "blob:shared"}},
        }
    }

    assert proposal_blob_reference_ids("set_pipeline", arguments) == ("shared",)


@pytest.mark.parametrize(
    "options",
    [
        {"blob_ref": 1},
        {"path": "blob:"},
        {"blob_ref": "one", "file": "blob:two"},
    ],
)
def test_guided_set_pipeline_rejects_malformed_blob_authority(options: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="set_pipeline"):
        proposal_blob_reference_ids("set_pipeline", {"sources": {"input": {"options": options}}})


def test_set_pipeline_preserves_legacy_singular_blob_authority() -> None:
    assert proposal_blob_reference_ids("set_pipeline", {"source": {"blob_id": "legacy"}}) == ("legacy",)
