"""Async lifecycle proofs for renewable session-operation authority."""

from __future__ import annotations

import asyncio
import threading
import traceback
from typing import cast
from uuid import UUID, uuid4

import pytest

from elspeth.web.coordination.contracts import (
    ArchiveDeleteReconciliation,
    FenceLossReason,
    SessionOperationContext,
    SessionOperationFence,
    SessionOperationFenceLost,
    SessionOperationKind,
    SessionOperationLeaseDisposition,
    SessionOperationTerminalOutcomeUnknown,
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
        self.context = SessionOperationContext(
            fence=self.fence,
            operation_kind=SessionOperationKind.COMPOSE,
        )
        self.acquire_result: SessionOperationContext | None = None
        self.acquire_calls: list[dict[str, object]] = []
        self.compare_and_swap_calls: list[SessionOperationContext] = []
        self.renew_calls: list[tuple[SessionOperationContext, int]] = []
        self.release_attempts: list[SessionOperationContext] = []
        self.release_calls: list[SessionOperationContext] = []
        self.archive_delete_calls: list[SessionOperationContext] = []
        self.reconcile_archive_delete_calls: list[SessionOperationContext] = []
        self.events: list[str] = []
        self.lease_active = False
        self.acquire_started = threading.Event()
        self.acquire_allowed = threading.Event()
        self.acquire_allowed.set()
        self.compare_and_swap_started = threading.Event()
        self.compare_and_swap_allowed = threading.Event()
        self.compare_and_swap_allowed.set()
        self.compare_and_swap_finished = threading.Event()
        self.renew_called = threading.Event()
        self.release_called = threading.Event()
        self.release_allowed = threading.Event()
        self.release_allowed.set()
        self.release_finished = threading.Event()
        self.acquire_error: BaseException | None = None
        self.compare_and_swap_error: BaseException | None = None
        self.renew_error: BaseException | None = None
        self.release_error: BaseException | None = None
        self.archive_delete_error: BaseException | None = None
        self.archive_delete_started = threading.Event()
        self.archive_delete_allowed = threading.Event()
        self.archive_delete_allowed.set()
        self.reconcile_archive_delete_result = ArchiveDeleteReconciliation.CURRENT
        self.reconcile_archive_delete_error: BaseException | None = None
        self._lock = threading.Lock()

    def acquire(
        self,
        *,
        session_id: UUID,
        operation_kind: SessionOperationKind,
        owner_instance_id: str,
        lease_seconds: int,
    ) -> SessionOperationContext:
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
        with self._lock:
            self.lease_active = True
            if self.acquire_result is not None:
                return self.acquire_result
            self.context = SessionOperationContext(
                fence=self.fence,
                operation_kind=operation_kind,
            )
            return self.context

    def compare_and_swap(self, context: SessionOperationContext) -> None:
        with self._lock:
            self.compare_and_swap_calls.append(context)
            self.events.append("compare_and_swap")
        self.compare_and_swap_started.set()
        self.compare_and_swap_allowed.wait(timeout=2)
        try:
            if self.compare_and_swap_error is not None:
                raise self.compare_and_swap_error
        finally:
            self.compare_and_swap_finished.set()

    def renew(
        self,
        context: SessionOperationContext,
        *,
        lease_seconds: int,
    ) -> SessionOperationContext:
        with self._lock:
            self.renew_calls.append((context, lease_seconds))
            self.events.append("renew")
        self.renew_called.set()
        if self.renew_error is not None:
            raise self.renew_error
        return context

    def release(self, context: SessionOperationContext) -> None:
        with self._lock:
            self.release_attempts.append(context)
            self.events.append("release")
            if type(context) is not SessionOperationContext:
                self.release_called.set()
                raise TypeError("strict authority release requires an exact SessionOperationContext")
            self.release_calls.append(context)
        self.release_called.set()
        self.release_allowed.wait(timeout=2)
        try:
            if self.release_error is not None:
                raise self.release_error
            with self._lock:
                self.lease_active = False
        finally:
            self.release_finished.set()

    def archive_delete(self, context: SessionOperationContext) -> None:
        with self._lock:
            self.archive_delete_calls.append(context)
            self.events.append("archive_delete")
        self.archive_delete_started.set()
        self.archive_delete_allowed.wait(timeout=2)
        if self.archive_delete_error is not None:
            raise self.archive_delete_error
        with self._lock:
            self.lease_active = False

    def reconcile_archive_delete(self, context: SessionOperationContext) -> ArchiveDeleteReconciliation:
        with self._lock:
            self.reconcile_archive_delete_calls.append(context)
            self.events.append("reconcile")
        if self.reconcile_archive_delete_error is not None:
            raise self.reconcile_archive_delete_error
        return self.reconcile_archive_delete_result


async def _wait_for_thread_event(event: threading.Event) -> None:
    assert await asyncio.wait_for(asyncio.to_thread(event.wait, 1), timeout=2)


def _lifecycle_traceback_local_text(error: BaseException) -> str:
    """Render only lifecycle-frame locals, including retained task state."""
    retained: list[str] = []
    for frame, _line_number in traceback.walk_tb(error.__traceback__):
        if not frame.f_code.co_filename.endswith("/elspeth/web/coordination/lifecycle.py"):
            continue
        for name, value in frame.f_locals.items():
            retained.append(f"{name}={value!r}")
            if isinstance(value, asyncio.Task):
                retained.append(f"task={value!r}")
    return "\n".join(retained)


async def _acquire(
    authority: _FakeAuthority,
    *,
    operation_kind: SessionOperationKind = SessionOperationKind.COMPOSE,
    renew_interval_seconds: float = 0.01,
) -> SessionOperationLease:
    return await SessionOperationLease.acquire(
        cast("SessionOperationAuthority", authority),
        session_id=UUID(authority.fence.session_id),
        operation_kind=operation_kind,
        owner_instance_id="instance-a",
        lease_seconds=30,
        renew_interval_seconds=renew_interval_seconds,
    )


@pytest.mark.asyncio
async def test_one_logical_operation_retains_one_immutable_fence_across_renewal() -> None:
    authority = _FakeAuthority()
    lease = await _acquire(authority, operation_kind=SessionOperationKind.BLOB_READ)
    initial_fence = lease.fence
    initial_context = lease.context

    await _wait_for_thread_event(authority.renew_called)
    lease.raise_if_lost()
    await lease.close()

    assert lease.context is initial_context
    assert initial_context.fence is initial_fence
    assert initial_context.operation_kind is SessionOperationKind.BLOB_READ
    assert lease.fence is initial_fence
    assert authority.acquire_calls == [
        {
            "session_id": UUID(authority.fence.session_id),
            "operation_kind": SessionOperationKind.BLOB_READ,
            "owner_instance_id": "instance-a",
            "lease_seconds": 30,
        }
    ]
    assert authority.renew_calls
    assert all(context is initial_context and seconds == 30 for context, seconds in authority.renew_calls)
    assert authority.release_calls == [initial_context]
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

    assert authority.release_calls == [authority.context]
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

    assert authority.release_calls == [authority.context]


@pytest.mark.asyncio
async def test_cancelled_adopt_joins_cas_and_releases_exact_context_before_first_cancellation_resumes() -> None:
    authority = _FakeAuthority()
    authority.compare_and_swap_allowed.clear()
    authority.release_allowed.clear()
    authority.lease_active = True
    context = authority.context
    adopt_task = asyncio.create_task(
        SessionOperationLease.adopt(
            cast("SessionOperationAuthority", authority),
            context,
            lease_seconds=30,
            renew_interval_seconds=10,
        )
    )
    await _wait_for_thread_event(authority.compare_and_swap_started)

    adopt_task.cancel("first-adoption-cancellation")
    await asyncio.sleep(0)
    cancellation_resumed_before_cas_finished = adopt_task.done()
    assert authority.release_calls == []

    authority.compare_and_swap_allowed.set()
    await _wait_for_thread_event(authority.compare_and_swap_finished)
    await _wait_for_thread_event(authority.release_called)
    assert adopt_task.done() is False
    adopt_task.cancel("second-adoption-cancellation")
    await asyncio.sleep(0)
    assert adopt_task.done() is False
    authority.release_allowed.set()
    await _wait_for_thread_event(authority.release_finished)
    with pytest.raises(asyncio.CancelledError) as raised:
        await adopt_task

    assert (
        cancellation_resumed_before_cas_finished,
        authority.release_calls,
    ) == (False, [context])
    assert raised.value.args == ("first-adoption-cancellation",)
    assert authority.compare_and_swap_calls == [context]
    assert authority.release_calls[0] is context
    assert authority.lease_active is False
    assert authority.renew_calls == []
    await asyncio.sleep(0)
    assert not any(task.get_name() == "session-operation-renewal" for task in asyncio.all_tasks() if task is not asyncio.current_task())


@pytest.mark.asyncio
async def test_cancelled_adopt_cas_and_release_failures_are_sanitized_and_cancellation_remains_primary() -> None:
    authority = _FakeAuthority()
    authority.compare_and_swap_allowed.clear()
    authority.lease_active = True
    context = authority.context
    cas_secret = "cas-secret-database-detail"
    release_secret = "release-secret-database-detail"  # secret-scan: allow-this-line
    authority.compare_and_swap_error = RuntimeError(cas_secret)
    authority.release_error = OSError(release_secret)
    adopt_task = asyncio.create_task(
        SessionOperationLease.adopt(
            cast("SessionOperationAuthority", authority),
            context,
            lease_seconds=30,
            renew_interval_seconds=10,
        )
    )
    await _wait_for_thread_event(authority.compare_and_swap_started)

    adopt_task.cancel("first-adoption-cancellation")
    asyncio.get_running_loop().call_soon(adopt_task.cancel, "second-adoption-cancellation")
    authority.compare_and_swap_allowed.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await adopt_task

    rendered = "".join(traceback.format_exception(raised.value))
    retained = _lifecycle_traceback_local_text(raised.value)
    notes = "\n".join(raised.value.__notes__)
    assert raised.value.args == ("first-adoption-cancellation",)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert cas_secret not in rendered
    assert release_secret not in rendered
    assert cas_secret not in retained
    assert release_secret not in retained
    assert "RuntimeError" in notes
    assert "OSError" in notes
    assert authority.compare_and_swap_calls == [context]
    assert authority.release_attempts == [context]
    assert authority.release_attempts[0] is context
    assert authority.renew_calls == []


@pytest.mark.asyncio
async def test_adopt_cas_failure_attempts_exact_release_and_preserves_sanitized_primary() -> None:
    authority = _FakeAuthority()
    authority.lease_active = True
    context = authority.context
    cas_error = RuntimeError("compare-and-swap acknowledgement failed")
    release_secret = "release-secret-database-detail"  # secret-scan: allow-this-line
    authority.compare_and_swap_error = cas_error
    authority.release_error = OSError(release_secret)

    with pytest.raises(RuntimeError) as raised:
        await SessionOperationLease.adopt(
            cast("SessionOperationAuthority", authority),
            context,
            lease_seconds=30,
            renew_interval_seconds=10,
        )

    rendered = "".join(traceback.format_exception(raised.value))
    retained = _lifecycle_traceback_local_text(raised.value)
    assert raised.value is cas_error
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert release_secret not in rendered
    assert release_secret not in retained
    assert "OSError" in "\n".join(raised.value.__notes__)
    assert authority.events == ["compare_and_swap", "release"]
    assert authority.release_attempts == [context]
    assert authority.release_attempts[0] is context
    assert authority.renew_calls == []


@pytest.mark.asyncio
async def test_adopt_cas_failure_cancellation_joins_blocked_release_and_sanitizes_cleanup_failures() -> None:
    authority = _FakeAuthority()
    authority.release_allowed.clear()
    authority.lease_active = True
    context = authority.context
    cas_secret = "compare-and-swap-secret-detail"  # secret-scan: allow-this-line
    release_secret = "release-secret-database-detail"  # secret-scan: allow-this-line
    authority.compare_and_swap_error = RuntimeError(cas_secret)
    authority.release_error = OSError(release_secret)
    adopt_task = asyncio.create_task(
        SessionOperationLease.adopt(
            cast("SessionOperationAuthority", authority),
            context,
            lease_seconds=30,
            renew_interval_seconds=10,
        )
    )
    await _wait_for_thread_event(authority.release_called)

    adopt_task.cancel("first-cleanup-cancellation")
    await asyncio.sleep(0)
    assert adopt_task.done() is False
    adopt_task.cancel("second-cleanup-cancellation")
    await asyncio.sleep(0)
    assert adopt_task.done() is False
    authority.release_allowed.set()
    await _wait_for_thread_event(authority.release_finished)
    with pytest.raises(asyncio.CancelledError) as raised:
        await adopt_task

    rendered = "".join(traceback.format_exception(raised.value))
    retained = _lifecycle_traceback_local_text(raised.value)
    notes = "\n".join(raised.value.__notes__)
    assert raised.value.args == ("first-cleanup-cancellation",)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert cas_secret not in rendered
    assert release_secret not in rendered
    assert cas_secret not in retained
    assert release_secret not in retained
    assert "RuntimeError" in notes
    assert "OSError" in notes
    assert authority.release_attempts == [context]
    assert authority.release_attempts[0] is context
    assert authority.renew_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lease_seconds", "renew_interval_seconds", "message", "release_fails"),
    [
        pytest.param(0, None, "lease_seconds", False, id="invalid-lease-seconds"),
        pytest.param(30, 30, "renew_interval_seconds", True, id="invalid-renew-interval"),
    ],
)
async def test_adopt_invalid_timing_releases_exact_context_before_preserving_validation_error(
    lease_seconds: int,
    renew_interval_seconds: float | None,
    message: str,
    release_fails: bool,
) -> None:
    authority = _FakeAuthority()
    authority.lease_active = True
    context = authority.context
    release_secret = "validation-release-secret-detail"  # secret-scan: allow-this-line
    if release_fails:
        authority.release_error = OSError(release_secret)

    with pytest.raises(ValueError, match=message) as raised:
        await SessionOperationLease.adopt(
            cast("SessionOperationAuthority", authority),
            context,
            lease_seconds=lease_seconds,
            renew_interval_seconds=renew_interval_seconds,
        )

    rendered = "".join(traceback.format_exception(raised.value))
    retained = _lifecycle_traceback_local_text(raised.value)
    notes = "\n".join(getattr(raised.value, "__notes__", ()))
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert release_secret not in rendered
    assert release_secret not in retained
    assert ("OSError" in notes) is release_fails
    assert authority.compare_and_swap_calls == []
    assert authority.release_attempts == [context]
    assert authority.release_attempts[0] is context
    assert authority.lease_active is release_fails
    assert authority.renew_calls == []


