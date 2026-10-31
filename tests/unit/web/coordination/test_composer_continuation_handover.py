"""U-35b: the live-owner handover, V2 Stop, V2 claim renewal and the V2 terminal.

Design §2.1 (handover), §2.7 (terminal and Stop), §2.8 (claim renew/release),
protocol R8 (saved revisions across segment fences).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select, update
from tests.unit.web.coordination.test_composer_continuation_claim_start import queued, start_segment
from tests.unit.web.coordination.test_composer_operation_authority import _admission_rows
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as _operation_store

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.coordination.composer_operation_authority import (
    ComposerAsyncOperationAuthority,
    settle_composer_operation_on_connection,
)
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.composer_operation_errors import request_cancelled_error, worker_lost_error
from elspeth.web.sessions.composer_operations import (
    ComposerOperationCancelledDuringTurn,
    ComposerOperationFenceLost,
    ComposerOperationRunning,
)
from elspeth.web.sessions.continuation import ComposerSegmentStartObservation, read_chunk
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import session_operation_fences_table as fences
from elspeth.web.sessions.time_normalization import restore_utc

operation_store = _operation_store


def _running(store):
    _engine, repository, _authority, _service, _sid = store
    authority, record = queued(store)
    (claim,) = authority.claim_segment(limit=1)
    return authority, record, ComposerOperationRunning(claim=claim, session_operation_context=start_segment(repository, claim))


def _cancel(authority, record):
    return authority.request_cancel(
        session_id=record.session_id, operation_id=record.operation_id, cancelled_failure=request_cancelled_error
    )


def test_handover_keeps_sequence_advances_generation_and_releases_fence(operation_store):
    engine, repository, _authority, _service, sid = operation_store
    authority, record, running = _running(operation_store)

    receipt = repository.suspend_composer_segment(running)

    assert (receipt.sequence, receipt.previous_generation, receipt.generation) == (1, 1, 2)
    with engine.connect() as conn:
        parent = conn.execute(select(jobs)).one()
        segment = conn.execute(select(segments)).one()
        attempt = conn.execute(select(attempts)).one()
        fence = conn.execute(select(fences).where(fences.c.session_id == str(sid))).one()
        event = conn.execute(select(events).order_by(events.c.sequence.desc()).limit(1)).one()
        payload = read_chunk(conn, session_id=str(sid), operation_id=record.operation_id, key=event.payload_chunk_key)
    assert (parent.status, parent.phase, parent.current_segment_sequence, parent.ownership_generation) == ("running", "reconciling", 1, 2)
    assert (segment.state, segment.generation, segment.claim_token) == ("interrupted", 2, None)
    assert attempt.outcome == "interrupted" and attempt.revoked_at is not None
    assert fence.released_at is not None
    # Recovery's exact bytes: every landed phase reader accepts a handover as a recovery.
    assert event.tag == "recovered"
    assert payload.decode() == canonical_json({"version": 2, "same_sequence": 1, "previous_generation": 1, "generation": 2})

    # Claimable at commit, with no lease-expiry wait.
    (claim,) = authority.claim_segment(limit=1)
    assert (claim.segment_sequence, claim.ownership_generation) == (1, 2)
    successor = start_segment(repository, claim)
    assert successor.fence.operation_epoch > running.session_operation_context.fence.operation_epoch


def test_handover_readback_survives_a_peer_starting_immediately(operation_store):
    _engine, repository, *_ = operation_store
    authority, _record, running = _running(operation_store)
    receipt = repository.suspend_composer_segment(running)
    assert receipt.released_context is running.session_operation_context
    repository.verify_recovery(receipt)
    (claim,) = authority.claim_segment(limit=1)
    successor = start_segment(repository, claim)
    repository.verify_recovery(receipt)
    following = repository.suspend_composer_segment(ComposerOperationRunning(claim, successor))
    repository.verify_recovery(receipt)
    repository.verify_recovery(following)


def test_worker_segment_claim_read_accepts_running_parent_and_durable_stop(operation_store):
    _engine, repository, *_ = operation_store
    authority, record, running = _running(operation_store)
    repository.suspend_composer_segment(running)
    (claim,) = authority.claim_segment(limit=1)
    observed = authority.read_segment_claim(claim)
    assert observed is not None and observed.status == "running"
    _cancel(authority, record)
    marked = authority.read_segment_claim(claim)
    assert marked is not None and marked.cancel_requested_at is not None
    assert authority.read_segment_claim(replace(claim, claim_token=claim.claim_token + "stale")) is None
    authority.release_claim(claim)
    assert authority.read_segment_claim(claim) is None


def test_owner_terminal_wrapper_uses_v2_cancellation_without_legacy_deadline(operation_store):
    engine, _repository, *_ = operation_store
    authority, record, running = _running(operation_store)
    _cancel(authority, record)
    terminal = authority.settle_continuation_owner(running, outcome=request_cancelled_error(request_id=record.request_id))
    assert terminal.failure_code == "request_cancelled"
    _assert_terminal_bundle(engine, running, status="failed", failure_code="request_cancelled")


def test_start_readback_returns_the_original_committing_context(operation_store):
    _engine, repository, _base_authority, _service, _sid = operation_store
    authority, _record = queued(operation_store)
    (claim,) = authority.claim_segment(limit=1)
    observation = ComposerSegmentStartObservation(claim)
    context = repository.start_composer_segment(
        claim,
        owner_instance_id=authority._owner_instance_id,
        lease_seconds=30,
        auth_provider_type="local",
        observation=observation,
    )
    assert observation.context is context and observation.committed
    assert repository.reconcile_composer_segment_start(observation) is context
    assert observation.reconciled


def test_start_readback_proves_unstarted_without_minting_a_context(operation_store):
    engine, repository, *_ = operation_store
    authority, _record = queued(operation_store)
    (claim,) = authority.claim_segment(limit=1)
    observation = ComposerSegmentStartObservation(claim)
    before = _admission_rows(engine)
    assert repository.reconcile_composer_segment_start(observation) is None
    assert observation.context is None and observation.reconciled and not observation.committed
    assert _admission_rows(engine) == before


@pytest.mark.parametrize("changed", ("generation", "cursor", "checkpoint", "release"))
def test_handover_readback_refuses_substituted_committed_identity(operation_store, changed):
    _engine, repository, *_ = operation_store
    _authority, _record, running = _running(operation_store)
    receipt = repository.suspend_composer_segment(running)
    if changed == "generation":
        receipt = replace(receipt, generation=receipt.generation + 1)
    elif changed == "cursor":
        receipt = replace(receipt, cursor=receipt.cursor + 1)
    elif changed == "checkpoint":
        receipt = replace(receipt, checkpoint_id="missing-checkpoint")
    else:
        context = running.session_operation_context
        receipt = replace(
            receipt, released_context=replace(context, fence=replace(context.fence, operation_epoch=context.fence.operation_epoch + 1))
        )
    with pytest.raises(AuditIntegrityError):
        repository.verify_recovery(receipt)


@pytest.mark.parametrize("stale", ("lease_token", "operation_epoch", "claim_token", "attempt"))
def test_handover_refuses_a_stale_owner_and_writes_nothing(operation_store, stale):
    engine, repository, *_ = operation_store
    _authority, _record, running = _running(operation_store)
    fence = running.session_operation_context.fence
    if stale == "lease_token":
        running = replace(
            running,
            session_operation_context=replace(running.session_operation_context, fence=replace(fence, lease_token=fence.lease_token + "x")),
        )
    elif stale == "operation_epoch":
        running = replace(
            running,
            session_operation_context=replace(
                running.session_operation_context, fence=replace(fence, operation_epoch=fence.operation_epoch + 7)
            ),
        )
    elif stale == "claim_token":
        running = replace(running, claim=replace(running.claim, claim_token=running.claim.claim_token + "x"))
    else:
        running = replace(running, claim=replace(running.claim, attempt=running.claim.attempt + 1))
    before = _admission_rows(engine)
    with pytest.raises(ComposerOperationFenceLost):
        repository.suspend_composer_segment(running)
    assert _admission_rows(engine) == before


def test_stop_wins_over_a_handover(operation_store):
    engine, repository, *_ = operation_store
    authority, record, running = _running(operation_store)
    _cancel(authority, record)
    before = _admission_rows(engine)
    with pytest.raises(ComposerOperationCancelledDuringTurn):
        repository.suspend_composer_segment(running)
    assert _admission_rows(engine) == before


def test_v2_request_cancel_marks_only_bumps_revision_and_makes_the_segment_eligible(operation_store):
    engine, *_ = operation_store
    authority, record = queued(operation_store)
    with engine.begin() as conn:
        backoff = database_now(conn) + timedelta(seconds=30)
        conn.execute(update(segments).values(next_eligible_at=backoff))
        revision = conn.execute(select(jobs.c.status_revision)).scalar_one()

    marked = _cancel(authority, record)

    assert marked.status == "queued" and marked.cancel_requested_at is not None
    with engine.connect() as conn:
        assert conn.execute(select(jobs.c.status_revision)).scalar_one() == revision + 1
        eligible = restore_utc(conn.execute(select(segments.c.next_eligible_at)).scalar_one())
        assert eligible < backoff - timedelta(seconds=20)
    # A repeated Stop changes nothing more.
    assert _cancel(authority, record) == marked
    with engine.connect() as conn:
        assert conn.execute(select(jobs.c.status_revision)).scalar_one() == revision + 1


def test_v2_request_cancel_leaves_a_live_claim_alone(operation_store):
    engine, *_ = operation_store
    authority, record = queued(operation_store)
    with engine.begin() as conn:
        # Eligible well before now, so a wrongful reset to now is observable.
        conn.execute(update(segments).values(next_eligible_at=database_now(conn) - timedelta(minutes=10)))
    authority.claim_segment(limit=1)
    with engine.connect() as conn:
        before = conn.execute(select(segments.c.next_eligible_at, segments.c.claim_token)).one()
    _cancel(authority, record)
    with engine.connect() as conn:
        assert conn.execute(select(segments.c.next_eligible_at, segments.c.claim_token)).one() == before


def test_v2_pre_start_claim_renews_and_releases_on_the_segment_row(operation_store):
    engine, *_ = operation_store
    authority, _record = queued(operation_store)
    (claim,) = authority.claim_segment(limit=1)
    # A short but live claim, so a real renewal is a strict extension.
    with engine.begin() as conn:
        short = database_now(conn) + timedelta(seconds=1)
        conn.execute(update(segments).values(claim_expires_at=short))
    with engine.connect() as conn:
        before_renewal = database_now(conn)
    authority.renew_claim(claim)
    with engine.connect() as conn:
        renewed = restore_utc(conn.execute(select(segments.c.claim_expires_at)).scalar_one())
        assert conn.execute(select(jobs.c.claim_token)).scalar_one() is None
    assert renewed > short
    assert renewed >= before_renewal + timedelta(seconds=authority._claim_lease_seconds) - timedelta(seconds=1)
    authority.release_claim(claim)
    with engine.connect() as conn:
        assert conn.execute(select(segments.c.claim_token, segments.c.claim_expires_at)).one() == (None, None)
    with pytest.raises(ComposerOperationFenceLost):
        authority.release_claim(claim)


def _settle(engine, running, outcome):
    with locked_session_transaction(engine, str(running.claim.session_id)) as conn:
        return settle_composer_operation_on_connection(conn, running, outcome=outcome)


def _assert_terminal_bundle(engine, running, *, status, failure_code):
    with engine.connect() as conn:
        parent = conn.execute(select(jobs)).one()
        segment = conn.execute(select(segments)).one()
        attempt = conn.execute(select(attempts)).one()
        (event,) = conn.execute(select(events).where(events.c.tag == "settled")).all()
        payload = read_chunk(
            conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id, key=event.payload_chunk_key
        )
    assert (parent.lifecycle_version, parent.status, parent.phase, parent.settled_by, parent.request_json) == (
        2,
        status,
        "settled",
        "owner_terminal",
        None,
    )
    assert payload.decode() == canonical_json(
        {
            "version": 1,
            "sequence": segment.sequence,
            "generation": segment.generation,
            "status": status,
            "failure_code": failure_code,
        }
    )
    assert segment.state == "settled" and segment.claim_token is None
    assert attempt.outcome == "settled" and attempt.revoked_at is not None


def test_v2_terminal_settles_with_phase_settled(operation_store):
    engine, *_ = operation_store
    _authority, record, running = _running(operation_store)

    settled = _settle(engine, running, worker_lost_error(request_id=record.request_id))

    assert settled.status == "failed" and settled.failure_code == "worker_lost"
    _assert_terminal_bundle(engine, running, status="failed", failure_code="worker_lost")
    # A terminal replays; it is never written twice.
    before = _admission_rows(engine)
    assert _settle(engine, running, worker_lost_error(request_id=record.request_id)) == settled
    assert _admission_rows(engine) == before


@pytest.mark.asyncio
async def test_v2_completed_terminal_settles_with_phase_settled(operation_store):
    from tests.unit.web.coordination.test_composer_operation_authority import response
    from tests.unit.web.sessions.test_continuation_ingress import begin, ingress

    from elspeth.web.sessions.schemas import MessageWithStateResponse

    engine, _repository, _authority, service, sid = operation_store
    running, work, selected, request, _seed = begin(operation_store)
    inserted = await ingress(operation_store, running, work, selected, request)
    work.coordinator.assert_completed()
    # Exercise the library terminal over actual V2 ingress and a real persisted
    # assistant. This does not stand in for the later END/worker integration.
    assistant = await service.add_message(
        sid,
        "assistant",
        "Completed library terminal fixture",
        writer_principal="compose_loop",
        session_operation_context=running.session_operation_context,
    )
    outcome = response(assistant, (), None)

    settled = _settle(engine, running, outcome)

    assert settled.status == "completed" and settled.failure_code is None
    assert settled.user_message_id == inserted.message.id
    assert MessageWithStateResponse.model_validate_json(settled.result_json) == outcome
    _assert_terminal_bundle(engine, running, status="completed", failure_code=None)
    before = _admission_rows(engine)
    assert _settle(engine, running, outcome) == settled
    assert _admission_rows(engine) == before


def test_v2_cancel_terminal_requires_its_durable_stop_marker(operation_store):
    engine, *_ = operation_store
    authority, record, running = _running(operation_store)
    before = _admission_rows(engine)
    with pytest.raises(AuditIntegrityError, match="durable Stop marker"):
        _settle(engine, running, request_cancelled_error(request_id=record.request_id))
    assert _admission_rows(engine) == before
    _cancel(authority, record)
    settled = _settle(engine, running, worker_lost_error(request_id=record.request_id))
    # A pending Stop wins the classification of a non-authoritative failure.
    assert settled.failure_code == "request_cancelled"


def test_v2_terminal_refuses_a_context_with_no_live_attempt(operation_store):
    engine, *_ = operation_store
    _authority, record, running = _running(operation_store)
    fence = running.session_operation_context.fence
    foreign = replace(
        running,
        session_operation_context=replace(
            running.session_operation_context, fence=replace(fence, operation_epoch=fence.operation_epoch + 7)
        ),
    )
    before = _admission_rows(engine)
    with pytest.raises(ComposerOperationFenceLost):
        _settle(engine, foreign, worker_lost_error(request_id=record.request_id))
    assert _admission_rows(engine) == before


@pytest.mark.asyncio
async def test_v2_saved_revisions_aggregate_every_segment_fence(operation_store):
    from elspeth.web.sessions.graph_recovery import checkpoint_data, empty_recovery_state

    engine, repository, _authority, service, sid = operation_store
    authority, record, first = _running(operation_store)
    await service.save_composition_state(
        sid, checkpoint_data(empty_recovery_state()), provenance="session_seed", session_operation_context=first.session_operation_context
    )
    repository.suspend_composer_segment(first)
    (claim,) = authority.claim_segment(limit=1)
    second = ComposerOperationRunning(claim=claim, session_operation_context=start_segment(repository, claim))
    latest = await service.save_composition_state(
        sid, checkpoint_data(empty_recovery_state()), provenance="session_seed", session_operation_context=second.session_operation_context
    )
    _settle(engine, second, worker_lost_error(request_id=record.request_id))

    (outcome,), _cursor = authority.list_failure_outcomes(session_id=sid, actor_user_id="alice")

    assert outcome.saved_revisions.attribution == "fenced"
    assert outcome.saved_revisions.count == 2
    assert outcome.saved_revisions.latest_state_id == latest.id


def test_a_peer_with_v2_off_still_claims_and_starts_a_handed_over_segment(operation_store):
    engine, repository, *_ = operation_store
    _authority, _record, running = _running(operation_store)
    repository.suspend_composer_segment(running)
    drained = ComposerAsyncOperationAuthority(engine, owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=None)
    (claim,) = drained.claim_segment(limit=1)
    start_segment(repository, claim)


async def _adopted(store, running, *, lease_seconds=30):
    from tests.unit.web.coordination.test_continuation_p4_recovery_start import inventory_work

    from elspeth.web.coordination.lifecycle import SessionOperationLease

    _engine, repository, *_ = store
    work = inventory_work(running)
    lease = await SessionOperationLease.adopt(
        repository, running.session_operation_context, lease_seconds=lease_seconds, required_work=work.coordinator
    )
    return lease, work.coordinator


def _fence_expiry(engine):
    with engine.connect() as conn:
        return restore_utc(conn.execute(select(fences.c.lease_expires_at)).scalar_one())


@pytest.mark.asyncio
async def test_lease_consume_marks_consumed_and_close_makes_no_second_release(operation_store):
    from elspeth.web.coordination.contracts import SessionOperationLeaseDisposition
    from elspeth.web.required_work import RequiredWorkSource

    engine, repository, *_ = operation_store
    _authority, _record, running = _running(operation_store)
    lease, coordinator = await _adopted(operation_store, running)
    async with lease:
        receipt = await lease.consume_continuation_handoff(lambda: repository.suspend_composer_segment(running))
        assert lease.disposition is SessionOperationLeaseDisposition.CONSUMED and lease.closed
        assert receipt.generation == 2 and receipt.lease_validity_remaining > timedelta(0)
        before = _admission_rows(engine)
    # The retained finish is only joined: no second release, no write.
    assert _admission_rows(engine) == before
    assert lease.disposition is SessionOperationLeaseDisposition.CONSUMED
    sources = [ticket.key.source for ticket in coordinator.tickets]
    assert RequiredWorkSource.CONTINUATION_HANDOFF_SQL in sources and RequiredWorkSource.LEASE_RELEASE not in sources
    coordinator.assert_completed()
    # Ownership ended at commit, so the coordinator is sealed afterwards.
    with pytest.raises(AuditIntegrityError, match="release barrier"):
        coordinator.reserve(RequiredWorkSource.OPERATION_TERMINAL_SQL)


@pytest.mark.asyncio
async def test_a_stop_refused_handoff_keeps_the_lease_live_for_the_cancel_settlement(operation_store):
    from elspeth.web.coordination.contracts import SessionOperationLeaseDisposition

    engine, repository, *_ = operation_store
    authority, record, running = _running(operation_store)
    _cancel(authority, record)
    # A 6 s lease renews about every 2 s, so a restarted renewal is observable
    # with margin for a loaded host; without the restart both expiries are equal.
    lease, coordinator = await _adopted(operation_store, running, lease_seconds=6)
    async with lease:
        with pytest.raises(ComposerOperationCancelledDuringTurn):
            await lease.consume_continuation_handoff(lambda: repository.suspend_composer_segment(running))
        # A no-write refusal: the owner still holds a renewed, unsealed lease.
        assert lease.disposition is SessionOperationLeaseDisposition.ACTIVE and not lease.closed
        await asyncio.sleep(2.5)
        renewed = _fence_expiry(engine)
        await asyncio.sleep(2.5)
        assert _fence_expiry(engine) > renewed
        settled = _settle(engine, running, request_cancelled_error(request_id=record.request_id))
        assert (settled.status, settled.failure_code) == ("failed", "request_cancelled")
    # The ordinary close then releases the fence exactly once.
    assert lease.disposition is SessionOperationLeaseDisposition.RELEASED
    coordinator.assert_completed()
    with engine.connect() as conn:
        assert conn.execute(select(fences.c.released_at)).scalar_one() is not None


@pytest.mark.asyncio
async def test_a_lost_fence_during_handoff_marks_the_lease_lost(operation_store):
    from elspeth.web.coordination.contracts import SessionOperationLeaseDisposition

    engine, repository, *_ = operation_store
    _authority, _record, running = _running(operation_store)
    stale = replace(running, claim=replace(running.claim, claim_token=running.claim.claim_token + "x"))
    lease, _coordinator = await _adopted(operation_store, running)
    before = _admission_rows(engine)
    with pytest.raises(ComposerOperationFenceLost):
        async with lease:
            await lease.consume_continuation_handoff(lambda: repository.suspend_composer_segment(stale))
    assert lease.disposition is SessionOperationLeaseDisposition.LOST and lease.closed
    assert _admission_rows(engine) == before


@pytest.mark.parametrize("stale", ("claim_token", "attempt"))
def test_v2_terminal_refuses_a_stale_claim_and_writes_nothing(operation_store, stale):
    engine, *_ = operation_store
    _authority, record, running = _running(operation_store)
    if stale == "claim_token":
        stale_running = replace(running, claim=replace(running.claim, claim_token=running.claim.claim_token + "x"))
    else:
        stale_running = replace(running, claim=replace(running.claim, attempt=running.claim.attempt + 1))
    before = _admission_rows(engine)
    with pytest.raises(ComposerOperationFenceLost):
        _settle(engine, stale_running, worker_lost_error(request_id=record.request_id))
    assert _admission_rows(engine) == before


@pytest.mark.parametrize("stale", ("operation_epoch", "lease_token", "attempt"))
def test_yield_outcome_refuses_a_caller_that_is_not_the_exact_attempt(operation_store, stale):
    _engine, repository, *_ = operation_store
    _authority, _record, running = _running(operation_store)
    # The exact live caller has an uncommitted yield.
    assert repository.read_yield_outcome(running) is None
    fence = running.session_operation_context.fence
    if stale == "operation_epoch":
        context = replace(running.session_operation_context, fence=replace(fence, operation_epoch=fence.operation_epoch + 7))
        stale_running = replace(running, session_operation_context=context)
    elif stale == "lease_token":
        context = replace(running.session_operation_context, fence=replace(fence, lease_token=fence.lease_token + "x"))
        stale_running = replace(running, session_operation_context=context)
    else:
        stale_running = replace(running, claim=replace(running.claim, attempt=running.claim.attempt + 1))
    with pytest.raises(AuditIntegrityError, match="exact attempt"):
        repository.read_yield_outcome(stale_running)


@pytest.mark.parametrize("started", (True, False))
def test_settle_lost_segment_pairs_revocation_and_settles_never_started_parents(operation_store, started):
    from elspeth.web.sessions.continuation import settle_segment_on_connection

    engine, *_ = operation_store
    if started:
        _authority, record, _running_owner = _running(operation_store)
    else:
        _authority, record = queued(operation_store)
    sid = str(record.session_id)
    with locked_session_transaction(engine, sid) as conn:
        parent = conn.execute(select(jobs)).one()
        segment = conn.execute(select(segments)).one()
        history = conn.execute(select(attempts)).one_or_none()
        assert (history is not None) is started
        settle_segment_on_connection(
            conn,
            session_id=sid,
            operation_id=record.operation_id,
            parent=parent,
            segment=segment,
            history=history,
            outcome=worker_lost_error(request_id=record.request_id),
            settled_by="settle_lost",
            attempt_outcome="interrupted",
            now=database_now(conn),
        )
    with engine.connect() as conn:
        parent = conn.execute(select(jobs)).one()
        segment = conn.execute(select(segments)).one()
        history = conn.execute(select(attempts)).one_or_none()
    assert (parent.status, parent.phase, parent.settled_by, parent.failure_code) == ("failed", "settled", "settle_lost", "worker_lost")
    if started:
        # Revocation and its outcome are written together (F6).
        assert history.outcome == "interrupted" and history.revoked_at is not None
        assert segment.state == "settled"
    else:
        # A never-started segment keeps its queued time bundle (F8).
        assert history is None and segment.state == "queued"
