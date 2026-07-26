"""Async ownership lifecycle for persistent session-operation fences.

The database authority remains synchronous and owns every transaction it
opens.  This module only manages the asynchronous lifetime around that
authority: acquire and renew calls run in the bounded worker pool, one
immutable fence is retained for the logical operation, and release happens
after every lifecycle-owned child task has settled.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from contextlib import suppress
from typing import TYPE_CHECKING, Any, final
from uuid import UUID

from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.coordination.contracts import SessionOperationFence, SessionOperationKind

if TYPE_CHECKING:
    from types import TracebackType

    from elspeth.web.sessions.protocol import SessionOperationAuthority

_MAX_RENEW_INTERVAL_SECONDS = 30.0


def _validate_lifecycle_timing(*, lease_seconds: int, renew_interval_seconds: float | None) -> float:
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
        raise ValueError("lease_seconds must be an exact integer from 1 through 3600")
    if renew_interval_seconds is None:
        return min(lease_seconds / 3, _MAX_RENEW_INTERVAL_SECONDS)
    if type(renew_interval_seconds) not in (int, float):
        raise ValueError("renew_interval_seconds must be a positive number shorter than lease_seconds")
    interval = float(renew_interval_seconds)
    if not 0 < interval < lease_seconds:
        raise ValueError("renew_interval_seconds must be a positive number shorter than lease_seconds")
    return interval


async def _finish_cancelled_acquire(
    authority: SessionOperationAuthority,
    acquire_task: asyncio.Task[SessionOperationFence],
) -> BaseException | None:
    """Retrieve a cancellation-surviving acquire and release its result."""
    try:
        fence = await acquire_task
    except BaseException as acquire_error:
        return acquire_error
    try:
        await run_sync_in_worker(authority.release, fence)
    except BaseException as release_error:
        return release_error
    return None


async def _join_shielded_task_after_cancellation[T](task: asyncio.Task[T]) -> T:
    """Wait for an owned task even when the awaiting task is being cancelled."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


