"""Actual SQL application, current journal reads and atomic rollback cuts."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256

import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import EffectFrontierEntry
from elspeth.web.composer.continuation_codec import (
    decode_continuation_response,
    decode_continuation_snapshot,
    encode_continuation_response,
    encode_continuation_snapshot,
)
from elspeth.web.composer.continuation_evidence_restore import restore_turn_evidence
from elspeth.web.composer.continuation_owed_p4 import materialize_owed_p4_env_inventory
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions import continuation_p4_application_store as application_store
from elspeth.web.sessions.continuation import ContinuationEvent, append_complete_snapshot, private_chunk_key, put_chunk
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from tests.helpers.continuation_inventory import inventory_advisor, inventory_for_handoff_test, inventory_work
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_evidence_restore import stored_rows
from tests.unit.web.sessions.test_continuation_owed_evidence import committed_evidence_case
from tests.unit.web.sessions.test_continuation_p4_owner import rows

operation_store = test_composer_operation_authority.operation_store


def continuation_rows(engine):
    with engine.connect() as conn:
        return tuple(
            tuple(conn.execute(select(table).order_by(*table.primary_key.columns)).all()) for table in (checkpoints, events, effects)
        )


async def selected_now(store, running, *, effect_key=None):
    _engine, _repository, authority, sessions, _sid = store
    work = inventory_work(running)
    selected = await load_selected_continuation(
        authority=authority,
        running=running,
        selected=await asyncio.to_thread(authority.load_continuation_snapshot, running),
        required_work=work,
    )
    work.coordinator.assert_completed()
    if effect_key is None:
        inventory = await inventory_for_handoff_test(store, running, authority.load_continuation_snapshot(running))
    else:
        work = inventory_work(running)
        inventory = await materialize_owed_p4_env_inventory(
            advisor=inventory_advisor(sessions), authority=authority, selected=selected, required_work=work, effect_key=effect_key
        )
        work.coordinator.assert_completed()
    return selected, inventory


async def apply_case(store, running, selected, inventory):
    frame = selected.stack.frames[0]
    batch = selected.stack.frames[-1]
    return await asyncio.to_thread(
        store[2].apply_continuation_p4_application,
        running,
        selected=selected,
        inventory=inventory,
        expected_evidence_chunk=next(chunk for chunk in selected.chunks if chunk.reference == frame.evidence),
        expected_pending_batch_chunk=next(chunk for chunk in selected.chunks if chunk.reference == batch.accumulator),
    )


async def read_current(store, running):
    selected, inventory = await selected_now(store, running)
    work = inventory_work(running)
    restored = await restore_turn_evidence(authority=store[2], selected=selected, inventory=inventory, required_work=work)
    work.coordinator.assert_completed()
    return selected, inventory, restored


async def second_pending(store, running):
    selected, inventory, restored = await read_current(store, running)
    return await committed_evidence_case(
        store,
        joint=True,
        current=(running, selected, inventory, restored),
        call_id="second-actual-p4-call",
    )


@pytest.mark.asyncio
async def test_two_actual_applications_restore_one_shared_graph_and_do_not_repeat_business_sql(operation_store):
    engine, repository, _authority, _service, _sid = operation_store
    running, selected, inventory, _snapshot, first = await committed_evidence_case(operation_store, graph=True, joint=True)
    try:
        business = rows(engine)
        applied = await apply_case(operation_store, running, selected, inventory)
        assert rows(engine) == business
        assert applied.snapshot.cursor == selected.stack.cursor + 1
        assert applied.bundle.entry.applications[0].effect_key == first.effect_key
        after = decode_continuation_snapshot(applied.snapshot.payload_json)
        assert not after.pending_owner_results
        assert after.effect_frontier == selected.stack.effect_frontier
        assert after.pending_settlement_ids == selected.stack.pending_settlement_ids
        selected, inventory, restored = await read_current(operation_store, running)
        assert restored.batch is restored.evidence._portable_p4_graph.selected
        assert restored.evidence.effects is restored.evidence._portable_p4_graph.effects
        assert restored.evidence.effects.committed_revision_ids == applied.bundle.evidence_after.snapshot.effects.committed_revision_ids
        assert restored.batch.batch.p4 is None
        assert restored.batch.batch.phase == "complete"
        running2, selected2, inventory2, _snapshot, second = await second_pending(operation_store, running)
        assert running2 is running and second.effect_key != first.effect_key
        business = rows(engine)
        second_application = await apply_case(operation_store, running, selected2, inventory2)
        assert rows(engine) == business
        assert tuple(record.effect_key for record in second_application.bundle.entry.applications) == (first.effect_key, second.effect_key)
        for _ in range(2):
            _current, _names, restored = await read_current(operation_store, running)
            assert len(restored.applications.applications) == 2
            assert restored.evidence.effects is restored.evidence._portable_p4_graph.effects
            assert restored.batch is restored.evidence._portable_p4_graph.selected
        assert rows(engine) == business
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", ("before_append", "after_append"))
async def test_actual_application_rolls_back_chunks_events_and_root_on_original_error(operation_store, monkeypatch, cut):
    engine, repository, _authority, _service, _sid = operation_store
    running, selected, inventory, _snapshot, _commit = await committed_evidence_case(operation_store, graph=True, joint=True)
    try:
        await apply_case(operation_store, running, selected, inventory)
        _running, selected, inventory, _snapshot, _commit = await second_pending(operation_store, running)
        before = stored_rows(engine), rows(engine), continuation_rows(engine)
        original = RuntimeError("actual atomic application rollback cut")
        append = application_store.append_complete_snapshot

        def fail(conn, **kwargs):
            if cut == "after_append":
                append(conn, **kwargs)
            raise original

        with monkeypatch.context() as patch:
            patch.setattr(application_store, "append_complete_snapshot", fail)
            with pytest.raises(RuntimeError) as caught:
                await apply_case(operation_store, running, selected, inventory)
            assert caught.value is original
        assert (stored_rows(engine), rows(engine), continuation_rows(engine)) == before
        await apply_case(operation_store, running, selected, inventory)
        assert rows(engine) == before[1]
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_application_refuses_wrong_nonempty_frontier_receipt_after_real_positive(operation_store):
    engine, repository, _authority, _service, sid = operation_store
    running, selected, inventory, _snapshot, _commit = await committed_evidence_case(operation_store, graph=True, joint=True)
    try:
        await apply_case(operation_store, running, selected, inventory)
        _running, selected, _inventory, _snapshot, commit = await second_pending(operation_store, running)
        wrong = selected.stack.model_copy(
            update={
                "effect_frontier": tuple(
                    EffectFrontierEntry(effect_id=item.effect_id, disposition="committed", receipt_id="different-nonempty-receipt")
                    if item.effect_id == commit.effect_key
                    else item
                    for item in selected.stack.effect_frontier
                )
            }
        )
        encoded = encode_continuation_snapshot(wrong)
        assert decode_continuation_snapshot(encoded) == wrong
        with locked_session_transaction(engine, str(sid)) as conn:
            append_complete_snapshot(
                conn,
                session_id=str(sid),
                operation_id=running.claim.operation_id,
                expected_checkpoint_id=selected.checkpoint_id,
                expected_cursor=selected.stack.cursor,
                snapshot_json=encoded,
                event_batch=(ContinuationEvent("checkpoint", '{"fixture":"wrong frontier receipt"}'),),
                handoff=False,
            )
        selected, inventory = await selected_now(operation_store, running, effect_key=commit.effect_key)
        before = stored_rows(engine), rows(engine)
        with pytest.raises(AuditIntegrityError, match="actual committed result frontier"):
            await apply_case(operation_store, running, selected, inventory)
        assert (stored_rows(engine), rows(engine)) == before
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_current_journal_rechecks_actual_old_tool_cursor_after_real_positive(operation_store):
    engine, repository, _authority, _service, sid = operation_store
    running, selected, inventory, _snapshot, _commit = await committed_evidence_case(operation_store, graph=True, joint=True)
    try:
        application = await apply_case(operation_store, running, selected, inventory)
        await read_current(operation_store, running)
        record = application.bundle.entry.applications[0]
        with engine.begin() as conn:
            old = conn.execute(select(checkpoints).where(checkpoints.c.checkpoint_id == record.checkpoint_id)).one()
            chunk = conn.execute(select(chunks).where(chunks.c.session_id == str(sid), chunks.c.chunk_key == old.snapshot_chunk_key)).one()
            root = decode_continuation_snapshot(chunk.payload.decode("utf-8"))
            changed = root.model_copy(update={"frames": (*root.frames[:-1], root.frames[-1].model_copy(update={"next_tool_index": 0}))})
            payload = encode_continuation_snapshot(changed).encode("utf-8")
            assert decode_continuation_snapshot(payload.decode("utf-8")) == changed
            conn.execute(
                update(chunks)
                .where(chunks.c.session_id == str(sid), chunks.c.chunk_key == old.snapshot_chunk_key)
                .values(payload=payload, sha256=sha256(payload).hexdigest(), byte_length=len(payload))
            )
        before = stored_rows(engine), rows(engine)
        with pytest.raises(AuditIntegrityError, match="selected frame inputs"):
            await read_current(operation_store, running)
        assert (stored_rows(engine), rows(engine)) == before
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_current_applied_journal_refuses_a_different_actual_response_after_real_positive(operation_store):
    engine, repository, _authority, _service, sid = operation_store
    running, selected, inventory, _snapshot, _commit = await committed_evidence_case(operation_store, graph=True, joint=True)
    try:
        await apply_case(operation_store, running, selected, inventory)
        selected, _inventory, _restored = await read_current(operation_store, running)
        batch = selected.stack.frames[-1]
        old = decode_continuation_response(next(chunk for chunk in selected.chunks if chunk.reference == batch.response))
        response = encode_continuation_response(replace(old, message=replace(old.message, content="different retained assistant prose")))
        assert decode_continuation_response(response).tool_batch.calls == old.tool_batch.calls
        assert response.reference != batch.response
        root = selected.stack.model_copy(
            update={"frames": (*selected.stack.frames[:-1], batch.model_copy(update={"response": response.reference}))}
        )
        encoded = encode_continuation_snapshot(root)
        assert decode_continuation_snapshot(encoded) == root
        with locked_session_transaction(engine, str(sid)) as conn:
            put_chunk(
                conn,
                session_id=str(sid),
                operation_id=running.claim.operation_id,
                key=private_chunk_key(response),
                payload=response.canonical.encode("utf-8"),
                codec=response.reference.codec,
            )
            append_complete_snapshot(
                conn,
                session_id=str(sid),
                operation_id=running.claim.operation_id,
                expected_checkpoint_id=selected.checkpoint_id,
                expected_cursor=selected.stack.cursor,
                snapshot_json=encoded,
                event_batch=(ContinuationEvent("checkpoint", '{"fixture":"different response"}'),),
                handoff=False,
            )
        before = stored_rows(engine), rows(engine), continuation_rows(engine)
        with pytest.raises(AuditIntegrityError, match="selected settled batch"):
            await read_current(operation_store, running)
        assert (stored_rows(engine), rows(engine), continuation_rows(engine)) == before
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", ("cancel", "segment", "both"))
async def test_owed_application_drains_stop_and_expiry_without_another_business_write(operation_store, cut):
    engine, repository, _authority, _service, sid = operation_store
    running, selected, inventory, _snapshot, commit = await committed_evidence_case(operation_store, graph=True, joint=True)
    try:
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
        business = rows(engine)
        applied = await apply_case(operation_store, running, selected, inventory)
        assert rows(engine) == business
        root = decode_continuation_snapshot(applied.snapshot.payload_json)
        assert not root.pending_owner_results
        assert root.frames[-1].settled_effect_ids == (commit.effect_key,)
        assert root.effect_frontier == selected.stack.effect_frontier
    finally:
        repository.release(running.session_operation_context)
