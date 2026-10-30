"""Real released-owner recovery and new physical claims at committed P4 cuts.

These are deterministic release/recovery tests, not hard-process-death proofs.
Every selected state and P4 publication uses the actual existing SQL owners.
"""

import asyncio

import pytest
from sqlalchemy import select
from tests.helpers.continuation_inventory import inventory_advisor, inventory_work
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_owed_evidence import committed_evidence_case
from tests.unit.web.sessions.test_continuation_p4_owner import rows

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_evidence_restore import restore_turn_evidence
from elspeth.web.composer.continuation_owed_p4 import materialize_owed_p4_env_inventory
from elspeth.web.composer.continuation_p4_application_restore import apply_selected_p4_to_cold_evidence
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.coordination.repository import PostgresSessionOperationRepository
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.sessions.composer_operations import ComposerOperationFenceLost, ComposerOperationRunning
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import session_operation_fences_table as fences
from elspeth.web.sessions.service import SessionServiceImpl

operation_store = test_composer_operation_authority.operation_store


def recovery_rows_on_connection(conn):
    return tuple(
        tuple(conn.execute(select(table).order_by(*table.primary_key.columns)).all())
        for table in (jobs, segments, attempts, fences, checkpoints, events, effects, chunks)
    )


def recovery_rows(engine):
    with engine.connect() as conn:
        return recovery_rows_on_connection(conn)


def new_owners(store):
    engine, _repository, authority, sessions, sid = store
    repository = (
        SQLiteLocalSessionOperationAuthority(engine) if engine.dialect.name == "sqlite" else PostgresSessionOperationRepository(engine)
    )
    current_authority = ComposerAsyncOperationAuthority(
        engine,
        owner_instance_id=authority._owner_instance_id,
        claim_lease_seconds=authority._claim_lease_seconds,
        continuation_policy=authority._continuation_policy,
    )
    current_sessions = SessionServiceImpl(
        engine,
        sessions._data_dir,
        telemetry=sessions._telemetry,
        log=sessions._log,
        session_operation_authority=repository,
        owner_instance_id=authority._owner_instance_id,
    )
    assert repository is not store[1] and current_authority is not authority and current_sessions is not sessions
    return engine, repository, current_authority, current_sessions, sid


