"""Actual current attachment source maxima and cold DATA without file rereads."""

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_attachment_read import read_current_attachment
from elspeth.web.composer.continuation_codec import encode_private_chunk
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v3_from_settings, continuation_policy_v4_from_settings
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions.continuation_attachment_v4_bound import (
    V4AttachmentReadBound,
    V4AttachmentReadRefusal,
    prepare_v4_attachment_read_bound_on_connection,
)
from elspeth.web.sessions.continuation_attachment_v4_source import read_v4_attachment_retained_value_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import blobs_table
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions import test_continuation_cold_restore as cold_restore
from tests.unit.web.sessions.test_continuation_attachment_read import read_case

operation_store = operations.operation_store


async def _case(operation_store, monkeypatch, raw, *, legacy=False):
    settings = continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576)
    policy = continuation_policy_v3_from_settings(settings) if legacy else continuation_policy_v4_from_settings(settings)
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(cold_restore, "minimal_continuation_policy", lambda: policy)
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    kwargs, path, actors = await read_case(store, raw)
    return store, kwargs, path, actors


def _bound(store, kwargs, *, tool_name=None, arguments_json=None):
    _snapshot, catalog = kwargs["service"]._policy_context.build("alice")
    with locked_session_transaction(store[0], str(store[4])) as conn:
        return prepare_v4_attachment_read_bound_on_connection(
            conn,
            running=kwargs["inventory"].running,
            selected=kwargs["selected"],
            inventory=kwargs["inventory"],
            tool_name=kwargs["tool_name"] if tool_name is None else tool_name,
            arguments_json=canonical_json(kwargs["arguments"]) if arguments_json is None else arguments_json,
            pre_state=kwargs["state"],
            catalog=catalog,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", (False, True), ids=("v4", "v3"))
@pytest.mark.parametrize("tool", ("get_blob_content", "read_attachment"))
async def test_original_selector_presence_preserves_v3_content_and_v4_bound_bytes(operation_store, monkeypatch, legacy, tool):
    raw = 'Original "quoted" content and é🙂'.encode()
    store, kwargs, _path, _actors = await _case(operation_store, monkeypatch, raw, legacy=legacy)
    kwargs["tool_name"] = tool
    blob_id = kwargs["arguments"]["blob_id"]
    variants = [{"blob_id": blob_id}, {"blob_id": blob_id, "expected_content_hash": None}]
    if tool == "get_blob_content":
        variants.extend(
            (
                {"blob_id": blob_id, "offset_chars": 0},
                {"blob_id": blob_id, "offset_chars": 0, "expected_content_hash": None},
            )
        )
    observed_arguments = set()
    for arguments in variants:
        kwargs["arguments"] = arguments
        expected = canonical_json(arguments)
        if not legacy:
            bound = _bound(store, kwargs)
            assert type(bound) is V4AttachmentReadBound
            assert bound.arguments_json == expected
        actual = await read_current_attachment(**kwargs)
        assert actual.result.success and actual.result.updated_state is kwargs["state"]
        assert actual.result.data is not None and actual.result.data["content"] == raw.decode()
        assert actual.retained.arguments_json == expected
        assert actual.retained.blob_versions[0].content_hash == sha256(raw).hexdigest()
        observed_arguments.add(actual.retained.arguments_json)
    assert len(observed_arguments) == len(variants)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ("get_blob_content", "read_attachment"))
