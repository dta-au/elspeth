"""Real attachment read/result/history transactions, including fresh-claim custody.

The fixture installs the actual prepared DATA input boundary. Unconsumed Compose
fields are explicit opaque bytes; no initializer/provider/runtime restore claimed.
"""

import asyncio
from dataclasses import replace
from hashlib import sha256

import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.continuation import ComposeFrame, ToolBatchFrame
from elspeth.web.composer.continuation_attachment_history import (
    RetainedAttachmentInspection,
    empty_attachment_history,
    encode_attachment_history,
)
from elspeth.web.composer.continuation_attachment_result import apply_attachment_history, read_attachment_result, run_attachment_result
from elspeth.web.composer.continuation_attachment_result_data import decode_attachment_history_batch
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors
from elspeth.web.composer.continuation_codec import encode_continuation_response, encode_continuation_snapshot, encode_private_chunk
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_evidence import encode_turn_evidence
from elspeth.web.composer.continuation_evidence_graph import capture_turn_evidence
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.tools import blobs
from elspeth.web.composer.turn_effects import TurnEvidence
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    ContinuationCASConflict,
    ContinuationEvent,
    append_complete_snapshot,
    private_chunk_key,
    put_chunk,
)
from elspeth.web.sessions.continuation_attachment_result import _retained_history
from elspeth.web.sessions.continuation_owner_results import ContinuationReadOwnerResult
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import blobs_table
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from tests.helpers.continuation_inventory import inventory_work
from tests.unit.web.composer.test_continuation_message_family_builders import completion_with_native
from tests.unit.web.composer.test_continuation_prepared_batch_data import capture, prepared_cut
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.coordination.test_continuation_p4_recovery_start import current_selected, new_owners, recovery_rows
from tests.unit.web.sessions.test_continuation_attachment_read import read_case

operation_store = test_composer_operation_authority.operation_store


