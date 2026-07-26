"""Async lifecycle proofs for renewable session-operation authority."""

from __future__ import annotations

import asyncio
import threading
from typing import cast
from uuid import UUID, uuid4

import pytest

from elspeth.web.coordination.contracts import (
    FenceLossReason,
    SessionOperationFence,
    SessionOperationFenceLost,
    SessionOperationKind,
)
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions.protocol import SessionOperationAuthority


class _FakeAuthority:
    """Thread-safe authority double with real blocking/cancellation seams."""

    def __init__(self) -> None:
        self.fence = SessionOperationFence(
            session_id=str(uuid4()),
            operation_id=str(uuid4()),
            lease_token="lease-token-for-tests",
            operation_epoch=7,
        )
        self.acquire_calls: list[dict[str, object]] = []
        self.renew_calls: list[tuple[SessionOperationFence, int]] = []
        self.release_calls: list[SessionOperationFence] = []
        self.acquire_started = threading.Event()
        self.acquire_allowed = threading.Event()
        self.acquire_allowed.set()
        self.renew_called = threading.Event()
        self.release_called = threading.Event()
        self.acquire_error: BaseException | None = None
        self.renew_error: BaseException | None = None
        self.release_error: BaseException | None = None
        self._lock = threading.Lock()

    def acquire(
        self,
        *,
        session_id: UUID,
        operation_kind: SessionOperationKind,
        owner_instance_id: str,
        lease_seconds: int,
    ) -> SessionOperationFence:
        self.acquire_started.set()
        self.acquire_allowed.wait(timeout=2)
        if self.acquire_error is not None:
            raise self.acquire_error
        with self._lock:
            self.acquire_calls.append(
                {
                    "session_id": session_id,
                    "operation_kind": operation_kind,
                    "owner_instance_id": owner_instance_id,
                    "lease_seconds": lease_seconds,
                }
            )
        return self.fence

    def renew(
        self,
        fence: SessionOperationFence,
        *,
        lease_seconds: int,
    ) -> SessionOperationFence:
        with self._lock:
            self.renew_calls.append((fence, lease_seconds))
        self.renew_called.set()
        if self.renew_error is not None:
            raise self.renew_error
        return fence

    def release(self, fence: SessionOperationFence) -> None:
        with self._lock:
            self.release_calls.append(fence)
        self.release_called.set()
        if self.release_error is not None:
            raise self.release_error


async def _wait_for_thread_event(event: threading.Event) -> None:
    assert await asyncio.wait_for(asyncio.to_thread(event.wait, 1), timeout=2)


async def _acquire(
    authority: _FakeAuthority,
    *,
    renew_interval_seconds: float = 0.01,
) -> SessionOperationLease:
    return await SessionOperationLease.acquire(
        cast("SessionOperationAuthority", authority),
        session_id=UUID(authority.fence.session_id),
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="instance-a",
        lease_seconds=30,
        renew_interval_seconds=renew_interval_seconds,
    )


@pytest.mark.asyncio
async def test_one_logical_operation_retains_one_immutable_fence_across_renewal() -> None:
    authority = _FakeAuthority()
    lease = await _acquire(authority)
    initial_fence = lease.fence

    await _wait_for_thread_event(authority.renew_called)
    lease.raise_if_lost()
    await lease.close()

    assert lease.fence is initial_fence
    assert authority.acquire_calls == [
        {
            "session_id": UUID(authority.fence.session_id),
            "operation_kind": SessionOperationKind.COMPOSE,
            "owner_instance_id": "instance-a",
            "lease_seconds": 30,
        }
    ]
    assert authority.renew_calls
    assert all(fence is initial_fence and seconds == 30 for fence, seconds in authority.renew_calls)
    assert authority.release_calls == [initial_fence]
    assert lease.closed is True


@pytest.mark.asyncio
async def test_renewal_waits_for_the_configured_cadence() -> None:
    authority = _FakeAuthority()
    lease = await _acquire(authority, renew_interval_seconds=0.05)

    await asyncio.sleep(0.005)
    assert authority.renew_calls == []
    await _wait_for_thread_event(authority.renew_called)
    await lease.close()


@pytest.mark.asyncio
async def test_renewal_fence_loss_is_recorded_surfaced_and_never_released() -> None:
    authority = _FakeAuthority()
    loss = SessionOperationFenceLost(FenceLossReason.STALE_EPOCH)
    authority.renew_error = loss
    lease = await _acquire(authority)

    observed = await lease.wait_until_lost()
    assert observed is loss
    with pytest.raises(SessionOperationFenceLost) as raised:
        lease.raise_if_lost()
    assert raised.value is loss
    with pytest.raises(SessionOperationFenceLost) as close_error:
        await lease.close()

    assert close_error.value is loss
    assert lease.renewal_error is loss
    assert authority.release_calls == []
    assert lease.closed is True


