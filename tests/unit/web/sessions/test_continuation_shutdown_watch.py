"""Real SQL watch admission during an owned Continuation drain.

These use the existing operation store and deadline owners. The real signal,
paid-call and full process teardown proof stays in the PostgreSQL process suite.
"""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from starlette.applications import Starlette

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web import async_workers
from elspeth.web.composer_watch_reads import ComposerOperationWatchReader
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.process_recovery import ProcessRecovery
from elspeth.web.required_executor import RequiredGenerationUnavailable
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkAuthority, RequiredWorkCoordinator
from elspeth.web.sessions.composer_async_worker import ComposerAsyncWorker
from elspeth.web.sessions.composer_drain import ComposerDrainDeadline
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_sql_deadline import _owners

operation_store = _operation_store


@asynccontextmanager
async def _segment_reader(store, monkeypatch):
    running, owner, signal = _owners(store)
    monkeypatch.setattr(async_workers, "_INSTANCE_DRAINING", signal.draining)
    authority = RequiredWorkAuthority(
        RequiredAuthorityKind.DURABLE_COMPOSE, running.session_operation_context, running.claim.operation_id, running.claim.attempt
    )
    coordinator = RequiredWorkCoordinator(authority, sql_deadline_owner=owner)
    lease = await SessionOperationLease.adopt(store[1], running.session_operation_context, lease_seconds=30, required_work=coordinator)
    worker = ComposerAsyncWorker(
        app=Starlette(),
        authority=store[2],
        concurrency=1,
        scan_interval_seconds=0.05,
        claim_lease_seconds=30,
        drain_seconds=owner.drain_seconds,
        owner_instance_id="test-owner",
        process_recovery=ProcessRecovery(watchdog=OwnedTestProcessWatchdog(signal.draining), instance_draining=signal.draining),
        instance_draining=signal.draining,
        drain_signal=signal,
    )
    key = (running.claim.session_id, running.claim.operation_id)
    worker._required_coordinators[key] = coordinator
    reader = ComposerOperationWatchReader.for_segment(worker, running, lease)
    calls = []
    actual_read = reader._invocation

    def observed_read(**kwargs):
        calls.append(threading.get_ident())
        return actual_read(**kwargs)

    reader._invocation = observed_read
    try:
        current, _now = await reader.read()
        assert current is not None and current.operation_id == running.claim.operation_id
        assert len(calls) == 1 and calls[0] != threading.get_ident()
        yield reader, owner, signal, calls
    finally:
        if not lease.closed:
            await lease.close()
        coordinator.assert_completed()
        assert async_workers.outstanding_admissions() == 0


def _begin_actual_drain(reader, signal):
    signal.request()
    assert signal.started_at is not None
    deadline = reader.worker.begin_drain(started_at=signal.started_at)
    assert deadline.expires_at == signal.started_at + reader.worker._drain_seconds
    assert reader.worker._instance_draining is async_workers._INSTANCE_DRAINING
    return deadline


@pytest.mark.asyncio
async def test_current_segment_keeps_actual_bounded_stop_read_during_process_drain(operation_store, monkeypatch):
    async with _segment_reader(operation_store, monkeypatch) as (reader, owner, signal, calls):
        original_generation = reader.generation
        deadline = _begin_actual_drain(reader, signal)
        current, _now = await reader.read()
        assert current is not None and current.cancel_requested_at is None
        stopped = await asyncio.to_thread(
            reader.worker._authority.request_cancel,
            session_id=reader.running.claim.session_id,
            operation_id=reader.running.claim.operation_id,
        )
        assert stopped is not None and stopped.cancel_requested_at is not None
        observed, _now = await reader.read()
        assert observed is not None and observed.cancel_requested_at == stopped.cancel_requested_at
        assert len(calls) == 3 and reader.generation is original_generation
        assert reader.worker.drain_deadline is deadline
        assert 0 < owner.remaining_statement(session_id=str(reader.running.claim.session_id), lifecycle=True) <= 1.0
        assert not reader.lease.closed and reader.lease.renewal_error is None
        ordinary_calls = []
        with pytest.raises(RequiredGenerationUnavailable):
            await async_workers.run_sync_in_worker(lambda: ordinary_calls.append("must not run"))
        assert ordinary_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("bare-latch", "unavailable", "quarantined-state", "replaced", "shutdown"))
