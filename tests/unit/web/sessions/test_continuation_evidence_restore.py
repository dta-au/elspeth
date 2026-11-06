"""Real current SQL custody for the bounded pre-P4 evidence owner.

The fixture installs the stored input boundary, not an initializer/provider.
Unconsumed frame references are explicit opaque test payloads; this module
qualifies no iteration/cache/attachment decoder or complete runtime restore.
"""

import asyncio
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256

import pytest
from sqlalchemy import insert, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_attachment_history import empty_attachment_history, encode_attachment_history
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors, encode_recorder_snapshot
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot, encode_private_chunk
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_evidence import encode_turn_evidence
from elspeth.web.composer.continuation_evidence_graph import capture_turn_evidence
from elspeth.web.composer.continuation_evidence_restore import restore_turn_evidence
from elspeth.web.composer.continuation_message_shelf import encode_message_shelf
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.turn_effects import TurnEvidence
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn, ComposerOperationFenceLost
from elspeth.web.sessions.continuation import ContinuationEvent, append_complete_snapshot, private_chunk_key, put_chunk
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table as messages_table
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks_table
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from tests.helpers.continuation_inventory import inventory_advisor, inventory_work
from tests.unit.web.composer.test_turn_effects import _state
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_cold_restore import admit_continuation, start_continuation

operation_store = test_composer_operation_authority.operation_store
REQUEST = "Preserve café and exact context"


async def stored_evidence(store):
    engine, repository, authority, sessions, sid = store
    record, _seed, _action = admit_continuation(store)
    running = start_continuation(repository, authority)
    head = authority.load_continuation_snapshot(running)
    root = decode_continuation_snapshot(head.payload_json)
    owner = TurnEvidence(
        session_id=str(sid),
        session_operation_context=running.session_operation_context,
        request_id=root.logical.original_user_message_id,
        request=REQUEST,
        initial_state_id=None,
        initial_state=_state("Untitled Pipeline"),
        actor_user_id="alice",
    )
    messages = [{"role": "user", "content": REQUEST}]
    owner.transcript.begin_messages(messages)
    # Real nonempty evidence, not an emptied reconstruction fixture.
    owner.transcript.observe_messages(({"role": "user", "content": "Keep this observed middle amendment"},))
    recorder = BufferingRecorder()
    recorder.record_withheld_reply("repair_gate_superseded", "A retained unpublished reply")
    snapshot = capture_turn_evidence(
        owner, messages, logical=root.logical, recorder=recorder, drained=RecorderDrainCursors(), env_ref_names=frozenset()
    )
    evidence = encode_turn_evidence(snapshot)
    state = encode_private_chunk(codec="composer.state.v1", canonical=canonical_json(owner.effects.initial_state.to_dict()))
    transcript = encode_message_shelf(snapshot.snapshot.transcript)
    audit = encode_recorder_snapshot(recorder.snapshot_for_continuation(drained=RecorderDrainCursors()))
    # These values are outside this owner. Their actual bytes/ref closure are
    # retained; no placeholder module or fake decoder is called or installed.
    opaque = {
        name: encode_private_chunk(codec="composer.messages.v1", canonical=canonical_json({"unconsumed_test_input": name}))
        for name in ("bootstrap", "iteration", "caches")
    }
    # Evidence restore reproves attachment applications through the real
    # attachment-history decoder, so this value is inside the owner: it is the
    # same empty history a fresh composition initialization writes.
    attachments = encode_attachment_history(empty_attachment_history(session_id=str(sid), operation_id=record.operation_id))
    current_messages = encode_private_chunk(
        codec="composer.messages.v1",
        canonical=canonical_json(
            {"messages_json": snapshot.snapshot.transcript.messages_json, "current_indexes": snapshot.snapshot.transcript.current_indexes}
        ),
    )
    frame = ComposeFrame(
        phase="action_ready",
        bootstrap=opaque["bootstrap"].reference,
        iteration=opaque["iteration"].reference,
        messages=current_messages.reference,
        tools=None,
        initial_state=state.reference,
        state=state.reference,
        evidence=evidence.reference,
        audit_buffer=audit.reference,
        transcript=transcript.reference,
        caches=opaque["caches"].reference,
        attachment_admissions=attachments.reference,
        initial_version=1,
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
        pending_response=None,
    )
    root = root.model_copy(update={"frames": (frame,)})
    with locked_session_transaction(engine, str(sid)) as conn:
        # Exact committed-ingress input fixture. This does not test or claim the
        # missing v2 initializer producer and does not bypass a current read guard.
        conn.execute(
            insert(messages_table).values(
                id=owner.request_id,
                session_id=str(sid),
                role="user",
                content=REQUEST,
                sequence_no=1,
                writer_principal="route_user_message",
                created_at=database_now(conn),
            )
        )
        conn.execute(
            update(jobs)
            .where(jobs.c.session_id == str(sid), jobs.c.operation_id == record.operation_id)
            .values(user_message_id=owner.request_id, ingress_phase="committed")
        )
        for chunk in (evidence, state, transcript, audit, current_messages, attachments, *opaque.values()):
            put_chunk(
                conn,
                session_id=str(sid),
                operation_id=record.operation_id,
                key=private_chunk_key(chunk),
                payload=chunk.canonical.encode("utf-8"),
                codec=chunk.reference.codec,
            )
        stored = append_complete_snapshot(
            conn,
            session_id=str(sid),
            operation_id=record.operation_id,
            expected_checkpoint_id=head.checkpoint_id,
            expected_cursor=head.cursor,
            snapshot_json=encode_continuation_snapshot(root),
            event_batch=(ContinuationEvent("checkpoint", '{"test_scope":"pre-P4 evidence input"}'),),
            handoff=False,
        )
    work = inventory_work(running)
    selected = await load_selected_continuation(authority=authority, running=running, selected=stored, required_work=work)
    inventory = await materialize_continuation_env_inventory(
        advisor=inventory_advisor(sessions), authority=authority, selected=selected, required_work=work
    )
    work.coordinator.assert_completed()
    return running, selected, inventory, snapshot