async def test_owned_manifest_preserves_original_complete_source_hash(operation_store, monkeypatch, tool):
    store, kwargs, _path, _actors = await _case(operation_store, monkeypatch, b"original source")
    kwargs["tool_name"] = tool
    bound = _bound(store, kwargs)
    assert type(bound) is V4AttachmentReadBound
    # This is the complete pre-refactor hash projection. The new owned model
    # must preserve its source UUID, ordered names and every original field.
    original_manifest = {
        "session_id": bound.session_id,
        "operation_id": bound.operation_id,
        "actor_user_id": bound.actor_user_id,
        "session_operation_id": bound.session_operation_id,
        "session_operation_epoch": bound.session_operation_epoch,
        "lease_sha256": bound.lease_sha256,
        "creator_sequence": bound.creator_sequence,
        "creator_generation": bound.creator_generation,
        "claim_attempt": bound.claim_attempt,
        "selected_checkpoint_id": bound.selected_checkpoint_id,
        "selected_cursor": bound.selected_cursor,
        "selected_snapshot_sha256": bound.selected_snapshot_sha256,
        "tool_name": bound.tool_name,
        "arguments_json": bound.arguments_json,
        "state_json": bound.state_json,
        "source": bound.source.model_dump(mode="json"),
        "names": sorted(bound.names),
        "catalog_snapshot_sha256": bound.catalog.snapshot.snapshot_hash,
        "inspection_fingerprint_bytes": 64,
        "maximum_response_bytes": bound.maximum_response_bytes,
        "maximum_tool_result_bytes": bound.maximum_tool_result_bytes,
        "maximum_owner_result_bytes": bound.maximum_owner_result_bytes,
    }
    assert bound.contract_sha256 == sha256(canonical_json(original_manifest).encode()).hexdigest()
    changed = {**original_manifest, "arguments_json": canonical_json({"blob_id": str(bound.source.blob_id), "expected_content_hash": None})}
    assert bound.contract_sha256 != sha256(canonical_json(changed).encode()).hexdigest()


@pytest.mark.asyncio
async def test_noncanonical_duplicate_and_invalid_selectors_refuse_before_file_access(operation_store, monkeypatch):
    store, kwargs, path, _actors = await _case(operation_store, monkeypatch, b"original source")
    blob_id = kwargs["arguments"]["blob_id"]
    canonical = canonical_json({"blob_id": blob_id})
    malformed = (
        f'{{ "blob_id": "{blob_id}" }}',
        f'{{"blob_id":"{blob_id}","blob_id":"{blob_id}"}}',
        canonical_json({"blob_id": blob_id, "extra": "not a selector"}),
        canonical_json({"blob_id": blob_id, "expected_content_hash": 0}),
        canonical_json({"blob_id": blob_id, "offset_chars": 0}),
    )
    original_open = Path.open

    def forbid_original_open(candidate, *args, **options):
        if candidate == path:
            raise AssertionError("preissue source file read")
        return original_open(candidate, *args, **options)

    with monkeypatch.context() as guard:
        guard.setattr(Path, "open", forbid_original_open)
        with pytest.raises(AssertionError, match="preissue source file read"):
            path.read_bytes()
        assert type(_bound(store, kwargs, arguments_json=canonical)) is V4AttachmentReadBound
        for encoded in malformed:
            assert _bound(store, kwargs, arguments_json=encoded) == V4AttachmentReadRefusal("read_attachment", "invalid_arguments")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ("read_attachment", "get_blob_content"))
