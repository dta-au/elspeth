"""Tests for run_sync_in_worker — async-over-sync executor bridge.

Pins the cancellation contract that triggered elspeth-e4949acbe1: when a
caller is cancelled (outer asyncio.wait_for timeout, or a CancelledError
from the request task itself), the shielded future continues running on
its worker thread. If that thread eventually raises, the asyncio Future
holds an unretrieved exception, and Python's GC emits a misleading
"Future exception was never retrieved" traceback through the asyncio
exception handler — operators saw this surface as a request-id
middleware traceback during composer load because the most recent
in-stack frame was the middleware's ``await call_next(request)``.

We intercept the asyncio loop's exception handler directly because
``redirect_stderr`` does not capture this output: asyncio routes the
warning through ``logger.getLogger("asyncio")`` whose default handler
holds a reference to the pre-redirect ``sys.stderr``.

The fix (as of elspeth-5269b43bca) is to cancel the abandoned wrapper
future in ``finally``: a cancelled ``asyncio`` future never receives the
worker's late exception, so there is nothing left unretrieved — and a
submission still queued is dropped before it ever occupies a thread. These
tests pin that contract so a future refactor of ``async_workers.py`` cannot
reintroduce the journal noise.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from elspeth.web.async_workers import run_sync_in_worker


class _LoopExceptionRecorder:
    """Capture asyncio loop exception events by replacing the handler."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._previous: Any | None = None

    def install(self, loop: asyncio.AbstractEventLoop) -> None:
        self._previous = loop.get_exception_handler()
        loop.set_exception_handler(self._handle)

    def uninstall(self, loop: asyncio.AbstractEventLoop) -> None:
        loop.set_exception_handler(self._previous)

    def _handle(self, loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        self.events.append(dict(context))

    def messages(self) -> list[str]:
        return [str(event.get("message", "")) for event in self.events]

    def has_unretrieved_future_exception(self) -> bool:
        return any("Future exception was never retrieved" in msg for msg in self.messages())


def _slow_then_raise(seconds: float, message: str) -> str:
    """Worker that sleeps and then raises — models a sync op that fails late."""
    time.sleep(seconds)
    raise RuntimeError(message)


def _slow_success(seconds: float) -> str:
    time.sleep(seconds)
    return "ok"


def _run_in_isolated_loop(coro_factory) -> _LoopExceptionRecorder:
    """Run ``coro_factory()`` in a fresh event loop and return the recorded
    exception events.

    A fresh loop is used because asyncio's "Future exception was never
    retrieved" event fires from ``Future.__del__`` during loop GC/close,
    not during normal coroutine execution. With pytest-asyncio's shared
    per-test loop, the warning would fire AFTER the test function returns
    — too late for in-test asserts. By owning the loop ourselves, we can
    observe events emitted during ``loop.close()`` while our exception
    handler is still installed.
    """
    recorder = _LoopExceptionRecorder()
    loop = asyncio.new_event_loop()
    recorder.install(loop)
    try:
        loop.run_until_complete(coro_factory())
        # Drain any callbacks (including future-finalisation callbacks)
        # that were scheduled but not yet run.
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()
    return recorder


def test_normal_completion_returns_value() -> None:
    """Sanity: happy path returns the worker's value with no events."""

    async def scenario() -> None:
        result = await run_sync_in_worker(_slow_success, 0.05)
        assert result == "ok"

    recorder = _run_in_isolated_loop(scenario)
    assert recorder.events == []


def test_normal_exception_propagates() -> None:
    """Sanity: when sync work raises and the caller awaits, the exception
    propagates and is consumed normally (no loop exception event)."""

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="propagated cleanly"):
            await run_sync_in_worker(_slow_then_raise, 0.05, "propagated cleanly")

    recorder = _run_in_isolated_loop(scenario)
    assert not recorder.has_unretrieved_future_exception()


