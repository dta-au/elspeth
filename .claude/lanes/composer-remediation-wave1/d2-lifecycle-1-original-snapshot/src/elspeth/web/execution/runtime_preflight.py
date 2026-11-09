"""Async coordination for runtime-equivalent composer preflight.

This module is transport-neutral L3 infrastructure. HTTP composer and
composer MCP can share the same process-local coordinator when they share a
logical session. Standalone MCP processes cannot share this lock with the web
process; cross-process safety comes from side-effect-free preflight mode.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.execution.schemas import ValidationResult


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


@dataclass(slots=True)
class _RetainedPreflight:
    task: asyncio.Task[RuntimePreflightEntry]
    awaiters_settled: asyncio.Event
    awaiters: int = 0
    observed: bool = False
    delivered: bool = False
    failure: BaseException | None = None


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
                result = await asyncio.shield(task)
            else:
                try:
                    # Expiry cancels only the shield, never the shared task.
                    result = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
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
                retained.failure = result.original_exc
        if retained.failure is None or retained.delivered:
            self._retained.pop(retained.task, None)

    def _delivered(self, retained: _RetainedPreflight) -> None:
        retained.delivered = True
        self._retained.pop(retained.task, None)

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
        left. Failures delivered through run() remain ordinary result values;
        abandoned failures are retained for this lifecycle join, including
        ones whose task was evicted before seal. Distinct undelivered failures
        and join cancellation are raised together, never discarded or emitted
        as a second preflight response. No unrelated host tasks are joined.
        """
        async with self._lock:
            self._sealed = True
            if self._join_task is None:
                self._join_task = asyncio.create_task(self._join(tuple(self._retained.values())))
            task = self._join_task
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
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
