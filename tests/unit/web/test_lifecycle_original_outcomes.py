"""Actual Future joins preserve original lifecycle faults and cancellations."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web import async_workers
from elspeth.web.application_finalizers import ApplicationFinalizerKind, ApplicationFinalizerOwner


async def _wait_for(event: threading.Event) -> None:
    async with asyncio.timeout(2):
        while not event.is_set():
            await asyncio.sleep(0.005)


def _capture_cancellations(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.CancelledError]:
    observed: list[asyncio.CancelledError] = []
    retain = async_workers._retain_cancellation

    def capture(cancellations: list[asyncio.CancelledError], original: asyncio.CancelledError) -> None:
        observed.append(original)
        retain(cancellations, original)

    monkeypatch.setattr(async_workers, "_retain_cancellation", capture)
    return observed


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellation_count", [1, 3])
@pytest.mark.parametrize("fails", [False, True])
async def test_finalizer_retains_original_cancellation_objects_after_actual_join(
    cancellation_count: int,
    fails: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    original_failure = AuditIntegrityError("finalizer failed")
    calls = 0

    def finalizer() -> None:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(5)
        if fails:
            raise original_failure

    owner = ApplicationFinalizerOwner()
    capability = owner.register(ApplicationFinalizerKind.EXECUTION_EXECUTOR_JOIN, finalizer)
    owner.seal()
    monkeypatch.setattr(async_workers, "_APPLICATION_FINALIZER_OWNER", owner)
    async_workers._INSTANCE_DRAINING.set()
    cancellations = _capture_cancellations(monkeypatch)
    task = asyncio.create_task(async_workers.run_application_finalizer_in_worker(capability))
    try:
        await _wait_for(entered)
        for index in range(cancellation_count):
            task.cancel(f"cancellation-{index}")
            await asyncio.sleep(0.015)
        assert not task.done()
        assert async_workers.outstanding_admissions() == 1
    finally:
        release.set()
    expected = asyncio.CancelledError if cancellation_count == 1 and not fails else BaseExceptionGroup
    with pytest.raises(expected) as outcome:
        await task
    if isinstance(outcome.value, BaseExceptionGroup):
        assert len(outcome.value.exceptions) == cancellation_count + int(fails)
        assert all(left is right for left, right in zip(outcome.value.exceptions[:cancellation_count], cancellations, strict=True))
        if fails:
            assert outcome.value.exceptions[-1] is original_failure
    else:
        assert outcome.value is cancellations[0]
    assert calls == 1
    assert async_workers.outstanding_admissions() == 0
    await async_workers.shutdown_async_workers()


class _ControlledShutdownExecutor(ThreadPoolExecutor):
    def __init__(self, *, original_failure: BaseException | None = None) -> None:
        super().__init__(max_workers=1)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.original_failure = original_failure
        self.physically_joined = False

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        assert wait is True and cancel_futures is False
        self.calls += 1
        self.entered.set()
        assert self.release.wait(5)
        super().shutdown(wait=True, cancel_futures=False)
        self.physically_joined = True
        if self.original_failure is not None:
            raise self.original_failure


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellation_count", [0, 1, 3])
async def test_shutdown_fault_does_not_skip_sibling_join_and_keeps_all_originals(
    cancellation_count: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_failure = AuditIntegrityError("executor join failed")
    shared = _ControlledShutdownExecutor(original_failure=original_failure)
    audit = _ControlledShutdownExecutor()
    audit_workers = async_workers._AuthAuditWorkers()
    audit_workers.executor = audit
    monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", shared)
    monkeypatch.setattr(async_workers, "_AUTH_AUDIT_WORKERS", audit_workers)
    monkeypatch.setattr(async_workers, "_GENERATION_CUSTODIAN", None)
    cancellations = _capture_cancellations(monkeypatch)
    task = asyncio.create_task(async_workers.shutdown_async_workers())
    try:
        await _wait_for(shared.entered)
        await _wait_for(audit.entered)
        for index in range(cancellation_count):
            task.cancel(f"shutdown-cancellation-{index}")
            await asyncio.sleep(0.015)
        shared.release.set()
        await asyncio.sleep(0.03)
        assert shared.physically_joined
        assert not task.done()
        assert not audit.physically_joined
    finally:
        shared.release.set()
        audit.release.set()
    expected = AuditIntegrityError if cancellation_count == 0 else BaseExceptionGroup
    with pytest.raises(expected) as outcome:
        await task
    if isinstance(outcome.value, BaseExceptionGroup):
        assert len(outcome.value.exceptions) == cancellation_count + 1
        assert all(left is right for left, right in zip(outcome.value.exceptions[:-1], cancellations, strict=True))
        assert outcome.value.exceptions[-1] is original_failure
    else:
        assert outcome.value is original_failure
    assert shared.physically_joined and audit.physically_joined
    assert shared.calls == audit.calls == 1
    assert async_workers._SHARED_SHUTDOWN_STARTED


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellation_count", [1, 3])
async def test_successful_shutdown_keeps_original_cancellations_after_both_joins(
    cancellation_count: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = _ControlledShutdownExecutor()
    audit = _ControlledShutdownExecutor()
    audit_workers = async_workers._AuthAuditWorkers()
    audit_workers.executor = audit
    monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", shared)
    monkeypatch.setattr(async_workers, "_AUTH_AUDIT_WORKERS", audit_workers)
    monkeypatch.setattr(async_workers, "_GENERATION_CUSTODIAN", None)
    cancellations = _capture_cancellations(monkeypatch)
    task = asyncio.create_task(async_workers.shutdown_async_workers())
    try:
        await _wait_for(shared.entered)
        await _wait_for(audit.entered)
        for index in range(cancellation_count):
            task.cancel(f"success-cancellation-{index}")
            await asyncio.sleep(0.015)
        assert not task.done()
    finally:
        shared.release.set()
        audit.release.set()
    expected = asyncio.CancelledError if cancellation_count == 1 else BaseExceptionGroup
    with pytest.raises(expected) as outcome:
        await task
    if isinstance(outcome.value, BaseExceptionGroup):
        assert all(left is right for left, right in zip(outcome.value.exceptions, cancellations, strict=True))
    else:
        assert outcome.value is cancellations[0]
    assert shared.calls == audit.calls == 1
    assert shared.physically_joined and audit.physically_joined
    assert async_workers._SHARED_EXECUTOR is None
