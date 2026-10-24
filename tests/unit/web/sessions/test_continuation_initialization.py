"""Real inserted SendMessage -> atomic pre-title bootstrap; no provider work."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer import continuation_initialization as initializer
from elspeth.web.composer.continuation import prepare_composer_continuation_admission
from elspeth.web.composer.continuation_bootstrap import bootstrap_values, decode_continuation_bootstrap
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_next_action import next_continuation_action_policy
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.execution.completion_gates import CompletionGateFacts, completion_gates_meta_from_facts
from elspeth.web.sessions import continuation_bootstrap_source as sql_owner
from elspeth.web.sessions.composer_operations import (
    ComposerOperationFenceLost,
    ComposerOperationRunning,
    composer_operation_v2_request_hash,
)
from elspeth.web.sessions.continuation import ContinuationCASConflict
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.schemas import SendMessageRequest
from elspeth.web.sessions.titles import format_default_session_title
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.continuation_inventory import inventory_work
from tests.helpers.continuation_policy import minimal_continuation_policy
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.coordination.test_continuation_p4_recovery_start import new_owners
from tests.unit.web.sessions.test_continuation_cold_restore import start_continuation
from tests.unit.web.sessions.test_continuation_ingress import begin, ingress, mutate_root, recovery_rows
from tests.unit.web.sessions.test_continuation_prompt_producers import current_service

operation_store = operations.operation_store


async def bootstrap_case(store, *, eligible=True, bound_base=False):
    engine, repository, authority, sessions, sid = store
    # An actual settled prior job, read by the real bounded history owner.
    prior, _ = operations.admit(authority, sid)
    authority.request_cancel(session_id=sid, operation_id=prior.operation_id, cancelled_failure=operations.cancelled)
    base = None
    if bound_base:
        seed_context = repository.acquire(
            session_id=sid, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
        )
        try:
            await sessions.add_message(
                sid, "user", "Earlier authenticated request", writer_principal="route_user_message", session_operation_context=seed_context
            )
            await sessions.add_message(
                sid,
                "assistant",
                "Earlier raw assistant text\n\nEarlier displayed assistant text",
                raw_content="Earlier raw assistant text",
                writer_principal="compose_loop",
                session_operation_context=seed_context,
            )
            base = await sessions.save_composition_state(
                sid,
                CompositionStateData(
                    metadata_=sql_owner._initial_composition_state().to_dict()["metadata"],
                    composer_meta={"completion_gates": completion_gates_meta_from_facts(CompletionGateFacts(None))},
                ),
                provenance="session_seed",
                session_operation_context=seed_context,
            )
        finally:
            repository.release(seed_context)
    title = format_default_session_title(datetime(2026, 10, 24, tzinfo=UTC)) if eligible else "Operator Supplied Name"
    with engine.begin() as conn:
        assert conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(title=title)).rowcount == 1
        trust_mode = conn.execute(select(sessions_table.c.trust_mode).where(sessions_table.c.id == str(sid))).scalar_one()
    request = SendMessageRequest(
        operation_id=str(uuid4()), content="Preserve café and original context", state_id=base.id if base is not None else None
    )
    policy = minimal_continuation_policy()
    seed = prepare_composer_continuation_admission(
        session_id=sid,
        operation_id=request.operation_id,
        kind="compose_message",
        request_json=request.model_dump_json(),
        request_hash=composer_operation_v2_request_hash(session_id=sid, kind="compose_message", request=request),
        base_state_id=request.state_id,
        policy_hash=policy.policy_hash,
        admission_policy_json=policy.model_dump_json(),
        initial_action_json=policy.actions.session_sql.model_dump_json(),
        initial_trust_mode=trust_mode,
    )
    _record, fresh = operations.admit(authority, sid, request, lifecycle_version=2, continuation_enabled=True, seed=seed)
    assert fresh
    running = start_continuation(repository, authority)
    selected = authority.load_continuation_snapshot(running)
    work = inventory_work(running)
    completed = await ingress(store, running, work, selected, request)
    work.coordinator.assert_completed()
    service, _actors = current_service(store)
    return running, completed, service, prior, base


async def publish(store, running, completed, service, work=None):
    work = inventory_work(running) if work is None else work
    result = await initializer.prepare_pretitle_bootstrap(
        service=service, authority=store[2], selected=completed.snapshot, required_work=work
    )
    work.coordinator.assert_completed()
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("eligible,bound_base", ((True, False), (False, True)))
async def test_actual_pretitle_bootstrap_is_complete_atomic_and_replays_without_preparation(
    operation_store, monkeypatch, eligible, bound_base
):
    running, completed, service, prior, base = await bootstrap_case(operation_store, eligible=eligible, bound_base=bound_base)
    sid = str(operation_store[4])
    # Seed the real in-memory disclosure owner as retained session DATA; this
    # test claims no new schema inspection/provider grant from that DATA.
    service._schema_disclosure.mark_plugin_schema_loaded(sid, "source", "csv")
    try:
        original = decode_continuation_snapshot(completed.snapshot.payload_json)
        with operation_store[0].connect() as conn:
            before_humans = tuple(conn.execute(select(messages).where(messages.c.session_id == sid)).all())
            before_physical = tuple(conn.execute(select(physical)).all())
            before_effects = tuple(conn.execute(select(effects)).all())
        result = await publish(operation_store, running, completed, service)
        root = decode_continuation_snapshot(result.selected.payload_json)
        snapshot = decode_continuation_bootstrap(result.bootstrap)
        values = bootstrap_values(snapshot)
        assert root.cursor == original.cursor + 1
        assert root.frames[0].ingress_phase == ("title_pending" if eligible else "ready")
        assert root.frames[0].bootstrap == result.bootstrap.reference
        assert root.frames[0].ingress_receipt_json == original.frames[0].ingress_receipt_json
        assert root.logical == original.logical and root.mutation_ceiling == original.mutation_ceiling
        assert root.provider_cost_policy == original.provider_cost_policy and root.admission_policy_json == original.admission_policy_json
        assert not root.effect_frontier and not root.pending_settlement_ids and not root.pending_owner_results
        assert values.records == completed.transcript and snapshot.original_last_sequence == completed.message.sequence_no
        assert snapshot.has_partial_turn is False
        if bound_base:
            assert len(values.history) == 2 and "Earlier raw assistant text" in snapshot.history_json
            assert values.records[1].raw_content == "Earlier raw assistant text"
        else:
            assert values.history == ()
        assert values.request.content == completed.message.content and snapshot.actor_user_id == "alice"
        assert snapshot.auto_title_eligible is eligible and snapshot.initial_schema_disclosure == (("source", "csv"),)
        assert values.retained_context is not None and values.retained_context.availability == "available"
        assert [item.operation_id for item in values.retained_context.outcomes] == [prior.operation_id]
        assert values.recovery_context.turn_start_state_id == (str(base.id) if base is not None else None)
        assert values.completion_gates == (CompletionGateFacts(None) if bound_base else None)
        assert service._skill_markdown_history_upserted is True
        with operation_store[0].connect() as conn:
            assert tuple(conn.execute(select(messages).where(messages.c.session_id == sid)).all()) == before_humans
            assert tuple(conn.execute(select(physical)).all()) == before_physical
            assert tuple(conn.execute(select(effects)).all()) == before_effects
        policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
        assert (
            next_continuation_action_policy(
                selected=SelectedContinuationState(result.selected.checkpoint_id, root, (result.bootstrap,)), policy=policy
            )
            == policy.actions.session_sql
        )

        async def unexpected(*_args, **_kwargs):
            raise AssertionError("published bootstrap repeated source preparation")

        monkeypatch.setattr(initializer, "_read_retained_operation_context", unexpected)
        monkeypatch.setattr(initializer, "materialize_continuation_env_inventory", unexpected)
        monkeypatch.setattr(service, "_maybe_upsert_skill_markdown_history", unexpected)
        before = recovery_rows(operation_store[0])
        replay_work = inventory_work(running)
        replay = await initializer.prepare_pretitle_bootstrap(
            service=service, authority=operation_store[2], selected=result.selected, required_work=replay_work
        )
        replay_work.coordinator.assert_completed()
        assert replay == result and recovery_rows(operation_store[0]) == before
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("eligible", (True, False))
@pytest.mark.parametrize("cut", ("inserted", "published"))
async def test_published_bootstrap_fresh_claim_uses_original_history_and_refuses_old_owner(operation_store, monkeypatch, eligible, cut):
    running, completed, service, _prior, _base = await bootstrap_case(operation_store, eligible=eligible)
    result = await publish(operation_store, running, completed, service) if cut == "published" else None
    operation_store[1].release(running.session_operation_context)
    current = new_owners(operation_store)
    engine, repository, authority, _sessions, sid = current
    assert (
        repository.recover_composer_segment(
            session_id=sid,
            operation_id=running.claim.operation_id,
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
    current_running = ComposerOperationRunning(claim=claim, session_operation_context=context)
    current_service_owner, _actors = current_service(current)
    try:

        async def unexpected(*_args, **_kwargs):
            raise AssertionError("fresh claim repeated pre-title source preparation")

        if cut == "published":
            monkeypatch.setattr(initializer, "_read_retained_operation_context", unexpected)
            monkeypatch.setattr(initializer, "materialize_continuation_env_inventory", unexpected)
            monkeypatch.setattr(current_service_owner, "_maybe_upsert_skill_markdown_history", unexpected)
        selected = authority.load_continuation_snapshot(current_running)
        before = recovery_rows(engine)
        work = inventory_work(current_running)
        restored = await initializer.prepare_pretitle_bootstrap(
            service=current_service_owner, authority=authority, selected=selected, required_work=work
        )
        work.coordinator.assert_completed()
        if result is not None:
            assert restored.bootstrap == result.bootstrap and restored.selected == selected
            original_root = decode_continuation_snapshot(result.selected.payload_json)
            assert selected.cursor == result.selected.cursor + 1
            assert selected.payload_json == encode_continuation_snapshot(original_root.model_copy(update={"cursor": selected.cursor}))
            assert recovery_rows(engine) == before
        else:
            restored_values = bootstrap_values(decode_continuation_bootstrap(restored.bootstrap))
            assert restored_values.records == completed.transcript
            assert selected.cursor == completed.snapshot.cursor + 1
            assert restored.selected.cursor == selected.cursor + 1
        assert context.fence != running.session_operation_context.fence
        old_work = inventory_work(running)
        before_old = recovery_rows(engine)
        with pytest.raises((AuditIntegrityError, SessionOperationFenceLost, ComposerOperationFenceLost, ContinuationCASConflict)):
            await initializer.prepare_pretitle_bootstrap(
                service=service, authority=operation_store[2], selected=restored.selected, required_work=old_work
            )
        old_work.coordinator.assert_completed()
        assert recovery_rows(engine) == before_old
    finally:
        repository.release(context)


@pytest.mark.asyncio
async def test_pretitle_rejects_reserved_or_forged_current_root_before_any_preparation(operation_store):
    running, work, selected, request, _seed = begin(operation_store)
    service, _actors = current_service(operation_store)
    try:
        before = recovery_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match="completed inserted ingress"):
            await initializer.prepare_pretitle_bootstrap(
                service=service, authority=operation_store[2], selected=selected, required_work=work
            )
        assert recovery_rows(operation_store[0]) == before
        completed = await ingress(operation_store, running, work, selected, request)
        good = await publish(operation_store, running, completed, service)
        root = decode_continuation_snapshot(good.selected.payload_json)
        forged = replace(
            good.selected,
            payload_json=encode_continuation_snapshot(
                root.model_copy(update={"logical": root.logical.model_copy(update={"request_hash": "f" * 64})})
            ),
        )
        before = recovery_rows(operation_store[0])
        with pytest.raises(ContinuationCASConflict, match="complete selected CAS"):
            await initializer.prepare_pretitle_bootstrap(service=service, authority=operation_store[2], selected=forged, required_work=work)
        assert recovery_rows(operation_store[0]) == before
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("title", "disclosure", "skill"))
async def test_pretitle_rechecks_real_source_across_preparation_awaits(operation_store, monkeypatch, fault):
    running, completed, service, _prior, _base = await bootstrap_case(operation_store)
    await service._maybe_upsert_skill_markdown_history()
    original_publish = initializer._publish
    after_fault = []

    def changed_source(actual_service, actual_running, prepared):
        assert prepared.source.selected == completed.snapshot
        assert bootstrap_values(decode_continuation_bootstrap(prepared.bootstrap)).retained_context is not None
        if fault == "title":
            with operation_store[0].begin() as conn:
                assert (
                    conn.execute(
                        update(sessions_table)
                        .where(sessions_table.c.id == str(operation_store[4]))
                        .values(title="Changed after preparation")
                    ).rowcount
                    == 1
                )
        elif fault == "disclosure":
            service._schema_disclosure.mark_plugin_schema_loaded(str(operation_store[4]), "source", "csv")
        else:
            service._composer_skill_hash = "f" * 64
        after_fault.append(recovery_rows(operation_store[0]))
        return original_publish(actual_service, actual_running, prepared)

    monkeypatch.setattr(initializer, "_publish", changed_source)
    work = inventory_work(running)
    try:
        with pytest.raises(AuditIntegrityError, match=r"changed session title|disclosure/skill source changed"):
            await initializer.prepare_pretitle_bootstrap(
                service=service, authority=operation_store[2], selected=completed.snapshot, required_work=work
            )
        assert len(after_fault) == 1 and recovery_rows(operation_store[0]) == after_fault[0]
        assert operation_store[2].load_continuation_snapshot(running) == completed.snapshot
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_pretitle_keeps_original_required_history_failure_without_publication(operation_store, monkeypatch):
    running, completed, service, _prior, _base = await bootstrap_case(operation_store)
    original = RuntimeError("actual bounded history read failed")

    def fail_history(**_kwargs):
        raise original

    monkeypatch.setattr(operation_store[2], "list_failure_outcomes", fail_history)
    work = inventory_work(running)
    before = recovery_rows(operation_store[0])
    try:
        with pytest.raises(RuntimeError) as caught:
            await initializer.prepare_pretitle_bootstrap(
                service=service, authority=operation_store[2], selected=completed.snapshot, required_work=work
            )
        assert caught.value is original and recovery_rows(operation_store[0]) == before
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_pretitle_keeps_original_skill_archive_failure_and_closes_its_producer(operation_store, monkeypatch):
    running, completed, service, _prior, _base = await bootstrap_case(operation_store)
    original = RuntimeError("actual skill archive failure")

    actual_archive = operation_store[3]._skill_markdown_history_authority

    def fail_archive(actual, **_kwargs):
        assert actual is actual_archive
        raise original

    monkeypatch.setattr(type(actual_archive), "upsert_exact", fail_archive)
    work = inventory_work(running)
    before = recovery_rows(operation_store[0])
    try:
        with pytest.raises(RuntimeError) as caught:
            await initializer.prepare_pretitle_bootstrap(
                service=service, authority=operation_store[2], selected=completed.snapshot, required_work=work
            )
        assert caught.value is original and recovery_rows(operation_store[0]) == before
        assert service._skill_markdown_history_upserted is False
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_pretitle_cancel_joins_original_skill_preparation_without_publishing(operation_store, monkeypatch):
    running, completed, service, _prior, _base = await bootstrap_case(operation_store)
    entered, release = asyncio.Event(), asyncio.Event()
    original_prepare = service._maybe_upsert_skill_markdown_history
    original_join = initializer._join_freeform_owned_task
    actual_cancellations = []

    async def hold_original_preparation():
        await original_prepare()
        entered.set()
        await release.wait()

    async def observe_join(task, *, cancellation_observations):
        result = await original_join(task, cancellation_observations=cancellation_observations)
        actual_cancellations.extend(cancellation_observations)
        return result

    monkeypatch.setattr(service, "_maybe_upsert_skill_markdown_history", hold_original_preparation)
    monkeypatch.setattr(initializer, "_join_freeform_owned_task", observe_join)
    work = inventory_work(running)
    task = asyncio.create_task(
        initializer.prepare_pretitle_bootstrap(
            service=service, authority=operation_store[2], selected=completed.snapshot, required_work=work
        )
    )
    try:
        async with asyncio.timeout(5):
            await entered.wait()
        # The original archive has completed; this snapshot includes its real
        # independently owned row, never filtered out of the failure oracle.
        before = recovery_rows(operation_store[0])
        task.cancel("caller cancellation during joined original skill preparation")
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert len(actual_cancellations) == 1 and caught.value is actual_cancellations[0]
        assert operation_store[2].load_continuation_snapshot(running) == completed.snapshot
        assert recovery_rows(operation_store[0]) == before
        work.coordinator.assert_completed()
    finally:
        release.set()
        if not task.done():
            await task
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_pretitle_publication_rolls_back_chunk_event_root_and_preserves_original_failure(operation_store, monkeypatch):
    running, completed, service, _prior, _base = await bootstrap_case(operation_store)
    await service._maybe_upsert_skill_markdown_history()
    original_append = sql_owner.append_complete_snapshot
    original = RuntimeError("failure after actual complete append")
    appended = []

    def fail_after_append(*args, **kwargs):
        appended.append(original_append(*args, **kwargs))
        raise original

    monkeypatch.setattr(sql_owner, "append_complete_snapshot", fail_after_append)
    work = inventory_work(running)
    before = recovery_rows(operation_store[0])
    try:
        with pytest.raises(RuntimeError) as caught:
            await initializer.prepare_pretitle_bootstrap(
                service=service, authority=operation_store[2], selected=completed.snapshot, required_work=work
            )
        assert caught.value is original and len(appended) == 1
        assert appended[0].cursor == completed.snapshot.cursor + 1
        assert recovery_rows(operation_store[0]) == before
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_pretitle_cancel_joins_actual_publication_and_replay_does_not_repeat_it(operation_store, monkeypatch):
    running, completed, service, _prior, _base = await bootstrap_case(operation_store)
    entered, release = Event(), Event()
    original_append = sql_owner.append_complete_snapshot
    original_finish = initializer.run_required_sql_finish_once
    actual_cancellations = []

    def hold_after_append(*args, **kwargs):
        result = original_append(*args, **kwargs)
        entered.set()
        assert release.wait(5), "test did not release actual publication transaction"
        return result

    async def observe_finish(*args, **kwargs):
        outcome = await original_finish(*args, **kwargs)
        actual_cancellations.extend(outcome.deferred_cancellations)
        return outcome

    monkeypatch.setattr(sql_owner, "append_complete_snapshot", hold_after_append)
    monkeypatch.setattr(initializer, "run_required_sql_finish_once", observe_finish)
    work = inventory_work(running)
    task = asyncio.create_task(
        initializer.prepare_pretitle_bootstrap(
            service=service, authority=operation_store[2], selected=completed.snapshot, required_work=work
        )
    )
    try:
        await operations._wait_for_thread_event(entered)
        task.cancel("caller cancellation at actual publication")
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert len(actual_cancellations) == 1 and caught.value is actual_cancellations[0]
        work.coordinator.assert_completed()
        selected = operation_store[2].load_continuation_snapshot(running)
        assert selected.cursor == completed.snapshot.cursor + 1
        before = recovery_rows(operation_store[0])
        replay_work = inventory_work(running)
        replay = await initializer.prepare_pretitle_bootstrap(
            service=service, authority=operation_store[2], selected=selected, required_work=replay_work
        )
        replay_work.coordinator.assert_completed()
        assert replay.selected == selected and recovery_rows(operation_store[0]) == before
    finally:
        release.set()
        if not task.done():
            await task
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_pretitle_two_actual_preparations_have_one_publication_cas(operation_store, monkeypatch):
    running, completed, service, _prior, _base = await bootstrap_case(operation_store)
    gate = Barrier(2)
    original_publish = initializer._publish

    def race_publish(*args, **kwargs):
        gate.wait(timeout=5)
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(initializer, "_publish", race_publish)
    works = [inventory_work(running), inventory_work(running)]
    try:
        results = await asyncio.gather(
            *(
                initializer.prepare_pretitle_bootstrap(
                    service=service, authority=operation_store[2], selected=completed.snapshot, required_work=work
                )
                for work in works
            ),
            return_exceptions=True,
        )
        assert sum(type(result) is sql_owner.StoredPretitleBootstrap for result in results) == 1
        assert sum(isinstance(result, ContinuationCASConflict) for result in results) == 1
        assert operation_store[2].load_continuation_snapshot(running).cursor == completed.snapshot.cursor + 1
        for work in works:
            work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ("title_attempt", "provider", "title_drift", "repeated_inserted", "foreign_event", "actor", "receipt", "missing_chunk")
)
async def test_published_pretitle_read_refuses_unowned_later_cuts(operation_store, fault):
    running, completed, service, _prior, _base = await bootstrap_case(operation_store)
    result = await publish(operation_store, running, completed, service)
    root = decode_continuation_snapshot(result.selected.payload_json)
    frame = root.frames[0]
    try:
        if fault == "title_drift":
            with operation_store[0].begin() as conn:
                assert (
                    conn.execute(
                        update(sessions_table).where(sessions_table.c.id == str(operation_store[4])).values(title="Later title owner")
                    ).rowcount
                    == 1
                )
            selected = result.selected
        elif fault == "actor":
            with operation_store[0].begin() as conn:
                ensure_test_identity(conn, identity_id="other-actor")
                assert (
                    conn.execute(
                        update(sessions_table).where(sessions_table.c.id == str(operation_store[4])).values(user_id="other-actor")
                    ).rowcount
                    == 1
                )
            selected = result.selected
        else:
            if fault == "title_attempt":
                frame = frame.model_copy(update={"title_attempt_id": "unowned-title-attempt"})
            elif fault == "provider":
                frame = frame.model_copy(update={"provider_effect_key": "provider:3:0", "provider_input_sha256": "a" * 64})
            elif fault == "repeated_inserted":
                frame = frame.model_copy(update={"ingress_phase": "inserted", "bootstrap": None})
            elif fault == "receipt":
                receipt = sql_owner._receipt(frame.ingress_receipt_json)
                changed_receipt = receipt.model_copy(update={"transcript_sha256": "f" * 64})
                frame = frame.model_copy(update={"ingress_receipt_json": canonical_json(changed_receipt.model_dump(mode="json"))})
            elif fault == "missing_chunk":
                frame = frame.model_copy(update={"bootstrap": frame.bootstrap.model_copy(update={"digest": "f" * 64})})
            selected = mutate_root(operation_store, running, result.selected, root.model_copy(update={"frames": (frame,)}))
        before = recovery_rows(operation_store[0])
        work = inventory_work(running)
        with pytest.raises((AuditIntegrityError, SessionOperationFenceLost, ComposerOperationFenceLost)):
            await initializer.prepare_pretitle_bootstrap(
                service=service, authority=operation_store[2], selected=selected, required_work=work
            )
        assert recovery_rows(operation_store[0]) == before
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


def _reclaim_pretitle(store, running):
    store[1].release(running.session_operation_context)
    current = new_owners(store)
    assert (
        current[1].recover_composer_segment(
            session_id=current[4],
            operation_id=running.claim.operation_id,
            expected_generation=running.claim.ownership_generation,
            actor_user_id="alice",
            auth_provider_type="local",
        )
        is True
    )
    (claim,) = current[2].claim_segment(limit=1)
    context = current[1].start_composer_segment(
        claim, owner_instance_id=current[2]._owner_instance_id, lease_seconds=30, auth_provider_type="local"
    )
    return current, ComposerOperationRunning(claim=claim, session_operation_context=context)


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", ("inserted", "published"))
async def test_pretitle_multiple_actual_reclaims_preserve_complete_original_cut(operation_store, cut):
    running, completed, service, _prior, _base = await bootstrap_case(operation_store)
    original = await publish(operation_store, running, completed, service) if cut == "published" else None
    current = operation_store
    try:
        for _ in range(2):
            current, running = _reclaim_pretitle(current, running)
        selected = current[2].load_continuation_snapshot(running)
        anchor = completed.snapshot if original is None else original.selected
        assert selected.cursor == anchor.cursor + 2
        anchor_root = decode_continuation_snapshot(anchor.payload_json)
        assert selected.payload_json == encode_continuation_snapshot(anchor_root.model_copy(update={"cursor": selected.cursor}))
        current_service_owner, _actors = current_service(current)
        work = inventory_work(running)
        before = recovery_rows(current[0])
        result = await initializer.prepare_pretitle_bootstrap(
            service=current_service_owner, authority=current[2], selected=selected, required_work=work
        )
        work.coordinator.assert_completed()
        if original is not None:
            assert result.bootstrap == original.bootstrap and result.selected == selected
            assert recovery_rows(current[0]) == before
        else:
            assert result.selected.cursor == selected.cursor + 1
            assert bootstrap_values(decode_continuation_bootstrap(result.bootstrap)).records == completed.transcript
            # Publication after earlier reclaim is itself readable under a later
            # real claim; both historical suffixes must be checked separately.
            current, running = _reclaim_pretitle(current, running)
            selected = current[2].load_continuation_snapshot(running)
            current_service_owner, _actors = current_service(current)
            work = inventory_work(running)
            before = recovery_rows(current[0])
            replay = await initializer.prepare_pretitle_bootstrap(
                service=current_service_owner, authority=current[2], selected=selected, required_work=work
            )
            work.coordinator.assert_completed()
            assert replay.bootstrap == result.bootstrap and replay.selected == selected
            assert recovery_rows(current[0]) == before
    finally:
        current[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ("event_generation", "event_sequence", "event_extra", "publication_creator", "recovery_root", "inserted_root")
)
async def test_reclaimed_pretitle_checks_actual_event_creator_and_complete_predecessors(operation_store, monkeypatch, fault):
    running, completed, service, _prior, _base = await bootstrap_case(operation_store)
    published = await publish(operation_store, running, completed, service)
    current, running = _reclaim_pretitle(operation_store, running)
    current_service_owner, _actors = current_service(current)
    selected = current[2].load_continuation_snapshot(running)
    generation, sequence = running.claim.ownership_generation, running.claim.segment_sequence
    assert type(generation) is int and type(sequence) is int
    original_read = sql_owner.read_chunk
    reads = []
    armed = False

    def observe_read(conn, *, session_id, operation_id, key):
        actual = original_read(conn, session_id=session_id, operation_id=operation_id, key=key)
        if not armed:
            return actual
        if fault.startswith("event_") and key == f"event:{selected.cursor}":
            value = {
                "version": 2,
                "same_sequence": sequence,
                "previous_generation": generation - 1,
                "generation": generation,
            }
            assert actual == canonical_json(value).encode("utf-8")
            if fault == "event_generation":
                value["generation"] += 1
            elif fault == "event_sequence":
                value["same_sequence"] += 1
            else:
                value["unowned"] = 1
            reads.append(key)
            return canonical_json(value).encode("utf-8")
        if fault == "publication_creator" and key == f"event:{published.selected.cursor}":
            original_value = sql_owner.PretitleBootstrapPublication.model_validate_json(
                canonical_json(json.loads(actual)["publication"]), strict=True
            )
            changed = original_value.model_copy(
                update={
                    "creator_session_operation_id": running.session_operation_context.fence.operation_id,
                    "creator_session_operation_epoch": running.session_operation_context.fence.operation_epoch,
                }
            )
            assert changed != original_value
            reads.append(key)
            return sql_owner._event(changed).encode("utf-8")
        expected_key = (
            f"snapshot:{published.selected.checkpoint_id}" if fault == "recovery_root" else f"snapshot:{completed.snapshot.checkpoint_id}"
        )
        if fault in ("recovery_root", "inserted_root") and key == expected_key:
            root = decode_continuation_snapshot(actual.decode("utf-8"))
            frame = root.frames[0].model_copy(update={"request_json": "{}"})
            changed = encode_continuation_snapshot(root.model_copy(update={"frames": (frame,)})).encode("utf-8")
            assert changed != actual
            reads.append(key)
            return changed
        return actual

    # Explicit read-boundary fault after the actual immutable chunk owner has
    # returned; no protected SQL row/trigger is rewritten and no claim is forged.
    monkeypatch.setattr(sql_owner, "read_chunk", observe_read)
    try:
        good_work = inventory_work(running)
        good = await initializer.prepare_pretitle_bootstrap(
            service=current_service_owner, authority=current[2], selected=selected, required_work=good_work
        )
        good_work.coordinator.assert_completed()
        assert good.bootstrap == published.bootstrap and good.selected == selected
        armed = True
        before = recovery_rows(current[0])
        work = inventory_work(running)
        expected = {
            "event_generation": "exact claim-rebinding event",
            "event_sequence": "exact claim-rebinding event",
            "event_extra": "exact claim-rebinding event",
            "publication_creator": "predecessor/creator lineage",
            "recovery_root": "retained source beyond its cursor",
            "inserted_root": "complete actual inserted predecessor",
        }[fault]
        with pytest.raises(AuditIntegrityError, match=expected):
            await initializer.prepare_pretitle_bootstrap(
                service=current_service_owner, authority=current[2], selected=selected, required_work=work
            )
        work.coordinator.assert_completed()
        assert reads and recovery_rows(current[0]) == before
    finally:
        current[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_pretitle_refuses_duplicate_recovery_event_without_another_actual_claim(operation_store):
    running, completed, service, _prior, _base = await bootstrap_case(operation_store)
    published = await publish(operation_store, running, completed, service)
    current, running = _reclaim_pretitle(operation_store, running)
    service, _actors = current_service(current)
    selected = current[2].load_continuation_snapshot(running)
    generation = running.claim.ownership_generation
    assert type(generation) is int
    try:
        work = inventory_work(running)
        positive = await initializer.prepare_pretitle_bootstrap(
            service=service, authority=current[2], selected=selected, required_work=work
        )
        work.coordinator.assert_completed()
        assert positive.bootstrap == published.bootstrap and positive.selected == selected
        with initializer.locked_session_transaction(current[0], str(current[4])) as conn:
            forged = sql_owner.append_complete_snapshot(
                conn,
                session_id=str(current[4]),
                operation_id=running.claim.operation_id,
                expected_checkpoint_id=selected.checkpoint_id,
                expected_cursor=selected.cursor,
                snapshot_json=selected.payload_json,
                event_batch=(
                    sql_owner.ContinuationEvent(
                        "recovered",
                        canonical_json(
                            {
                                "version": 2,
                                "same_sequence": running.claim.segment_sequence,
                                "previous_generation": generation - 1,
                                "generation": generation,
                            }
                        ),
                    ),
                ),
                handoff=False,
            )
        before = recovery_rows(current[0])
        work = inventory_work(running)
        with pytest.raises(AuditIntegrityError, match="preceding actual generation"):
            await initializer.prepare_pretitle_bootstrap(service=service, authority=current[2], selected=forged, required_work=work)
        work.coordinator.assert_completed()
        assert recovery_rows(current[0]) == before
    finally:
        current[1].release(running.session_operation_context)
