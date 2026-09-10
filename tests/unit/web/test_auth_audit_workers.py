"""Reserved audit workers remain available and bounded during auth overload."""

from __future__ import annotations

import asyncio
import gc
import threading
from collections.abc import Callable
from concurrent.futures import Future

import pytest

from elspeth.web import async_workers


async def _wait_until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def _audit_outstanding() -> int:
    with async_workers._AUTH_AUDIT_WORKERS.lock:
        return async_workers._AUTH_AUDIT_WORKERS.outstanding


def test_auth_audit_progresses_when_general_pool_is_saturated() -> None:
    release = threading.Event()

    def held_work() -> None:
        assert release.wait(10)

    async def scenario() -> None:
        tasks = [asyncio.create_task(async_workers.run_sync_in_worker(held_work)) for _ in range(async_workers.ADMISSION_CAPACITY)]
        try:
            await _wait_until(lambda: async_workers.outstanding_admissions() == async_workers.ADMISSION_CAPACITY)
            with pytest.raises(async_workers.AsyncWorkerAdmissionTimeoutError):
                await async_workers.run_sync_in_worker(lambda: None)
            loop_thread = threading.get_ident()
            audit_thread = await asyncio.wait_for(async_workers.run_auth_audit_in_worker(threading.get_ident), timeout=2)
            assert audit_thread != loop_thread
            assert async_workers.outstanding_admissions() == async_workers.ADMISSION_CAPACITY
        finally:
            release.set()
            await asyncio.gather(*tasks)
            await asyncio.wait_for(async_workers.shutdown_async_workers(), timeout=5)

    asyncio.run(scenario())


def test_cancelled_audits_keep_capacity_and_queued_audits_still_run() -> None:
    release = threading.Event()
    started: list[int] = []
    completed: list[int] = []
    lock = threading.Lock()

    def record(index: int) -> None:
        with lock:
            started.append(index)
        assert release.wait(10)
        with lock:
            completed.append(index)

    def running_count() -> int:
        with lock:
            return len(started)

    async def scenario() -> None:
        tasks = [
            asyncio.create_task(async_workers.run_auth_audit_in_worker(record, index))
            for index in range(async_workers.AUTH_AUDIT_ADMISSION_CAPACITY)
        ]
        try:
            await _wait_until(lambda: _audit_outstanding() == async_workers.AUTH_AUDIT_ADMISSION_CAPACITY)
            await _wait_until(lambda: running_count() == async_workers.AUTH_AUDIT_MAX_WORKERS)
            for task in tasks:
                task.cancel()
            for task in tasks:
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert _audit_outstanding() == async_workers.AUTH_AUDIT_ADMISSION_CAPACITY
            refused_started = threading.Event()
            with pytest.raises(async_workers.AsyncWorkerAdmissionTimeoutError, match="auth audit worker pool saturated"):
                await async_workers.run_auth_audit_in_worker(refused_started.set)
            assert not refused_started.is_set()
            assert running_count() == async_workers.AUTH_AUDIT_MAX_WORKERS
            # Ordinary synchronous work and the event loop can still progress.
            assert await async_workers.run_sync_in_worker(lambda: "responsive") == "responsive"
            await asyncio.sleep(0)
            assert not release.is_set()
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.wait_for(async_workers.shutdown_async_workers(), timeout=5)
        assert sorted(completed) == list(range(async_workers.AUTH_AUDIT_ADMISSION_CAPACITY))
        assert _audit_outstanding() == 0

    asyncio.run(scenario())


def test_audit_wait_does_not_block_event_loop_and_shutdown_drains_it() -> None:
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    def record() -> None:
        started.set()
        assert release.wait(10)
        completed.set()

    async def scenario() -> None:
        task = asyncio.create_task(async_workers.run_auth_audit_in_worker(record))
        try:
            await _wait_until(started.is_set)
            # This loop turn must happen while the synchronous audit is held.
            assert not completed.is_set()
            shutdown = asyncio.create_task(async_workers.shutdown_async_workers())
            await asyncio.sleep(0.05)
            assert not shutdown.done()
            release.set()
            await asyncio.wait_for(shutdown, timeout=2)
            await task
            assert completed.is_set()
            assert async_workers._AUTH_AUDIT_WORKERS.executor is None
            assert async_workers._SHARED_EXECUTOR is None
        finally:
            release.set()
            await task
            await asyncio.wait_for(async_workers.shutdown_async_workers(), timeout=5)

    asyncio.run(scenario())


def test_audit_errors_propagate_and_capacity_recovers() -> None:
    def failed_audit() -> None:
        raise RuntimeError("audit persistence failed")

    def successful_audit(*, request_id: str) -> str:
        return request_id

    async def scenario() -> None:
        try:
            with pytest.raises(RuntimeError, match="audit persistence failed"):
                await async_workers.run_auth_audit_in_worker(failed_audit)
            assert await async_workers.run_auth_audit_in_worker(successful_audit, request_id="audit-request") == "audit-request"
            assert _audit_outstanding() == 0
        finally:
            await asyncio.wait_for(async_workers.shutdown_async_workers(), timeout=5)

    asyncio.run(scenario())


def test_completed_failure_is_observed_when_cancellation_wins_waiter_race() -> None:
    messages: list[str] = []

    def record_loop_error(_loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
        messages.append(str(context["message"]))

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(record_loop_error)
        completed: Future[None] = Future()
        completed.set_exception(RuntimeError("audit failed just before disconnect"))
        task = asyncio.create_task(async_workers._await_worker_future(completed, cancel_queued=False))
        # The waiter starts first; cancellation is delivered after the bridge
        # has copied the completed concurrent future's exception.
        loop.call_soon(task.cancel)
        with pytest.raises(asyncio.CancelledError):
            await task
        del task
        await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)

    asyncio.run(scenario())
    gc.collect()
    assert messages == []