@pytest.mark.asyncio
async def test_adopt_invalid_timing_cancellation_joins_blocked_release_and_sanitizes_failures() -> None:
    authority = _FakeAuthority()
    authority.release_allowed.clear()
    authority.lease_active = True
    context = authority.context
    release_secret = "validation-release-secret-detail"  # secret-scan: allow-this-line
    validation_detail = "lease_seconds must be an exact integer from 1 through 3600"
    authority.release_error = OSError(release_secret)
    adopt_task = asyncio.create_task(
        SessionOperationLease.adopt(
            cast("SessionOperationAuthority", authority),
            context,
            lease_seconds=0,
        )
    )
    release_started = await asyncio.wait_for(asyncio.to_thread(authority.release_called.wait, 1), timeout=2)
    if not release_started:
        with pytest.raises(ValueError, match="lease_seconds"):
            await adopt_task
    assert release_started

    adopt_task.cancel("first-validation-cleanup-cancellation")
    await asyncio.sleep(0)
    assert adopt_task.done() is False
    adopt_task.cancel("second-validation-cleanup-cancellation")
    await asyncio.sleep(0)
    assert adopt_task.done() is False
    authority.release_allowed.set()
    await _wait_for_thread_event(authority.release_finished)
    with pytest.raises(asyncio.CancelledError) as raised:
        await adopt_task

    rendered = "".join(traceback.format_exception(raised.value))
    notes = "\n".join(raised.value.__notes__)
    assert raised.value.args == ("first-validation-cleanup-cancellation",)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert validation_detail not in rendered
    assert release_secret not in rendered
    assert "ValueError" in notes
    assert "OSError" in notes
    assert authority.compare_and_swap_calls == []
    assert authority.release_attempts == [context]
    assert authority.release_attempts[0] is context
    assert authority.renew_calls == []