def stored_rows(engine):
    with engine.connect() as conn:
        return tuple(tuple(tuple(row) for row in conn.execute(select(table))) for table in (jobs, segments, messages_table, chunks_table))


@pytest.mark.asyncio
async def test_actual_current_read_restores_full_graph_without_writes(operation_store):
    engine, _repository, authority, _sessions, _sid = operation_store
    running, selected, inventory, expected = await stored_evidence(operation_store)
    before = stored_rows(engine)
    work = inventory_work(running)
    actual = await restore_turn_evidence(authority=authority, selected=selected, inventory=inventory, required_work=work)
    assert actual.evidence.operation_id == running.session_operation_context.fence.operation_id
    assert actual.evidence._continuation_current_epoch == running.session_operation_context.fence.operation_epoch
    assert actual.evidence.effects.initial_state is actual.evidence.effects.latest_verified_state
    assert (
        capture_turn_evidence(
            actual.evidence,
            actual.messages,
            recorder=actual.recorder,
            drained=actual.drained,
            logical=selected.stack.logical,
            env_ref_names=inventory.names,
        ).snapshot
        == expected.snapshot
    )
    assert actual.recorder.withheld_replies[0].content == "A retained unpublished reply"
    assert "middle amendment" in actual.evidence.transcript.snapshot().provider_json
    assert stored_rows(engine) == before
    work.coordinator.assert_completed()
    assert all(not ticket.permits_process_drain for ticket in work.coordinator.tickets)


@pytest.mark.asyncio
async def test_same_checkpoint_cursor_substituted_root_refuses_after_positive(operation_store):
    _engine, _repository, authority, _sessions, _sid = operation_store
    running, selected, inventory, _expected = await stored_evidence(operation_store)
    await restore_turn_evidence(authority=authority, selected=selected, inventory=inventory, required_work=inventory_work(running))
    frame = selected.stack.frames[0]
    altered_frame = frame.model_copy(update={"composition_total": frame.composition_total + 1})
    altered = replace(selected, stack=selected.stack.model_copy(update={"frames": (altered_frame,)}))
    work = inventory_work(running)
    with pytest.raises(AuditIntegrityError, match="complete selected root"):
        await restore_turn_evidence(authority=authority, selected=altered, inventory=inventory, required_work=work)
    work.coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ("evidence", "audit_buffer", "transcript", "initial_state", "messages"))
