"""Async helpers for running bounded synchronous work off the event loop."""

from __future__ import annotations

import asyncio
import functools
import threading
from collections.abc import Callable
from concurrent.futures import Future as ConcurrentFuture
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Final

#: Threads in the process-wide worker pool.
MAX_WORKERS: Final[int] = 16
#: Submissions allowed to sit queued behind busy threads. Together with
#: ``MAX_WORKERS`` this bounds outstanding admissions (running + queued);
#: everything past it is rejected fast rather than queued for nobody.
MAX_QUEUED: Final[int] = 16
ADMISSION_CAPACITY: Final[int] = MAX_WORKERS + MAX_QUEUED
#: How long a caller waits for an admission slot before it is rejected.
#: Healthy work is millisecond-scale, so this only bites once the pool is
#: saturated by work that is not finishing — the case where waiting longer
#: cannot help and hanging the caller hides the fault.
ADMISSION_WAIT_SECONDS: Final[float] = 1.0
_ADMISSION_POLL_SECONDS: Final[float] = 0.02

_SHARED_EXECUTOR: ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()
# Outstanding admissions: submissions whose sync work has not yet finished
# (queued or running). Guarded by ``_EXECUTOR_LOCK`` because it is released
# from worker threads, and process-wide because the pool is process-wide.
_OUTSTANDING_ADMISSIONS = 0

AUTH_AUDIT_MAX_WORKERS: Final[int] = 2
AUTH_AUDIT_ADMISSION_CAPACITY: Final[int] = 4


class _AuthAuditWorkers:
    """Reserved capacity for must-fire refusals when ordinary workers are full."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.executor: ThreadPoolExecutor | None = None
        self.outstanding = 0

    def get_executor(self) -> ThreadPoolExecutor:
        with self.lock:
            if self.executor is None:
                self.executor = ThreadPoolExecutor(max_workers=AUTH_AUDIT_MAX_WORKERS, thread_name_prefix="auth-audit")
            return self.executor

    def try_admit(self) -> bool:
        with self.lock:
            if self.outstanding >= AUTH_AUDIT_ADMISSION_CAPACITY:
                return False
            self.outstanding += 1
            return True

    def release(self) -> None:
        with self.lock:
            self.outstanding -= 1

    def release_when_finished(self, _finished: ConcurrentFuture[Any]) -> None:
        self.release()

    def detach_executor(self) -> ThreadPoolExecutor | None:
        with self.lock:
            executor = self.executor
            self.executor = None
            return executor


_AUTH_AUDIT_WORKERS = _AuthAuditWorkers()


class AsyncWorkerAdmissionTimeoutError(TimeoutError):
    """The shared worker pool could not admit new work within the bounded wait.

    Raised instead of queueing indefinitely once ``ADMISSION_CAPACITY``
    submissions are outstanding — typically because earlier workers are hung
    and their callers have already timed out (elspeth-5269b43bca). It is a
    ``TimeoutError`` on purpose: every deadline-bounded caller already
    classifies a bounded wait that expired as its own TIMEOUT outcome.
    """


def _get_shared_executor() -> ThreadPoolExecutor:
    """Return the process-wide bounded worker pool, constructing it once.

    ``run_sync_in_worker`` previously built a fresh ``ThreadPoolExecutor`` on
    every call, so N concurrent callers spawned N unbounded threads. A single
    bounded pool caps that at ``max_workers``. It is safe against re-entrant
    deadlock because ``run_sync_in_worker`` requires a running event loop and
    therefore cannot be called from inside a worker thread.
    """
    global _SHARED_EXECUTOR
    if _SHARED_EXECUTOR is None:
        with _EXECUTOR_LOCK:
            if _SHARED_EXECUTOR is None:
                _SHARED_EXECUTOR = ThreadPoolExecutor(
                    max_workers=MAX_WORKERS,
                    thread_name_prefix="async-worker",
                )
    return _SHARED_EXECUTOR


async def shutdown_async_workers() -> None:
    """Drain both worker pools (called at app lifespan shutdown)."""
    global _SHARED_EXECUTOR
    loop = asyncio.get_running_loop()
    executors = []
    if _SHARED_EXECUTOR is not None:
        executors.append(_SHARED_EXECUTOR)
        _SHARED_EXECUTOR = None
    audit_executor = _AUTH_AUDIT_WORKERS.detach_executor()
    if audit_executor is not None:
        executors.append(audit_executor)
    await asyncio.gather(*(loop.run_in_executor(None, executor.shutdown, True) for executor in executors))


def outstanding_admissions() -> int:
    """Return submissions whose sync work has not finished (queued + running)."""
    with _EXECUTOR_LOCK:
        return _OUTSTANDING_ADMISSIONS


def _try_admit() -> bool:
    global _OUTSTANDING_ADMISSIONS
    with _EXECUTOR_LOCK:
        if _OUTSTANDING_ADMISSIONS >= ADMISSION_CAPACITY:
            return False
        _OUTSTANDING_ADMISSIONS += 1
        return True


def _release_admission() -> None:
    global _OUTSTANDING_ADMISSIONS
    with _EXECUTOR_LOCK:
        _OUTSTANDING_ADMISSIONS -= 1


def _release_admission_when_finished(_finished: ConcurrentFuture[Any]) -> None:
    """Free one admission slot when the sync work actually finishes.

    Attached to the *concurrent* future so it fires on the worker thread at
    completion — or immediately when a still-queued submission is cancelled —
    regardless of whether the awaiting event loop is still alive. Admission
    therefore follows the worker's lifetime, never the awaiter's.
    """
    _release_admission()


async def _acquire_admission() -> None:
    """Wait a bounded time for an admission slot, then reject.

    Polls rather than binding an ``asyncio`` primitive to one loop: the
    admission count is process-wide (the pool is process-wide, and slots are
    released from worker threads), and this helper must stay usable from any
    running loop.
    """
    await _wait_for_admission(_try_admit, ADMISSION_CAPACITY, "async worker")


async def _wait_for_admission(try_admit: Callable[[], bool], capacity: int, pool_name: str) -> None:
    if try_admit():
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ADMISSION_WAIT_SECONDS
    while True:
        await asyncio.sleep(_ADMISSION_POLL_SECONDS)
        if try_admit():
            return
        if loop.time() >= deadline:
            raise AsyncWorkerAdmissionTimeoutError(
                f"{pool_name} pool saturated: {capacity} submissions outstanding for {ADMISSION_WAIT_SECONDS}s"
            )


async def run_sync_in_worker[**P, T](func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Run synchronous work on the shared bounded worker pool without blocking the loop.

    Admission is bounded through the WORKER's lifetime (elspeth-5269b43bca):
    a caller that times out or is cancelled does not free the slot its sync
    work still occupies, so a retry storm against hung workers stops at
    ``ADMISSION_CAPACITY`` with a fast ``AsyncWorkerAdmissionTimeoutError``
    instead of queueing behind them indefinitely and starving unrelated
    worker-backed calls.

    The short wait loop keeps an explicit event-loop timer active while the
    worker runs.  This preserves the normal async-over-sync contract even in
    sandboxed runtimes where executor completion can fail to wake the selector
    promptly.
    """
    executor = _get_shared_executor()
    await _acquire_admission()
    try:
        concurrent_future = executor.submit(functools.partial(func, *args, **kwargs))
    except BaseException:
        _release_admission()
        raise
    concurrent_future.add_done_callback(_release_admission_when_finished)
    return await _await_worker_future(concurrent_future, cancel_queued=True)