@pytest.mark.asyncio
async def test_adopt_invalid_context_does_not_attempt_cleanup_without_safe_exact_capability() -> None:
    authority = _FakeAuthority()

    with pytest.raises(TypeError, match="exact SessionOperationContext"):
        await SessionOperationLease.adopt(
            cast("SessionOperationAuthority", authority),
            cast("SessionOperationContext", object()),
            lease_seconds=0,
        )

    assert authority.compare_and_swap_calls == []
    assert authority.release_attempts == []
    assert authority.renew_calls == []


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
async def test_non_exact_acquired_context_is_released_before_validation_failure_surfaces() -> None:
    class DerivedContext(SessionOperationContext):
        pass

    authority = _FakeAuthority()
    returned_context = DerivedContext(
        fence=authority.fence,
        operation_kind=SessionOperationKind.COMPOSE,
    )
    authority.acquire_result = returned_context

    with pytest.raises(TypeError, match="exact SessionOperationContext"):
        await _acquire(authority)

    safe_context = SessionOperationContext(
        fence=returned_context.fence,
        operation_kind=returned_context.operation_kind,
    )
    assert len(authority.acquire_calls) == 1
    assert authority.release_attempts == [safe_context]
    assert authority.release_calls == [safe_context]
    assert type(authority.release_calls[0]) is SessionOperationContext
    assert authority.lease_active is False
    assert authority.renew_calls == []


