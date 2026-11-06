"""Actual reserved-ID service ingress, current SQL replay and atomic rollback."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_bootstrap import capture_continuation_bootstrap, encode_continuation_bootstrap
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_next_action import next_continuation_action_policy
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.graph_recovery import GraphRecoveryContext
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions import continuation_ingress as owner
from elspeth.web.sessions.composer_operations import ComposerOperationFenceLost, ComposerOperationRunning
from elspeth.web.sessions.continuation import ContinuationCASConflict, ContinuationEvent, append_complete_snapshot
from elspeth.web.sessions.continuation_ingress import ContinuationIngressCheckpoint
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import message_ingress_receipts_table as receipts
from elspeth.web.sessions.models import metadata
from elspeth.web.sessions.models import sessions_table as sessions
from elspeth.web.sessions.routes._helpers import _initial_composition_state
from elspeth.web.sessions.schemas import SendMessageRequest
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.continuation_inventory import inventory_work
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.coordination.test_continuation_p4_recovery_start import new_owners
from tests.unit.web.sessions.test_continuation_cold_restore import admit_continuation, start_continuation

operation_store = operations.operation_store


def recovery_rows(engine):
    """Complete actual local SQL state, including user/receipt/ledger/title rows."""
    with engine.connect() as conn:
        return tuple(
            (table.name, tuple(conn.execute(select(table).order_by(*table.primary_key.columns)).all())) for table in metadata.sorted_tables
        )


def begin(store):
    record, seed, _action = admit_continuation(store)
    running = start_continuation(store[1], store[2])
    selected = store[2].load_continuation_snapshot(running)
    turn_input = store[2].get_for_turn(session_id=store[4], operation_id=record.operation_id)
    assert turn_input is not None and turn_input.request_json is not None
    request = SendMessageRequest.model_validate_json(turn_input.request_json)
    return running, inventory_work(running), selected, request, seed


async def ingress(store, running, work, selected, request, **changes):
    sql, projection = work.reserve_pair(RequiredWorkSource.INGRESS_SQL, RequiredWorkSource.INGRESS_PROJECTION)
    arguments = {
        "operation_id": UUID(request.operation_id),
        "requested_state_id": request.state_id,
        "composition_state_id": request.state_id,
        "writer_principal": "route_user_message",
        "session_operation_context": running.session_operation_context,
        "running": running,
        "required_work": sql,
        "continuation": selected,
    }
    arguments.update(changes)
    try:
        result = await store[3].add_message_with_transcript(store[4], "user", request.content, **arguments)
    except BaseException as error:
        projection.complete_owned(error)
        raise
    projection.complete_owned()
    assert type(result) is ContinuationIngressCheckpoint
    return result


def mutate_root(store, running, selected, root):
    with locked_session_transaction(store[0], str(store[4])) as conn:
        return append_complete_snapshot(
            conn,
            session_id=str(store[4]),
            operation_id=running.claim.operation_id,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.cursor,
            snapshot_json=encode_continuation_snapshot(root),
            event_batch=(ContinuationEvent("checkpoint", '{"fixture":"coherent selected initialization mutation"}'),),
            handoff=False,
        )


@pytest.mark.asyncio
async def test_real_reserved_ingress_commits_one_user_receipt_and_complete_root_then_replays(operation_store):
    running, work, selected, request, seed = begin(operation_store)
    root = decode_continuation_snapshot(selected.payload_json)
    try:
        initial_rows = recovery_rows(operation_store[0])
        first = await ingress(operation_store, running, work, selected, request)
        assert recovery_rows(operation_store[0]) != initial_rows
        assert str(first.message.id) == seed.reserved_user_message_id == root.logical.original_user_message_id
        assert first.message.operation_id == UUID(request.operation_id) and first.disposition == "inserted"
        assert first.transcript[-1] is first.message and first.message.content == request.content
        after = decode_continuation_snapshot(first.snapshot.payload_json)
        assert after.cursor == selected.cursor + 1 and after.frames[0].ingress_phase == "inserted"
        assert after.logical == root.logical and after.mutation_ceiling == root.mutation_ceiling
        assert after.provider_cost_policy == root.provider_cost_policy and after.admission_policy_json == root.admission_policy_json
        with operation_store[0].connect() as conn:
            human = conn.execute(select(messages).where(messages.c.id == str(first.message.id))).one()
            receipt = conn.execute(select(receipts).where(receipts.c.operation_id == request.operation_id)).one()
            parent = conn.execute(select(jobs).where(jobs.c.operation_id == request.operation_id)).one()
        assert human.role == "user" and human.writer_principal == "route_user_message"
        assert receipt.user_message_id == parent.user_message_id == parent.reserved_user_message_id == str(first.message.id)
        assert parent.ingress_phase == "committed"
        before = recovery_rows(operation_store[0])
        replay = await ingress(operation_store, running, work, first.snapshot, request)
        assert replay.disposition == "replayed" and replay.snapshot == first.snapshot
        assert replay.message == first.message and replay.transcript == first.transcript
        assert recovery_rows(operation_store[0]) == before
        # A stale caller must reacquire the selected head even if its paid/user
        # intent is the same. It cannot treat the old CAS as replay permission.
        with pytest.raises(ContinuationCASConflict, match="selected CAS"):
            await ingress(operation_store, running, work, selected, request)
        assert recovery_rows(operation_store[0]) == before
    finally:
        work.coordinator.assert_completed()
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", ("reserved", "inserted"))
async def test_actual_release_new_claim_ingress_uses_same_reserved_source_without_second_row(operation_store, cut):
    running, work, selected, request, seed = begin(operation_store)
    first = None
    if cut == "inserted":
        first = await ingress(operation_store, running, work, selected, request)
    work.coordinator.assert_completed()
    operation_store[1].release(running.session_operation_context)
    current = new_owners(operation_store)
    _engine, repository, authority, _sessions, sid = current
    assert (
        repository.recover_composer_segment(
            session_id=sid,
            operation_id=request.operation_id,
            expected_generation=running.claim.ownership_generation,
            actor_user_id="alice",
            auth_provider_type="local",
        )
        is True
    )
    (claim,) = authority.claim_segment(limit=1)
    context = repository.start_composer_segment(
        claim, owner_instance_id=authority._owner_instance_id, lease_seconds=30, auth_provider_type="local"
    )
    now = ComposerOperationRunning(claim=claim, session_operation_context=context)
    fresh_work = inventory_work(now)
    try:
        assert context.fence != running.session_operation_context.fence
        selected_now = authority.load_continuation_snapshot(now)
        before = recovery_rows(operation_store[0])
        result = await ingress(current, now, fresh_work, selected_now, request)
        assert str(result.message.id) == seed.reserved_user_message_id
        if cut == "inserted":
            assert result.disposition == "replayed" and result.message == first.message
            assert recovery_rows(operation_store[0]) == before
        else:
            assert result.disposition == "inserted"
        latest = authority.load_continuation_snapshot(now)
        unchanged = recovery_rows(operation_store[0])
        assert (await ingress(current, now, fresh_work, latest, request)).disposition == "replayed"
        assert recovery_rows(operation_store[0]) == unchanged
        stale_work = inventory_work(running)
        try:
            with pytest.raises((AuditIntegrityError, SessionOperationFenceLost, ComposerOperationFenceLost)):
                await ingress(operation_store, running, stale_work, selected, request)
        finally:
            stale_work.coordinator.assert_completed()
    finally:
        fresh_work.coordinator.assert_completed()
        repository.release(context)


@pytest.mark.asyncio
async def test_ingress_root_write_failure_rolls_back_real_row_sequence_parent_receipt_and_preserves_error(operation_store, monkeypatch):
    running, work, selected, request, _seed = begin(operation_store)
    original = OSError("actual initialized root publication failure")
    before = recovery_rows(operation_store[0])
    original_append = owner.append_complete_snapshot
    observed = []

    def fail_after_real_receipt(*args, **kwargs):
        conn = args[0]
        assert conn.execute(select(receipts).where(receipts.c.operation_id == request.operation_id)).one()
        assert conn.execute(
            select(messages).where(messages.c.id == decode_continuation_snapshot(selected.payload_json).logical.original_user_message_id)
        ).one()
        observed.append(True)
        raise original

    monkeypatch.setattr(owner, "append_complete_snapshot", fail_after_real_receipt)
    try:
        with pytest.raises(OSError) as caught:
            await ingress(operation_store, running, work, selected, request)
        assert caught.value is original and observed == [True]
        assert recovery_rows(operation_store[0]) == before
        monkeypatch.setattr(owner, "append_complete_snapshot", original_append)
        result = await ingress(operation_store, running, work, selected, request)
        assert result.disposition == "inserted" and result.message.sequence_no == 1
    finally:
        work.coordinator.assert_completed()
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("title_receipt", "created_at"))
async def test_inserted_replay_reproves_original_event_and_complete_sql_transcript_after_positive(operation_store, change):
    running, work, selected, request, _seed = begin(operation_store)
    try:
        result = await ingress(operation_store, running, work, selected, request)
        assert (await ingress(operation_store, running, work, result.snapshot, request)).disposition == "replayed"
        current = result.snapshot
        if change == "title_receipt":
            root = decode_continuation_snapshot(current.payload_json)
            receipt = owner.ContinuationIngressReceipt.model_validate_json(root.frames[0].ingress_receipt_json)
            receipt = receipt.model_copy(update={"original_session_title": receipt.original_session_title + " changed"})
            frame = root.frames[0].model_copy(update={"ingress_receipt_json": canonical_json(receipt.model_dump(mode="json"))})
            current = mutate_root(operation_store, running, current, root.model_copy(update={"frames": (frame,)}))
            message = "exact original ingress source/event"
        else:
            with operation_store[0].begin() as conn:
                row = conn.execute(select(messages).where(messages.c.id == str(result.message.id))).one()
                changed = conn.execute(
                    update(messages).where(messages.c.id == row.id).values(created_at=row.created_at + timedelta(seconds=1))
                )
                assert changed.rowcount == 1
            message = "complete original SQL user/transcript source"
        before = recovery_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match=message):
            await ingress(operation_store, running, work, current, request)
        assert recovery_rows(operation_store[0]) == before
    finally:
        work.coordinator.assert_completed()
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("operation", "context", "source", "parent", "actor", "preexisting"))
async def test_real_ingress_refuses_mismatched_current_request_source_actor_fence_or_preexisting_id(operation_store, change):
    running, work, selected, request, seed = begin(operation_store)
    try:
        result = await ingress(operation_store, running, work, selected, request)
        assert (await ingress(operation_store, running, work, result.snapshot, request)).disposition == "replayed"
        selected = result.snapshot
        if change == "preexisting":
            root = decode_continuation_snapshot(result.snapshot.payload_json)
            reserved = root.frames[0].model_copy(update={"ingress_phase": "reserved", "ingress_receipt_json": None})
            selected = mutate_root(operation_store, running, result.snapshot, root.model_copy(update={"frames": (reserved,)}))
        changes = {}
        if change == "operation":
            changes["operation_id"] = uuid4()
        elif change == "context":
            changes["session_operation_context"] = replace(running.session_operation_context)
        elif change == "source":
            selected = replace(selected, payload_json=selected.payload_json + " ")
        elif change == "parent":
            selected = replace(selected, checkpoint_id=str(uuid4()))
        elif change == "actor":
            # Keep the original immutable operation actor. A different current
            # session owner must fail the real live SQL actor/segment join.
            with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
                ensure_test_identity(conn, identity_id="another-user")
                original_actor = conn.execute(select(jobs.c.actor_user_id).where(jobs.c.operation_id == request.operation_id)).scalar_one()
                assert original_actor == "alice"
                changed = conn.execute(
                    update(sessions)
                    .where(sessions.c.id == str(operation_store[4]), sessions.c.user_id == original_actor)
                    .values(user_id="another-user")
                )
                assert changed.rowcount == 1
        before = recovery_rows(operation_store[0])
        with pytest.raises((AuditIntegrityError, ComposerOperationFenceLost, SessionOperationFenceLost)):
            await ingress(operation_store, running, work, selected, request, **changes)
        assert recovery_rows(operation_store[0]) == before
        assert seed.reserved_user_message_id == str(result.message.id)
    finally:
        work.coordinator.assert_completed()
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_actual_two_locked_ingress_writers_only_one_reserved_cas_succeeds(operation_store, monkeypatch):
    running, work, selected, request, _seed = begin(operation_store)
    try:
        barrier = Barrier(2)
        original_begin = operation_store[3]._session_process_locked_begin

        def meet_before_actual_transaction(session_id):
            barrier.wait(timeout=10)
            return original_begin(session_id)

        monkeypatch.setattr(operation_store[3], "_session_process_locked_begin", meet_before_actual_transaction)
        values = await asyncio.gather(
            ingress(operation_store, running, work, selected, request),
            ingress(operation_store, running, work, selected, request),
            return_exceptions=True,
        )
        monkeypatch.setattr(operation_store[3], "_session_process_locked_begin", original_begin)
        successes = [value for value in values if type(value) is ContinuationIngressCheckpoint]
        failures = [value for value in values if isinstance(value, BaseException)]
        assert len(successes) == 1 and len(failures) == 1
        assert isinstance(failures[0], ContinuationCASConflict)
        current = operation_store[2].load_continuation_snapshot(running)
        before = recovery_rows(operation_store[0])
        assert (await ingress(operation_store, running, work, current, request)).disposition == "replayed"
        assert recovery_rows(operation_store[0]) == before
    finally:
        work.coordinator.assert_completed()
        operation_store[1].release(running.session_operation_context)


@pytest.mark.parametrize("change", ("title_attempt", "provider_locator", "title_pending", "ready"))
@pytest.mark.asyncio
async def test_structural_ingress_allowance_does_not_grant_title_or_other_provider_phases(operation_store, change):
    running, work, selected, request, _seed = begin(operation_store)
    try:
        produced = await ingress(operation_store, running, work, selected, request)
        selected = produced.snapshot
        root = decode_continuation_snapshot(selected.payload_json)
        policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
        actual = SelectedContinuationState(selected.checkpoint_id, root, ())
        assert next_continuation_action_policy(selected=actual, policy=policy) == policy.actions.session_sql
        frame = root.frames[0]
        chunks = ()
        if change == "title_attempt":
            frame = frame.model_copy(update={"title_attempt_id": "unconsumed-title-attempt"})
        elif change == "provider_locator":
            frame = frame.model_copy(update={"provider_effect_key": "provider:3:0", "provider_input_sha256": "a" * 64})
        else:
            receipt = owner.ContinuationIngressReceipt.model_validate_json(frame.ingress_receipt_json)
            bootstrap = encode_continuation_bootstrap(
                capture_continuation_bootstrap(
                    logical=root.logical,
                    actor_user_id="alice",
                    request=request,
                    records=produced.transcript,
                    initial_state=_initial_composition_state(),
                    recovery_context=GraphRecoveryContext(None, 0, None, 0, False, ()),
                    completion_gates=None,
                    retained_context=None,
                    session_title=receipt.original_session_title,
                    initial_schema_disclosure=(),
                )
            )
            frame = frame.model_copy(update={"ingress_phase": change, "bootstrap": bootstrap.reference})
            chunks = (bootstrap,)
        candidate = SelectedContinuationState(selected.checkpoint_id, root.model_copy(update={"frames": (frame,)}), chunks)
        candidate.__post_init__()
        expected_refusal = (
            "^next allowance lost its closed TITLE provider cut$"
            if change == "provider_locator"
            else "^next allowance has no closed reserved/inserted ingress cut$"
        )
        with pytest.raises(AuditIntegrityError, match=expected_refusal):
            next_continuation_action_policy(selected=candidate, policy=policy)
    finally:
        work.coordinator.assert_completed()
        operation_store[1].release(running.session_operation_context)
