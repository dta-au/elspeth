"""Finite real recovery boundaries; deterministic expiry is not process death."""

import asyncio
from datetime import timedelta
from hashlib import sha256
from threading import Barrier

import pytest
from sqlalchemy import select, update
from tests.helpers.continuation_inventory import inventory_advisor, inventory_work
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.coordination.test_continuation_p4_recovery_start import current_selected, new_owners, recovery_rows
from tests.unit.web.sessions.test_continuation_owed_evidence import committed_evidence_case
from tests.unit.web.sessions.test_continuation_p4_owner import rows

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_evidence_restore import restore_turn_evidence
from elspeth.web.composer.continuation_p4_application_restore import apply_selected_p4_to_cold_evidence
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.composer_operations import (
    ComposerOperationCancelledDuringTurn,
    ComposerOperationFenceLost,
    ComposerOperationRunning,
)
from elspeth.web.sessions.continuation import ContinuationCASConflict
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import session_operation_fences_table as fences
from elspeth.web.sessions.models import web_instances_table as instances
from elspeth.web.sessions.service import SessionServiceImpl

operation_store = operations.operation_store


def peer_owners(store):
    engine, repository, old_authority, sessions, sid = new_owners(store)
    peer = "restart-boundary-peer"
    if engine.dialect.name == "postgresql":
        from tests.testcontainer.web.test_session_operation_fence_postgres import _register_instance

        _register_instance(engine, instance_id=peer, lease_delta=timedelta(minutes=10))
    authority = ComposerAsyncOperationAuthority(
        engine,
        owner_instance_id=peer,
        claim_lease_seconds=old_authority._claim_lease_seconds,
        continuation_policy=old_authority._continuation_policy,
    )
    current_sessions = SessionServiceImpl(
        engine,
        sessions._data_dir,
        telemetry=sessions._telemetry,
        log=sessions._log,
        session_operation_authority=repository,
        owner_instance_id=peer,
    )
    return engine, repository, authority, current_sessions, sid


def recover(store, first):
    return store[1].recover_composer_segment(
        session_id=store[4],
        operation_id=first.claim.operation_id,
        expected_generation=first.claim.ownership_generation,
        actor_user_id="alice",
        auth_provider_type="local",
    )


def start_claim(store, claim):
    context = store[1].start_composer_segment(
        claim,
        owner_instance_id=store[2]._owner_instance_id,
        lease_seconds=30,
        auth_provider_type="local",
    )
    return ComposerOperationRunning(claim=claim, session_operation_context=context)


async def applied_case(store):
    first, selected, inventory, _snapshot, commit = await committed_evidence_case(store, graph=True, joint=True)
    work = inventory_work(first)
    applied = await apply_selected_p4_to_cold_evidence(
        authority=store[2],
        selected=selected,
        inventory=inventory,
        required_work=work,
    )
    work.coordinator.assert_completed()
    assert len(applied.restored.applications.applications) == 1
    return first, commit


async def restore_current(store, running):
    selected, inventory = await current_selected(store, running)
    work = inventory_work(running)
    restored = await restore_turn_evidence(
        authority=store[2],
        selected=selected,
        inventory=inventory,
        required_work=work,
    )
    work.coordinator.assert_completed()
    assert restored.evidence.operation_id == running.session_operation_context.fence.operation_id
    assert len(restored.applications.applications) == 1
    return restored