@pytest.mark.asyncio
async def test_non_fence_renewal_failure_is_surfaced_without_uncertain_release() -> None:
    authority = _FakeAuthority()
    failure = OSError("renewal transport unavailable")
    authority.renew_error = failure
    lease = await _acquire(authority)

    assert await lease.wait_until_lost() is failure
    with pytest.raises(OSError) as close_error:
        await lease.close()

    assert close_error.value is failure
    assert authority.release_calls == []


@pytest.mark.asyncio
async def test_body_cancellation_still_joins_cleanup_and_releases_current_fence() -> None:
    authority = _FakeAuthority()

    async def cancelled_operation() -> None:
        async with await _acquire(authority, renew_interval_seconds=10):
            raise asyncio.CancelledError

    task = asyncio.create_task(cancelled_operation())
    with pytest.raises(asyncio.CancelledError):
        await task

    assert authority.release_calls == [authority.fence]
    assert task.done()


@pytest.mark.asyncio
async def test_cancelled_acquire_releases_fence_minted_by_surviving_worker() -> None:
    authority = _FakeAuthority()
    authority.acquire_allowed.clear()
    acquire_task = asyncio.create_task(_acquire(authority))
    await _wait_for_thread_event(authority.acquire_started)

    acquire_task.cancel()
    authority.acquire_allowed.set()
    with pytest.raises(asyncio.CancelledError):
        await acquire_task
    await _wait_for_thread_event(authority.release_called)

    assert authority.release_calls == [authority.fence]


@pytest.mark.asyncio
async def test_acquire_failure_does_not_start_renewal_or_attempt_release() -> None:
    authority = _FakeAuthority()
    failure = RuntimeError("claim rejected")
    authority.acquire_error = failure

    with pytest.raises(RuntimeError) as raised:
        await _acquire(authority)

    assert raised.value is failure
    assert authority.renew_calls == []
    assert authority.release_calls == []


@pytest.mark.asyncio
async def test_owned_task_is_joined_before_release() -> None:
    authority = _FakeAuthority()
    task_started = asyncio.Event()
    task_allowed = asyncio.Event()

    async def mutation_tail() -> None:
        task_started.set()
        await task_allowed.wait()

    lease = await _acquire(authority, renew_interval_seconds=10)
    owned = lease.create_task(mutation_tail(), name="test-session-mutation-tail")
    await task_started.wait()
    close_task = asyncio.create_task(lease.close())
    await asyncio.sleep(0)

    assert close_task.done() is False
    assert authority.release_calls == []
    task_allowed.set()
    await close_task

    assert owned.done()
    assert authority.release_calls == [authority.fence]


@pytest.mark.asyncio
async def test_known_loss_cancels_owned_tasks_and_leaks_no_background_renewal() -> None:
    authority = _FakeAuthority()
    authority.renew_error = SessionOperationFenceLost(FenceLossReason.LEASE_EXPIRED)
    owned_cancelled = asyncio.Event()

    async def child() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            owned_cancelled.set()

    lease = await _acquire(authority)
    owned = lease.create_task(child(), name="test-session-owned-child")
    await lease.wait_until_lost()
    with pytest.raises(SessionOperationFenceLost):
        await lease.close()
    renew_count_after_close = len(authority.renew_calls)
    await asyncio.sleep(0.03)

    assert owned.cancelled()
    assert owned_cancelled.is_set()
    assert len(authority.renew_calls) == renew_count_after_close
    assert authority.release_calls == []


@pytest.mark.asyncio
async def test_owned_task_exception_is_joined_then_propagated_after_release() -> None:
    authority = _FakeAuthority()
    failure = ValueError("mutation tail failed")

    async def failing_tail() -> None:
        raise failure

    lease = await _acquire(authority, renew_interval_seconds=10)
    lease.create_task(failing_tail(), name="test-session-failing-tail")

    with pytest.raises(ValueError) as raised:
        await lease.close()

    assert raised.value is failure
    assert authority.release_calls == [authority.fence]


def test_invalid_renew_interval_fails_before_authority_acquisition() -> None:
    authority = _FakeAuthority()

    async def acquire_with_interval(interval: float) -> None:
        await _acquire(authority, renew_interval_seconds=interval)

    for interval in (0.0, -1.0, 30.0, 31.0):
        with pytest.raises(ValueError, match="renew_interval_seconds"):
            asyncio.run(acquire_with_interval(interval))
    assert authority.acquire_calls == []