async def run_auth_audit_in_worker[**P, T](func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Record a refusal off-loop using capacity reserved from ordinary work.

    At most two audits run and two wait. Admission failure propagates: an
    unaudited refusal must not be reported as successfully audited. Once
    admitted, audit work survives caller cancellation even while queued.
    Its slot is released by the concurrent future only when work finishes.
    """
    executor = _AUTH_AUDIT_WORKERS.get_executor()
    await _wait_for_admission(_AUTH_AUDIT_WORKERS.try_admit, AUTH_AUDIT_ADMISSION_CAPACITY, "auth audit worker")
    try:
        concurrent_future = executor.submit(functools.partial(func, *args, **kwargs))
    except BaseException:
        _AUTH_AUDIT_WORKERS.release()
        raise
    concurrent_future.add_done_callback(_AUTH_AUDIT_WORKERS.release_when_finished)
    return await _await_worker_future(concurrent_future, cancel_queued=False)


def _retrieve_abandoned_exception(future: asyncio.Future[Any]) -> None:
    """Consume the outcome after the audit caller has disconnected."""
    if not future.cancelled():
        future.exception()


async def _await_worker_future[T](concurrent_future: ConcurrentFuture[T], *, cancel_queued: bool) -> T:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[T] = asyncio.wrap_future(concurrent_future, loop=loop)
    try:
        # ``asyncio.wait`` returns ``(done, pending)`` on each 0.1s tick rather
        # than raising on timeout, so there is no error-shaped sentinel to
        # catch — the empty ``done`` set is the explicit "not finished yet"
        # signal and we simply loop again. The short timeout keeps an explicit
        # event-loop timer active so the selector wakes promptly even in
        # sandboxed runtimes. Unlike ``wait_for(shield(...))``, ``asyncio.wait``
        # never cancels the future it is waiting on, so no ``shield`` wrapper is
        # needed to keep the worker alive across a caller cancellation.
        while True:
            done, _pending = await asyncio.wait({future}, timeout=0.1)
            if done:
                # ``future.result()`` retrieves the value or re-raises the
                # worker's exception on the awaited (non-cancelled) path.
                return future.result()
    finally:
        # For ordinary work abandoned mid-flight (cancellation, outer
        # timeout), cancel the wrapper:
        #
        # * still QUEUED — the concurrent future is cancelled too, so work
        #   nobody will read never occupies a thread, and its admission slot
        #   is released right away;
        # * already RUNNING — the thread cannot be interrupted and keeps its
        #   admission until it finishes, but the cancelled wrapper never
        #   receives the eventual result or exception, so a late failure
        #   cannot fire asyncio's "Future exception was never retrieved"
        #   handler (elspeth-e4949acbe1: that traceback surfaced in the
        #   journal as a bogus request-id-middleware crash).
        #
        # The abandoned outcome is intentionally discarded: the caller has
        # already given up on it. A real infrastructure fault surfaces
        # through concurrent requests' own paths, not this echo. The executor
        # is the process-wide shared pool, so it is NOT shut down here —
        # teardown happens once via ``shutdown_async_workers()``.
        if future.done():
            # Completion can win the race with cancellation of the waiter.
            _retrieve_abandoned_exception(future)
        elif cancel_queued:
            future.cancel()
        else:
            # A refused request already owes this audit. Keep queued work
            # as well as running work; observe any late exception without
            # publishing a second response to a disconnected caller.
            future.add_done_callback(_retrieve_abandoned_exception)