@pytest.mark.asyncio
async def test_expired_committed_owner_requires_backend_liveness_then_real_new_fence(operation_store):
    engine, _repository, _authority, _sessions, sid = operation_store
    first, commit = await applied_case(operation_store)
    peer = peer_owners(operation_store)
    assert (await restore_current(operation_store, first)).applications.applications[0].effect_key == commit.effect_key
    before = recovery_rows(engine), rows(engine)
    assert recover(peer, first) is False
    assert (recovery_rows(engine), rows(engine)) == before
    # Real SQL expiry boundary, not a mocked clock or killed-process claim.
    with engine.begin() as conn:
        now = database_now(conn)
        assert (
            conn.execute(update(fences).where(fences.c.session_id == str(sid)).values(lease_expires_at=now - timedelta(seconds=1))).rowcount
            == 1
        )
    if engine.dialect.name == "postgresql":
        before = recovery_rows(engine), rows(engine)
        assert recover(peer, first) is False
        assert (recovery_rows(engine), rows(engine)) == before
        with engine.begin() as conn:
            assert (
                conn.execute(
                    update(instances)
                    .where(instances.c.instance_id == "test-owner")
                    .values(lease_expires_at=database_now(conn) - timedelta(seconds=1))
                ).rowcount
                == 1
            )
    assert recover(peer, first) is True
    (claim,) = peer[2].claim_segment(limit=1)
    second = start_claim(peer, claim)
    try:
        assert claim.ownership_generation == first.claim.ownership_generation + 1
        assert second.session_operation_context.fence.operation_epoch == first.session_operation_context.fence.operation_epoch + 1
        restored = await restore_current(peer, second)
        assert restored.applications.applications[0].creator_operation_id == first.session_operation_context.fence.operation_id
        with engine.connect() as conn:
            current = conn.execute(select(fences).where(fences.c.session_id == str(sid))).one()
        assert current.owner_instance_id == "restart-boundary-peer" and current.released_at is None
    finally:
        peer[1].release(second.session_operation_context)
    # The first expired owner is deliberately not released using lost custody.


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", ("pending", "applied"))
async def test_restart_stop_allows_only_exact_committed_pending_application(operation_store, cut):
    engine, repository, authority, _sessions, sid = operation_store
    first, selected, inventory, _snapshot, commit = await committed_evidence_case(operation_store, graph=True, joint=True)
    if cut == "applied":
        work = inventory_work(first)
        await apply_selected_p4_to_cold_evidence(authority=authority, selected=selected, inventory=inventory, required_work=work)
        work.coordinator.assert_completed()
    stopped = authority.request_cancel(session_id=sid, operation_id=first.claim.operation_id, cancelled_failure=operations.cancelled)
    assert stopped is not None and stopped.cancel_requested_at is not None and stopped.status == "running"
    repository.release(first.session_operation_context)
    peer = peer_owners(operation_store)
    assert recover(peer, first) is True
    (claim,) = peer[2].claim_segment(limit=1)
    second = start_claim(peer, claim)
    try:
        business = rows(engine)
        head = peer[2].load_continuation_snapshot(second)
        work = inventory_work(second)
        current = await load_selected_continuation(authority=peer[2], running=second, selected=head, required_work=work)
        work.coordinator.assert_completed()
        work = inventory_work(second)
        with pytest.raises(ComposerOperationCancelledDuringTurn):
            await materialize_continuation_env_inventory(
                advisor=inventory_advisor(peer[3]),
                authority=peer[2],
                selected=current,
                required_work=work,
            )
        work.coordinator.assert_completed()
        if cut == "pending":
            selected, inventory = await current_selected(peer, second, effect_key=commit.effect_key)
            work = inventory_work(second)
            result = await apply_selected_p4_to_cold_evidence(
                authority=peer[2],
                selected=selected,
                inventory=inventory,
                required_work=work,
            )
            work.coordinator.assert_completed()
            assert len(result.restored.applications.applications) == 1
            assert result.restored.applications.applications[0].effect_key == commit.effect_key
        assert rows(engine) == business
        # The Stop remains current after draining; no ordinary-use clearance.
        latest = peer[2].load_continuation_snapshot(second)
        work = inventory_work(second)
        latest_selected = await load_selected_continuation(authority=peer[2], running=second, selected=latest, required_work=work)
        work.coordinator.assert_completed()
        work = inventory_work(second)
        with pytest.raises(ComposerOperationCancelledDuringTurn):
            await materialize_continuation_env_inventory(
                advisor=inventory_advisor(peer[3]),
                authority=peer[2],
                selected=latest_selected,
                required_work=work,
            )
        work.coordinator.assert_completed()
    finally:
        peer[1].release(second.session_operation_context)


@pytest.mark.asyncio
async def test_reclaimed_real_segment_claim_and_old_checkpoint_cannot_start_or_restore(operation_store):
    engine, repository, authority, _sessions, sid = operation_store
    first, _commit = await applied_case(operation_store)
    old_head = authority.load_continuation_snapshot(first)
    repository.release(first.session_operation_context)
    peer = peer_owners(operation_store)
    assert recover(peer, first) is True
    (old_claim,) = peer[2].claim_segment(limit=1)
    assert peer[2].claim_segment(limit=1) == ()
    with engine.begin() as conn:
        assert (
            conn.execute(
                update(segments)
                .where(
                    segments.c.session_id == str(sid),
                    segments.c.operation_id == first.claim.operation_id,
                    segments.c.sequence == old_claim.segment_sequence,
                )
                .values(claim_expires_at=database_now(conn) - timedelta(seconds=1))
            ).rowcount
            == 1
        )
    (current_claim,) = peer[2].claim_segment(limit=1)
    assert current_claim.attempt == old_claim.attempt + 1 and current_claim.claim_token != old_claim.claim_token
    before = recovery_rows(engine), rows(engine)
    for stale_claim in (first.claim, old_claim):
        with pytest.raises(ComposerOperationFenceLost):
            start_claim(peer, stale_claim)
        assert (recovery_rows(engine), rows(engine)) == before
    second = start_claim(peer, current_claim)
    try:
        work = inventory_work(second)
        with pytest.raises(ContinuationCASConflict):
            await load_selected_continuation(authority=peer[2], running=second, selected=old_head, required_work=work)
        work.coordinator.assert_completed()
        await restore_current(peer, second)
    finally:
        peer[1].release(second.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ("history_attempt", "history_token", "physical_token", "private_utf8"))