async def result_case(store, monkeypatch, *, raw=b"Complete customer prose", tool="read_attachment", wrong_hash=False):
    reader, path, actors = await read_case(store, raw)
    engine, _repository, authority, _sessions, sid = store
    running = reader["inventory"].running
    arguments = dict(reader["arguments"])
    if tool == "get_blob_content":
        arguments.update(offset_chars=1, expected_content_hash="f" * 64 if wrong_hash else sha256(raw).hexdigest())
    with monkeypatch.context() as clock:
        batch, acc, calls, dialect = prepared_cut(clock, tool=tool, arguments=arguments)
        batch.current_call.audit = replace(batch.current_call.audit, actor="composer-web:user-alice")
        batch.current_call.original_creator_id = running.session_operation_context.fence.operation_id
        batch.current_call.original_creator_epoch = running.session_operation_context.fence.operation_epoch
        prepared_json = capture((batch, acc, calls, dialect))
    prepared = encode_private_chunk(codec="composer.tool-batch.v1", canonical=prepared_json)
    response = encode_continuation_response(completion_with_native(calls=calls))
    state = encode_private_chunk(codec="composer.state.v1", canonical=canonical_json(batch.state.to_dict()))
    root = reader["selected"].stack
    history = encode_attachment_history(empty_attachment_history(session_id=str(sid), operation_id=root.logical.operation_id))
    # The actual selected_case request/actor and prepared state bind these
    # closed DATA sources; this does not claim initialized runtime dispatch.
    request = "Preserve café and exact context"
    evidence_owner = TurnEvidence(
        session_id=str(sid),
        session_operation_context=running.session_operation_context,
        request_id=root.logical.original_user_message_id,
        request=request,
        initial_state_id=root.logical.base_state_id,
        initial_state=batch.state,
        actor_user_id="alice",
    )
    messages = [{"role": "user", "content": request}]
    evidence_owner.transcript.begin_messages(messages)
    admitted = capture_turn_evidence(
        evidence_owner,
        messages,
        logical=root.logical,
        recorder=BufferingRecorder(),
        drained=RecorderDrainCursors(),
        env_ref_names=reader["inventory"].names,
    )
    evidence = encode_turn_evidence(admitted, env_ref_names=reader["inventory"].names)
    opaque = encode_private_chunk(codec="composer.messages.v1", canonical='{"unconsumed_test_input":"outside_attachment_owner"}')
    compose = ComposeFrame(
        phase="response_retained",
        bootstrap=opaque.reference,
        iteration=opaque.reference,
        messages=opaque.reference,
        tools=None,
        initial_state=state.reference,
        state=state.reference,
        evidence=evidence.reference,
        audit_buffer=admitted.recorder_chunk.reference,
        transcript=opaque.reference,
        caches=opaque.reference,
        attachment_admissions=history.reference,
        initial_version=batch.state.version,
        current_state_id=None,
        slice_sequence=running.claim.segment_sequence,
        composition_slice=0,
        discovery_slice=0,
        composition_total=0,
        discovery_total=0,
        advisor_calls=0,
        repair_turns=0,
        advisor_checkpoint_passes=0,
        mutation_success_seen=False,
        persisted_assistant_message_id=None,
        persisted_assistant_content=None,
        persisted_tool_call_turn=False,
        advisor_repair_context_introduced=False,
        pending_advisor_elision_indices=(),
        pending_response=response.reference,
    )
    frame = ToolBatchFrame(
        phase="tool_ready",
        response=response.reference,
        accumulator=prepared.reference,
        next_tool_index=batch.next_tool_index,
        settled_effect_ids=(),
    )
    changed = root.model_copy(update={"frames": (compose, frame)})

    def install():
        with locked_session_transaction(engine, str(sid)) as conn:
            for chunk in (prepared, response, state, history, opaque, evidence, *admitted.dependency_chunks):
                put_chunk(
                    conn,
                    session_id=str(sid),
                    operation_id=running.claim.operation_id,
                    key=private_chunk_key(chunk),
                    payload=chunk.canonical.encode("utf-8"),
                    codec=chunk.reference.codec,
                )
            return append_complete_snapshot(
                conn,
                session_id=str(sid),
                operation_id=running.claim.operation_id,
                expected_checkpoint_id=reader["selected"].checkpoint_id,
                expected_cursor=root.cursor,
                snapshot_json=encode_continuation_snapshot(changed),
                event_batch=(ContinuationEvent("checkpoint", '{"actual_prepared_attachment_input":true}'),),
                handoff=False,
            )

    stored = await asyncio.to_thread(install)
    work = reader["required_work"]
    selected = await load_selected_continuation(authority=authority, running=running, selected=stored, required_work=work)
    inventory = await materialize_continuation_env_inventory(
        advisor=reader["service"]._advisor_checkpoint, authority=authority, selected=selected, required_work=work
    )
    return (
        {"service": reader["service"], "authority": authority, "selected": selected, "inventory": inventory, "required_work": work},
        path,
        actors,
        prepared_json,
    )


async def refreshed(kwargs):
    authority, work = kwargs["authority"], kwargs["required_work"]
    stored = await asyncio.to_thread(authority.load_continuation_snapshot, work.running)
    selected = await load_selected_continuation(authority=authority, running=work.running, selected=stored, required_work=work)
    inventory = await materialize_continuation_env_inventory(
        advisor=kwargs["service"]._advisor_checkpoint, authority=authority, selected=selected, required_work=work
    )
    return {**kwargs, "selected": selected, "inventory": inventory}


def without_service(kwargs):
    return {key: value for key, value in kwargs.items() if key != "service"}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ("read_attachment", "get_blob_content"))
