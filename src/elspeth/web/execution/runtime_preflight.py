"""Async coordination for runtime-equivalent composer preflight.

This module is transport-neutral L3 infrastructure. HTTP composer and
composer MCP can share the same process-local coordinator when they share a
logical session. Standalone MCP processes cannot share this lock with the web
process; cross-process safety comes from side-effect-free preflight mode.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.execution.schemas import ValidationResult

# Unconfigured stdio hosts need stdlib's stderr fallback. Configured hosts
# retain their existing root handlers and structured formatting.
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RuntimePreflightKey:
    session_scope: str
    state_version: int
    state_content_hash: str
    settings_hash: str
    # True for the authoring-masked re-validation that verifies a pending
    # interpretation-review handoff (elspeth-5a372d3267). Keyed separately so
    # a tolerant entry never satisfies a strict lookup or vice versa.
    interpretation_tolerant: bool = False
    # Blob evidence is authorized under this operation. A worker can outlive
    # its caller, but its verdict must never satisfy a different operation.
    session_operation_context: SessionOperationContext | None = None


@dataclass(frozen=True, slots=True)
class RuntimePreflightFailure:
    original_exc: Exception


RuntimePreflightEntry = ValidationResult | RuntimePreflightFailure
RuntimePreflightWorker = Callable[[], Awaitable[ValidationResult]]


class RuntimePreflightUnavailable(RuntimeError):
    """The lifecycle sealed preflight admission before this request arrived."""


def _shield[T](task: asyncio.Task[T]) -> asyncio.Future[T]:
    """Await ``task`` without letting the awaiter's cancellation reach it.

    ``asyncio.shield`` semantics, minus one Python 3.14 addition: once a
    shielded awaiter is cancelled, 3.14's ``shield`` hands any later task
    exception to the loop exception handler, which logs it with its message
    and traceback. This coordinator owns every task outcome and reports an
    abandoned failure by class only, so that log would publish worker content
    the coordinator deliberately withholds.
    """
    if task.done():
        # As ``asyncio.shield``: a finished task is its own outcome, which
        # lets a zero-budget ``wait_for`` still deliver a completed worker.
        return task
    outer: asyncio.Future[T] = task.get_loop().create_future()

    def _relay(inner: asyncio.Task[T]) -> None:
        if outer.cancelled():
            return
        if inner.cancelled():
            outer.cancel()
            return
        failure = inner.exception()
        if failure is not None:
            outer.set_exception(failure)
        else:
            outer.set_result(inner.result())

    def _detach(_outer: asyncio.Future[T]) -> None:
        if not task.done():
            task.remove_done_callback(_relay)

    task.add_done_callback(_relay)
    outer.add_done_callback(_detach)
    return outer


@dataclass(slots=True)
class _RetainedPreflight:
    task: asyncio.Task[RuntimePreflightEntry]
    awaiters_settled: asyncio.Event
    awaiters: int = 0
    observed: bool = False
    evicted: bool = False
    delivered: bool = False
    join_owned: bool = False
    failure: BaseException | None = None
    ordinary_failure_class: str | None = None


class RuntimePreflightCoordinator:
    """Deduplicate in-flight runtime preflight for one Python process.

    Admission follows the WORKER's lifetime, not the awaiter's
    (elspeth-5269b43bca): a caller whose ``timeout`` expires receives a
    ``RuntimePreflightFailure`` carrying the ``TimeoutError``, but the
    in-flight entry stays until the underlying worker actually finishes, so
    a same-key retry joins the running worker instead of submitting a second
    copy to the shared thread pool. The entry is evicted exactly once, at
    actual completion.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._inflight: dict[RuntimePreflightKey, asyncio.Task[RuntimePreflightEntry]] = {}
        self._retained: dict[asyncio.Task[RuntimePreflightEntry], _RetainedPreflight] = {}
        self._sealed = False
        self._join_task: asyncio.Task[tuple[BaseException, ...]] | None = None

    async def run(
        self,
        key: RuntimePreflightKey,
        worker: RuntimePreflightWorker,
        *,
        timeout: float | None = None,
    ) -> RuntimePreflightEntry:
        """Join or start the in-flight preflight for ``key``.

        ``worker`` must be the UNTIMED preflight coroutine; the per-caller
        budget is ``timeout`` (seconds, event-loop clock). Wrapping the
        timeout inside ``worker`` would make its expiry the shared task's
        outcome — evicting the entry while the sync work still runs — which
        is exactly the early release this coordinator exists to prevent.
        """
        async with self._lock:
            if self._sealed:
                return RuntimePreflightFailure(RuntimePreflightUnavailable("runtime preflight admission is sealed"))
            if key not in self._inflight:
                if timeout is not None and timeout <= 0:
                    # No budget left and nothing to join: do not admit work
                    # this caller will never await.
                    return RuntimePreflightFailure(TimeoutError("runtime preflight budget exhausted before admission"))
                task = asyncio.create_task(self._capture(worker))
                self._inflight[key] = task
                retained = _RetainedPreflight(task, asyncio.Event())
                self._retained[task] = retained

                # Drop the in-flight entry as soon as the task finishes, even
                # if all awaiters have been cancelled. Without this callback,
                # a mid-flight cancellation followed by no future caller (the
                # common case, because state_version rotates on every state
                # mutation) would leak the entry for the life of the process.
                # The membership check, identity comparison, and `del` below
                # run with no intervening await, so they are atomic under the
                # GIL; the identity guard prevents deleting a replacement task
                # scheduled by a later run() after this one was evicted.
                def _evict(finished: asyncio.Task[RuntimePreflightEntry]) -> None:
                    self._observe(retained)
                    if key in self._inflight and self._inflight[key] is finished:
                        del self._inflight[key]
                    retained.evicted = True
                    self._release(retained)

                task.add_done_callback(_evict)

            task = self._inflight[key]
            retained = self._retained[task]
            retained.awaiters += 1
            retained.awaiters_settled.clear()

        caller = asyncio.current_task()
        assert caller is not None
        cancellations_before = caller.cancelling()
        try:
            if timeout is None:
                result = await _shield(task)
            else:
                try:
                    # Expiry cancels only the shield, never the shared task.
                    result = await asyncio.wait_for(_shield(task), timeout=timeout)
                except TimeoutError as exc:
                    return RuntimePreflightFailure(exc)
        except BaseException:
            # A caller's cancellation does not deliver the worker's outcome.
            # A cancellation raised by the worker itself does: shield creates
            # a replacement CancelledError, so recover the retained original.
            if task.done() and caller.cancelling() == cancellations_before:
                self._observe(retained)
                self._delivered(retained)
                assert retained.failure is not None
                raise retained.failure from None
            raise
        else:
            self._delivered(retained)
            return result
        finally:
            retained.awaiters -= 1
            if retained.awaiters == 0:
                retained.awaiters_settled.set()
            self._release(retained)

    def _observe(self, retained: _RetainedPreflight) -> None:
        """Retrieve each task outcome once, even when eviction wins the race."""
        if retained.observed:
            return
        retained.observed = True
        try:
            result = retained.task.result()
        except BaseException as failure:
            retained.failure = failure
        else:
            if isinstance(result, RuntimePreflightFailure):
                retained.ordinary_failure_class = type(result.original_exc).__name__

    def _delivered(self, retained: _RetainedPreflight) -> None:
        retained.delivered = True
        self._release(retained)

    def _release(self, retained: _RetainedPreflight) -> None:
        if retained.evicted and retained.awaiters == 0 and retained.observed and retained.task in self._retained:
            # No caller can report an abandoned result, and preflight has no
            # pipeline run to audit. Report that lost error-delivery channel
            # with class-only operational evidence, never user content or a
            # traceback. Completed history must not accumulate in Web's
            # long-lived coordinator. A live join owns actual task failures.
            if not retained.delivered:
                if retained.ordinary_failure_class is not None:
                    logger.error("runtime_preflight_abandoned_failure exc_class=%s", retained.ordinary_failure_class)
                elif retained.failure is not None and not retained.join_owned:
                    logger.error("runtime_preflight_abandoned_task_failure exc_class=%s", type(retained.failure).__name__)
            del self._retained[retained.task]

    async def _join(self, retained: tuple[_RetainedPreflight, ...]) -> tuple[BaseException, ...]:
        if retained:
            await asyncio.wait([entry.task for entry in retained])
        failures: list[BaseException] = []
        for entry in retained:
            self._observe(entry)
            # Let existing run() callers receive the outcome before deciding
            # whether it was abandoned. Their cancellation never releases work.
            if entry.awaiters:
                await entry.awaiters_settled.wait()
            if not entry.delivered and entry.failure is not None and all(entry.failure is not failure for failure in failures):
                failures.append(entry.failure)
        return tuple(failures)

    async def seal_and_join(self) -> None:
        """Permanently refuse new admissions and settle all admitted preflight.

        Sealing and run admission share one lock. Already admitted callers keep
        their result; later calls receive RuntimePreflightFailure containing
        RuntimePreflightUnavailable, including calls for an existing key.
        Join never cancels workers, and repeated calls share one retained join.
        Caller cancellation is deferred until every owned task and existing
        awaiter has settled, then re-raised with its original identity.

        Task outcomes are retrieved at completion even after every caller has
        left. Ordinary failure envelopes remain result values: an abandoned
        one is reported once using its exception class and released after
        eviction and waiter settlement, never raised as a cleanup error.
        Already reported/released tasks are completed history, not unresolved
        work. A join retains its snapshot of unfinished task/awaiter obligations
        and raises genuine undelivered task failures with caller cancellation.
        Abandoned task failures completing outside a join are also reported
        and released. No unrelated host tasks or historical archive are joined.
        """
        async with self._lock:
            self._sealed = True
            if self._join_task is None:
                retained = tuple(self._retained.values())
                for entry in retained:
                    entry.join_owned = True
                self._join_task = asyncio.create_task(self._join(retained))
            task = self._join_task
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await _shield(task)
            except asyncio.CancelledError as original:
                if cancellation is None:
                    cancellation = original
        failures = list(task.result())
        if cancellation is not None and all(cancellation is not failure for failure in failures):
            failures.append(cancellation)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("Runtime preflight join obligations failed", failures)

    async def _capture(self, worker: RuntimePreflightWorker) -> RuntimePreflightEntry:
        try:
            return await worker()
        except Exception as exc:
            return RuntimePreflightFailure(exc)