@pytest.mark.asyncio
async def test_cancelled_acquire_releases_safe_exact_context_from_non_exact_worker_result() -> None:
    class DerivedContext(SessionOperationContext):
        pass

    authority = _FakeAuthority()
    returned_context = DerivedContext(
        fence=authority.fence,
        operation_kind=SessionOperationKind.COMPOSE,
    )
    authority.acquire_result = returned_context
    authority.acquire_allowed.clear()
    acquire_task = asyncio.create_task(_acquire(authority))
    await _wait_for_thread_event(authority.acquire_started)

    acquire_task.cancel()
    authority.acquire_allowed.set()
    with pytest.raises(asyncio.CancelledError):
        await acquire_task

    safe_context = SessionOperationContext(
        fence=returned_context.fence,
        operation_kind=returned_context.operation_kind,
    )
    assert authority.release_attempts == [safe_context]
    assert authority.release_calls == [safe_context]
    assert type(authority.release_calls[0]) is SessionOperationContext
    assert authority.lease_active is False


@pytest.mark.asyncio
async def test_wrong_kind_acquired_context_is_released_before_protocol_failure_surfaces() -> None:
    authority = _FakeAuthority()
    returned_context = SessionOperationContext(
        fence=authority.fence,
        operation_kind=SessionOperationKind.EXECUTE,
    )
    authority.acquire_result = returned_context

    with pytest.raises(RuntimeError, match="operation kind"):
        await _acquire(authority, operation_kind=SessionOperationKind.COMPOSE)

    assert len(authority.acquire_calls) == 1
    assert authority.release_calls == [returned_context]
    assert type(authority.release_calls[0]) is SessionOperationContext
    assert authority.renew_calls == []


