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


@dataclass(frozen=True, slots=True)
class RuntimePreflightFailure:
    original_exc: Exception


RuntimePreflightEntry = ValidationResult | RuntimePreflightFailure
RuntimePreflightWorker = Callable[[], Awaitable[ValidationResult]]


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
            if key not in self._inflight:
                if timeout is not None and timeout <= 0:
                    # No budget left and nothing to join: do not admit work
                    # this caller will never await.
                    return RuntimePreflightFailure(TimeoutError("runtime preflight budget exhausted before admission"))
                task = asyncio.create_task(self._capture(worker))
                self._inflight[key] = task

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
                    if key in self._inflight and self._inflight[key] is finished:
                        del self._inflight[key]

                task.add_done_callback(_evict)

            task = self._inflight[key]

        if timeout is None:
            return await asyncio.shield(task)
        try:
            # ``wait_for`` cancels only the shield wrapper on expiry; the
            # shared task — and the sync work it awaits — keeps running and
            # keeps its admission until it actually finishes.
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except TimeoutError as exc:
            return RuntimePreflightFailure(exc)

    async def _capture(self, worker: RuntimePreflightWorker) -> RuntimePreflightEntry:
        try:
            return await worker()
        except Exception as exc:
            return RuntimePreflightFailure(exc)