async def test_recovery_refuses_exact_owned_corruption_without_partial_transition(operation_store, channel):
    engine, repository, authority, _sessions, sid = operation_store
    first, _commit = await applied_case(operation_store)
    await restore_current(operation_store, first)
    selected = authority.load_continuation_snapshot(first)
    repository.release(first.session_operation_context)
    peer = peer_owners(operation_store)
    with engine.begin() as conn:
        if channel == "history_attempt":
            changed = conn.execute(update(attempts).where(attempts.c.session_id == str(sid)).values(claim_attempt=first.claim.attempt + 1))
        elif channel == "history_token":
            changed = conn.execute(
                update(attempts).where(attempts.c.session_id == str(sid)).values(session_operation_lease_token="foreign-history-token")
            )
        elif channel == "physical_token":
            changed = conn.execute(update(fences).where(fences.c.session_id == str(sid)).values(lease_token="foreign-physical-token"))
        else:
            from elspeth.web.composer.continuation_codec import decode_continuation_snapshot

            ref = decode_continuation_snapshot(selected.payload_json).frames[-1].accumulator
            payload = b"\xff"
            changed = conn.execute(
                update(chunks)
                .where(
                    chunks.c.session_id == str(sid),
                    chunks.c.operation_id == first.claim.operation_id,
                    chunks.c.chunk_key == f"value:{ref.codec}:{ref.digest}",
                )
                .values(payload=payload, byte_length=1, sha256=sha256(payload).hexdigest())
            )
        assert changed.rowcount == 1
    before = recovery_rows(engine), rows(engine)
    message = (
        "exact immutable attempt"
        if channel.startswith("history")
        else "actual previous physical fence"
        if channel == "physical_token"
        else "strict UTF-8"
    )
    with pytest.raises(AuditIntegrityError, match=message) as caught:
        recover(peer, first)
    if channel == "private_utf8":
        assert type(caught.value.__cause__) is UnicodeDecodeError
    assert (recovery_rows(engine), rows(engine)) == before


@pytest.mark.asyncio
async def test_two_real_recovery_callers_commit_one_generation_and_one_root_cas(operation_store):
    engine, repository, _authority, _sessions, sid = operation_store
    first, _commit = await applied_case(operation_store)
    repository.release(first.session_operation_context)
    left, right = new_owners(operation_store), new_owners(operation_store)
    before = recovery_rows(engine)
    business = rows(engine)
    entered = Barrier(2)

    def compete(store):
        entered.wait(timeout=10)
        return recover(store, first)

    outcomes = await asyncio.gather(asyncio.to_thread(compete, left), asyncio.to_thread(compete, right))
    assert sorted(outcomes) == [False, True]
    after = recovery_rows(engine)
    # Actual complete tables from recovery_rows: jobs/segments/history/fence/
    # checkpoints/events/effects/chunks. One attributed recovery transition.
    assert len(after[4]) == len(before[4]) + 1
    assert len(after[5]) == len(before[5]) + 1
    assert after[3] == before[3] and after[6] == before[6]
    with engine.connect() as conn:
        parent = conn.execute(select(jobs).where(jobs.c.session_id == str(sid))).one()
        segment = conn.execute(select(segments).where(segments.c.session_id == str(sid))).one()
        history = conn.execute(select(attempts).where(attempts.c.session_id == str(sid))).one()
    assert parent.ownership_generation == segment.generation == first.claim.ownership_generation + 1
    assert parent.journal_cursor == before[0][0].journal_cursor + 1
    assert history.revoked_at is not None and history.outcome == "interrupted"
    (claim,) = left[2].claim_segment(limit=1)
    assert right[2].claim_segment(limit=1) == ()
    second = start_claim(left, claim)
    try:
        await restore_current(left, second)
        assert rows(engine) == business
    finally:
        left[1].release(second.session_operation_context)