@pytest.mark.asyncio
async def test_non_exact_operation_kind_is_rejected_before_authority_acquisition() -> None:
    authority = _FakeAuthority()

    with pytest.raises(TypeError, match="exact SessionOperationKind"):
        await _acquire(
            authority,
            operation_kind=cast("SessionOperationKind", "compose"),
        )

    assert authority.acquire_calls == []
    assert authority.release_calls == []
    assert authority.renew_calls == []


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
    assert authority.release_calls == [authority.context]


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
    assert authority.release_calls == [authority.context]
    assert lease.disposition is SessionOperationLeaseDisposition.RELEASED


@pytest.mark.asyncio
async def test_archive_consume_joins_children_stops_renewal_and_never_releases_after_confirmed_cascade() -> None:
    authority = _FakeAuthority()
    lease = await _acquire(authority, operation_kind=SessionOperationKind.ARCHIVE, renew_interval_seconds=10)
    child_allowed = asyncio.Event()

    async def child() -> None:
        authority.events.append("child_started")
        await child_allowed.wait()
        authority.events.append("child_done")

    owned = lease.create_task(child(), name="archive-owned-child")
    consume_task = asyncio.create_task(lease.consume_archive())
    await asyncio.sleep(0)
    assert authority.archive_delete_calls == []

    child_allowed.set()
    await consume_task

    assert owned.done()
    assert authority.archive_delete_calls == [lease.context]
    assert authority.reconcile_archive_delete_calls == []
    assert authority.release_calls == []
    assert authority.events[-2:] == ["child_done", "archive_delete"]
    assert lease.disposition is SessionOperationLeaseDisposition.CONSUMED
    assert lease.closed is True
    renew_count = len(authority.renew_calls)
    await asyncio.sleep(0.02)
    assert len(authority.renew_calls) == renew_count


@pytest.mark.asyncio
async def test_archive_consume_cancellation_after_confirmed_cascade_waits_and_never_releases() -> None:
    authority = _FakeAuthority()
    authority.archive_delete_allowed.clear()
    lease = await _acquire(authority, operation_kind=SessionOperationKind.ARCHIVE, renew_interval_seconds=10)

    consume_task = asyncio.create_task(lease.consume_archive())
    await _wait_for_thread_event(authority.archive_delete_started)
    consume_task.cancel()
    authority.archive_delete_allowed.set()

    with pytest.raises(asyncio.CancelledError):
        await consume_task

    assert authority.archive_delete_calls == [lease.context]
    assert authority.release_calls == []
    assert lease.disposition is SessionOperationLeaseDisposition.CONSUMED
    assert lease.closed is True


@pytest.mark.asyncio
async def test_archive_consume_cancelled_precommit_failure_reconciles_current_releases_then_raises_cancellation() -> None:
    authority = _FakeAuthority()
    authority.archive_delete_allowed.clear()
    authority.archive_delete_error = RuntimeError("precommit archive failure")
    authority.reconcile_archive_delete_result = ArchiveDeleteReconciliation.CURRENT
    lease = await _acquire(authority, operation_kind=SessionOperationKind.ARCHIVE, renew_interval_seconds=10)

    consume_task = asyncio.create_task(lease.consume_archive())
    await _wait_for_thread_event(authority.archive_delete_started)
    consume_task.cancel()
    authority.archive_delete_allowed.set()

    with pytest.raises(asyncio.CancelledError):
        await consume_task

    assert authority.reconcile_archive_delete_calls == [lease.context]
    assert authority.release_calls == [lease.context]
    assert lease.disposition is SessionOperationLeaseDisposition.RELEASED


@pytest.mark.asyncio
async def test_archive_consume_ambiguous_error_reconciles_consumed_without_release() -> None:
    authority = _FakeAuthority()
    authority.archive_delete_error = RuntimeError("connection dropped")
    authority.reconcile_archive_delete_result = ArchiveDeleteReconciliation.CONSUMED
    lease = await _acquire(authority, operation_kind=SessionOperationKind.ARCHIVE, renew_interval_seconds=10)
    callbacks: list[str] = []

    async def restore_current() -> None:
        callbacks.append("restore")

    async def finalize_consumed() -> None:
        callbacks.append("finalize")

    await lease.consume_archive(
        restore_current=restore_current,
        finalize_consumed=finalize_consumed,
    )

    assert authority.reconcile_archive_delete_calls == [lease.context]
    assert authority.release_calls == []
    assert callbacks == ["finalize"]
    assert lease.disposition is SessionOperationLeaseDisposition.CONSUMED