async def test_coherent_sql_bytes_corruption_is_owned_refusal_after_positive(operation_store, channel):
    engine, _repository, authority, _sessions, _sid = operation_store
    running, selected, inventory, _expected = await stored_evidence(operation_store)
    await restore_turn_evidence(authority=authority, selected=selected, inventory=inventory, required_work=inventory_work(running))
    frame = selected.stack.frames[0]
    references = {
        "evidence": frame.evidence,
        "audit_buffer": frame.audit_buffer,
        "transcript": frame.transcript,
        "initial_state": frame.initial_state,
        "messages": frame.messages,
    }
    reference = references[channel]
    raw = b"\xff"
    with engine.begin() as conn:
        conn.execute(
            update(chunks_table)
            .where(chunks_table.c.chunk_key == f"value:{reference.codec}:{reference.digest}")
            .values(payload=raw, sha256=sha256(raw).hexdigest(), byte_length=len(raw))
        )
    work = inventory_work(running)
    with pytest.raises(AuditIntegrityError, match="storage differs"):
        await restore_turn_evidence(authority=authority, selected=selected, inventory=inventory, required_work=work)
    work.coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", ("cancel", "segment"))
async def test_ordinary_evidence_restore_does_not_inherit_owed_p4_allowances(operation_store, cut):
    engine, _repository, authority, _sessions, sid = operation_store
    running, selected, inventory, _expected = await stored_evidence(operation_store)
    await restore_turn_evidence(authority=authority, selected=selected, inventory=inventory, required_work=inventory_work(running))
    with engine.begin() as conn:
        now = database_now(conn)
        if cut == "cancel":
            conn.execute(update(jobs).where(jobs.c.session_id == str(sid)).values(cancel_requested_at=now))
        else:
            conn.execute(
                update(segments)
                .where(segments.c.session_id == str(sid))
                .values(
                    started_at=now - timedelta(seconds=3), cooperative_at=now - timedelta(seconds=2), deadline_at=now - timedelta(seconds=1)
                )
            )
    work = inventory_work(running)
    with pytest.raises((ComposerOperationCancelledDuringTurn, ComposerOperationFenceLost)):
        await restore_turn_evidence(authority=authority, selected=selected, inventory=inventory, required_work=work)
    work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_actual_admission_cancellation_preserves_one_original_and_submits_nothing(operation_store, monkeypatch):
    from elspeth.web import async_workers

    authority = operation_store[2]
    running, selected, inventory, _expected = await stored_evidence(operation_store)
    await restore_turn_evidence(authority=authority, selected=selected, inventory=inventory, required_work=inventory_work(running))
    work = inventory_work(running)
    entered = asyncio.Event()
    cancellations, calls = [], []
    actual_admission = async_workers._acquire_admission
    actual_read = authority.read_continuation_turn_evidence

    async def observed_admission():
        entered.set()
        try:
            await actual_admission()
        except asyncio.CancelledError as original:
            cancellations.append(original)
            raise

    def observed_read(*args, **kwargs):
        calls.append((args, kwargs))
        return actual_read(*args, **kwargs)

    monkeypatch.setattr(async_workers, "ADMISSION_CAPACITY", 0)
    monkeypatch.setattr(async_workers, "ADMISSION_WAIT_SECONDS", 30.0)
    monkeypatch.setattr(async_workers, "_acquire_admission", observed_admission)
    monkeypatch.setattr(authority, "read_continuation_turn_evidence", observed_read)
    task = asyncio.create_task(restore_turn_evidence(authority=authority, selected=selected, inventory=inventory, required_work=work))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
    finally:
        task.cancel("actual evidence admission cancellation")
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert len(cancellations) == 1 and caught.value is cancellations[0]
    assert calls == []
    work.coordinator.assert_completed()
    sql, _projection = work.coordinator.tickets
    assert sql._future is None and sql._no_submission