@pytest.mark.parametrize("raw", (b"original prose", b'\\"\x00\n' * 100, "é🙂".encode() * 100))
async def test_bound_covers_original_result_and_cold_never_reads_the_file(operation_store, monkeypatch, tool, raw):
    store, kwargs, path, _actors = await _case(operation_store, monkeypatch, raw)
    kwargs["tool_name"] = tool
    original_open = Path.open

    def forbid_original_open(candidate, *args, **options):
        if candidate == path:
            raise AssertionError("preissue source file read")
        return original_open(candidate, *args, **options)

    with monkeypatch.context() as guard:
        guard.setattr(Path, "open", forbid_original_open)
        with pytest.raises(AssertionError, match="preissue source file read"):
            path.read_bytes()
        plan = _bound(store, kwargs)
    assert type(plan) is V4AttachmentReadBound
    actual = await read_current_attachment(**kwargs)
    source = encode_private_chunk(
        codec="composer.owner-result.v1", canonical=canonical_json(actual.retained.model_dump(mode="json")), env_ref_names=plan.names
    )
    assert len(source.canonical.encode("utf-8")) <= plan.maximum_owner_result_bytes
    assert len(actual.retained.response_json.encode("utf-8")) <= plan.maximum_response_bytes
    assert len(actual.retained.tool_result_json.encode("utf-8")) <= plan.maximum_tool_result_bytes
    # Cold source proof uses the retained original and SQL version. Removing
    # the file after the actual read must not cause a physical reread.
    path.unlink()
    with store[0].connect() as conn:
        restored = read_v4_attachment_retained_value_on_connection(conn, bound=plan, source_chunk=source, pre_state=kwargs["state"])
    assert canonical_json(restored.to_dict()) == canonical_json(actual.result.to_dict())
    assert restored.updated_state is kwargs["state"]
    with store[0].connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(update(blobs_table).where(blobs_table.c.id == str(plan.source.blob_id)).values(content_hash="f" * 64))
            with pytest.raises(AuditIntegrityError, match="current immutable version"):
                read_v4_attachment_retained_value_on_connection(conn, bound=plan, source_chunk=source, pre_state=kwargs["state"])
        finally:
            transaction.rollback()
    with store[0].connect() as conn, pytest.raises(AuditIntegrityError, match="original complete result maxima"):
        read_v4_attachment_retained_value_on_connection(
            conn,
            bound=replace(plan, maximum_owner_result_bytes=plan.maximum_owner_result_bytes + 1),
            source_chunk=source,
            pre_state=kwargs["state"],
        )
    with store[0].connect() as conn, pytest.raises(AuditIntegrityError, match="original source contract"):
        read_v4_attachment_retained_value_on_connection(
            conn, bound=replace(plan, contract_sha256="f" * 64), source_chunk=source, pre_state=kwargs["state"]
        )
    with store[0].connect() as conn, pytest.raises(AuditIntegrityError, match="original source contract"):
        read_v4_attachment_retained_value_on_connection(
            conn, bound=replace(plan, actor_user_id="different-actor"), source_chunk=source, pre_state=kwargs["state"]
        )


@pytest.mark.asyncio
async def test_unproved_source_and_oversized_owner_tail_refuse_without_reading(operation_store, monkeypatch):
    store, kwargs, path, _actors = await _case(operation_store, monkeypatch, b"x" * 100_000)
    source_refused = _bound(store, kwargs, tool_name="inspect_source")
    assert source_refused == V4AttachmentReadRefusal("inspect_source", "unsupported_producer_bound")
    whole_refused = _bound(store, kwargs)
    assert whole_refused == V4AttachmentReadRefusal("read_attachment", "private_document_bound")
    assert path.exists()
    kwargs["tool_name"] = "get_blob_content"
    page = _bound(store, kwargs)
    assert type(page) is V4AttachmentReadBound


@pytest.mark.asyncio
async def test_inspect_source_observes_actual_verified_version_and_refuses_changed_source(operation_store, monkeypatch):
    store, kwargs, _path, _actors = await _case(operation_store, monkeypatch, b"name,age\nAlice,32\n")
    kwargs["tool_name"] = "inspect_source"
    actual = await read_current_attachment(**kwargs)
    assert actual.result.success and actual.retained.attachment_inspections == ()
    assert len(actual.retained.blob_versions) == 1
    original = actual.retained.blob_versions[0]
    from elspeth.web.sessions.continuation_owner_results import require_read_versions_on_connection

    with store[0].connect() as conn:
        require_read_versions_on_connection(conn, session_id=str(store[4]), result=actual.retained)
        transaction = conn.get_transaction()
        assert transaction is not None
        try:
            conn.execute(update(blobs_table).where(blobs_table.c.id == str(original.blob_id)).values(content_hash="f" * 64))
            with pytest.raises(AuditIntegrityError, match="current immutable version"):
                require_read_versions_on_connection(conn, session_id=str(store[4]), result=actual.retained)
        finally:
            transaction.rollback()