@pytest.mark.asyncio
async def test_archive_current_outcome_restores_before_release() -> None:
    authority = _FakeAuthority()
    primary = RuntimeError("archive action failed")
    authority.archive_delete_error = primary
    authority.reconcile_archive_delete_result = ArchiveDeleteReconciliation.CURRENT
    lease = await _acquire(authority, operation_kind=SessionOperationKind.ARCHIVE, renew_interval_seconds=10)
    restore_calls = 0
    finalize_calls = 0

    async def restore_current() -> None:
        nonlocal restore_calls
        restore_calls += 1
        assert authority.lease_active is True
        assert authority.release_calls == []
        authority.events.append("restore_current")

    async def finalize_consumed() -> None:
        nonlocal finalize_calls
        finalize_calls += 1

    with pytest.raises(RuntimeError) as raised:
        await lease.consume_archive(
            restore_current=restore_current,
            finalize_consumed=finalize_consumed,
        )

    assert raised.value is primary
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert restore_calls == 1
    assert finalize_calls == 0
    assert authority.events[-3:] == ["reconcile", "restore_current", "release"]
    assert authority.release_calls == [lease.context]
    assert lease.disposition is SessionOperationLeaseDisposition.RELEASED


@pytest.mark.asyncio
async def test_archive_restore_failure_never_releases_and_preserves_primary() -> None:
    authority = _FakeAuthority()
    primary_secret = "primary-operation-secret"
    restore_secret = "restore-filesystem-secret"
    primary = RuntimeError(primary_secret)
    authority.archive_delete_error = primary
    authority.reconcile_archive_delete_result = ArchiveDeleteReconciliation.CURRENT
    lease = await _acquire(authority, operation_kind=SessionOperationKind.ARCHIVE, renew_interval_seconds=10)
    restore_calls = 0

    async def restore_current() -> None:
        nonlocal restore_calls
        restore_calls += 1
        raise OSError(restore_secret)

    async def finalize_consumed() -> None:
        raise AssertionError("finalize must not run for CURRENT")

    with pytest.raises(RuntimeError) as raised:
        await lease.consume_archive(
            restore_current=restore_current,
            finalize_consumed=finalize_consumed,
        )

    rendered = "".join(traceback.format_exception(raised.value))
    assert raised.value is primary
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert restore_calls == 1
    assert authority.release_calls == []
    assert lease.disposition is SessionOperationLeaseDisposition.UNKNOWN
    assert "OSError" in "\n".join(raised.value.__notes__)
    assert restore_secret not in rendered


@pytest.mark.asyncio
async def test_archive_consumed_outcome_finalizes_once_and_never_releases() -> None:
    authority = _FakeAuthority()
    lease = await _acquire(authority, operation_kind=SessionOperationKind.ARCHIVE, renew_interval_seconds=10)
    finalize_started = asyncio.Event()
    finalize_allowed = asyncio.Event()
    finalize_calls = 0
    restore_calls = 0

    async def restore_current() -> None:
        nonlocal restore_calls
        restore_calls += 1

    async def finalize_consumed() -> None:
        nonlocal finalize_calls
        finalize_calls += 1
        finalize_started.set()
        await finalize_allowed.wait()

    consume_task = asyncio.create_task(
        lease.consume_archive(
            restore_current=restore_current,
            finalize_consumed=finalize_consumed,
        )
    )
    await finalize_started.wait()

    assert authority.release_calls == []
    assert consume_task.done() is False
    finalize_allowed.set()
    await consume_task

    assert finalize_calls == 1
    assert restore_calls == 0
    assert authority.release_calls == []
    assert lease.disposition is SessionOperationLeaseDisposition.CONSUMED


@pytest.mark.asyncio
async def test_archive_unknown_outcome_runs_no_compensation_callback() -> None:
    authority = _FakeAuthority()
    authority.archive_delete_error = RuntimeError("archive action failed")
    authority.reconcile_archive_delete_error = OSError("reconciliation unavailable")
    lease = await _acquire(authority, operation_kind=SessionOperationKind.ARCHIVE, renew_interval_seconds=10)
    callbacks: list[str] = []

    async def restore_current() -> None:
        callbacks.append("restore")

    async def finalize_consumed() -> None:
        callbacks.append("finalize")

    with pytest.raises(SessionOperationTerminalOutcomeUnknown):
        await lease.consume_archive(
            restore_current=restore_current,
            finalize_consumed=finalize_consumed,
        )

    assert callbacks == []
    assert authority.release_calls == []
    assert lease.disposition is SessionOperationLeaseDisposition.UNKNOWN


@pytest.mark.asyncio
async def test_archive_cancellation_joins_restore_before_release() -> None:
    authority = _FakeAuthority()
    authority.archive_delete_error = RuntimeError("archive action failed")
    authority.reconcile_archive_delete_result = ArchiveDeleteReconciliation.CURRENT
    lease = await _acquire(authority, operation_kind=SessionOperationKind.ARCHIVE, renew_interval_seconds=10)
    restore_started = asyncio.Event()
    restore_allowed = asyncio.Event()
    restore_calls = 0

    async def restore_current() -> None:
        nonlocal restore_calls
        restore_calls += 1
        restore_started.set()
        await restore_allowed.wait()

    async def finalize_consumed() -> None:
        raise AssertionError("finalize must not run for CURRENT")

    consume_task = asyncio.create_task(
        lease.consume_archive(
            restore_current=restore_current,
            finalize_consumed=finalize_consumed,
        )
    )
    await restore_started.wait()
    consume_task.cancel("first-cancellation")
    asyncio.get_running_loop().call_soon(consume_task.cancel, "second-cancellation")

    assert authority.release_calls == []
    restore_allowed.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await consume_task

    assert raised.value.args == ("first-cancellation",)
    assert restore_calls == 1
    assert authority.release_calls == [lease.context]
    assert lease.disposition is SessionOperationLeaseDisposition.RELEASED


