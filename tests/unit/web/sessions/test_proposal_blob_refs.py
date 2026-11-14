"""Unit tests for the closed proposal blob-reference extractor.

``proposal_blob_reference_ids`` decides which blobs a pending proposal retains
and which staged references must prove ownership, including nested inline
markers and ``blob:<uuid>`` path sentinels.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from elspeth.web.composer.pipeline_proposal import OWNED_COMPOSITION_STATE_AUTHORITY
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.proposal_blob_refs import (
    MalformedProposalBlobReferenceError,
    ProposalResourceUnavailableError,
    proposal_blob_reference_ids,
)
from tests.fixtures.resource_identity import BLOB_UUID, UUID_SPELLINGS, uuid_spelling


@pytest.mark.parametrize("spelling", UUID_SPELLINGS)
@pytest.mark.parametrize(
    "tool", ["update_blob", "delete_blob", "set_source_from_blob", "wire_blob_inline_ref", "set_source_from_blobs", "set_pipeline"]
)
def test_reference_projection_canonicalizes_selector_custody_without_rewriting_arguments(spelling, tool):
    from copy import deepcopy

    alias = uuid_spelling(BLOB_UUID, spelling)
    arguments = {"blob_id": alias}
    if tool == "set_source_from_blobs":
        arguments = {"blob_ids": [alias, BLOB_UUID]}
    elif tool == "set_pipeline":
        # Conservative custody projection does not admit this graph marker.
        arguments = {"source": {"blob_id": alias, "options": {"nested": [{"blob_ref": alias}], "path": f"blob:{BLOB_UUID}"}}}
    original = deepcopy(arguments)
    assert proposal_blob_reference_ids(tool, arguments) == (BLOB_UUID,)
    assert arguments == original


@pytest.mark.parametrize("spelling", UUID_SPELLINGS)
@pytest.mark.parametrize("availability", ["ready", "missing", "foreign", "pending", "error", "corrupt"])
def test_alias_reference_validation_preserves_ownership_status_and_corruption(spelling, availability):
    from sqlalchemy import Column, MetaData, String, Table

    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.sessions.proposal_blob_refs import validate_proposal_blob_references

    engine = create_session_engine("sqlite:///:memory:")
    metadata = MetaData()
    blobs = Table("blobs", metadata, Column("id", String), Column("session_id", String), Column("status", String))
    metadata.create_all(engine)
    arguments = {"blob_id": uuid_spelling(BLOB_UUID, spelling)}
    with engine.begin() as conn:
        if availability != "missing":
            conn.execute(
                blobs.insert().values(
                    id=BLOB_UUID,
                    session_id="foreign" if availability == "foreign" else "owned",
                    status="invalid" if availability == "corrupt" else availability,
                )
            )
        if availability == "ready":
            validate_proposal_blob_references(conn, session_id="owned", tool_name="delete_blob", arguments=arguments)
        else:
            expected = AuditIntegrityError if availability == "corrupt" else ProposalResourceUnavailableError
            with pytest.raises(expected):
                validate_proposal_blob_references(conn, session_id="owned", tool_name="delete_blob", arguments=arguments)
    engine.dispose()


@pytest.mark.parametrize("spelling", ["uppercase", "mixed", "braced", "hyphenless", "urn"])
def test_conservative_projection_never_relaxes_strict_sentinel_grammar(spelling):
    with pytest.raises(ValueError, match="non-canonical"):
        proposal_blob_reference_ids("set_pipeline", {"source": {"options": {"path": "blob:" + uuid_spelling(BLOB_UUID, spelling)}}})


def test_persisted_pending_row_with_malformed_blob_ref_stays_a_tier1_integrity_error():
    """Negative control for the Tier-3 admission refusal: our own persisted row is not repairable by the planner."""
    from sqlalchemy import JSON, Column, MetaData, String, Table

    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.sessions.proposal_blob_refs import pending_proposal_reference_id

    engine = create_session_engine("sqlite:///:memory:")
    metadata = MetaData()
    proposals = Table(
        "composition_proposals",
        metadata,
        Column("id", String),
        Column("session_id", String),
        Column("status", String),
        Column("tool_name", String),
        Column("arguments_json", JSON),
    )
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            proposals.insert().values(
                id="corrupt",
                session_id="owned",
                status="pending",
                tool_name="set_output",
                arguments_json={"options": {"schema": {"blob_ref": 5}}},
            )
        )
        with pytest.raises(AuditIntegrityError, match="malformed blob authority"):
            pending_proposal_reference_id(conn, session_id="owned", blob_id=BLOB_UUID)
    engine.dispose()


@pytest.mark.parametrize("spelling", UUID_SPELLINGS)
def test_pending_alias_retains_blob_and_only_exact_acceptance_is_excluded(spelling):
    from sqlalchemy import JSON, Column, MetaData, String, Table

    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.sessions.proposal_blob_refs import pending_proposal_reference_id

    engine = create_session_engine("sqlite:///:memory:")
    metadata = MetaData()
    proposals = Table(
        "composition_proposals",
        metadata,
        Column("id", String),
        Column("session_id", String),
        Column("status", String),
        Column("tool_name", String),
        Column("arguments_json", JSON),
    )
    metadata.create_all(engine)
    arguments = {"blob_id": uuid_spelling(BLOB_UUID, spelling), "content": "private-original"}
    with engine.begin() as conn:
        conn.execute(
            proposals.insert().values(
                id="accepting", session_id="owned", status="pending", tool_name="update_blob", arguments_json=arguments
            )
        )
        assert pending_proposal_reference_id(conn, session_id="owned", blob_id=BLOB_UUID) == "accepting"
        assert pending_proposal_reference_id(conn, session_id="foreign", blob_id=BLOB_UUID) is None
        assert (
            pending_proposal_reference_id(
                conn, session_id="owned", blob_id=BLOB_UUID, accepting_proposal_id="accepting", accepting_tool_name="update_blob"
            )
            is None
        )
        with pytest.raises(AuditIntegrityError, match="tool does not match"):
            pending_proposal_reference_id(
                conn, session_id="owned", blob_id=BLOB_UUID, accepting_proposal_id="accepting", accepting_tool_name="delete_blob"
            )
        conn.execute(
            proposals.insert().values(
                id="second",
                session_id="owned",
                status="pending",
                tool_name="delete_blob",
                arguments_json={"blob_id": uuid_spelling(BLOB_UUID, "urn")},
            )
        )
        assert (
            pending_proposal_reference_id(
                conn, session_id="owned", blob_id=BLOB_UUID, accepting_proposal_id="accepting", accepting_tool_name="update_blob"
            )
            == "second"
        )
        assert conn.execute(proposals.select().where(proposals.c.id == "accepting")).one().arguments_json == arguments
    engine.dispose()


@pytest.mark.parametrize("availability", ["missing", "foreign", "pending", "error", "ready", "corrupt", "null"])
def test_final_reference_check_distinguishes_resource_refusal_from_owned_corruption(availability: str) -> None:
    from sqlalchemy import Column, MetaData, String, Table

    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.sessions import proposal_blob_refs

    engine = create_session_engine("sqlite:///:memory:")
    metadata = MetaData()
    blobs = Table("blobs", metadata, Column("id", String), Column("session_id", String), Column("status", String))
    metadata.create_all(engine)
    with engine.begin() as conn:
        if availability != "missing":
            conn.execute(
                blobs.insert().values(
                    id="private-resource",
                    session_id="foreign" if availability == "foreign" else "owned",
                    status=None if availability == "null" else "unknown" if availability == "corrupt" else availability,
                )
            )
        if availability == "ready":
            proposal_blob_refs.validate_proposal_blob_references(
                conn, session_id="owned", tool_name="delete_blob", arguments={"blob_id": "private-resource"}
            )
        elif availability in {"corrupt", "null"}:
            with pytest.raises(AuditIntegrityError):
                proposal_blob_refs.validate_proposal_blob_references(
                    conn, session_id="owned", tool_name="delete_blob", arguments={"blob_id": "private-resource"}
                )
        else:
            with pytest.raises(ProposalResourceUnavailableError) as caught:
                proposal_blob_refs.validate_proposal_blob_references(
                    conn, session_id="owned", tool_name="delete_blob", arguments={"blob_id": "private-resource"}
                )
            assert type(caught.value) is ProposalResourceUnavailableError
            assert "private-resource" not in str(caught.value)
    engine.dispose()


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        # Model-supplied (Tier 3): a repairable, typed admission refusal.
        ({"source": {"options": {"blob_ref": 7}}}, MalformedProposalBlobReferenceError),
        ({"source": {"options": {"path": "blob:malformed"}}}, MalformedProposalBlobReferenceError),
        # Server-owned authority (Tier 1): corruption stays a loud plain ValueError.
        ({"authority_kind": OWNED_COMPOSITION_STATE_AUTHORITY, "sources": []}, ValueError),
    ],
)
def test_malformed_authority_is_not_an_expected_resource_refusal(arguments: dict, expected: type[Exception]) -> None:

    from elspeth.web.sessions.proposal_blob_refs import validate_proposal_blob_references

    engine = create_session_engine("sqlite:///:memory:")
    # No table exists: a malformed authority must fail before any resource query.
    with engine.connect() as conn, pytest.raises(expected) as caught:
        validate_proposal_blob_references(conn, session_id="owned", tool_name="set_pipeline", arguments=arguments)
    assert type(caught.value) is expected
    assert not isinstance(caught.value, ProposalResourceUnavailableError)
    engine.dispose()


def _sha() -> str:
    return "a" * 64


@pytest.mark.parametrize(
    "tool", ["patch_node_options", "patch_source_options", "patch_output_options", "upsert_node", "set_source", "set_output"]
)
def test_mutation_options_retain_only_owned_reference_positions(tool: str) -> None:
    field = "patch" if tool.startswith("patch_") else "options"
    arguments = {field: {"prompts": [{"blob_ref": BLOB_UUID, "mode": "inline_content", "sha256": _sha()}]}}
    assert proposal_blob_reference_ids(tool, arguments) == (BLOB_UUID,)
    assert proposal_blob_reference_ids(tool, {"description": arguments[field]}) == ()
    assert proposal_blob_reference_ids("unknown_tool", arguments) == ()
    with pytest.raises(ValueError, match="must be a mapping"):
        proposal_blob_reference_ids(tool, {field: []})


@pytest.mark.parametrize(
    "tool", ["patch_node_options", "patch_source_options", "patch_output_options", "upsert_node", "set_source", "set_output"]
)
def test_mutation_options_absent_or_null_have_no_references(tool: str) -> None:
    field = "patch" if tool.startswith("patch_") else "options"
    assert proposal_blob_reference_ids(tool, {}) == ()
    assert proposal_blob_reference_ids(tool, {field: None}) == ()


@pytest.mark.parametrize(
    "tool", ["patch_node_options", "patch_source_options", "patch_output_options", "upsert_node", "set_source", "set_output"]
)
@pytest.mark.parametrize("value", [[], "wrong-type"])
def test_mutation_options_wrong_type_is_corruption(tool: str, value: object) -> None:
    field = "patch" if tool.startswith("patch_") else "options"
    with pytest.raises(ValueError, match="must be a mapping"):
        proposal_blob_reference_ids(tool, {field: value})


class TestLegacySetPipelineShapes:
    def test_source_blob_id_is_extracted(self) -> None:
        blob_id = str(uuid4())
        arguments = {"source": {"plugin": "csv", "blob_id": blob_id, "options": {"schema": {"mode": "observed"}}}}
        assert proposal_blob_reference_ids("set_pipeline", arguments) == (blob_id,)

    def test_source_options_path_sentinel_is_extracted(self) -> None:
        """Direct repro from elspeth-b3feba9a7c: source.options.path='blob:<uuid>'."""
        blob_id = str(uuid4())
        arguments = {"source": {"plugin": "csv", "options": {"path": f"blob:{blob_id}", "schema": {"mode": "observed"}}}}
        assert proposal_blob_reference_ids("set_pipeline", arguments) == (blob_id,)

    def test_source_options_file_sentinel_is_extracted(self) -> None:
        blob_id = str(uuid4())
        arguments = {"source": {"plugin": "json", "options": {"file": f"blob:{blob_id}"}}}
        assert proposal_blob_reference_ids("set_pipeline", arguments) == (blob_id,)

    def test_source_options_blob_ref_is_extracted(self) -> None:
        blob_id = str(uuid4())
        arguments = {"source": {"plugin": "csv", "options": {"blob_ref": blob_id, "path": "/ignored/raw/path.csv"}}}
        assert proposal_blob_reference_ids("set_pipeline", arguments) == (blob_id,)

    def test_nested_inline_marker_in_source_options_is_extracted(self) -> None:
        blob_id = str(uuid4())
        arguments = {
            "source": {
                "plugin": "csv",
                "options": {"queries": {"blob_ref": blob_id, "mode": "inline_content", "sha256": _sha()}},
            }
        }
        assert proposal_blob_reference_ids("set_pipeline", arguments) == (blob_id,)

    def test_blob_id_and_sentinel_dedupe(self) -> None:
        blob_id = str(uuid4())
        arguments = {"source": {"plugin": "csv", "blob_id": blob_id, "options": {"path": f"blob:{blob_id}"}}}
        assert proposal_blob_reference_ids("set_pipeline", arguments) == (blob_id,)

    def test_non_blob_path_yields_nothing(self) -> None:
        arguments = {"source": {"plugin": "csv", "options": {"path": "/data/rows.csv"}}}
        assert proposal_blob_reference_ids("set_pipeline", arguments) == ()

    def test_malformed_sentinel_raises(self) -> None:
        arguments = {"source": {"plugin": "csv", "options": {"path": "blob:not-a-uuid"}}}
        with pytest.raises(ValueError):
            proposal_blob_reference_ids("set_pipeline", arguments)

    def test_non_string_nested_blob_ref_raises(self) -> None:
        arguments = {"source": {"plugin": "csv", "options": {"queries": {"blob_ref": 7}}}}
        with pytest.raises(ValueError):
            proposal_blob_reference_ids("set_pipeline", arguments)


class TestSetSourceFromBlobsRetention:
    """The plural binding's inputs must be retained while a proposal is
    pending (elspeth-0c6a343921 review): the tool's blob_ids and the
    persisted blob_rows blobs[].blob_id identity shape both create
    custody/retention edges."""

    def test_blob_ids_are_extracted(self) -> None:
        blob_a = str(uuid4())
        blob_b = str(uuid4())
        arguments = {"blob_ids": [blob_a, blob_b], "on_success": "continue"}
        assert proposal_blob_reference_ids("set_source_from_blobs", arguments) == (blob_a, blob_b)

    def test_blob_ids_dedupe(self) -> None:
        blob_id = str(uuid4())
        arguments = {"blob_ids": [blob_id, blob_id], "on_success": "continue"}
        assert proposal_blob_reference_ids("set_source_from_blobs", arguments) == (blob_id,)

    def test_missing_blob_ids_yields_nothing(self) -> None:
        assert proposal_blob_reference_ids("set_source_from_blobs", {"on_success": "continue"}) == ()

    def test_non_list_blob_ids_raises(self) -> None:
        with pytest.raises(ValueError):
            proposal_blob_reference_ids("set_source_from_blobs", {"blob_ids": "not-a-list"})

    def test_non_string_member_raises(self) -> None:
        with pytest.raises(ValueError):
            proposal_blob_reference_ids("set_source_from_blobs", {"blob_ids": [str(uuid4()), 7]})


class TestBlobRowsEntryShapes:
    def test_legacy_set_pipeline_blob_rows_entries_are_extracted(self) -> None:
        blob_a = str(uuid4())
        blob_b = str(uuid4())
        arguments = {
            "source": {
                "plugin": "blob_rows",
                "options": {
                    "blobs": [
                        {"blob_id": blob_a, "payload_ref": _sha(), "filename": "a.pdf", "mime_type": "application/pdf", "size_bytes": 10},
                        {"blob_id": blob_b, "payload_ref": _sha(), "filename": "b.png", "mime_type": "image/png", "size_bytes": 20},
                    ],
                    "schema": {"mode": "observed"},
                },
            }
        }
        assert set(proposal_blob_reference_ids("set_pipeline", arguments)) == {blob_a, blob_b}

    def test_non_string_entry_blob_id_raises(self) -> None:
        arguments = {
            "source": {
                "plugin": "blob_rows",
                "options": {"blobs": [{"blob_id": 7, "payload_ref": _sha()}]},
            }
        }
        with pytest.raises(ValueError):
            proposal_blob_reference_ids("set_pipeline", arguments)


class TestOwnedAuthorityShapes:
    def _owned(self, *, sources: dict, nodes: list | None = None, outputs: list | None = None) -> dict:
        return {
            "authority_kind": OWNED_COMPOSITION_STATE_AUTHORITY,
            "sources": sources,
            "nodes": nodes if nodes is not None else [],
            "edges": [],
            "outputs": outputs if outputs is not None else [],
            "metadata": {"name": "owned", "description": ""},
        }

    def test_owned_blob_rows_entries_are_extracted(self) -> None:
        """An owned set_pipeline proposal persisting a blob_rows source must
        retain every blobs[].blob_id while pending (elspeth-0c6a343921)."""
        blob_id = str(uuid4())
        arguments = self._owned(
            sources={
                "documents": {
                    "plugin": "blob_rows",
                    "options": {
                        "blobs": [
                            {
                                "blob_id": blob_id,
                                "payload_ref": _sha(),
                                "filename": "doc.pdf",
                                "mime_type": "application/pdf",
                                "size_bytes": 10,
                            }
                        ],
                        "schema": {"mode": "observed"},
                    },
                }
            }
        )
        assert proposal_blob_reference_ids("set_pipeline", arguments) == (blob_id,)

    def test_named_source_path_sentinels_are_extracted(self) -> None:
        blob_a = str(uuid4())
        blob_b = str(uuid4())
        arguments = self._owned(
            sources={
                "orders": {"plugin": "csv", "options": {"path": f"blob:{blob_a}"}},
                "refunds": {"plugin": "csv", "options": {"file": f"blob:{blob_b}"}},
            }
        )
        assert set(proposal_blob_reference_ids("set_pipeline", arguments)) == {blob_a, blob_b}

    def test_node_and_output_nested_markers_are_extracted(self) -> None:
        blob_node = str(uuid4())
        blob_output = str(uuid4())
        arguments = self._owned(
            sources={"source": {"plugin": "csv", "options": {"path": "/data/rows.csv"}}},
            nodes=[
                {
                    "id": "enrich",
                    "node_type": "transform",
                    "plugin": "llm",
                    "options": {"prompts": [{"blob_ref": blob_node, "mode": "inline_content", "sha256": _sha()}]},
                }
            ],
            outputs=[
                {
                    "name": "result",
                    "plugin": "json",
                    "options": {"template": {"blob_ref": blob_output, "mode": "inline_content", "sha256": _sha()}},
                }
            ],
        )
        assert set(proposal_blob_reference_ids("set_pipeline", arguments)) == {blob_node, blob_output}

    def test_top_level_blob_ref_still_extracted(self) -> None:
        blob_id = str(uuid4())
        arguments = self._owned(sources={"source": {"plugin": "csv", "options": {"blob_ref": blob_id, "path": "/raw.csv"}}})
        assert proposal_blob_reference_ids("set_pipeline", arguments) == (blob_id,)
