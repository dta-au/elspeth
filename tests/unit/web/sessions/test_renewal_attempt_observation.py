"""Draft local controls for snapshot renewal joins without stopping ticks."""

from __future__ import annotations

import asyncio
import threading
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.coordination import lifecycle
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkAuthority, RequiredWorkCoordinator, RequiredWorkSource
from elspeth.web.sessions.service import ComposerTerminalSQLCompletionUnknown


class _HeldRenewalAuthority:
    def __init__(self, error: BaseException | None) -> None:
        self.entered = threading.Event()
        self.release_renewal = threading.Event()
        self.error = error
        self.released: list[SessionOperationContext] = []

    def renew(self, context: SessionOperationContext, *, lease_seconds: int) -> SessionOperationContext:
        self.entered.set()
        self.release_renewal.wait()
        if self.error is not None:
            raise self.error
        return context

    def release(self, context: SessionOperationContext) -> None:
        self.released.append(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("sql_fails", (False, True))
async def test_current_required_renewal_joins_actual_sql_and_original_cancels_without_stopping_ticks(monkeypatch, sql_fails) -> None:
    context = SessionOperationContext(SessionOperationFence(str(uuid4()), str(uuid4()), "owned renewal", 1), SessionOperationKind.COMPOSE)
    coordinator = RequiredWorkCoordinator(RequiredWorkAuthority(RequiredAuthorityKind.DURABLE_COMPOSE, context, str(uuid4()), 1))
    original = OperationalError("controlled renew", {}, RuntimeError("controlled driver"))
    authority = _HeldRenewalAuthority(original if sql_fails else None)
    lease = lifecycle.SessionOperationLease(authority, context, lease_seconds=30, renew_interval_seconds=0.01, required_work=coordinator)
    production_sleep = asyncio.sleep
    delivered: list[asyncio.CancelledError] = []
    observer_started = asyncio.Event()

    async def capture_sleep(seconds):
        current = asyncio.current_task()
        if current is not None and current.get_name() == "owned-renewal-observer":
            observer_started.set()
        try:
            await production_sleep(seconds)
        except asyncio.CancelledError as cancellation:
            current = asyncio.current_task()
            if current is not None and current.get_name() == "owned-renewal-observer":
                delivered.append(cancellation)
            raise

    observer = None
    try:
        while not authority.entered.is_set():
            await production_sleep(0.001)
        assert lease._renewal_attempt is not None
        snapshot = lease._renewal_attempt
        assert snapshot.task is not None and not snapshot.task.done()
        monkeypatch.setattr(lifecycle.asyncio, "sleep", capture_sleep)
        observer = asyncio.create_task(lease.observe_current_renewal_attempt(), name="owned-renewal-observer")
        await observer_started.wait()
        for index in range(3):
            observer.cancel(f"renewal observer original {index}")
            while len(delivered) <= index:
                await production_sleep(0.001)
            assert not observer.done() and not snapshot.task.done()
            assert not lease._stop_renewal.is_set()
        authority.release_renewal.set()
        errors = await observer
        assert len(errors) == 3 + int(sql_fails)
        assert all(actual is expected for actual, expected in zip(errors[:3], delivered, strict=True))
        assert snapshot.outcome_observed and snapshot.task.done()
        if sql_fails:
            assert errors[-1] is original and snapshot.error is original
        else:
            assert snapshot.returned is context
            assert not lease._stop_renewal.is_set() and not lease._renewal_task.done()
    finally:
        authority.release_renewal.set()
        if observer is not None and not observer.done():
            await observer
        if sql_fails:
            with pytest.raises(OperationalError) as failure:
                await lease.close()
            assert failure.value is original
        else:
            await lease.close()
            assert authority.released == [context]


@pytest.mark.asyncio
async def test_unentered_cancelled_renewal_task_has_no_successful_receipt() -> None:
    context = SessionOperationContext(SessionOperationFence(str(uuid4()), str(uuid4()), "owned renewal", 1), SessionOperationKind.COMPOSE)
    coordinator = RequiredWorkCoordinator(RequiredWorkAuthority(RequiredAuthorityKind.DURABLE_COMPOSE, context, str(uuid4()), 1))
    ticket = coordinator.reserve(RequiredWorkSource.LEASE_RENEWAL)
    attempt = lifecycle._RenewalAttemptObservation(context, ticket)

    async def must_not_enter():
        raise AssertionError("Cancelled-before-entry task ran")

    attempt.task = asyncio.create_task(must_not_enter())
    attempt.task.cancel("controlled before entry")
    errors = await lifecycle._join_renewal_attempt_observation(attempt)
    assert len(errors) == 1 and isinstance(errors[0], ComposerTerminalSQLCompletionUnknown)
    assert isinstance(errors[0].__cause__, asyncio.CancelledError)
    assert not attempt.outcome_observed and not ticket.complete and not coordinator.all_completed


@pytest.mark.asyncio
async def test_factory_allocates_then_raises_retains_pending_physical_work_and_nominal_barrier(monkeypatch) -> None:
    context = SessionOperationContext(SessionOperationFence(str(uuid4()), str(uuid4()), "owned renewal", 1), SessionOperationKind.COMPOSE)
    coordinator = RequiredWorkCoordinator(RequiredWorkAuthority(RequiredAuthorityKind.DURABLE_COMPOSE, context, str(uuid4()), 1))
    authority = _HeldRenewalAuthority(None)
    original = RuntimeError("controlled task factory allocated then raised")
    production_create_task = asyncio.create_task
    production_sleep = asyncio.sleep
    allocated: list[asyncio.Task] = []

    def allocate_then_raise(coroutine, *args, **kwargs):
        task = production_create_task(coroutine, *args, **kwargs)
        if kwargs.get("name") == "session-operation-renewal-attempt":
            allocated.append(task)
            raise original
        return task

    monkeypatch.setattr(lifecycle.asyncio, "create_task", allocate_then_raise)
    lease = lifecycle.SessionOperationLease(authority, context, lease_seconds=30, renew_interval_seconds=0.01, required_work=coordinator)
    try:
        while not authority.entered.is_set():
            await production_sleep(0.001)
        errors = await lease.observe_current_renewal_attempt()
        assert len(errors) == 1 and isinstance(errors[0], ComposerTerminalSQLCompletionUnknown)
        assert errors[0].__cause__ is original
        assert lease._renewal_attempt is not None and lease._renewal_attempt.task is None
        assert len(allocated) == 1 and not allocated[0].done()
        assert not coordinator.all_completed and authority.released == []
        authority.release_renewal.set()
        await allocated[0]
        retained = await lease.observe_current_renewal_attempt()
        assert retained[0] is errors[0]
        assert authority.released == []
    finally:
        authority.release_renewal.set()
        if allocated and not allocated[0].done():
            await allocated[0]
        with pytest.raises(ComposerTerminalSQLCompletionUnknown) as failure:
            await lease.close()
        assert failure.value.__cause__ is original
        assert authority.released == []