@pytest.mark.asyncio
async def test_archive_cancellation_after_commit_joins_finalize() -> None:
    authority = _FakeAuthority()
    lease = await _acquire(authority, operation_kind=SessionOperationKind.ARCHIVE, renew_interval_seconds=10)
    finalize_started = asyncio.Event()
    finalize_allowed = asyncio.Event()
    finalize_calls = 0

    async def restore_current() -> None:
        raise AssertionError("restore must not run after commit")

    async def finalize_consumed() -> None:
        nonlocal finalize_calls
        finalize_calls += 1
        finalize_started.set()
        await finalize_allowed.wait()

    consume_task = asyncio.create_task(
        lease.consume_archive(
            restore_current=restore_current,
            finalize_consumed=finalize_consumed,
        )
    )
    await finalize_started.wait()
    consume_task.cancel("first-cancellation")
    asyncio.get_running_loop().call_soon(consume_task.cancel, "second-cancellation")

    assert consume_task.done() is False
    assert authority.release_calls == []
    finalize_allowed.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await consume_task

    assert raised.value.args == ("first-cancellation",)
    assert finalize_calls == 1
    assert authority.release_calls == []
    assert lease.disposition is SessionOperationLeaseDisposition.CONSUMED


@pytest.mark.asyncio
async def test_archive_cleanup_failure_preserves_cancellation_primacy_and_redacts_detail() -> None:
    authority = _FakeAuthority()
    lease = await _acquire(authority, operation_kind=SessionOperationKind.ARCHIVE, renew_interval_seconds=10)
    finalize_started = asyncio.Event()
    finalize_allowed = asyncio.Event()
    cleanup_secret = "finalize-secret-path"

    async def restore_current() -> None:
        raise AssertionError("restore must not run after commit")

    async def finalize_consumed() -> None:
        finalize_started.set()
        await finalize_allowed.wait()
        raise OSError(cleanup_secret)

    consume_task = asyncio.create_task(
        lease.consume_archive(
            restore_current=restore_current,
            finalize_consumed=finalize_consumed,
        )
    )
    await finalize_started.wait()
    consume_task.cancel("first-cancellation")
    asyncio.get_running_loop().call_soon(consume_task.cancel, "second-cancellation")
    finalize_allowed.set()

    with pytest.raises(asyncio.CancelledError) as raised:
        await consume_task

    rendered = "".join(traceback.format_exception(raised.value))
    assert raised.value.args == ("first-cancellation",)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert cleanup_secret not in rendered
    assert "OSError" in "\n".join(raised.value.__notes__)
    assert authority.release_calls == []
    assert lease.disposition is SessionOperationLeaseDisposition.CONSUMED


@pytest.mark.asyncio
async def test_archive_consume_unprovable_outcome_is_unknown_and_never_released() -> None:
    authority = _FakeAuthority()
    operation_id = "operation-secret-123"
    lease_token = "lease-secret-456"
    primary = RuntimeError(f"connection dropped operation_id={operation_id}")
    probe = OSError(f"database unavailable lease_token={lease_token}")
    authority.archive_delete_error = primary
    authority.reconcile_archive_delete_error = probe
    lease = await _acquire(authority, operation_kind=SessionOperationKind.ARCHIVE, renew_interval_seconds=10)

    with pytest.raises(SessionOperationTerminalOutcomeUnknown) as raised:
        await lease.consume_archive()

    rendered = "".join(traceback.format_exception(raised.value))
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert operation_id not in rendered
    assert lease_token not in rendered
    assert "RuntimeError" in "\n".join(raised.value.__notes__)
    assert "OSError" in "\n".join(raised.value.__notes__)
    assert authority.release_calls == []
    assert lease.disposition is SessionOperationLeaseDisposition.UNKNOWN


@pytest.mark.asyncio
async def test_archive_consume_reconciliation_fence_loss_is_lost_and_never_released() -> None:
    authority = _FakeAuthority()
    operation_id = "operation-secret-789"
    lease_token = "lease-secret-987"
    primary = RuntimeError(f"connection dropped operation_id={operation_id}")
    reconciliation_loss = SessionOperationFenceLost(FenceLossReason.STALE_EPOCH)
    reconciliation_loss.add_note(f"lease_token={lease_token}")
    authority.archive_delete_error = primary
    authority.reconcile_archive_delete_error = reconciliation_loss
    lease = await _acquire(authority, operation_kind=SessionOperationKind.ARCHIVE, renew_interval_seconds=10)

    with pytest.raises(SessionOperationFenceLost) as raised:
        await lease.consume_archive()

    rendered = "".join(traceback.format_exception(raised.value))
    assert raised.value is not reconciliation_loss
    assert raised.value.reason is FenceLossReason.STALE_EPOCH
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert operation_id not in rendered
    assert lease_token not in rendered
    assert "RuntimeError" in "\n".join(raised.value.__notes__)
    assert authority.release_calls == []
    assert lease.disposition is SessionOperationLeaseDisposition.LOST