@final
class SessionOperationLease:
    """Renewable async lifetime around one immutable operation fence.

    Nested services receive :attr:`fence`; the authority itself and lifecycle
    methods stay private to the owner.  No connection or transaction remains
    open while the caller performs async, network, or filesystem work.
    """

    __slots__ = (
        "_authority",
        "_close_task",
        "_closed",
        "_fence",
        "_lease_seconds",
        "_lost_event",
        "_owned_tasks",
        "_renew_interval_seconds",
        "_renewal_error",
        "_renewal_task",
        "_stop_renewal",
    )

    def __init__(
        self,
        authority: SessionOperationAuthority,
        fence: SessionOperationFence,
        *,
        lease_seconds: int,
        renew_interval_seconds: float,
    ) -> None:
        self._authority = authority
        self._fence = fence
        self._lease_seconds = lease_seconds
        self._renew_interval_seconds = renew_interval_seconds
        self._stop_renewal = asyncio.Event()
        self._lost_event = asyncio.Event()
        self._renewal_error: BaseException | None = None
        self._owned_tasks: set[asyncio.Task[Any]] = set()
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False
        self._renewal_task = asyncio.create_task(
            self._renew_forever(),
            name="session-operation-renewal",
        )

    @classmethod
    async def acquire(
        cls,
        authority: SessionOperationAuthority,
        *,
        session_id: UUID,
        operation_kind: SessionOperationKind,
        owner_instance_id: str,
        lease_seconds: int,
        renew_interval_seconds: float | None = None,
    ) -> SessionOperationLease:
        """Acquire one fence without blocking the event loop.

        Cancellation cannot orphan an acquisition that already reached the
        worker: the worker result is retrieved and, when it minted a fence,
        that exact fence is released before cancellation resumes.
        """
        interval = _validate_lifecycle_timing(
            lease_seconds=lease_seconds,
            renew_interval_seconds=renew_interval_seconds,
        )
        acquire_task = asyncio.create_task(
            run_sync_in_worker(
                authority.acquire,
                session_id=session_id,
                operation_kind=operation_kind,
                owner_instance_id=owner_instance_id,
                lease_seconds=lease_seconds,
            ),
            name="session-operation-acquire",
        )
        try:
            fence = await asyncio.shield(acquire_task)
        except asyncio.CancelledError as cancellation:
            cleanup_task = asyncio.create_task(
                _finish_cancelled_acquire(authority, acquire_task),
                name="session-operation-cancelled-acquire-cleanup",
            )
            cleanup_error = await _join_shielded_task_after_cancellation(cleanup_task)
            if cleanup_error is not None:
                cancellation.add_note(f"Session-operation acquire cancellation cleanup also failed with {type(cleanup_error).__name__}.")
            raise
        return cls(
            authority,
            fence,
            lease_seconds=lease_seconds,
            renew_interval_seconds=interval,
        )

    @property
    def fence(self) -> SessionOperationFence:
        """The immutable authority passed to nested service mutations."""
        return self._fence

    @property
    def renewal_error(self) -> BaseException | None:
        """First renewal failure, retained without low-level retry laundering."""
        return self._renewal_error

    @property
    def closed(self) -> bool:
        return self._closed

    def raise_if_lost(self) -> None:
        """Surface renewal loss before starting another external side effect."""
        if self._renewal_error is not None:
            raise self._renewal_error

    async def wait_until_lost(self) -> BaseException:
        """Wait until renewal proves or reports that authority is no longer safe."""
        await self._lost_event.wait()
        error = self._renewal_error
        if error is None:
            raise RuntimeError("session operation loss event has no recorded error")
        return error

    def create_task[T](
        self,
        coroutine: Coroutine[Any, Any, T],
        *,
        name: str | None = None,
    ) -> asyncio.Task[T]:
        """Create a child that must settle before the fence can be released."""
        if self._closed or self._close_task is not None:
            coroutine.close()
            raise RuntimeError("session operation lease is closing or closed")
        task = asyncio.create_task(coroutine, name=name)
        self._owned_tasks.add(task)
        return task

    async def _renew_forever(self) -> None:
        while not self._stop_renewal.is_set():
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop_renewal.wait(),
                    timeout=self._renew_interval_seconds,
                )
            if self._stop_renewal.is_set():
                return
            try:
                renewed = await run_sync_in_worker(
                    self._authority.renew,
                    self._fence,
                    lease_seconds=self._lease_seconds,
                )
                if renewed != self._fence:
                    raise RuntimeError("session operation renewal changed immutable fence identity")
            except asyncio.CancelledError:
                raise
            except Exception as renewal_error:
                self._record_renewal_error(renewal_error)
                return

    def _record_renewal_error(self, error: BaseException) -> None:
        if self._renewal_error is not None:
            return
        self._renewal_error = error
        self._lost_event.set()
        for task in tuple(self._owned_tasks):
            if not task.done():
                task.cancel()

    async def _join_owned_tasks(self) -> BaseException | None:
        if not self._owned_tasks:
            return None
        tasks = tuple(self._owned_tasks)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        first_error: BaseException | None = None
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError) and first_error is None:
                first_error = result
        self._owned_tasks.clear()
        return first_error

    async def _close(self) -> None:
        owned_error: BaseException | None = None
        release_error: BaseException | None = None
        try:
            owned_error = await self._join_owned_tasks()
            self._stop_renewal.set()
            await self._renewal_task
            if self._renewal_error is None:
                try:
                    await run_sync_in_worker(self._authority.release, self._fence)
                except BaseException as error:
                    release_error = error
        finally:
            self._closed = True

        if self._renewal_error is not None:
            raise self._renewal_error
        if release_error is not None:
            raise release_error
        if owned_error is not None:
            raise owned_error

    async def close(self) -> None:
        """Join children, stop renewal, and release only known-current authority."""
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close(),
                name="session-operation-close",
            )
        close_task = self._close_task
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError as cancellation:
            try:
                await _join_shielded_task_after_cancellation(close_task)
            except BaseException as cleanup_error:
                cancellation.add_note(f"Session-operation close also failed with {type(cleanup_error).__name__}.")
            raise

    async def __aenter__(self) -> SessionOperationLease:
        if self._closed or self._close_task is not None:
            raise RuntimeError("session operation lease is closing or closed")
        self.raise_if_lost()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exc_type, traceback
        try:
            await self.close()
        except BaseException as cleanup_error:
            if exc_value is None:
                raise
            if cleanup_error is not exc_value:
                exc_value.add_note(f"Session-operation lifecycle cleanup also failed with {type(cleanup_error).__name__}.")
        return False