async def test_real_read_publishes_full_result_then_atomic_history_without_cursor_advance(operation_store, monkeypatch, tool):
    kwargs, _path, actors, original = await result_case(operation_store, monkeypatch, tool=tool)
    inspected = []
    actual_inspect = blobs.inspect_attachment_original

    def observe(**kw):
        value = actual_inspect(**kw)
        inspected.append(value)
        return value

    monkeypatch.setattr(blobs, "inspect_attachment_original", observe)
    await run_attachment_result(**kwargs)
    kwargs = await refreshed(kwargs)
    pending = await read_attachment_result(**without_service(kwargs))
    assert pending.result.success and not pending.history_applied and pending.history.reads == ()
    call = pending.prepared.batch.current_call
    assert pending.result.updated_state is call.pre_state is pending.prepared.batch.state is pending.prepared.accumulator.state
    assert pending.prepared.batch.tool_outcomes[0].response is pending.prepared.batch.tool_outcomes[1].response
    assert pending.prepared.accumulator.authority_revocation is not None
    assert pending.prepared.batch.advisor_calls_used == 4 and pending.prepared.accumulator.advisor_calls_used == 2
    assert len(inspected) == 1 and actors == ["alice"]
    before_index = kwargs["selected"].stack.frames[-1].next_tool_index
    before_frontier = kwargs["selected"].stack.effect_frontier
    await apply_attachment_history(**without_service(kwargs))
    kwargs = await refreshed(kwargs)
    applied = await read_attachment_result(**without_service(kwargs))
    assert applied.history_applied and len(applied.history.reads) == 1
    assert applied.history.reads[0].inspections == pending.retained.attachment_inspections
    assert applied.retained == pending.retained and len(inspected) == 1
    frame = kwargs["selected"].stack.frames[-1]
    assert frame.next_tool_index == before_index == 2 and frame.phase == "result_retained"
    assert not kwargs["selected"].stack.pending_owner_results and kwargs["selected"].stack.effect_frontier == before_frontier
    selected_batch = next(chunk for chunk in kwargs["selected"].chunks if chunk.reference == frame.accumulator)
    assert decode_attachment_history_batch(selected_batch, env_ref_names=kwargs["inventory"].names).prepared_json == original
    before = recovery_rows(operation_store[0])
    assert await apply_attachment_history(**without_service(kwargs)) == applied.snapshot
    assert recovery_rows(operation_store[0]) == before
    # Historical inspections never authorize a new read: the actual reader runs
    # its current locked original/policy scan again even on the same object.
    from elspeth.web.composer.continuation_attachment_read import read_current_attachment

    await read_current_attachment(
        service=kwargs["service"],
        authority=kwargs["authority"],
        selected=kwargs["selected"],
        inventory=kwargs["inventory"],
        required_work=kwargs["required_work"],
        tool_name=call.tool_name,
        arguments=call.arguments,
        state=call.pre_state,
    )
    assert len(inspected) == 2
    kwargs["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ("credential", "utf8", "post_inspection"))
async def test_failed_real_reads_retain_only_actual_inspections(operation_store, monkeypatch, failure):
    raw = b'{"api_key":"opaque-private-value"}' if failure == "credential" else b"\xff" if failure == "utf8" else b"safe text"
    kwargs, _path, _actors, _original = await result_case(
        operation_store,
        monkeypatch,
        raw=raw,
        tool="get_blob_content" if failure == "post_inspection" else "read_attachment",
        wrong_hash=failure == "post_inspection",
    )
    await run_attachment_result(**kwargs)
    kwargs = await refreshed(kwargs)
    value = await read_attachment_result(**without_service(kwargs))
    assert value.result.success is False
    assert bool(value.retained.attachment_inspections) is (failure == "post_inspection")
    await apply_attachment_history(**without_service(kwargs))
    kwargs = await refreshed(kwargs)
    after = await read_attachment_result(**without_service(kwargs))
    assert bool(after.history.reads) is (failure == "post_inspection")
    assert after.retained == value.retained and after.result.success is False
    kwargs["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
async def test_current_blob_version_change_refuses_before_history_root_write(operation_store, monkeypatch):
    kwargs, _path, _actors, _original = await result_case(operation_store, monkeypatch)
    await run_attachment_result(**kwargs)
    kwargs = await refreshed(kwargs)
    value = await read_attachment_result(**without_service(kwargs))
    assert value.result.success
    with operation_store[0].begin() as conn:
        changed = conn.execute(
            update(blobs_table).where(blobs_table.c.id == str(value.retained.blob_versions[0].blob_id)).values(filename="changed.txt")
        )
        assert changed.rowcount == 1
        assert (
            conn.execute(
                select(blobs_table.c.filename).where(blobs_table.c.id == str(value.retained.blob_versions[0].blob_id))
            ).scalar_one()
            == "changed.txt"
        )
    before = recovery_rows(operation_store[0])
    with pytest.raises(AuditIntegrityError):
        await apply_attachment_history(**without_service(kwargs))
    assert recovery_rows(operation_store[0]) == before
    kwargs["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
async def test_same_checkpoint_changed_root_and_stale_cas_refuse_after_positive(operation_store, monkeypatch):
    kwargs, _path, _actors, _original = await result_case(operation_store, monkeypatch)
    await run_attachment_result(**kwargs)
    kwargs = await refreshed(kwargs)
    assert (await read_attachment_result(**without_service(kwargs))).result.success
    selected = kwargs["selected"]
    changed = replace(
        selected, stack=selected.stack.model_copy(update={"logical": selected.stack.logical.model_copy(update={"request_hash": "e" * 64})})
    )
    before = recovery_rows(operation_store[0])
    with pytest.raises(AuditIntegrityError, match="complete selected root"):
        await read_attachment_result(**without_service({**kwargs, "selected": changed}))
    assert recovery_rows(operation_store[0]) == before
    await apply_attachment_history(**without_service(kwargs))
    after = recovery_rows(operation_store[0])
    with pytest.raises(ContinuationCASConflict):
        await apply_attachment_history(**without_service(kwargs))
    assert recovery_rows(operation_store[0]) == after
    kwargs["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("applied", (False, True))
async def test_released_read_result_restarts_with_new_claim_and_keeps_original_creator(operation_store, monkeypatch, applied):
    kwargs, _path, _actors, _original = await result_case(operation_store, monkeypatch)
    await run_attachment_result(**kwargs)
    kwargs = await refreshed(kwargs)
    first = kwargs["required_work"].running
    if applied:
        await apply_attachment_history(**without_service(kwargs))
    kwargs["required_work"].coordinator.assert_completed()
    operation_store[1].release(first.session_operation_context)
    current = new_owners(operation_store)
    _engine, repository, authority, _sessions, sid = current
    assert (
        repository.recover_composer_segment(
            session_id=sid,
            operation_id=first.claim.operation_id,
            expected_generation=first.claim.ownership_generation,
            actor_user_id="alice",
            auth_provider_type="local",
        )
        is True
    )
    (claim,) = authority.claim_segment(limit=1)
    context = repository.start_composer_segment(
        claim, owner_instance_id=authority._owner_instance_id, lease_seconds=30, auth_provider_type="local"
    )
    running = ComposerOperationRunning(claim=claim, session_operation_context=context)
    try:
        assert running.claim.ownership_generation == first.claim.ownership_generation + 1
        assert context.fence.operation_id != first.session_operation_context.fence.operation_id
        selected, inventory = await current_selected(current, running)
        work = inventory_work(running)
        arguments = {"authority": authority, "selected": selected, "inventory": inventory, "required_work": work}
        result = await read_attachment_result(**arguments)
        assert result.history_applied is applied and result.result.success
        assert result.prepared.batch.current_call.original_creator_id == first.session_operation_context.fence.operation_id
        with operation_store[0].connect() as conn:
            effect = conn.execute(select(effects).where(effects.c.effect_key == result.effect_key)).one()
        assert effect.creator_generation == first.claim.ownership_generation
        assert effect.creator_sequence == first.claim.segment_sequence

        def never_inspect(**kw):
            raise AssertionError("immutable retained result must not redispatch filesystem work")

        monkeypatch.setattr(blobs, "inspect_attachment_original", never_inspect)
        await apply_attachment_history(**arguments)
        work.coordinator.assert_completed()
    finally:
        repository.release(context)


@pytest.mark.asyncio
async def test_sql_history_application_rollback_preserves_pending_and_all_chunks(operation_store, monkeypatch):
    from elspeth.web.sessions.continuation_attachment_result import apply_attachment_history_on_connection

    kwargs, _path, _actors, _original = await result_case(operation_store, monkeypatch)
    await run_attachment_result(**kwargs)
    kwargs = await refreshed(kwargs)
    assert (await read_attachment_result(**without_service(kwargs))).result.success
    before = recovery_rows(operation_store[0])
    rollback = RuntimeError("controlled actual transaction rollback")

    def rollback_after_complete_write():
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            following = apply_attachment_history_on_connection(
                conn, kwargs["required_work"].running, selected=kwargs["selected"], inventory=kwargs["inventory"]
            )
            assert following.cursor == kwargs["selected"].stack.cursor + 1
            raise rollback

    with pytest.raises(RuntimeError) as caught:
        await asyncio.to_thread(rollback_after_complete_write)
    assert caught.value is rollback and recovery_rows(operation_store[0]) == before
    await apply_attachment_history(**without_service(kwargs))
    assert recovery_rows(operation_store[0]) != before
    kwargs["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ("result_bytes", "creator"))
async def test_retained_source_corruption_refuses_after_positive(operation_store, monkeypatch, channel):
    from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks

    kwargs, _path, _actors, _original = await result_case(operation_store, monkeypatch)
    await run_attachment_result(**kwargs)
    kwargs = await refreshed(kwargs)
    value = await read_attachment_result(**without_service(kwargs))
    assert value.result.success
    with operation_store[0].begin() as conn:
        effect = conn.execute(select(effects).where(effects.c.effect_key == value.effect_key)).one()
        if channel == "result_bytes":
            raw = b"\xff"
            conn.execute(
                update(chunks)
                .where(chunks.c.chunk_key == effect.result_chunk_key)
                .values(payload=raw, sha256=sha256(raw).hexdigest(), byte_length=1)
            )
        else:
            from elspeth.web.composer.continuation_attachment_result_data import (
                decode_attachment_published_result,
                encode_attachment_published_result,
            )
            from elspeth.web.sessions.continuation import _effect_snapshot

            selected = kwargs["selected"]
            pending = selected.stack.pending_owner_results[0]
            chunk = next(item for item in selected.chunks if item.reference == pending.result)
            published = decode_attachment_published_result(chunk, env_ref_names=kwargs["inventory"].names)
            changed = encode_attachment_published_result(
                published.model_copy(update={"creator_operation_epoch": published.creator_operation_epoch + 1}),
                env_ref_names=kwargs["inventory"].names,
            )
            put_chunk(
                conn,
                session_id=str(operation_store[4]),
                operation_id=kwargs["required_work"].running.claim.operation_id,
                key=private_chunk_key(changed),
                payload=changed.canonical.encode("utf-8"),
                codec=changed.reference.codec,
            )
            receipt = f"attachment-result:{changed.reference.digest}"
            conn.execute(
                update(effects)
                .where(effects.c.effect_key == value.effect_key)
                .values(result_chunk_key=private_chunk_key(changed), receipt_key=receipt)
            )
            root = selected.stack.model_copy(update={"pending_owner_results": (pending.model_copy(update={"result": changed.reference}),)})
            encoded = _effect_snapshot(
                encode_continuation_snapshot(root), effect_key=value.effect_key, disposition="committed", receipt_id=receipt
            )
            append_complete_snapshot(
                conn,
                session_id=str(operation_store[4]),
                operation_id=kwargs["required_work"].running.claim.operation_id,
                expected_checkpoint_id=selected.checkpoint_id,
                expected_cursor=selected.stack.cursor,
                snapshot_json=encoded,
                event_batch=(ContinuationEvent("checkpoint", '{"coherent_creator_corruption":true}'),),
                handoff=False,
            )
    if channel == "creator":
        kwargs = await refreshed(kwargs)
    before = recovery_rows(operation_store[0])
    with pytest.raises(AuditIntegrityError):
        await read_attachment_result(**without_service(kwargs))
    assert recovery_rows(operation_store[0]) == before
    kwargs["required_work"].coordinator.assert_completed()


def test_retained_history_refuses_a_read_from_another_session():
    """Before extend_attachment_history this returned a history holding another
    session's read. Control: build with model_copy -> red."""
    foreign = RetainedAttachmentInspection(
        session_id="other",
        blob_id="blob",
        content_hash="a" * 64,
        size_bytes=1,
        detector_version="detector",
        policy_fingerprint="b" * 64,
    )
    result = ContinuationReadOwnerResult(
        tool_name="read_attachment",
        arguments_json="{}",
        response_json="{}",
        tool_result_json="{}",
        blob_versions=(),
        attachment_inspections=(foreign,),
    )
    with pytest.raises(ValueError, match="another session"):
        _retained_history(empty_attachment_history(session_id="session", operation_id="operation"), "read:one", result)