def test_outer_timeout_cancels_during_failing_sync_does_not_warn() -> None:
    """The elspeth-e4949acbe1 reproduction.

    Outer wait_for cancels the run_sync_in_worker await; the worker
    thread eventually raises; the shielded future is no longer awaited;
    asyncio MUST NOT emit a "Future exception was never retrieved" event
    via the loop's exception handler.

    Composer routes wrap ``run_sync_in_worker(_runtime_preflight, ...)``
    in ``asyncio.wait_for(timeout=composer_runtime_preflight_timeout_seconds)``.
    A timeout there + a real validation error in the underlying preflight
    is the production trigger that produced the misleading request_id
    middleware traceback in the journal.
    """

    async def scenario() -> None:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                run_sync_in_worker(_slow_then_raise, 1.0, "thread raised after cancel"),
                timeout=0.2,
            )
        # Let the worker thread finish so the future transitions to done.
        await asyncio.sleep(1.5)

    recorder = _run_in_isolated_loop(scenario)
    assert not recorder.has_unretrieved_future_exception(), (
        "run_sync_in_worker did not drain the shielded future's exception. "
        "asyncio fired its 'Future exception was never retrieved' handler "
        "with this context history:\n"
        + "\n".join(f"  - {msg}" for msg in recorder.messages())
        + "\n\nThis regression surfaces in the production journal as a "
        "traceback whose most recent in-stack frame is request_id.py:98 "
        "(the request-id middleware's await call_next), making operators "
        "believe the middleware crashed when it did not."
    )


def test_direct_cancel_during_failing_sync_does_not_warn() -> None:
    """Same contract as the outer-timeout test, but the cancel originates
    from a direct ``task.cancel()`` — modelling the new client-disconnect
    cancellation path added with elspeth-29e8bd8a1f. The drain MUST hold
    on this entry point too or the in-flight observability work will
    actively make the journal noise worse.
    """

    async def scenario() -> None:
        task = asyncio.create_task(run_sync_in_worker(_slow_then_raise, 1.0, "thread raised after task cancel"))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(1.5)

    recorder = _run_in_isolated_loop(scenario)
    assert not recorder.has_unretrieved_future_exception(), "run_sync_in_worker did not drain under direct task.cancel():\n" + "\n".join(
        f"  - {msg}" for msg in recorder.messages()
    )


def test_uses_shared_bounded_executor() -> None:
    """run_sync_in_worker shares one bounded pool instead of a new per-call one.

    Concurrent calls run on threads from the same "async-worker" pool, and
    shutdown_async_workers() tears it down so a fresh pool is lazily rebuilt.
    """

    async def scenario() -> None:
        def _get_thread_info() -> tuple[int, str]:
            return threading.get_ident(), threading.current_thread().name

        tasks = [asyncio.create_task(run_sync_in_worker(_get_thread_info)) for _ in range(5)]
        results = await asyncio.gather(*tasks)

        for _, name in results:
            assert name.startswith("async-worker")

        import elspeth.web.async_workers as async_workers

        assert async_workers._SHARED_EXECUTOR is not None
        await async_workers.shutdown_async_workers()
        assert async_workers._SHARED_EXECUTOR is None

    _run_in_isolated_loop(scenario)


# ---------------------------------------------------------------------------
# elspeth-5269b43bca — admission is bounded by WORKER lifetime, not awaiter
# lifetime.  A caller that times out and gives up does not free the slot its
# sync work still occupies; the pool therefore stops admitting new work at a
# fixed ceiling instead of queueing forever, and every abandoned submission
# that had not yet started is dropped rather than left to run for nobody.
# ---------------------------------------------------------------------------


def _hung_worker(started: threading.Event, release: threading.Event, started_count: list[int], count_lock: threading.Lock) -> str:
    with count_lock:
        started_count[0] += 1
    started.set()
    release.wait()
    return "released"


