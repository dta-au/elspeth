"""The PostgreSQL delete path's blob-reference walker (elspeth-f123a7b3d2).

``PostgresSessionOperationRepository._require_blob_deletion_retention_clear``
walks the active run's composition state with this module's own copy of the
blob store's walker. A copy that recognises less vocabulary, or compares a
bound UUID by spelling, lets a referenced blob be deleted from under a queued
run on exactly the multi-replica deployment 0.8.0 targets. Every row here is
a case variant or a vocabulary arm that the walker refused before the fix.
The comparison is the contract's ``names_same_blob`` (the helper the blob
store's own walker uses, so the two walkers cannot drift apart); reverting it
to an exact comparison must turn the case-variant rows red again.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.repository import _composition_references_blob

_BLOB_ID = "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
_UPPER = _BLOB_ID.upper()
_MIXED = "0F1e2D3c-4b5A-6978-8A9b-0c1D2e3F4a5B"


def _state(**options: Any) -> dict[str, Any]:
    return {
        "sources": {"primary": {"plugin": "csv", "options": {"path": "data.csv"}}},
        "transforms": [{"name": "classify", "plugin": "llm", "options": options}],
        "sinks": {"output": {"plugin": "csv", "options": {"path": "out.csv"}}},
    }


@pytest.mark.parametrize("spelling", [_BLOB_ID, _UPPER, _MIXED])
def test_a_blob_ref_marker_is_found_in_either_hex_case(spelling: str) -> None:
    state = _state(system_prompt={"blob_ref": spelling, "mode": "inline_content", "sha256": "a" * 64})
    assert _composition_references_blob(state, _BLOB_ID, "/unused")


@pytest.mark.parametrize("spelling", [_BLOB_ID, _UPPER, _MIXED])
def test_a_blob_rows_custody_key_is_found_in_either_hex_case(spelling: str) -> None:
    """``blob_rows`` persists custody as ``blobs[].blob_id`` (elspeth-0c6a343921)."""
    state = _state(blobs=[{"blob_id": spelling, "filename": "input.csv"}])
    assert _composition_references_blob(state, _BLOB_ID, "/unused")


def test_a_nested_marker_inside_a_list_is_found() -> None:
    state = _state(prompts=[{"unrelated": 1}, [{"blob_ref": _UPPER}]])
    assert _composition_references_blob(state, _BLOB_ID, "/unused")


def test_the_store_id_may_itself_be_upper_case() -> None:
    state = _state(system_prompt={"blob_ref": _BLOB_ID})
    assert _composition_references_blob(state, _UPPER, "/unused")


def test_the_legacy_storage_path_match_is_exact() -> None:
    state = _state(input={"file": "/blob/storage.csv"})
    assert _composition_references_blob(state, "other-blob", "/blob/storage.csv")
    assert not _composition_references_blob(state, "other-blob", "/BLOB/STORAGE.CSV")


@pytest.mark.parametrize(
    "spelling",
    [
        "{0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b}",
        "0f1e2d3c4b5a69788a9b0c1d2e3f4a5b",
        "urn:uuid:0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b",
        "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5c",
    ],
)
def test_spellings_the_contract_never_binds_are_not_matches(spelling: str) -> None:
    state = _state(system_prompt={"blob_ref": spelling})
    assert not _composition_references_blob(state, _BLOB_ID, "/unused")


@pytest.mark.parametrize("key", ["blob_ref", "blob_id"])
@pytest.mark.parametrize("marker", [None, 1234, [_BLOB_ID]])
def test_a_non_string_marker_value_is_not_a_match(key: str, marker: Any) -> None:
    """A present-but-non-str id value is a non-match, never a crash (parity with the store's walker)."""
    state = _state(system_prompt={key: marker})
    assert not _composition_references_blob(state, _BLOB_ID, "/unused")


def test_an_unrelated_state_is_not_a_match() -> None:
    assert not _composition_references_blob(_state(system_prompt={"blob_ref": "0000-not-it"}), _BLOB_ID, "/unused")


def test_corrupt_options_still_crash_rather_than_read_as_unreferenced() -> None:
    state = {"sources": {"primary": {"plugin": "csv", "options": ["not", "a", "dict"]}}}
    with pytest.raises(AuditIntegrityError):
        _composition_references_blob(state, _BLOB_ID, "/unused")
