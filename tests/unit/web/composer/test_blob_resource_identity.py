"""External UUID selector identity through real tool and custody boundaries."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from elspeth.web.blobs.service import content_hash
from elspeth.web.composer.state import SourceSpec
from elspeth.web.composer.tools.blobs import admit_blob_mutation
from elspeth.web.sessions.models import blobs_table, sessions_table
from tests.fixtures.resource_identity import BLOB_UUID, UUID_SPELLINGS, uuid_spelling
from tests.unit.web.composer.test_blob_inline_tools import _inline_ref_state
from tests.unit.web.composer.test_set_source_from_blobs import _catalog_with_blob_rows
from tests.unit.web.composer.test_tools import (
    _blob_operation,
    _empty_state,
    _session_engine_with_session,
    _trained_tool_context,
    _verbatim_blob_context,
    execute_tool,
)


@pytest.fixture
def resource(tmp_path):
    engine, session_id = _session_engine_with_session()
    path = tmp_path / "blobs" / session_id / f"{BLOB_UUID}_data.csv"
    path.parent.mkdir(parents=True)
    content = b"name,amount\nalice,12\n"
    path.write_bytes(content)
    with engine.begin() as conn:
        conn.execute(
            blobs_table.insert().values(
                id=BLOB_UUID,
                session_id=session_id,
                filename="data.csv",
                mime_type="text/csv",
                size_bytes=len(content),
                content_hash=content_hash(content),
                storage_path=str(path),
                created_at=datetime.now(UTC),
                created_by="user",
                status="ready",
            )
        )
    yield engine, session_id, path, tmp_path
    engine.dispose()


@pytest.mark.parametrize("spelling", UUID_SPELLINGS)
@pytest.mark.parametrize(
    "tool",
    [
        "get_blob_metadata",
        "inspect_source",
        "get_blob_content",
        "read_attachment",
        "set_source_from_blob",
        "set_source_from_blobs",
        "wire_blob_inline_ref",
    ],
)
def test_accepted_selector_resolves_authoritative_canonical_blob(resource, spelling, tool):
    engine, session_id, path, root = resource
    selector = uuid_spelling(BLOB_UUID, spelling)
    arguments = {"blob_id": selector}
    state = _empty_state()
    if tool in {"set_source_from_blob", "set_source_from_blobs"}:
        arguments.update(on_success="rows", on_validation_failure="discard")
    if tool == "set_source_from_blob":
        arguments["options"] = {"schema": {"mode": "observed"}}
    if tool == "set_source_from_blobs":
        arguments = {"blob_ids": [selector], "on_success": "rows", "on_validation_failure": "discard"}
    if tool == "wire_blob_inline_ref":
        state = _inline_ref_state()
        arguments["field_path"] = "node:classify.options.prompt_template"
    with _blob_operation(engine, session_id) as (authority, operation):
        result = execute_tool(
            tool,
            arguments,
            state,
            _catalog_with_blob_rows(),
            session_engine=engine,
            session_id=session_id,
            data_dir=str(root),
            session_operation_authority=authority,
            session_operation_context=operation,
        )
    assert result.success, result.validation
    assert arguments.get("blob_id", arguments.get("blob_ids")) == (selector if tool != "set_source_from_blobs" else [selector])
    if tool == "get_blob_metadata":
        assert set(result.data) == {"id", "filename", "mime_type", "size_bytes", "content_hash", "status"}
        assert result.data["id"] == BLOB_UUID
    elif tool in {"get_blob_content", "read_attachment"}:
        assert result.data["blob_id"] == BLOB_UUID
        assert result.data["content"] == path.read_text()
        assert result.data["content_hash"] == content_hash(path.read_bytes())
    elif tool == "set_source_from_blob":
        assert result.updated_state.sources["source"].options["blob_ref"] == BLOB_UUID
    elif tool == "set_source_from_blobs":
        assert result.updated_state.sources["source"].options["blobs"][0]["blob_id"] == BLOB_UUID
    elif tool == "wire_blob_inline_ref":
        assert result.updated_state.nodes[0].options["prompt_template"]["blob_ref"] == BLOB_UUID
    else:
        assert result.data["redacted_identity"]["blob_id"] == BLOB_UUID


@pytest.mark.parametrize("spelling", UUID_SPELLINGS)
@pytest.mark.parametrize("tool", ["update_blob", "delete_blob"])
@pytest.mark.parametrize("binding", ["blob_ref", "sentinel", "nested"])
def test_bound_selector_alias_refuses_admission_and_effect_without_resource_change(resource, spelling, tool, binding):
    engine, session_id, path, root = resource
    options = {"path": "elsewhere.csv", "blob_ref": BLOB_UUID}
    if binding == "sentinel":
        options = {"path": f"blob:{BLOB_UUID}"}
    elif binding == "nested":
        options = {
            "path": "elsewhere.csv",
            "nested": [{"prompt": {"blob_ref": BLOB_UUID, "mode": "inline_content", "sha256": content_hash(path.read_bytes())}}],
        }
    state = replace(
        _empty_state(), sources={"source": SourceSpec(plugin="csv", options=options, on_success="rows", on_validation_failure="discard")}
    )
    arguments = {"blob_id": uuid_spelling(BLOB_UUID, spelling)}
    if tool == "update_blob":
        arguments["content"] = "new"
    provenance = _verbatim_blob_context(engine, session_id, "new")
    with _blob_operation(engine, session_id) as (authority, operation):
        kwargs = dict(
            session_engine=engine,
            session_id=session_id,
            data_dir=str(root),
            session_operation_authority=authority,
            session_operation_context=operation,
            **provenance,
        )
        with engine.connect() as conn:
            before = conn.execute(select(blobs_table)).all()
        original_bytes = path.read_bytes()
        admitted = admit_blob_mutation(tool, arguments, state, _trained_tool_context(_catalog_with_blob_rows(), **kwargs))
        assert admitted is not None and not admitted.success
        assert "current composition" in admitted.validation.errors[0].message
        result = execute_tool(tool, arguments, state, _catalog_with_blob_rows(), **kwargs)
        assert not result.success
        assert "current composition" in result.validation.errors[0].message
        assert result.updated_state is state
        assert path.read_bytes() == original_bytes
        with engine.connect() as conn:
            assert conn.execute(select(blobs_table)).all() == before


@pytest.mark.parametrize("spelling", UUID_SPELLINGS)
@pytest.mark.parametrize("tool", ["update_blob", "delete_blob"])
def test_unbound_selector_alias_admits_and_mutates_with_canonical_echo(resource, spelling, tool):
    engine, session_id, path, root = resource
    arguments = {"blob_id": uuid_spelling(BLOB_UUID, spelling)}
    if tool == "update_blob":
        arguments["content"] = "new"
    with _blob_operation(engine, session_id) as (authority, operation):
        kwargs = dict(
            session_engine=engine,
            session_id=session_id,
            data_dir=str(root),
            session_operation_authority=authority,
            session_operation_context=operation,
            **_verbatim_blob_context(engine, session_id, "new"),
        )
        assert admit_blob_mutation(tool, arguments, _empty_state(), _trained_tool_context(_catalog_with_blob_rows(), **kwargs)) is None
        assert path.read_bytes() == b"name,amount\nalice,12\n"
        result = execute_tool(tool, arguments, _empty_state(), _catalog_with_blob_rows(), **kwargs)
    assert result.success, result.validation
    assert result.data["blob_id"] == BLOB_UUID
    if tool == "update_blob":
        assert path.read_bytes() == b"new"
    else:
        assert not path.exists()
        with engine.connect() as conn:
            assert conn.execute(select(blobs_table).where(blobs_table.c.id == BLOB_UUID)).first() is None


@pytest.mark.parametrize(
    "tool",
    [
        "get_blob_metadata",
        "inspect_source",
        "get_blob_content",
        "read_attachment",
        "set_source_from_blob",
        "set_source_from_blobs",
        "wire_blob_inline_ref",
    ],
)
@pytest.mark.parametrize("unavailable", ["missing", "foreign", "pending", "malformed", "whitespace"])
def test_selector_normalization_preserves_unavailability_and_argument_refusals(resource, tool, unavailable):
    engine, session_id, _path, root = resource
    selector = uuid_spelling(BLOB_UUID, "braced")
    with engine.begin() as conn:
        if unavailable == "missing":
            conn.execute(blobs_table.delete())
        elif unavailable == "foreign":
            foreign_session = str(uuid4())
            conn.execute(
                sessions_table.insert().values(
                    id=foreign_session,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="foreign",
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
            conn.execute(blobs_table.update().values(session_id=foreign_session))
        elif unavailable == "pending":
            conn.execute(blobs_table.update().values(status="pending"))
        elif unavailable == "malformed":
            selector = "malformed-uuid"
        else:
            selector = " " + BLOB_UUID + " "
    arguments = {"blob_id": selector}
    state = _empty_state()
    if tool == "set_source_from_blob":
        arguments.update(on_success="rows", on_validation_failure="discard", options={"schema": {"mode": "observed"}})
    elif tool == "set_source_from_blobs":
        arguments = {"blob_ids": [selector], "on_success": "rows", "on_validation_failure": "discard"}
    elif tool == "wire_blob_inline_ref":
        state = _inline_ref_state()
        arguments["field_path"] = "node:classify.options.prompt_template"
    with _blob_operation(engine, session_id) as (authority, operation):
        result = execute_tool(
            tool,
            arguments,
            state,
            _catalog_with_blob_rows(),
            session_engine=engine,
            session_id=session_id,
            data_dir=str(root),
            session_operation_authority=authority,
            session_operation_context=operation,
        )
    if tool == "get_blob_metadata" and unavailable == "pending":
        assert result.success and result.data["status"] == "pending"
    else:
        assert not result.success
        assert result.updated_state is state
        if tool == "get_blob_metadata":
            assert selector not in str(result.validation)
            if unavailable in {"missing", "foreign"}:
                assert result.validation.errors[0].message == "Blob not found for this session."


@pytest.mark.parametrize("spelling", ["braced", "hyphenless"])
def test_conservative_marker_custody_does_not_make_invalid_marker_executable(spelling):
    from elspeth.contracts.blobs_inline import BlobContentResolutionError
    from elspeth.core.blobs_inline import _discover_blob_content_refs
    from elspeth.web.sessions.proposal_blob_refs import proposal_blob_reference_ids

    marker = {"blob_ref": uuid_spelling(BLOB_UUID, spelling), "mode": "inline_content", "sha256": "a" * 64}
    arguments = {"source": {"options": {"prompt": marker}}}
    assert proposal_blob_reference_ids("set_pipeline", arguments) == (BLOB_UUID,)
    with pytest.raises(BlobContentResolutionError):
        _discover_blob_content_refs(arguments)
    valid = {"source": {"options": {"prompt": {**marker, "blob_ref": BLOB_UUID}}}}
    assert str(_discover_blob_content_refs(valid)[0].blob_id) == BLOB_UUID