def test_admission_is_bounded_while_timed_out_workers_continue() -> None:
    """Timed-out callers must not free admission; a saturated pool rejects fast.

    Phase A is the ticket's retry storm: every ``wait_for`` abandons its
    awaiter after 50ms while the sync worker stays parked on ``release``.
    Before the fix each retry queued another copy behind the 16 hung threads
    (unbounded queue depth). Now the hung threads keep their admission, the
    abandoned queued copies are dropped, and outstanding admissions equal the
    work that is actually running — never the number of retries.

    Phase B saturates the pool with LIVE awaiters (queued, not abandoned) and
    proves an unrelated worker-backed call is rejected within the bounded
    admission wait rather than sitting behind them until its client gives up.

    Phase C proves recovery once the hung workers finish.
    """
    from elspeth.web import async_workers

    release = threading.Event()
    started = threading.Event()
    started_count = [0]
    count_lock = threading.Lock()
    max_workers = async_workers.MAX_WORKERS
    capacity = async_workers.ADMISSION_CAPACITY

    async def settle() -> None:
        # ``_chain_future`` propagates the wrapper's cancellation to the
        # concurrent future through a loop callback, so give it a tick.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def scenario() -> None:
        assert async_workers.outstanding_admissions() == 0

        # Phase A — retry storm past capacity, every caller giving up.
        for _ in range(capacity + 8):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(
                    run_sync_in_worker(_hung_worker, started, release, started_count, count_lock),
                    timeout=0.05,
                )
            await settle()
            assert async_workers.outstanding_admissions() <= max_workers
        assert started_count[0] == max_workers, "abandoned queued retries must never start"
        assert async_workers.outstanding_admissions() == max_workers

        # Phase B — saturate with live awaiters and probe an unrelated call.
        queued = [
            asyncio.create_task(run_sync_in_worker(_hung_worker, started, release, started_count, count_lock))
            for _ in range(capacity - max_workers)
        ]
        await asyncio.sleep(0.05)
        assert async_workers.outstanding_admissions() == capacity
        unrelated_started = time.monotonic()
        with pytest.raises(async_workers.AsyncWorkerAdmissionTimeoutError):
            await run_sync_in_worker(lambda: "unrelated")
        elapsed = time.monotonic() - unrelated_started
        assert elapsed < async_workers.ADMISSION_WAIT_SECONDS + 1.0, elapsed
        # Rejection did not disturb the outstanding accounting.
        assert async_workers.outstanding_admissions() == capacity

        # Phase C — recovery once the hung workers finish.
        release.set()
        assert await asyncio.gather(*queued) == ["released"] * len(queued)
        deadline = time.monotonic() + 5.0
        while async_workers.outstanding_admissions() != 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert async_workers.outstanding_admissions() == 0
        assert started_count[0] == capacity
        assert await run_sync_in_worker(lambda: "unrelated") == "unrelated"

    try:
        _run_in_isolated_loop(scenario)
    finally:
        release.set()


def test_abandoned_queued_work_never_starts() -> None:
    """A submission abandoned before a thread picked it up must be dropped.

    Fill every worker thread with awaited (not abandoned) hung work, submit
    one more under a tiny ``wait_for``, and let it time out while still
    queued. When the pool drains, that queued function must not run: an
    abandoned request's work is not load-bearing and, left queued, it would
    hold admission and a thread for nobody.
    """
    from elspeth.web import async_workers

    release = threading.Event()
    started = threading.Event()
    started_count = [0]
    count_lock = threading.Lock()
    orphan_ran = threading.Event()

    def orphan() -> None:
        orphan_ran.set()

    async def scenario() -> None:
        assert async_workers.outstanding_admissions() == 0
        occupants = [
            asyncio.create_task(run_sync_in_worker(_hung_worker, started, release, started_count, count_lock))
            for _ in range(async_workers.MAX_WORKERS)
        ]
        deadline = time.monotonic() + 5.0
        while started_count[0] < async_workers.MAX_WORKERS and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert started_count[0] == async_workers.MAX_WORKERS

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(run_sync_in_worker(orphan), timeout=0.05)
        # The abandoned submission released its admission as soon as the
        # wrapper's cancellation reached the queued concurrent future (one
        # loop callback later) — nothing is left to finish.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert async_workers.outstanding_admissions() == async_workers.MAX_WORKERS

        release.set()
        assert await asyncio.gather(*occupants) == ["released"] * async_workers.MAX_WORKERS
        # Give the pool a moment to pick up anything still queued.
        await asyncio.sleep(0.2)
        assert not orphan_ran.is_set(), "abandoned queued work ran after its caller gave up"
        assert async_workers.outstanding_admissions() == 0

    try:
        _run_in_isolated_loop(scenario)
    finally:
        release.set()
