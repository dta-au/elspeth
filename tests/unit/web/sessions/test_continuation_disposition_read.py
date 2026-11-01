"""Actual timed disposition reads without reopening ordinary work in drain."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web import async_workers
from elspeth.web.composer.continuation_clock import ContinuationTimingObservation
from elspeth.web.composer.continuation_initialization import _registered
from elspeth.web.required_executor import RequiredGenerationUnavailable
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
    RequiredWorkSource,
    required_failure_leaves,
)
from elspeth.web.sessions import continuation_sql_deadline
from elspeth.web.sessions.composer_drain import ComposerDrainDeadline
from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn, ComposerOperationFenceLost
from elspeth.web.sessions.continuation import ComposerSegmentRecoveryReceipt
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as _operation_store
from tests.unit.web.coordination.test_continuation_p4_recovery_start import new_owners
from tests.unit.web.sessions.test_continuation_cold_restore import start_continuation
from tests.unit.web.sessions.test_continuation_ingress import recovery_rows
from tests.unit.web.sessions.test_continuation_sql_deadline import _owners

operation_store = _operation_store
DISPOSITION_READ = RequiredWorkSource.CONTINUATION_DISPOSITION_READ_SQL


@asynccontextmanager
async def _disposition_work(store, monkeypatch, *, timed=True):
    running, owner, signal = _owners(store)
    owner.begin_settlement()
    monkeypatch.setattr(async_workers, "_INSTANCE_DRAINING", signal.draining)
    authority = RequiredWorkAuthority(
        RequiredAuthorityKind.DURABLE_COMPOSE, running.session_operation_context, running.claim.operation_id, running.claim.attempt
    )
    coordinator = RequiredWorkCoordinator(authority, sql_deadline_owner=owner if timed else None)
    work = RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN, running)
    await async_workers.run_sync_in_worker(lambda: None)
    signal.request()
    assert signal.started_at is not None
    deadline = ComposerDrainDeadline.from_start(started_at=signal.started_at, drain_seconds=owner.drain_seconds)
    try:
        yield running, owner, work, deadline
    finally:
        coordinator.assert_completed()
        assert async_workers.outstanding_admissions() == 0


async def _timing(store, work, deadline):
    return await _registered(
        work=work,
        source=DISPOSITION_READ,
        operation=lambda: store[2].read_continuation_timing(work.running, deadline=deadline),
        expected=ContinuationTimingObservation,
    )


@pytest.mark.asyncio
async def test_actual_timed_read_joins_during_drain_while_business_read_and_state_remain_closed(operation_store, monkeypatch):
    async with _disposition_work(operation_store, monkeypatch) as (running, owner, work, deadline):
        observed = await _timing(operation_store, work, deadline)
        assert (observed.sequence, observed.generation) == (running.claim.segment_sequence, running.claim.ownership_generation)
        assert owner.remaining_statement(session_id=str(running.claim.session_id), deadline=deadline) > 0
        for ordinary in (RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_STATE_SQL):
            callbacks = []
            with pytest.raises(RequiredGenerationUnavailable):
                await _registered(
                    work=work, source=ordinary, operation=lambda callbacks=callbacks: callbacks.append("forbidden"), expected=list
                )
            assert callbacks == []
        with pytest.raises(RequiredGenerationUnavailable):
            await async_workers.run_sync_in_worker(lambda: 1)


@pytest.mark.asyncio
async def test_disposition_source_without_actual_sql_clock_never_submits(operation_store, monkeypatch):
    async with _disposition_work(operation_store, monkeypatch, timed=False) as (_running, _owner, work, _deadline):
        callbacks = []
        with pytest.raises(RequiredGenerationUnavailable):
            await _registered(work=work, source=DISPOSITION_READ, operation=lambda: callbacks.append("forbidden"), expected=list)
        assert callbacks == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("context", "logical-operation", "claim-attempt", "expired"))
async def test_disposition_read_preserves_original_physical_owner_and_clock(operation_store, monkeypatch, fault):
    async with _disposition_work(operation_store, monkeypatch) as (running, owner, work, deadline):
        callbacks = []
        with monkeypatch.context() as changed:
            if fault == "context":
                changed.setattr(owner, "running", replace(running, session_operation_context=replace(running.session_operation_context)))
            elif fault == "logical-operation":
                changed.setattr(
                    owner, "running", replace(running, claim=replace(running.claim, operation_id=str(running.claim.session_id)))
                )
            elif fault == "claim-attempt":
                changed.setattr(owner, "running", replace(running, claim=replace(running.claim, attempt=running.claim.attempt + 1)))
            else:

                class ExpiredClock:
                    @staticmethod
                    def monotonic():
                        return deadline.expires_at + 1

                changed.setattr(continuation_sql_deadline, "time", ExpiredClock)
            with pytest.raises(TimeoutError if fault == "expired" else AuditIntegrityError):
                await _registered(work=work, source=DISPOSITION_READ, operation=lambda: callbacks.append("forbidden"), expected=list)
        assert callbacks == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("unavailable", "quarantined-state", "replacement", "shutdown"))
async def test_disposition_read_cannot_reopen_unavailable_generation(operation_store, monkeypatch, fault):
    async with _disposition_work(operation_store, monkeypatch) as (_running, _owner, work, deadline):
        generation = async_workers._GENERATION_CUSTODIAN
        assert generation is not None
        with monkeypatch.context() as unavailable:
            if fault == "unavailable":
                marker = threading.Event()
                marker.set()
                unavailable.setattr(async_workers, "_GENERATION_UNAVAILABLE", marker)
            elif fault == "quarantined-state":
                unavailable.setattr(generation, "state", "quarantined")
            elif fault == "shutdown":
                unavailable.setattr(async_workers, "_SHARED_SHUTDOWN_STARTED", True)
            else:
                # A required ticket does not pin a generation before submission;
                # an already quarantined replacement is still unavailable.
                with ThreadPoolExecutor(max_workers=1) as foreign:
                    unavailable.setattr(async_workers, "_SHARED_EXECUTOR", foreign)
                    unavailable.setattr(generation, "executor", foreign)
                    unavailable.setattr(generation, "state", "quarantined")
                    with pytest.raises(RequiredGenerationUnavailable):
                        await _timing(operation_store, work, deadline)
                return
            with pytest.raises(RequiredGenerationUnavailable):
                await _timing(operation_store, work, deadline)


@pytest.mark.asyncio
async def test_classification_data_preserves_stop_and_actual_handoff_refuses(operation_store, monkeypatch):
    async with _disposition_work(operation_store, monkeypatch) as (running, _owner, work, deadline):
        stopped = await asyncio.to_thread(
            operation_store[2].request_cancel, session_id=running.claim.session_id, operation_id=running.claim.operation_id
        )
        assert stopped is not None and stopped.cancel_requested_at is not None
        before = recovery_rows(operation_store[0])
        classification = await _registered(
            work=work,
            source=DISPOSITION_READ,
            operation=lambda: operation_store[2].classify_continuation_owner(running, env_ref_names=frozenset(), deadline=deadline),
            expected=str,
        )
        assert classification == "handover" and recovery_rows(operation_store[0]) == before
        with pytest.raises(ComposerOperationCancelledDuringTurn):
            await asyncio.to_thread(operation_store[1].suspend_composer_segment, running, deadline=deadline)
        assert recovery_rows(operation_store[0]) == before


@pytest.mark.asyncio
async def test_exact_handoff_history_read_survives_peer_start_without_reusing_released_fence(operation_store, monkeypatch):
    async with _disposition_work(operation_store, monkeypatch) as (running, _owner, work, deadline):
        receipt = await asyncio.to_thread(operation_store[1].suspend_composer_segment, running, deadline=deadline)
        peer_store = new_owners(operation_store)
        peer = await asyncio.to_thread(start_continuation, peer_store[1], peer_store[2])
        assert peer.claim.ownership_generation == receipt.generation == running.claim.ownership_generation + 1

        def verify(actual):
            operation_store[1].verify_recovery(actual, deadline=deadline)
            return actual

        observed = await _registered(
            work=work, source=DISPOSITION_READ, operation=lambda: verify(receipt), expected=ComposerSegmentRecoveryReceipt
        )
        assert observed is receipt and observed.released_context is running.session_operation_context
        with pytest.raises(ComposerOperationFenceLost):
            await _timing(operation_store, work, deadline)
        with pytest.raises(AuditIntegrityError):
            await _registered(
                work=work,
                source=DISPOSITION_READ,
                operation=lambda: verify(replace(receipt, cursor=receipt.cursor + 1)),
                expected=ComposerSegmentRecoveryReceipt,
            )


@pytest.mark.asyncio
async def test_physical_disposition_failure_and_each_delivered_cancellation_are_retained(operation_store, monkeypatch):
    async with _disposition_work(operation_store, monkeypatch) as (_running, _owner, work, _deadline):
        entered, release = threading.Event(), threading.Event()
        original = RuntimeError("controlled physical disposition-read original")

        def physical():
            entered.set()
            assert release.wait(timeout=5), "physical disposition read was not released"
            raise original

        task = asyncio.create_task(_registered(work=work, source=DISPOSITION_READ, operation=physical, expected=int))
        try:
            assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 3), timeout=4)
            task.cancel("first delivered cancellation")
            await asyncio.sleep(0)
            task.cancel("second delivered cancellation")
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(BaseExceptionGroup) as failed:
                await asyncio.wait_for(asyncio.shield(task), timeout=3)
            leaves = required_failure_leaves(failed.value)
            assert any(leaf is original for leaf in leaves)
            assert [leaf.args for leaf in leaves if isinstance(leaf, asyncio.CancelledError)] == [
                ("first delivered cancellation",),
                ("second delivered cancellation",),
            ]
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