async def test_process_drain_never_reopens_unavailable_or_foreign_generation(operation_store, monkeypatch, fault):
    async with _segment_reader(operation_store, monkeypatch) as (reader, _owner, signal, calls):
        if fault == "bare-latch":
            signal.draining.set()
            assert reader.worker.drain_deadline is None
        else:
            _begin_actual_drain(reader, signal)
        with monkeypatch.context() as unavailable:
            if fault == "unavailable":
                marker = threading.Event()
                marker.set()
                unavailable.setattr(async_workers, "_GENERATION_UNAVAILABLE", marker)
            elif fault == "quarantined-state":
                assert reader.generation is not None
                unavailable.setattr(reader.generation, "state", "quarantined")
            elif fault == "shutdown":
                unavailable.setattr(async_workers, "_SHARED_SHUTDOWN_STARTED", True)
            if fault == "replaced":
                with ThreadPoolExecutor(max_workers=1) as replacement_executor:
                    unavailable.setattr(async_workers, "_SHARED_EXECUTOR", replacement_executor)
                    unavailable.setattr(async_workers, "_GENERATION_CUSTODIAN", None)
                    with pytest.raises(RequiredGenerationUnavailable):
                        await reader.read()
            else:
                with pytest.raises(RequiredGenerationUnavailable):
                    await reader.read()
        assert len(calls) == 1 and async_workers.outstanding_admissions() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("coordinator", "physical-owner", "deadline"))
async def test_bounded_watch_refuses_changed_original_process_or_segment_owner(operation_store, monkeypatch, fault):
    async with _segment_reader(operation_store, monkeypatch) as (reader, owner, signal, calls):
        deadline = _begin_actual_drain(reader, signal)
        with monkeypatch.context() as foreign:
            if fault == "coordinator":
                foreign.setattr(reader.worker, "_required_coordinators", {})
            elif fault == "physical-owner":
                foreign.setattr(owner, "running", replace(reader.running))
            else:
                foreign.setattr(reader.worker, "_drain_deadline", ComposerDrainDeadline(deadline.expires_at + 1))
            with pytest.raises(AuditIntegrityError):
                await reader.read()
        assert len(calls) == 1 and reader.worker.drain_deadline is deadline


@pytest.mark.asyncio
async def test_closed_physical_lease_cannot_observe_during_process_drain(operation_store, monkeypatch):
    async with _segment_reader(operation_store, monkeypatch) as (reader, _owner, signal, calls):
        _begin_actual_drain(reader, signal)
        await reader.lease.close()
        with pytest.raises(asyncio.CancelledError):
            await reader.read()
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_retained_renewal_original_still_refuses_bounded_drain_read(operation_store, monkeypatch):
    async with _segment_reader(operation_store, monkeypatch) as (reader, _owner, signal, calls):
        _begin_actual_drain(reader, signal)
        repository = operation_store[1]
        await asyncio.to_thread(repository.release, reader.lease.context)
        with pytest.raises(SessionOperationFenceLost) as renewing:
            await asyncio.to_thread(repository.renew, reader.lease.context, lease_seconds=30)
        original = renewing.value
        reader.lease._record_renewal_error(original)
        with pytest.raises(SessionOperationFenceLost) as caught:
            await reader.read()
        assert caught.value is original and len(calls) == 1
        with pytest.raises(SessionOperationFenceLost) as closing:
            await reader.lease.close()
        assert closing.value is original and reader.lease.closed


@pytest.mark.asyncio
async def test_original_drain_expiry_cannot_be_extended_by_watch_admission(operation_store, monkeypatch):
    async with _segment_reader(operation_store, monkeypatch) as (reader, owner, signal, calls):
        deadline = _begin_actual_drain(reader, signal)
        with monkeypatch.context() as expired:
            from elspeth.web.sessions import continuation_sql_deadline

            class ExpiredClock:
                @staticmethod
                def monotonic():
                    return deadline.expires_at + 1

            expired.setattr(continuation_sql_deadline, "time", ExpiredClock)
            with pytest.raises(TimeoutError, match="original action/drain deadline"):
                await reader.read()
        assert len(calls) == 1 and reader.worker.drain_deadline is deadline
        assert owner.hard_expires_at > time.monotonic()