async def current_selected(store, running, *, effect_key=None):
    _engine, _repository, authority, sessions, _sid = store
    head = await asyncio.to_thread(authority.load_continuation_snapshot, running)
    work = inventory_work(running)
    selected = await load_selected_continuation(authority=authority, running=running, selected=head, required_work=work)
    work.coordinator.assert_completed()
    work = inventory_work(running)
    if effect_key is None:
        inventory = await materialize_continuation_env_inventory(
            advisor=inventory_advisor(sessions), authority=authority, selected=selected, required_work=work
        )
    else:
        inventory = await materialize_owed_p4_env_inventory(
            advisor=inventory_advisor(sessions), authority=authority, selected=selected, required_work=work, effect_key=effect_key
        )
    work.coordinator.assert_completed()
    return selected, inventory


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", ("pending_application", "applied_application"))
@pytest.mark.parametrize("handoff", ("release_then_recover", "live_handover"))
async def test_released_committed_p4_cut_restarts_through_real_recovery_claim_and_fence(operation_store, cut, handoff):
    engine, repository, authority, _sessions, sid = operation_store
    first, selected, inventory, _snapshot, commit = await committed_evidence_case(operation_store, graph=True, joint=True)
    first_released = False
    second = None
    current_store = new_owners(operation_store)
    _engine, current_repository, current_authority, _current_sessions, _sid = current_store
    try:
        if cut == "applied_application":
            work = inventory_work(first)
            await apply_selected_p4_to_cold_evidence(authority=authority, selected=selected, inventory=inventory, required_work=work)
            work.coordinator.assert_completed()
        before = authority.load_continuation_snapshot(first)
        root_before = decode_continuation_snapshot(before.payload_json)
        business = rows(engine)
        if handoff == "live_handover":
            # The live owner hands over in one transaction (design §3.4 B); the
            # landed P4 owner must read it exactly as it reads a recovery.
            repository.suspend_composer_segment(first)
            first_released = True
        else:
            repository.release(first.session_operation_context)
            first_released = True
            assert (
                current_repository.recover_composer_segment(
                    session_id=sid,
                    operation_id=first.claim.operation_id,
                    expected_generation=first.claim.ownership_generation,
                    actor_user_id="alice",
                    auth_provider_type="local",
                )
                is True
            )
        after_recovery = recovery_rows(engine)
        assert (
            current_repository.recover_composer_segment(
                session_id=sid,
                operation_id=first.claim.operation_id,
                expected_generation=first.claim.ownership_generation,
                actor_user_id="alice",
                auth_provider_type="local",
            )
            is False
        )
        assert recovery_rows(engine) == after_recovery
        (claim,) = current_authority.claim_segment(limit=1)
        context = current_repository.start_composer_segment(
            claim, owner_instance_id=current_authority._owner_instance_id, lease_seconds=30, auth_provider_type="local"
        )
        second = ComposerOperationRunning(claim=claim, session_operation_context=context)
        assert claim.segment_sequence == first.claim.segment_sequence
        assert claim.ownership_generation == first.claim.ownership_generation + 1
        assert claim.attempt == first.claim.attempt + 1 and claim.claim_token != first.claim.claim_token
        assert context.fence.operation_id != first.session_operation_context.fence.operation_id
        assert context.fence.operation_epoch > first.session_operation_context.fence.operation_epoch
        assert context.fence.lease_token != first.session_operation_context.fence.lease_token
        with pytest.raises((ComposerOperationFenceLost, SessionOperationFenceLost)):
            current_authority.load_continuation_snapshot(first)
        if cut == "pending_application":
            selected, inventory = await current_selected(current_store, second, effect_key=commit.effect_key)
            work = inventory_work(second)
            application = await apply_selected_p4_to_cold_evidence(
                authority=current_authority, selected=selected, inventory=inventory, required_work=work
            )
            work.coordinator.assert_completed()
            restored = application.restored
        else:
            selected, inventory = await current_selected(current_store, second)
            work = inventory_work(second)
            restored = await restore_turn_evidence(authority=current_authority, selected=selected, inventory=inventory, required_work=work)
            work.coordinator.assert_completed()
        assert rows(engine) == business
        assert restored.evidence.operation_id == context.fence.operation_id
        assert restored.batch is restored.evidence._portable_p4_graph.selected
        assert restored.evidence.effects is restored.evidence._portable_p4_graph.effects
        assert len(restored.applications.applications) == 1
        original = restored.applications.applications[0]
        assert original.effect_key == commit.effect_key
        assert original.creator_generation == first.claim.ownership_generation
        assert original.creator_operation_id == first.session_operation_context.fence.operation_id
        assert original.creator_operation_epoch == first.session_operation_context.fence.operation_epoch
        final = decode_continuation_snapshot(current_authority.load_continuation_snapshot(second).payload_json)
        assert not final.pending_owner_results
        assert final.effect_frontier == root_before.effect_frontier
        assert final.pending_settlement_ids == root_before.pending_settlement_ids
        assert final.logical == root_before.logical and final.policy_hash == root_before.policy_hash
        assert final.frames[0].iteration == root_before.frames[0].iteration
        with engine.connect() as conn:
            history = conn.execute(select(attempts).order_by(attempts.c.sequence, attempts.c.generation)).all()
            actual_effect = conn.execute(select(effects).where(effects.c.effect_key == commit.effect_key)).one()
        assert len(history) == 2
        assert history[0].revoked_at is not None and history[0].outcome == "interrupted"
        assert history[1].revoked_at is None and history[1].claim_attempt == claim.attempt
        assert history[1].session_operation_id == context.fence.operation_id
        assert actual_effect.creator_generation == history[0].generation
        assert actual_effect.creator_sequence == history[0].sequence
    finally:
        if second is not None:
            current_repository.release(second.session_operation_context)
        elif not first_released:
            repository.release(first.session_operation_context)


@pytest.mark.asyncio
async def test_recovery_refuses_live_owner_and_wrong_generation_without_writes(operation_store):
    engine, repository, _authority, _sessions, sid = operation_store
    running, _selected, _inventory, _snapshot, _commit = await committed_evidence_case(operation_store, graph=True, joint=True)
    try:
        current_repository = new_owners(operation_store)[1]
        before = recovery_rows(engine), rows(engine)
        for generation in (running.claim.ownership_generation, running.claim.ownership_generation + 1):
            assert (
                current_repository.recover_composer_segment(
                    session_id=sid,
                    operation_id=running.claim.operation_id,
                    expected_generation=generation,
                    actor_user_id="alice",
                    auth_provider_type="local",
                )
                is False
            )
            assert (recovery_rows(engine), rows(engine)) == before
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_recovery_preserves_refusal_for_foreign_prepared_uncommitted_business_sql(operation_store):
    engine, repository, _authority, _sessions, sid = operation_store
    running, _selected, _inventory, _snapshot, _commit = await committed_evidence_case(
        operation_store, graph=True, joint=True, persist_result=False
    )
    repository.release(running.session_operation_context)
    current_repository = new_owners(operation_store)[1]
    before = recovery_rows(engine), rows(engine)
    with pytest.raises(AuditIntegrityError, match="prepared or unknown business effect"):
        current_repository.recover_composer_segment(
            session_id=sid,
            operation_id=running.claim.operation_id,
            expected_generation=running.claim.ownership_generation,
            actor_user_id="alice",
            auth_provider_type="local",
        )
    assert (recovery_rows(engine), rows(engine)) == before