@pytest.mark.asyncio
async def test_archive_consume_child_failure_releases_current_context_before_raising() -> None:
    authority = _FakeAuthority()
    child_error = ValueError("archive child failed")
    lease = await _acquire(authority, operation_kind=SessionOperationKind.ARCHIVE, renew_interval_seconds=10)

    async def failing_child() -> None:
        raise child_error

    lease.create_task(failing_child())
    with pytest.raises(ValueError) as raised:
        await lease.consume_archive()

    assert raised.value is child_error
    assert authority.archive_delete_calls == []
    assert authority.release_calls == [lease.context]
    assert lease.disposition is SessionOperationLeaseDisposition.RELEASED


@pytest.mark.asyncio
async def test_archive_consume_refuses_after_renewal_loss() -> None:
    authority = _FakeAuthority()
    loss = SessionOperationFenceLost(FenceLossReason.LEASE_EXPIRED)
    authority.renew_error = loss
    lease = await _acquire(authority, operation_kind=SessionOperationKind.ARCHIVE)
    await _wait_for_thread_event(authority.renew_called)
    assert await lease.wait_until_lost() is loss

    with pytest.raises(SessionOperationFenceLost) as raised:
        await lease.consume_archive()

    assert raised.value is loss
    assert authority.archive_delete_calls == []
    assert authority.release_calls == []
    assert lease.disposition is SessionOperationLeaseDisposition.LOST


@pytest.mark.asyncio
async def test_external_effect_guard_reproves_exact_authority_with_compare_and_swap() -> None:
    authority = _FakeAuthority()
    lease = await _acquire(authority, operation_kind=SessionOperationKind.EXECUTE, renew_interval_seconds=10)
    authority.compare_and_swap_calls.clear()

    lease.guard_external_effect()

    assert authority.compare_and_swap_calls == [lease.context]
    await lease.close()


@pytest.mark.asyncio
async def test_external_effect_guard_refuses_before_cas_after_renewal_loss() -> None:
    authority = _FakeAuthority()
    loss = SessionOperationFenceLost(FenceLossReason.LEASE_EXPIRED)
    authority.renew_error = loss
    lease = await _acquire(authority, operation_kind=SessionOperationKind.EXECUTE)
    authority.compare_and_swap_calls.clear()
    await _wait_for_thread_event(authority.renew_called)
    assert await lease.wait_until_lost() is loss

    with pytest.raises(SessionOperationFenceLost) as raised:
        lease.guard_external_effect()

    assert raised.value is loss
    assert authority.compare_and_swap_calls == []
    with pytest.raises(SessionOperationFenceLost):
        await lease.close()


@pytest.mark.asyncio
async def test_archive_consume_is_idempotent_for_same_lease() -> None:
    authority = _FakeAuthority()
    authority.archive_delete_allowed.clear()
    lease = await _acquire(authority, operation_kind=SessionOperationKind.ARCHIVE, renew_interval_seconds=10)

    first = asyncio.create_task(lease.consume_archive())
    await _wait_for_thread_event(authority.archive_delete_started)
    second = asyncio.create_task(lease.consume_archive())
    authority.archive_delete_allowed.set()
    await asyncio.gather(first, second)
    await lease.close()

    assert authority.archive_delete_calls == [lease.context]
    assert authority.release_calls == []
    assert lease.disposition is SessionOperationLeaseDisposition.CONSUMED


@pytest.mark.asyncio
async def test_archive_consume_rejects_non_archive_and_after_normal_close_begins() -> None:
    ordinary = _FakeAuthority()
    ordinary_lease = await _acquire(ordinary, renew_interval_seconds=10)
    with pytest.raises(RuntimeError, match="ARCHIVE"):
        await ordinary_lease.consume_archive()
    await ordinary_lease.close()

    authority = _FakeAuthority()
    lease = await _acquire(authority, operation_kind=SessionOperationKind.ARCHIVE, renew_interval_seconds=10)
    child_allowed = asyncio.Event()
    lease.create_task(child_allowed.wait())
    close_task = asyncio.create_task(lease.close())
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="normal close"):
        await lease.consume_archive()
    child_allowed.set()
    await close_task
    assert lease.disposition is SessionOperationLeaseDisposition.RELEASED


def test_invalid_renew_interval_fails_before_authority_acquisition() -> None:
    authority = _FakeAuthority()

    async def acquire_with_interval(interval: float) -> None:
        await _acquire(authority, renew_interval_seconds=interval)

    for interval in (0.0, -1.0, 30.0, 31.0):
        with pytest.raises(ValueError, match="renew_interval_seconds"):
            asyncio.run(acquire_with_interval(interval))
    assert authority.acquire_calls == []
