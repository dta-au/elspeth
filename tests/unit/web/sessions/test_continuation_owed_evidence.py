"""Real owed P4/evidence reads over an explicitly stored input boundary."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.audit_storage import redacted_tool_invocation_content_and_envelope
from elspeth.web.composer.continuation import EffectFrontierEntry, RetainedOwnerResultRef, ToolBatchFrame
from elspeth.web.composer.continuation_audit_buffer import encode_recorder_snapshot
from elspeth.web.composer.continuation_batch_data import RestoredBatchData, encode_coupled_batch_chunk
from elspeth.web.composer.continuation_codec import encode_continuation_response, encode_continuation_snapshot, encode_private_chunk
from elspeth.web.composer.continuation_evidence import encode_turn_evidence
from elspeth.web.composer.continuation_evidence_graph import capture_turn_evidence
from elspeth.web.composer.continuation_evidence_restore import restore_turn_evidence
from elspeth.web.composer.continuation_message_shelf import encode_message_shelf
from elspeth.web.composer.continuation_owed_evidence import restore_owed_p4_evidence
from elspeth.web.composer.continuation_owed_p4 import materialize_owed_p4_env_inventory
from elspeth.web.composer.continuation_p4_batch import encode_p4_pending_batch_chunk
from elspeth.web.composer.continuation_prepared_p4 import encode_prepared_p4, prepared_p4_members
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.transcript_blob_reader import CurrentTranscriptBlobReader
from elspeth.web.composer.withheld_replies import withheld_reply_envelope
from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn, ComposerOperationFenceLost
from elspeth.web.sessions.continuation import ContinuationEvent, append_complete_snapshot, private_chunk_key, put_chunk
from elspeth.web.sessions.continuation_p4 import retain_p4_result_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table as chat_messages
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import session_operation_fences_table as fences
from tests.helpers.continuation_inventory import inventory_advisor, inventory_work
from tests.unit.web.composer.test_transcript_blob_reader import current_service
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_evidence_restore import stored_evidence, stored_rows
from tests.unit.web.sessions.test_continuation_p4_owner import completed_cut, recovered, rows

operation_store = test_composer_operation_authority.operation_store


def persist_retained_library_cut(service, running, prepared, commit):
    """Real business rows and low-level retention, deliberately before application.

    Normal continuation persistence retains and applies atomically. These owed
    reader tests exercise the separate retained DATA library seam: the ordinary
    writer produces the real business result, then the actual immutable audit
    cohort and retention primitive bind that result to the prepared P4 input.
    No production application is disabled or reported complete.
    """
    members = prepared_p4_members(prepared)
    sid = str(running.claim.session_id)
    outcome = service.persist_compose_turn(
        session_id=sid,
        writer_principal="compose_loop",
        session_operation_context=running.session_operation_context,
        assistant_content=prepared.assistant_content,
        raw_content=prepared.raw_content,
        redacted_assistant_tool_calls=members.redacted_calls,
        redacted_tool_rows=members.redacted_rows,
        revoked_tool_rows=members.revoked_rows,
        mutation_ceiling=members.mutation_ceiling,
        authority_revocation=members.authority_revocation,
        private_tool_evidence=members.private_tool_evidence,
        private_tool_failures=members.private_tool_failures,
        rejection_records=members.rejection_records,
        parent_composition_state_id=prepared.parent_composition_state_id,
        expected_current_state_id=prepared.expected_current_state_id,
        plugin_crash_pending=prepared.crash_pending,
    )
    assert not outcome.unwind_audit_failed and outcome.assistant_id is not None
    with locked_session_transaction(service._engine, sid) as conn, service._session_write_lock(conn, sid):
        assistant = conn.execute(
            select(chat_messages).where(
                chat_messages.c.session_id == sid,
                chat_messages.c.id == outcome.assistant_id,
            )
        ).one()
        drafts = []
        revoked = {
            row.tool_call_id
            for row in members.redacted_rows
            if outcome.authority_revocation is not None and row.composition_state_payload is not None
        }
        for invocation in members.invocations:
            content, envelope = redacted_tool_invocation_content_and_envelope(
                invocation,
                authority_revoked=invocation.tool_call_id in revoked,
            )
            drafts.append((content, envelope))
        drafts.extend((reply.content, withheld_reply_envelope(reply.origin, reply.content)) for reply in members.withheld_replies)
        if drafts:
            first = service._reserve_sequence_range(conn, sid, count=len(drafts))
            assert first == assistant.sequence_no + 1 + 2 * len(outcome.published_tool_rows) + len(members.private_tool_failures)
            for offset, (content, envelope) in enumerate(drafts):
                service._insert_chat_message(
                    conn,
                    session_id=sid,
                    role="audit",
                    content=content,
                    raw_content=None,
                    tool_calls=[envelope],
                    sequence_no=first + offset,
                    writer_principal="compose_loop",
                    composition_state_id=outcome.current_state_id,
                    tool_call_id=None,
                    parent_assistant_id=None,
                    created_at=assistant.created_at,
                    session_operation_context=running.session_operation_context,
                )
        retain_p4_result_on_connection(
            conn,
            commit,
            prepared=prepared,
            outcome=outcome,
            base_sequence=assistant.sequence_no,
            created_at=assistant.created_at,
        )
    return outcome


async def committed_evidence_case(
    store, *, graph=False, reader_service=None, joint=False, current=None, call_id="owned-p4-call", persist_result=True
):
    engine, _repository, authority, service, sid = store
    if current is None:
        running, selected, inventory, _snapshot = await stored_evidence(store)
        work = inventory_work(running)
        restored = await restore_turn_evidence(authority=authority, selected=selected, inventory=inventory, required_work=work)
        work.coordinator.assert_completed()
    else:
        running, selected, inventory, restored = current
    if reader_service is not None:
        restored.evidence.transcript.use_blob_reader(
            CurrentTranscriptBlobReader.from_service(reader_service, session_id=str(sid), context=running.session_operation_context)
        )
    prior_withheld = (
        restored.recorder.snapshot_for_continuation(drained=restored.drained).withheld_replies[restored.drained.withheld_replies :]
        if joint
        else ()
    )
    batch, accumulator, response, prepared = completed_cut(
        running,
        selected.stack,
        graph=graph,
        initial_state=restored.evidence.effects.latest_verified_state,
        call_id=call_id,
        current_state_id=restored.evidence.effects.latest_committed_state_id,
        prior_withheld_replies=prior_withheld,
    )
    batch.assistant_message_index = len(restored.messages)
    members = prepared_p4_members(prepared)
    restored.messages.append(
        {
            "role": "assistant",
            "content": response.message.content,
            "tool_calls": [
                {"id": call.id, "type": "function", "function": {"name": call.function.name, "arguments": call.function.arguments}}
                for call in response.tool_batch.calls
            ],
        }
    )
    for row in members.redacted_rows:
        restored.messages.append({"role": "tool", "tool_call_id": row.tool_call_id, "content": row.content})
    restored.evidence.transcript.observe_messages(restored.messages)
    for authoring in members.authorings:
        restored.recorder.record_private_tool_authoring(authoring)
    for invocation in members.invocations:
        restored.recorder.record(invocation)
        row = next(row for row in members.redacted_rows if row.tool_call_id == invocation.tool_call_id)
        if row.visible_event is not None:
            restored.recorder.record_visible_tool_event(restored.recorder.invocations[-1], row.visible_event)
    for reply in members.withheld_replies[len(prior_withheld) :]:
        restored.recorder.record_withheld_reply(reply.origin, reply.content)
    pending_batch = replace(batch, phase="p4_pending", p4_input_json=encode_prepared_p4(prepared))
    snapshot = capture_turn_evidence(
        restored.evidence,
        restored.messages,
        logical=selected.stack.logical,
        recorder=restored.recorder,
        drained=restored.drained,
        env_ref_names=inventory.names,
        data=RestoredBatchData(pending_batch, accumulator, response.tool_batch.calls) if joint else None,
    )
    evidence = encode_turn_evidence(snapshot, env_ref_names=inventory.names)
    audit = encode_recorder_snapshot(restored.recorder.snapshot_for_continuation(drained=restored.drained))
    transcript = encode_message_shelf(snapshot.snapshot.transcript)
    messages = encode_private_chunk(
        codec="composer.messages.v1",
        canonical=canonical_json(
            {
                "messages_json": snapshot.snapshot.transcript.messages_json,
                "current_indexes": snapshot.snapshot.transcript.current_indexes,
            }
        ),
    )
    batch_chunk = encode_coupled_batch_chunk(batch=batch, accumulator=accumulator, calls=response.tool_batch.calls)
    response_chunk = encode_continuation_response(response)
    compose = selected.stack.frames[0].model_copy(
        update={
            "evidence": evidence.reference,
            "audit_buffer": audit.reference,
            "transcript": transcript.reference,
            "messages": messages.reference,
        }
    )
    stack = selected.stack.model_copy(
        update={
            "frames": (
                compose,
                ToolBatchFrame(
                    phase="tool_ready",
                    response=response_chunk.reference,
                    accumulator=batch_chunk.reference,
                    next_tool_index=batch.next_tool_index,
                    settled_effect_ids=(),
                ),
            )
        }
    )
    with locked_session_transaction(engine, str(sid)) as conn:
        for chunk in (evidence, audit, transcript, messages, batch_chunk, response_chunk):
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
            snapshot_json=encode_continuation_snapshot(stack),
            event_batch=(ContinuationEvent("tool_prepared", '{"fixture":"owed evidence input"}'),),
            handoff=False,
        )
    pending = encode_p4_pending_batch_chunk(
        batch=pending_batch,
        accumulator=accumulator,
        calls=response.tool_batch.calls,
    )
    commit = authority.prepare_continuation_p4(
        running,
        expected_checkpoint_id=stored.checkpoint_id,
        expected_cursor=stored.cursor,
        pending_batch=pending,
    )
    if persist_result:
        await asyncio.to_thread(persist_retained_library_cut, service, running, prepared, commit)
    selected_work = inventory_work(running)
    selected = await load_selected_continuation(
        authority=authority,
        running=running,
        selected=authority.load_continuation_snapshot(running),
        required_work=selected_work,
    )
    selected_work.coordinator.assert_completed()
    if not persist_result:
        return running, selected, None, snapshot, commit
    inventory_work_owner = inventory_work(running)
    owed_inventory = await materialize_owed_p4_env_inventory(
        advisor=inventory_advisor(service),
        authority=authority,
        selected=selected,
        required_work=inventory_work_owner,
        effect_key=commit.effect_key,
    )
    inventory_work_owner.coordinator.assert_completed()
    return running, selected, owed_inventory, snapshot, commit


@pytest.mark.asyncio
@pytest.mark.parametrize("graph", (False, True))
@pytest.mark.parametrize("cut", ("ordinary", "cancel", "segment", "both"))
async def test_actual_owed_evidence_restores_before_result_without_repeating_writer(operation_store, graph, cut):
    engine, repository, authority, _service, sid = operation_store
    running, selected, inventory, expected, commit = await committed_evidence_case(operation_store, graph=graph)
    try:
        expected_result = recovered(authority, running, commit)
        with engine.begin() as conn:
            now = database_now(conn)
            if cut in ("cancel", "both"):
                conn.execute(update(jobs).where(jobs.c.session_id == str(sid)).values(cancel_requested_at=now))
            if cut in ("segment", "both"):
                conn.execute(
                    update(segments)
                    .where(segments.c.session_id == str(sid))
                    .values(
                        started_at=now - timedelta(seconds=3),
                        cooperative_at=now - timedelta(seconds=2),
                        deadline_at=now - timedelta(seconds=1),
                    )
                )
        before = stored_rows(engine), rows(engine)
        ordinary = inventory_work(running)
        with pytest.raises((AuditIntegrityError, ComposerOperationCancelledDuringTurn, ComposerOperationFenceLost)):
            await restore_turn_evidence(authority=authority, selected=selected, inventory=inventory, required_work=ordinary)
        ordinary.coordinator.assert_completed()
        for _ in range(2):
            work = inventory_work(running)
            actual = await restore_owed_p4_evidence(
                authority=authority,
                selected=selected,
                inventory=inventory,
                required_work=work,
                effect_key=commit.effect_key,
            )
            assert actual.checked.result == expected_result
            assert actual.checked.result_chunk.reference == selected.stack.pending_owner_results[0].result
            assert actual.checked.creator_sequence == running.claim.segment_sequence
            assert actual.checked.creator_generation == running.claim.ownership_generation
            assert actual.checked.creator_operation_id == running.session_operation_context.fence.operation_id
            assert actual.checked.creator_operation_epoch == running.session_operation_context.fence.operation_epoch
            assert actual.restored.evidence.effects.committed_revision_ids == ()
            assert actual.restored.evidence.effects.initial_state is actual.restored.evidence.effects.latest_verified_state
            assert (
                capture_turn_evidence(
                    actual.restored.evidence,
                    actual.restored.messages,
                    logical=selected.stack.logical,
                    recorder=actual.restored.recorder,
                    drained=actual.restored.drained,
                    env_ref_names=inventory.names,
                ).snapshot
                == expected.snapshot
            )
            work.coordinator.assert_completed()
        assert (stored_rows(engine), rows(engine)) == before
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("root", "lease", "result_bytes", "evidence_bytes"))
async def test_owed_evidence_rechecks_actual_current_root_lease_and_result(operation_store, change):
    engine, repository, authority, _service, sid = operation_store
    running, selected, inventory, _expected, commit = await committed_evidence_case(operation_store)
    try:
        positive = inventory_work(running)
        await restore_owed_p4_evidence(
            authority=authority,
            selected=selected,
            inventory=inventory,
            required_work=positive,
            effect_key=commit.effect_key,
        )
        positive.coordinator.assert_completed()
        if change == "root":
            selected = replace(selected, stack=selected.stack.model_copy(update={"pending_settlement_ids": ("changed",)}))
        else:
            with engine.begin() as conn:
                if change == "lease":
                    conn.execute(
                        update(fences)
                        .where(fences.c.session_id == str(sid))
                        .values(lease_expires_at=database_now(conn) - timedelta(seconds=1))
                    )
                else:
                    reference = (
                        selected.stack.frames[0].evidence if change == "evidence_bytes" else selected.stack.pending_owner_results[0].result
                    )
                    conn.execute(
                        update(chunks)
                        .where(
                            chunks.c.session_id == str(sid),
                            chunks.c.operation_id == running.claim.operation_id,
                            chunks.c.chunk_key == f"value:{reference.codec}:{reference.digest}",
                        )
                        .values(
                            payload=b"\xff",
                            sha256=sha256(b"\xff").hexdigest(),
                            byte_length=1,
                        )
                    )
        work = inventory_work(running)
        with pytest.raises((AuditIntegrityError, ComposerOperationFenceLost)):
            await restore_owed_p4_evidence(
                authority=authority,
                selected=selected,
                inventory=inventory,
                required_work=work,
                effect_key=commit.effect_key,
            )
        work.coordinator.assert_completed()
    finally:
        if change == "lease":
            with pytest.raises(SessionOperationFenceLost) as caught:
                repository.release(running.session_operation_context)
            assert caught.value.reason is FenceLossReason.LEASE_EXPIRED
        else:
            repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", (False, True))
async def test_owed_restore_rebuilds_required_reader_and_refuses_missing_service(operation_store, tmp_path, cancelled):
    engine, repository, authority, _sessions, sid = operation_store
    service = current_service(operation_store, tmp_path)
    running, selected, inventory, _expected, commit = await committed_evidence_case(operation_store, reader_service=service)
    try:
        if cancelled:
            with engine.begin() as conn:
                conn.execute(update(jobs).where(jobs.c.session_id == str(sid)).values(cancel_requested_at=database_now(conn)))
        missing = inventory_work(running)
        with pytest.raises(AuditIntegrityError, match="actual current reader service"):
            await restore_owed_p4_evidence(
                authority=authority,
                selected=selected,
                inventory=inventory,
                required_work=missing,
                effect_key=commit.effect_key,
            )
        missing.coordinator.assert_completed()
        work = inventory_work(running)
        actual = await restore_owed_p4_evidence(
            authority=authority,
            selected=selected,
            inventory=inventory,
            required_work=work,
            effect_key=commit.effect_key,
            service=service,
        )
        reader = actual.restored.evidence.transcript._blob_reader
        assert type(reader) is CurrentTranscriptBlobReader
        assert reader.service is service and reader.context is running.session_operation_context
        assert actual.checked.snapshot.snapshot.requires_blob_reader
        if cancelled:
            with pytest.raises(ComposerOperationCancelledDuringTurn):
                await asyncio.to_thread(reader, str(uuid4()))
        else:
            assert await asyncio.to_thread(reader, str(uuid4())) is None
        work.coordinator.assert_completed()
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_owed_evidence_admission_cancellation_keeps_original_without_submission(operation_store, monkeypatch):
    from elspeth.web import async_workers

    _engine, repository, authority, _service, _sid = operation_store
    running, selected, inventory, _expected, commit = await committed_evidence_case(operation_store)
    try:
        positive = inventory_work(running)
        await restore_owed_p4_evidence(
            authority=authority,
            selected=selected,
            inventory=inventory,
            required_work=positive,
            effect_key=commit.effect_key,
        )
        positive.coordinator.assert_completed()
        entered = asyncio.Event()
        cancellations, reads = [], []
        admission = async_workers._acquire_admission
        actual_read = authority.read_continuation_owed_p4_evidence

        async def observe_admission():
            entered.set()
            try:
                await admission()
            except asyncio.CancelledError as original:
                cancellations.append(original)
                raise

        def observe_read(*args, **kwargs):
            reads.append((args, kwargs))
            return actual_read(*args, **kwargs)

        monkeypatch.setattr(async_workers, "ADMISSION_CAPACITY", 0)
        monkeypatch.setattr(async_workers, "ADMISSION_WAIT_SECONDS", 30.0)
        monkeypatch.setattr(async_workers, "_acquire_admission", observe_admission)
        monkeypatch.setattr(authority, "read_continuation_owed_p4_evidence", observe_read)
        work = inventory_work(running)
        task = asyncio.create_task(
            restore_owed_p4_evidence(
                authority=authority,
                selected=selected,
                inventory=inventory,
                required_work=work,
                effect_key=commit.effect_key,
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
        finally:
            task.cancel("owed evidence actual admission cancellation")
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert len(cancellations) == 1 and caught.value is cancellations[0]
        assert reads == []
        work.coordinator.assert_completed()
        sql, _projection = work.coordinator.tickets
        assert sql._future is None and sql._no_submission
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_owed_evidence_refuses_an_extra_pending_cohort_after_real_positive(operation_store):
    engine, repository, authority, service, sid = operation_store
    running, selected, inventory, _expected, commit = await committed_evidence_case(operation_store)
    try:
        positive = inventory_work(running)
        await restore_owed_p4_evidence(
            authority=authority,
            selected=selected,
            inventory=inventory,
            required_work=positive,
            effect_key=commit.effect_key,
        )
        positive.coordinator.assert_completed()
        extra_key = "negative-extra-pending"
        root = selected.stack.model_copy(
            update={
                "effect_frontier": (
                    *selected.stack.effect_frontier,
                    EffectFrontierEntry(effect_id=extra_key, disposition="committed", receipt_id="negative-extra-receipt"),
                ),
                "pending_owner_results": (
                    *selected.stack.pending_owner_results,
                    RetainedOwnerResultRef(effect_key=extra_key, kind="read", result=selected.stack.pending_owner_results[0].result),
                ),
            }
        )
        with locked_session_transaction(engine, str(sid)) as conn:
            stored = append_complete_snapshot(
                conn,
                session_id=str(sid),
                operation_id=running.claim.operation_id,
                expected_checkpoint_id=selected.checkpoint_id,
                expected_cursor=selected.stack.cursor,
                snapshot_json=encode_continuation_snapshot(root),
                event_batch=(ContinuationEvent("checkpoint", '{"negative_fixture":"extra pending cohort"}'),),
                handoff=False,
            )
        work = inventory_work(running)
        selected = await load_selected_continuation(
            authority=authority,
            running=running,
            selected=stored,
            required_work=work,
        )
        inventory = await materialize_owed_p4_env_inventory(
            advisor=inventory_advisor(service),
            authority=authority,
            selected=selected,
            required_work=work,
            effect_key=commit.effect_key,
        )
        work.coordinator.assert_completed()
        refused = inventory_work(running)
        with pytest.raises(AuditIntegrityError, match="one exact pending P4 result"):
            await restore_owed_p4_evidence(
                authority=authority,
                selected=selected,
                inventory=inventory,
                required_work=refused,
                effect_key=commit.effect_key,
            )
        refused.coordinator.assert_completed()
    finally:
        repository.release(running.session_operation_context)
