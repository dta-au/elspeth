"""Actual service/current-read provenance and original blob-read behavior."""

import asyncio
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from elspeth.contracts.blobs import BlobIntegrityError
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.composer.continuation_codec import encode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_evidence import decode_turn_evidence, encode_turn_evidence
from elspeth.web.composer.continuation_evidence_graph import _restore_current_evidence, capture_turn_evidence
from elspeth.web.composer.continuation_evidence_restore import restore_turn_evidence
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.transcript_blob_reader import CurrentTranscriptBlobReader
from elspeth.web.coordination.repository import SessionOperationFenceLost
from elspeth.web.sessions.continuation import ContinuationEvent, append_complete_snapshot, private_chunk_key, put_chunk
from elspeth.web.sessions.locking import locked_session_transaction
from tests.helpers.continuation_inventory import inventory_advisor, inventory_work
from tests.unit.web.composer.conftest import _make_settings, _mock_catalog
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_evidence_restore import stored_evidence

operation_store = test_composer_operation_authority.operation_store


def current_service(store, data_dir):
    return ComposerServiceImpl.for_trained_operator(
        catalog=_mock_catalog(), settings=_make_settings(data_dir), sessions_service=store[3], session_engine=store[0]
    )


@pytest.mark.asyncio
async def test_real_reader_preserves_bytes_missing_result_integrity_and_current_fence(operation_store, tmp_path):
    engine, repository, _authority, sessions, sid = operation_store
    context = repository.acquire(
        session_id=sid, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    service = current_service(operation_store, tmp_path)
    blobs = BlobServiceImpl(engine, tmp_path, session_operation_authority=sessions.session_operation_authority)
    blob = await blobs.create_blob(sid, "retained.txt", b"Exact retained cafe tail", "text/plain", session_operation_context=context)
    reader = CurrentTranscriptBlobReader.from_service(service, session_id=str(sid), context=context)
    value = await asyncio.to_thread(reader, str(blob.id))
    assert value is not None and value[1] == b"Exact retained cafe tail"
    assert value[0]["content_hash"] == blob.content_hash and value[0]["session_id"] == str(sid)
    assert await asyncio.to_thread(reader, str(uuid4())) is None
    Path(blob.storage_path).write_bytes(b"changed content")
    with pytest.raises(BlobIntegrityError):
        await asyncio.to_thread(reader, str(blob.id))
    Path(blob.storage_path).write_bytes(b"Exact retained cafe tail")
    assert (await asyncio.to_thread(reader, str(blob.id)))[1] == b"Exact retained cafe tail"
    Path(blob.storage_path).unlink()
    assert await asyncio.to_thread(reader, str(blob.id)) is None
    repository.release(context)
    with pytest.raises(SessionOperationFenceLost):
        await asyncio.to_thread(reader, str(blob.id))


@pytest.mark.asyncio
async def test_reader_keeps_original_primitive_exception_identity(operation_store, tmp_path, monkeypatch):
    import elspeth.web.composer.transcript_blob_reader as reader_module

    _engine, repository, _authority, _sessions, sid = operation_store
    context = repository.acquire(
        session_id=sid, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    reader = CurrentTranscriptBlobReader.from_service(current_service(operation_store, tmp_path), session_id=str(sid), context=context)
    assert await asyncio.to_thread(reader, str(uuid4())) is None
    original = AuditIntegrityError("original owned reader failure")

    def fail(*args, **kwargs):
        raise original

    monkeypatch.setattr(reader_module, "_locked_read_ready_blob", fail)
    with pytest.raises(AuditIntegrityError) as caught:
        await asyncio.to_thread(reader, str(uuid4()))
    assert caught.value is original
    repository.release(context)


async def stored_reader_cut(store, data_dir):
    engine, _repository, authority, sessions, sid = store
    running, selected, _inventory, prior = await stored_evidence(store)
    service = current_service(store, data_dir)
    graph = _restore_current_evidence(prior, context=running.session_operation_context)
    reader = CurrentTranscriptBlobReader.from_service(service, session_id=str(sid), context=running.session_operation_context)
    graph.evidence.transcript.use_blob_reader(reader)
    retained = capture_turn_evidence(
        graph.evidence,
        graph.messages,
        logical=selected.stack.logical,
        recorder=graph.recorder,
        drained=graph.drained,
        env_ref_names=frozenset(),
    )
    assert retained.requires_blob_reader
    chunk = encode_turn_evidence(retained)
    frame = selected.stack.frames[0].model_copy(update={"evidence": chunk.reference})
    root = selected.stack.model_copy(update={"frames": (frame,)})
    with locked_session_transaction(engine, str(sid)) as conn:
        put_chunk(
            conn,
            session_id=str(sid),
            operation_id=running.claim.operation_id,
            key=private_chunk_key(chunk),
            payload=chunk.canonical.encode("utf-8"),
            codec=chunk.reference.codec,
        )
        stored = append_complete_snapshot(
            conn,
            session_id=str(sid),
            operation_id=running.claim.operation_id,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.stack.cursor,
            snapshot_json=encode_continuation_snapshot(root),
            event_batch=(ContinuationEvent("checkpoint", '{"test_scope":"current transcript reader"}'),),
            handoff=False,
        )
    work = inventory_work(running)
    selected = await load_selected_continuation(authority=authority, running=running, selected=stored, required_work=work)
    inventory = await materialize_continuation_env_inventory(
        advisor=inventory_advisor(sessions), authority=authority, selected=selected, required_work=work
    )
    work.coordinator.assert_completed()
    return service, reader, running, selected, inventory, retained


@pytest.mark.asyncio
async def test_actual_current_restore_rebuilds_required_reader_without_serializing_capability(operation_store, tmp_path):
    service, original, running, selected, inventory, retained = await stored_reader_cut(operation_store, tmp_path)
    chunk = encode_turn_evidence(retained)
    assert decode_turn_evidence(chunk, env_ref_names=inventory.names) == retained
    assert not {"service", "engine", "data_dir", "context", "lease_token"}.intersection(retained.model_dump())
    work = inventory_work(running)
    restored = await restore_turn_evidence(
        authority=operation_store[2], selected=selected, inventory=inventory, required_work=work, service=service
    )
    actual = restored.evidence.transcript._blob_reader
    assert type(actual) is CurrentTranscriptBlobReader and actual is not original
    assert actual.service is service and actual.context is running.session_operation_context
    assert actual.engine is operation_store[0] and actual.data_dir == str(tmp_path)
    assert await asyncio.to_thread(actual, str(uuid4())) is None
    assert (
        capture_turn_evidence(
            restored.evidence,
            restored.messages,
            recorder=restored.recorder,
            drained=restored.drained,
            logical=selected.stack.logical,
            env_ref_names=inventory.names,
        )
        == retained
    )
    work.coordinator.assert_completed()
    refusal = inventory_work(running)
    with pytest.raises(AuditIntegrityError, match="actual current reader service"):
        await restore_turn_evidence(authority=operation_store[2], selected=selected, inventory=inventory, required_work=refusal)
    refusal.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_required_reader_cannot_be_dropped_by_internal_graph_constructor(operation_store, tmp_path):
    service, original, running, _selected, _inventory, retained = await stored_reader_cut(operation_store, tmp_path)
    _restore_current_evidence(retained, context=running.session_operation_context, reader=original)
    with pytest.raises(AuditIntegrityError, match="required fresh current reader"):
        _restore_current_evidence(retained, context=running.session_operation_context)
    changed = replace(original, context=replace(original.context, fence=replace(original.context.fence, operation_epoch=99)))
    with pytest.raises(AuditIntegrityError, match="another evidence physical owner"):
        changed.require_for_evidence(
            _restore_current_evidence(retained, context=running.session_operation_context, reader=original).evidence
        )
    assert service is original.service


@pytest.mark.asyncio
async def test_selected_reader_service_must_share_actual_engine_before_sql_submission(operation_store, tmp_path):
    service, _original, running, selected, inventory, _retained = await stored_reader_cut(operation_store, tmp_path)
    await restore_turn_evidence(
        authority=operation_store[2], selected=selected, inventory=inventory, required_work=inventory_work(running), service=service
    )
    from elspeth.web.sessions.engine import create_session_engine

    other = create_session_engine(f"sqlite:///{tmp_path / 'unselected.db'}")
    original_engine = service._session_engine
    try:
        service._session_engine = other
        work = inventory_work(running)
        with pytest.raises(AuditIntegrityError, match="same actual store"):
            await restore_turn_evidence(
                authority=operation_store[2], selected=selected, inventory=inventory, required_work=work, service=service
            )
        assert work.coordinator.tickets == ()
    finally:
        service._session_engine = original_engine
        other.dispose()


@pytest.mark.asyncio
async def test_reader_constructor_refuses_a_foreign_session_after_real_positive(operation_store, tmp_path):
    _engine, repository, _authority, _sessions, sid = operation_store
    context = repository.acquire(
        session_id=sid, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    reader = CurrentTranscriptBlobReader.from_service(current_service(operation_store, tmp_path), session_id=str(sid), context=context)
    assert await asyncio.to_thread(reader, str(uuid4())) is None
    with pytest.raises(AuditIntegrityError, match="actual service/store/current context"):
        replace(reader, session_id=str(uuid4()))
    repository.release(context)


@pytest.mark.asyncio
async def test_required_reader_bit_cannot_default_when_omitted(operation_store, tmp_path):
    from elspeth.contracts.hashing import canonical_json
    from elspeth.web.composer.continuation_codec import encode_private_chunk

    _service, _reader, _running, _selected, inventory, retained = await stored_reader_cut(operation_store, tmp_path)
    assert decode_turn_evidence(encode_turn_evidence(retained), env_ref_names=inventory.names) == retained
    payload = retained.model_dump(mode="json")
    del payload["requires_blob_reader"]
    chunk = encode_private_chunk(codec="composer.evidence.v1", canonical=canonical_json(payload))
    with pytest.raises(AuditIntegrityError):
        decode_turn_evidence(chunk, env_ref_names=inventory.names)
