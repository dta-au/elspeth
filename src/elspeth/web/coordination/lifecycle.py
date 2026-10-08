"""Async ownership lifecycle for persistent session-operation fences.

The database authority remains synchronous and owns every transaction it
opens.  This module only manages the asynchronous lifetime around that
authority: acquire and renew calls run in the bounded worker pool, one
immutable operation context is retained for the logical operation, and release happens
after every lifecycle-owned child task has settled.
"""

from __future__ import annotations

import asyncio
import errno
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Never, cast, final
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from elspeth.contracts import errors as contract_errors
from elspeth.contracts.errors import ComposerOwnedSettlementFailure
from elspeth.web.async_workers import (
    _raise_lifecycle_originals,
    _retain_cancellation,
    run_execution_lease_sql_finish_once,
    run_required_sql_finish_once,
    run_required_sql_in_worker,
    run_stream_read_in_worker,
    run_sync_in_worker,
)
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
from elspeth.web.execution_lease_cleanup import ExecutionAcquisitionObligation
from elspeth.web.required_sql_outcomes import RequiredSQLFinishOnce, RequiredSQLRaised
from elspeth.web.required_work import RequiredWorkCoordinator, RequiredWorkSource, RequiredWorkTicket

if TYPE_CHECKING:
    from types import TracebackType

    from elspeth.web.sessions.protocol import SessionForkAuthority, SessionOperationAuthority


@dataclass(slots=True)
class _RenewalAttemptObservation:
    context: SessionOperationContext
    ticket: RequiredWorkTicket
    actual_outcome: RequiredSQLFinishOnce[SessionOperationContext] | None = None
    task: asyncio.Task[SessionOperationContext] | None = None
    outcome_observed: bool = False
    returned: SessionOperationContext | None = None
    error: BaseException | None = None
    allocation_failure: BaseException | None = None


async def _join_renewal_attempt_observation(attempt: _RenewalAttemptObservation) -> tuple[BaseException, ...]:
    task = attempt.task
    if task is None:
        from elspeth.web.sessions.service import ComposerTerminalSQLCompletionUnknown

        if isinstance(attempt.allocation_failure, ComposerTerminalSQLCompletionUnknown):
            return (attempt.allocation_failure,) if attempt.error is None else (attempt.allocation_failure, attempt.error)
        unknown = ComposerTerminalSQLCompletionUnknown("Renewal attempt lacks its actual owned task")
        unknown.__cause__ = attempt.error
        return (unknown,)
    errors: list[BaseException] = []
    while not task.done():
        try:
            await asyncio.sleep(0.01)
        except asyncio.CancelledError as cancellation:
            if all(cancellation is not earlier for earlier in errors):
                errors.append(cancellation)
    if not attempt.outcome_observed:
        # Preserve the first actual task cancellation/error witness on this attempt.
        from elspeth.web.sessions.service import ComposerTerminalSQLCompletionUnknown

        if isinstance(attempt.error, ComposerTerminalSQLCompletionUnknown):
            unknown = attempt.error
            # Retrieve the actual completed Task fault without replacing its first witness.
            with suppress(BaseException):
                task.result()
        else:
            unknown = ComposerTerminalSQLCompletionUnknown("Renewal attempt has no actual joined outcome")
            try:
                task.result()
            except BaseException as original:
                unknown.__cause__ = original
            attempt.error = unknown
        errors.append(unknown)
    elif attempt.error is not None:
        if all(attempt.error is not earlier for earlier in errors):
            errors.append(attempt.error)
        # Consume the actual Task failure without replacing the retained object.
        if not task.cancelled():
            task.exception()
    elif attempt.returned is None or attempt.returned != attempt.context:
        errors.append(contract_errors.AuditIntegrityError("Renewal attempt outcome changed immutable context"))
    return tuple(errors)


_MAX_RENEW_INTERVAL_SECONDS = 30.0
type ArchiveLifecycleCallback = Callable[[], Awaitable[None]]


async def _noop_archive_lifecycle_callback() -> None:
    return


def _has_required_lifecycle_failure(error: BaseException) -> bool:
    """Retain only nominal integrity/storage/accounting evidence at owned seams."""
    if isinstance(error, BaseExceptionGroup):
        return any(_has_required_lifecycle_failure(child) for child in error.exceptions)
    if isinstance(error, (*contract_errors.TIER_1_ERRORS, SQLAlchemyError, ComposerOwnedSettlementFailure)):
        return True
    if isinstance(error, OSError) and error.errno in (errno.EIO, errno.ENOSPC, errno.EROFS):
        return True
    return isinstance(error, asyncio.CancelledError) and error.__cause__ is not None and _has_required_lifecycle_failure(error.__cause__)


def _preserve_failures(
    primary: BaseException,
    secondaries: Iterable[BaseException | None],
    *,
    note_prefix: str | None,
    group_message: str,
) -> BaseException:
    """Return what must escape when more than one lifecycle step failed.

    Every nominal integrity/storage/accounting failure survives as its instance, grouped with
    ``primary``.  Ordinary secondary failures are reduced to a class-name note
    on ``primary`` (when ``note_prefix`` is given) so provider or database
    detail never rides an outward exception; a caller whose terminal
    classification already records those names passes ``None``.
    """
    preserved: list[BaseException] = [primary]
    for secondary in secondaries:
        if secondary is None or any(secondary is kept for kept in preserved):
            continue
        if _has_required_lifecycle_failure(secondary):
            preserved.append(secondary)
        elif note_prefix is not None:
            primary.add_note(f"{note_prefix} also failed with {type(secondary).__name__}.")
    if len(preserved) == 1:
        return primary
    return BaseExceptionGroup(group_message, preserved)


def _failure_after_cancellation(
    cancellation: asyncio.CancelledError,
    failures: Iterable[tuple[str, BaseException | None]],
    *,
    group_message: str,
) -> BaseException:
    """Return what escapes once cancellation-time cleanup has been joined.

    Cancellation stays primary while every joined failure is ordinary; those
    are recorded on it as class-name notes. A nominal required failure is
    never reduced to a note: it (or a group of them) escapes instead, carrying
    the ordinary notes, with the cancellation left as its ``__context__``.
    """
    integrity: list[BaseException] = []
    notes: list[str] = []
    for note_prefix, failure in failures:
        if failure is None or any(failure is kept for kept in integrity):
            continue
        if _has_required_lifecycle_failure(failure):
            integrity.append(failure)
        else:
            notes.append(f"{note_prefix} also failed with {type(failure).__name__}.")
    escaping: BaseException
    if not integrity:
        escaping = cancellation
    elif len(integrity) == 1:
        escaping = integrity[0]
    else:
        escaping = BaseExceptionGroup(group_message, integrity)
    for note in notes:
        escaping.add_note(note)
    return escaping


def _retain_adoption_cancellation_outcome(observations: list[asyncio.CancelledError] | None, failure: BaseException | None) -> None:
    """Keep cancelled physical outcomes as objects even when selection reduces them to notes."""
    if observations is None or failure is None:
        return
    pending = [failure]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        if isinstance(current, asyncio.CancelledError):
            _retain_cancellation(observations, current)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)


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


def _acquired_context_error(
    context: object,
    *,
    requested_kind: SessionOperationKind,
) -> BaseException | None:
    if type(context) is not SessionOperationContext:
        return TypeError("authority.acquire must return an exact SessionOperationContext")
    if context.operation_kind is not requested_kind:
        return RuntimeError("authority.acquire returned a context for a different operation kind")
    return None


def _is_context_instance(context: object) -> bool:
    return isinstance(context, SessionOperationContext)


def _safe_cleanup_context(context: object) -> SessionOperationContext | None:
    """Derive an exact cleanup capability only from a Context instance."""
    if not _is_context_instance(context):
        return None
    candidate = cast(SessionOperationContext, context)
    fence = candidate.fence
    operation_kind = candidate.operation_kind
    if type(fence) is not SessionOperationFence or type(operation_kind) is not SessionOperationKind:
        return None
    return SessionOperationContext(
        fence=fence,
        operation_kind=operation_kind,
    )


async def _release_acquired_context(
    authority: SessionOperationAuthority,
    context: object,
) -> None:
    safe_context = _safe_cleanup_context(context)
    if safe_context is None:
        raise TypeError("acquired result does not contain a safe exact SessionOperationContext cleanup capability")
    await run_stream_read_in_worker(authority.release, safe_context)


async def _finish_cancelled_acquire(
    authority: SessionOperationAuthority,
    acquire_task: asyncio.Task[SessionOperationContext],
) -> BaseException | None:
    """Retrieve a cancellation-surviving acquire and release its result."""
    try:
        context = await acquire_task
    except BaseException as acquire_error:
        return acquire_error
    try:
        await _release_acquired_context(authority, context)
    except BaseException as release_error:
        return release_error
    return None


async def _finish_cancelled_adopt(
    authority: SessionOperationAuthority,
    compare_and_swap_task: asyncio.Task[Any],
    context: SessionOperationContext,
) -> tuple[BaseException | None, BaseException | None]:
    """Join a cancellation-surviving adoption and release its exact context.

    Both failures are returned as instances; the caller decides which may
    escape and which are reduced to class-name notes.
    """
    compare_and_swap_error: BaseException | None = None
    try:
        await compare_and_swap_task
    except BaseException as error:
        compare_and_swap_error = error

    release_error = await _capture_adopt_release_error(authority, context)
    return compare_and_swap_error, release_error


async def _capture_adopt_release_error(
    authority: SessionOperationAuthority,
    context: SessionOperationContext,
) -> BaseException | None:
    """Release an exact adopted context and return its failure, if any."""
    try:
        await run_stream_read_in_worker(authority.release, context)
    except BaseException as error:
        return error
    return None


async def _raise_adopt_failure_after_release(
    authority: SessionOperationAuthority,
    context: SessionOperationContext,
    failure: BaseException,
    *,
    phase: Literal["validation", "compare-and-swap"],
    cancellation_observations: list[asyncio.CancelledError] | None = None,
) -> Never:
    """Release an exact context before surfacing adoption failure or cancellation."""
    failure_refs = [failure]
    failure_type = type(failure).__name__
    del failure
    release_tasks = [
        asyncio.create_task(
            _capture_adopt_release_error(authority, context),
            name="session-operation-failed-adopt-release",
        )
    ]
    try:
        shielded_release_error = await asyncio.shield(release_tasks[0])
    except asyncio.CancelledError as cancellation:
        if cancellation_observations is not None:
            _retain_cancellation(cancellation_observations, cancellation)
        joined_release_error = await _join_shielded_task_after_cancellation(
            release_tasks[0], cancellation_observations=cancellation_observations
        )
        _retain_adoption_cancellation_outcome(cancellation_observations, joined_release_error)
        cancellation.add_note(f"Session-operation adoption {phase} failed with {failure_type} before cancellation.")
        escaping = _failure_after_cancellation(
            cancellation,
            (
                (f"Session-operation adoption {phase}", failure_refs[0]),
                ("Session-operation failed-adoption release", joined_release_error),
            ),
            group_message="Session operation adoption integrity failures",
        )
        failure_refs.clear()
        release_tasks.clear()
        del joined_release_error
        if escaping is cancellation:
            cancellation.__cause__ = None
            cancellation.__context__ = None
            raise cancellation from None
        raise escaping from (escaping.__cause__ if escaping.__cause__ is not None else cancellation)
    else:
        primary = failure_refs.pop()
        release_tasks.clear()
        _retain_adoption_cancellation_outcome(cancellation_observations, shielded_release_error)
        escaping = _preserve_failures(
            primary,
            (shielded_release_error,),
            note_prefix="Session-operation failed-adoption release",
            group_message="Session operation adoption and release failed",
        )
        del shielded_release_error
        if not _has_required_lifecycle_failure(primary):
            primary.__cause__ = None
            primary.__context__ = None
        raise escaping from escaping.__cause__


async def _join_shielded_task_after_cancellation[T](
    task: asyncio.Task[T], *, cancellation_observations: list[asyncio.CancelledError] | None = None
) -> T:
    """Wait for an owned task even when the awaiting task is being cancelled."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            if cancellation_observations is not None:
                _retain_cancellation(cancellation_observations, cancellation)
            continue
    return task.result()


@final
class SessionOperationLease:
    """Renewable async lifetime around one immutable operation context.

    Nested services receive :attr:`context`; the authority itself and lifecycle
    methods stay private to the owner.  :attr:`fence` is a convenience view of
    that same context.  No connection or transaction remains open while the
    caller performs async, network, or filesystem work.
    """

    __slots__ = (
        "_authority",
        "_close_task",
        "_closed",
        "_context",
        "_disposition",
        "_execution_obligation",
        "_finish_mode",
        "_fork_authority",
        "_lease_seconds",
        "_lost_event",
        "_owned_tasks",
        "_renew_interval_seconds",
        "_renewal_attempt",
        "_renewal_error",
        "_renewal_ordinal",
        "_renewal_task",
        "_required_work",
        "_stop_renewal",
    )

    def __init__(
        self,
        authority: SessionOperationAuthority,
        context: SessionOperationContext,
        *,
        lease_seconds: int,
        renew_interval_seconds: float,
        fork_authority: SessionForkAuthority | None = None,
        required_work: RequiredWorkCoordinator | None = None,
        execution_obligation: ExecutionAcquisitionObligation | None = None,
    ) -> None:
        if required_work is not None:
            if type(required_work) is not RequiredWorkCoordinator:
                raise contract_errors.AuditIntegrityError("Lease requires an owned required-work coordinator")
            if required_work.authority.context != context:
                raise contract_errors.AuditIntegrityError("Lease required-work scope disagrees with context")
        if execution_obligation is not None:
            if type(execution_obligation) is not ExecutionAcquisitionObligation or execution_obligation.lease is not self:
                raise contract_errors.AuditIntegrityError("EXECUTE lease lacks retained construction ownership")
            if context is not execution_obligation.context or required_work is not None:
                raise contract_errors.AuditIntegrityError("EXECUTE lease changed exact acquisition scope")
        self._execution_obligation = execution_obligation
        self._required_work = required_work
        self._renewal_ordinal = 0
        self._authority = authority
        self._context = context
        self._lease_seconds = lease_seconds
        self._renew_interval_seconds = renew_interval_seconds
        self._fork_authority = fork_authority
        self._stop_renewal = asyncio.Event()
        self._lost_event = asyncio.Event()
        self._renewal_error: BaseException | None = None
        self._renewal_attempt: _RenewalAttemptObservation | None = None
        self._owned_tasks: set[asyncio.Task[Any]] = set()
        self._close_task: asyncio.Task[None] | None = None
        self._finish_mode: Literal["close", "consume"] | None = None
        self._disposition = SessionOperationLeaseDisposition.ACTIVE
        self._closed = False
        self._renewal_task: asyncio.Task[None] | None = None
        if execution_obligation is not None:
            execution_obligation.declare_renewal_allocation(self)
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
        execution_obligation: ExecutionAcquisitionObligation | None = None,
    ) -> SessionOperationLease:
        """Acquire one operation context without blocking the event loop.

        Cancellation cannot orphan an acquisition that already reached the
        worker: the worker result is retrieved and, when it minted a context,
        that exact context is released before cancellation resumes.
        """
        if type(operation_kind) is not SessionOperationKind:
            raise TypeError("operation_kind must be an exact SessionOperationKind")
        interval = _validate_lifecycle_timing(
            lease_seconds=lease_seconds,
            renew_interval_seconds=renew_interval_seconds,
        )
        if execution_obligation is not None:
            if type(execution_obligation) is not ExecutionAcquisitionObligation or operation_kind is not SessionOperationKind.EXECUTE:
                raise contract_errors.AuditIntegrityError("Execution obligation cannot authorize another operation")
            execution_obligation.validate_acquisition_request(
                authority, session_id=session_id, owner_instance_id=owner_instance_id, lease_seconds=lease_seconds
            )
            return await cls._acquire_execution(authority, execution_obligation, interval)
        # Held in a list so the finished task (whose repr carries its result or
        # exception) can be dropped from this frame on every path before a
        # failure escapes; a plain local cannot be deleted on one branch only.
        acquire_tasks: list[asyncio.Task[SessionOperationContext]] = [
            asyncio.create_task(
                run_stream_read_in_worker(
                    authority.acquire,
                    session_id=session_id,
                    operation_kind=operation_kind,
                    owner_instance_id=owner_instance_id,
                    lease_seconds=lease_seconds,
                ),
                name="session-operation-acquire",
            )
        ]
        try:
            context = await asyncio.shield(acquire_tasks[0])
        except asyncio.CancelledError as cancellation:
            cleanup_task = asyncio.create_task(
                _finish_cancelled_acquire(authority, acquire_tasks[0]),
                name="session-operation-cancelled-acquire-cleanup",
            )
            cleanup_error = await _join_shielded_task_after_cancellation(cleanup_task)
            cancelled_escaping = _failure_after_cancellation(
                cancellation,
                (("Session-operation acquire cancellation cleanup", cleanup_error),),
                group_message="Session operation acquire cancellation integrity failures",
            )
            del cleanup_error, cleanup_task
            acquire_tasks.clear()
            if cancelled_escaping is cancellation:
                raise
            raise cancelled_escaping from (cancelled_escaping.__cause__ if cancelled_escaping.__cause__ is not None else cancellation)
        acquire_tasks.clear()
        context_error = _acquired_context_error(context, requested_kind=operation_kind)
        if context_error is not None:
            release_task = asyncio.create_task(
                _release_acquired_context(authority, context),
                name="session-operation-invalid-context-release",
            )
            escaping: BaseException = context_error
            try:
                await asyncio.shield(release_task)
            except asyncio.CancelledError as cancellation:
                release_error: BaseException | None = None
                try:
                    await _join_shielded_task_after_cancellation(release_task)
                except BaseException as error:
                    release_error = error
                escaping = _failure_after_cancellation(
                    cancellation,
                    (
                        ("Session-operation invalid-context release", release_error),
                        ("Session-operation context validation", context_error),
                    ),
                    group_message="Session operation invalid-context integrity failures",
                )
                del release_error, release_task
                if escaping is cancellation:
                    raise
                raise escaping from (escaping.__cause__ if escaping.__cause__ is not None else cancellation)
            except BaseException as release_error:
                escaping = _preserve_failures(
                    context_error,
                    (release_error,),
                    note_prefix="Session-operation invalid-context release",
                    group_message="Session operation context validation and release failed",
                )
            del release_task
            raise escaping from escaping.__cause__
        return cls(
            authority,
            context,
            lease_seconds=lease_seconds,
            renew_interval_seconds=interval,
        )

    @classmethod
    async def _acquire_execution(
        cls, authority: SessionOperationAuthority, obligation: ExecutionAcquisitionObligation, interval: float
    ) -> SessionOperationLease:
        if cls is not SessionOperationLease:
            raise contract_errors.AuditIntegrityError("EXECUTE lease constructor must retain its owned concrete type")
        outcome = await run_execution_lease_sql_finish_once(obligation.acquire_submission)
        cancellations = list(outcome.deferred_cancellations)
        if isinstance(outcome, RequiredSQLRaised):
            _raise_lifecycle_originals(cancellations, [outcome.error])
            raise contract_errors.AuditIntegrityError("Raised EXECUTE outcome escaped original projection")
        context = outcome.value
        if type(context) is not SessionOperationContext or context is not obligation.context:
            failure = contract_errors.AuditIntegrityError("EXECUTE acquisition lacks exact joined context")
            obligation.registry.record_failure(failure)
            _raise_lifecycle_originals(cancellations, [failure])
            raise failure
        if cancellations:
            release = await run_execution_lease_sql_finish_once(obligation.issue_release(context))
            for original in release.deferred_cancellations:
                _retain_cancellation(cancellations, original)
            failures = [release.error] if type(release) is RequiredSQLRaised else []
            _raise_lifecycle_originals(cancellations, failures)
        lease = cls.__new__(cls)
        obligation.retain_lease_construction(lease)
        try:
            cls.__init__(
                lease,
                authority,
                context,
                lease_seconds=obligation.lease_seconds,
                renew_interval_seconds=interval,
                execution_obligation=obligation,
            )
        except BaseException as original:
            obligation.record_construction_failure(original)
            cleanup_failures: list[BaseException] = [original]
            try:
                await lease.close()
            except BaseException as cleanup:
                if cleanup is not original:
                    cleanup_failures.append(cleanup)
            _raise_lifecycle_originals(cancellations, cleanup_failures)
            raise
        return lease

    @classmethod
    async def adopt(
        cls,
        authority: SessionOperationAuthority,
        context: SessionOperationContext,
        *,
        lease_seconds: int,
        renew_interval_seconds: float | None = None,
        required_work: RequiredWorkCoordinator | None = None,
        cancellation_observations: list[asyncio.CancelledError] | None = None,
    ) -> SessionOperationLease:
        """Adopt a context atomically minted by a composite authority method.

        Cancellation cannot orphan a context after its compare-and-swap reached
        the worker.  The worker is joined and that exact context is released
        before the original cancellation resumes.
        """
        if type(context) is not SessionOperationContext:
            raise TypeError("context must be an exact SessionOperationContext")
        try:
            interval = _validate_lifecycle_timing(
                lease_seconds=lease_seconds,
                renew_interval_seconds=renew_interval_seconds,
            )
            if required_work is not None:
                if type(required_work) is not RequiredWorkCoordinator:
                    raise contract_errors.AuditIntegrityError("Lease adoption requires an owned required-work coordinator")
                if required_work.authority.context != context:
                    raise contract_errors.AuditIntegrityError("Lease adoption required-work scope disagrees with context")
        except BaseException as validation_error:
            validation_cleanup = _raise_adopt_failure_after_release(
                authority,
                context,
                validation_error,
                phase="validation",
                cancellation_observations=cancellation_observations,
            )
            del validation_error
            await validation_cleanup
        adoption_ticket = required_work.reserve(RequiredWorkSource.LEASE_ADOPTION) if required_work is not None else None
        compare_and_swap_work = (
            run_required_sql_in_worker(adoption_ticket, authority.compare_and_swap, context)
            if adoption_ticket is not None
            else run_stream_read_in_worker(authority.compare_and_swap, context)
        )
        compare_and_swap_tasks = [
            asyncio.create_task(
                compare_and_swap_work,
                name="session-operation-adopt-compare-and-swap",
            )
        ]
        try:
            await asyncio.shield(compare_and_swap_tasks[0])
        except asyncio.CancelledError as cancellation:
            if cancellation_observations is not None:
                _retain_cancellation(cancellation_observations, cancellation)
            cleanup_tasks = [
                asyncio.create_task(
                    _finish_cancelled_adopt(authority, compare_and_swap_tasks[0], context),
                    name="session-operation-cancelled-adopt-cleanup",
                )
            ]
            cancelled_compare_and_swap_error, cancelled_release_error = await _join_shielded_task_after_cancellation(
                cleanup_tasks[0], cancellation_observations=cancellation_observations
            )
            _retain_adoption_cancellation_outcome(cancellation_observations, cancelled_compare_and_swap_error)
            _retain_adoption_cancellation_outcome(cancellation_observations, cancelled_release_error)
            escaping = _failure_after_cancellation(
                cancellation,
                (
                    ("Session-operation adoption cancellation compare-and-swap", cancelled_compare_and_swap_error),
                    ("Session-operation adoption cancellation release", cancelled_release_error),
                ),
                group_message="Session operation adoption cancellation integrity failures",
            )
            del cancelled_compare_and_swap_error, cancelled_release_error
            cleanup_tasks.clear()
            compare_and_swap_tasks.clear()
            if escaping is cancellation:
                cancellation.__cause__ = None
                cancellation.__context__ = None
                raise cancellation from None
            raise escaping from (escaping.__cause__ if escaping.__cause__ is not None else cancellation)
        except BaseException as compare_and_swap_error:
            compare_and_swap_tasks.clear()
            compare_and_swap_cleanup = _raise_adopt_failure_after_release(
                authority,
                context,
                compare_and_swap_error,
                phase="compare-and-swap",
                cancellation_observations=cancellation_observations,
            )
            del compare_and_swap_error
            await compare_and_swap_cleanup
        return cls(
            authority,
            context,
            lease_seconds=lease_seconds,
            renew_interval_seconds=interval,
            required_work=required_work,
        )

    @classmethod
    async def adopt_fork_child(
        cls,
        authority: SessionOperationAuthority,
        fork_authority: SessionForkAuthority,
        *,
        lease_seconds: int,
        renew_interval_seconds: float | None = None,
    ) -> SessionOperationLease:
        """Adopt only a child proven by its exact live fork composite.

        The hidden child is intentionally archived while staging, so generic
        session CAS/renew must continue to reject it. Validation and renewal
        instead prove the parent fence, operation receipt, child lineage, and
        exact child fence together under canonical pair locks.
        """
        from elspeth.web.sessions.protocol import SessionForkAuthority as RuntimeSessionForkAuthority

        if type(fork_authority) is not RuntimeSessionForkAuthority:
            raise TypeError("fork_authority must be an exact SessionForkAuthority")
        context = fork_authority.child_context
        try:
            interval = _validate_lifecycle_timing(
                lease_seconds=lease_seconds,
                renew_interval_seconds=renew_interval_seconds,
            )
        except BaseException as validation_error:
            validation_cleanup = _raise_adopt_failure_after_release(
                authority,
                context,
                validation_error,
                phase="validation",
            )
            del validation_error
            await validation_cleanup
        validation_tasks = [
            asyncio.create_task(
                run_stream_read_in_worker(authority.validate_fork_child_lease, fork_authority),
                name="session-fork-child-adopt-validation",
            )
        ]
        try:
            validated = await asyncio.shield(validation_tasks[0])
            if type(validated) is not SessionOperationContext or validated != context:
                raise RuntimeError("fork child validation changed immutable context")
        except asyncio.CancelledError as cancellation:
            cleanup_tasks = [
                asyncio.create_task(
                    _finish_cancelled_adopt(authority, validation_tasks[0], context),
                    name="session-fork-child-cancelled-adopt-cleanup",
                )
            ]
            cancelled_validation_error, cancelled_release_error = await _join_shielded_task_after_cancellation(cleanup_tasks[0])
            escaping = _failure_after_cancellation(
                cancellation,
                (
                    ("Fork-child adoption cancellation validation", cancelled_validation_error),
                    ("Fork-child adoption cancellation release", cancelled_release_error),
                ),
                group_message="Fork-child adoption cancellation integrity failures",
            )
            del cancelled_validation_error, cancelled_release_error
            cleanup_tasks.clear()
            validation_tasks.clear()
            if escaping is cancellation:
                cancellation.__cause__ = None
                cancellation.__context__ = None
                raise cancellation from None
            raise escaping from (escaping.__cause__ if escaping.__cause__ is not None else cancellation)
        except BaseException as validation_error:
            validation_tasks.clear()
            validation_cleanup = _raise_adopt_failure_after_release(
                authority,
                context,
                validation_error,
                phase="compare-and-swap",
            )
            del validation_error
            await validation_cleanup
        return cls(
            authority,
            context,
            lease_seconds=lease_seconds,
            renew_interval_seconds=interval,
            fork_authority=fork_authority,
        )

    @property
    def fence(self) -> SessionOperationFence:
        """The immutable fence carried by :attr:`context`."""
        return self._context.fence

    @property
    def context(self) -> SessionOperationContext:
        """The immutable fence and operation kind for nested service work."""
        return self._context

    @property
    def execution_obligation(self) -> ExecutionAcquisitionObligation | None:
        return self._execution_obligation

    @property
    def renewal_error(self) -> BaseException | None:
        """First renewal failure, retained without low-level retry laundering."""
        return self._renewal_error

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def disposition(self) -> SessionOperationLeaseDisposition:
        """Exact terminal handling observed for this lease."""
        return self._disposition

    def raise_if_lost(self) -> None:
        """Surface renewal loss before starting another external side effect."""
        if self._renewal_error is not None:
            raise self._renewal_error

    def guard_external_effect(self) -> None:
        """Reprove this exact lease immediately before an external effect.

        The renewal task is an early-warning channel, not an authority proof:
        its most recent success can become stale before the next filesystem,
        network, or broadcast call starts.  This guard therefore checks the
        in-memory loss latch, performs an authoritative compare-and-swap for
        the immutable context, and checks the latch again for a renewal loss
        recorded while the CAS was in flight.
        """
        if self._closed or self._close_task is not None:
            raise RuntimeError("session operation lease is closing or closed")
        self.raise_if_lost()
        if self._fork_authority is None:
            self._authority.compare_and_swap(self._context)
        else:
            validated = self._authority.validate_fork_child_lease(self._fork_authority)
            if type(validated) is not SessionOperationContext or validated != self._context:
                raise RuntimeError("fork child authority guard changed immutable context")
        self.raise_if_lost()

    async def wait_until_lost(self, *, cancellation_observations: list[asyncio.CancelledError] | None = None) -> BaseException:
        """Wait until renewal proves or reports that authority is no longer safe."""
        try:
            await self._lost_event.wait()
        except asyncio.CancelledError as original:
            # This is the actual owned event-wait delivery boundary, distinct
            # from a child outcome thrown or returned by another producer.
            if cancellation_observations is not None:
                _retain_cancellation(cancellation_observations, original)
            raise
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

    @property
    def required_work(self) -> RequiredWorkCoordinator | None:
        return self._required_work

    def bind_required_work(self, coordinator: RequiredWorkCoordinator) -> None:
        if type(coordinator) is not RequiredWorkCoordinator:
            raise contract_errors.AuditIntegrityError("Lease binding requires an owned required-work coordinator")
        if coordinator.authority.context != self.context or (self._required_work is not None and self._required_work is not coordinator):
            raise contract_errors.AuditIntegrityError("Lease required-work binding changed immutable authority")
        self._required_work = coordinator

    async def _renew_forever(self) -> None:
        obligation = self._execution_obligation
        if obligation is not None:
            actual_task = asyncio.current_task()
            if actual_task is None:
                raise contract_errors.AuditIntegrityError("EXECUTE renewal lacks actual Task ownership")
            obligation.bind_actual_renewal_task(self, actual_task)
            self._renewal_task = actual_task
        while not self._stop_renewal.is_set():
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop_renewal.wait(),
                    timeout=self._renew_interval_seconds,
                )
            if self._stop_renewal.is_set():
                return
            try:
                if self._fork_authority is None:
                    if self._required_work is None:
                        renewed = await run_stream_read_in_worker(self._authority.renew, self._context, lease_seconds=self._lease_seconds)
                    else:
                        ticket = self._required_work.reserve(RequiredWorkSource.LEASE_RENEWAL, recurrence_ordinal=self._renewal_ordinal)
                        self._renewal_ordinal += 1
                        attempt = _RenewalAttemptObservation(self._context, ticket)
                        self._renewal_attempt = attempt
                        try:
                            attempt.task = asyncio.create_task(
                                self._run_required_renewal_attempt(attempt, ticket), name="session-operation-renewal-attempt"
                            )
                        except BaseException as allocation_error:
                            from elspeth.web.sessions.service import ComposerTerminalSQLCompletionUnknown

                            # Factory failure may follow allocation: no inferred absence,
                            # coroutine close, task cancellation or successful receipt.
                            unknown = ComposerTerminalSQLCompletionUnknown("Renewal task allocation custody is unknown")
                            unknown.__cause__ = allocation_error
                            attempt.allocation_failure = unknown
                            raise unknown from allocation_error
                        errors = await _join_renewal_attempt_observation(attempt)
                        if len(errors) == 1:
                            raise errors[0]
                        if errors:
                            raise BaseExceptionGroup("Renewal attempt retained original outcomes", errors)
                        if attempt.returned is None:
                            raise contract_errors.AuditIntegrityError("Renewal attempt omitted its actual context")
                        renewed = attempt.returned
                else:
                    renewed = await run_stream_read_in_worker(
                        self._authority.renew_fork_child_lease,
                        self._fork_authority,
                        lease_seconds=self._lease_seconds,
                    )
                if type(renewed) is not SessionOperationContext or renewed != self._context:
                    raise RuntimeError("session operation renewal changed immutable context")
            except asyncio.CancelledError:
                raise
            except BaseExceptionGroup as renewal_error:
                # Actual required SQL may retain both cancellation and a fault.
                self._record_renewal_error(renewal_error)
                return
            except Exception as renewal_error:
                self._record_renewal_error(renewal_error)
                return

    async def _run_required_renewal_attempt(
        self, attempt: _RenewalAttemptObservation, ticket: RequiredWorkTicket
    ) -> SessionOperationContext:
        try:
            outcome = await run_required_sql_finish_once(ticket, self._authority.renew, attempt.context, lease_seconds=self._lease_seconds)
            try:
                coordinator = self._required_work
                if type(coordinator) is not RequiredWorkCoordinator:
                    raise contract_errors.AuditIntegrityError("Renewal outcome lacks its exact owned coordinator")
                if coordinator.authority.context != attempt.context or attempt.context != self._context:
                    raise contract_errors.AuditIntegrityError("Renewal outcome changed immutable required-work context")
                coordinator.validate_joined_lifecycle_sql_outcome(
                    ticket=ticket,
                    expected_source=RequiredWorkSource.LEASE_RENEWAL,
                    actual_outcome=outcome,
                )
            except BaseException as receipt_error:
                from elspeth.web.sessions.service import ComposerTerminalSQLCompletionUnknown

                raise ComposerTerminalSQLCompletionUnknown("Renewal outcome has no verified physical receipt") from receipt_error
            attempt.actual_outcome = outcome
            attempt.outcome_observed = True
            errors: list[BaseException] = list(outcome.deferred_cancellations)
            if isinstance(outcome, RequiredSQLRaised) and all(outcome.error is not earlier for earlier in errors):
                errors.append(outcome.error)
            if len(errors) == 1:
                raise errors[0]
            if errors:
                raise BaseExceptionGroup("Renewal SQL retained original outcomes", errors)
            if isinstance(outcome, RequiredSQLRaised):
                raise outcome.error
            renewed = outcome.value
            if type(renewed) is not SessionOperationContext or renewed != attempt.context:
                raise contract_errors.AuditIntegrityError("session operation renewal changed immutable context")
        except BaseException as error:
            attempt.error = error
            raise
        attempt.returned = renewed
        return renewed

    async def observe_current_renewal_attempt(self) -> tuple[BaseException, ...]:
        """Join a snapshot of the current required attempt; renewal ticks continue."""
        attempt = self._renewal_attempt
        if attempt is None:
            return ()
        return await _join_renewal_attempt_observation(attempt)

    def _record_renewal_error(self, error: BaseException) -> None:
        if self._renewal_error is not None:
            return
        self._renewal_error = error
        self._lost_event.set()
        for task in tuple(self._owned_tasks):
            if not task.done():
                task.cancel()

    async def _join_owned_tasks(self) -> tuple[BaseException, ...]:
        """Join every owned child and return each distinct non-cancellation failure."""
        if not self._owned_tasks:
            return ()
        tasks = tuple(self._owned_tasks)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        errors: list[BaseException] = []
        for result in results:
            if not isinstance(result, BaseException) or (
                isinstance(result, asyncio.CancelledError) and not _has_required_lifecycle_failure(result)
            ):
                continue
            if any(result is kept for kept in errors):
                continue
            errors.append(result)
        self._owned_tasks.clear()
        return tuple(errors)

    async def _stop_and_join_renewal(self) -> None:
        self._stop_renewal.set()
        while self._renewal_task is None:
            if self._execution_obligation is None:
                raise contract_errors.AuditIntegrityError("Lease renewal task was not retained")
            # A factory exception may follow allocation. The registered
            # coroutine entry, not an absent wrapper, closes that uncertainty.
            await asyncio.sleep(0.01)
        await self._renewal_task

    async def _release_current(self) -> None:
        try:
            if self._execution_obligation is not None:
                outcome = await run_execution_lease_sql_finish_once(self._execution_obligation.issue_release(self._context))
                _raise_lifecycle_originals(
                    list(outcome.deferred_cancellations), [outcome.error] if type(outcome) is RequiredSQLRaised else []
                )
            elif self._required_work is None:
                await run_stream_read_in_worker(self._authority.release, self._context)
            else:
                ticket = self._required_work.prepare_lease_release()
                await run_required_sql_in_worker(ticket, self._authority.release, self._context)
        except SessionOperationFenceLost:
            self._disposition = SessionOperationLeaseDisposition.LOST
            raise
        except BaseException:
            self._disposition = SessionOperationLeaseDisposition.UNKNOWN
            raise
        self._disposition = SessionOperationLeaseDisposition.RELEASED

    async def _close(self) -> None:
        if self._execution_obligation is not None:
            actual_task = asyncio.current_task()
            if actual_task is None:
                raise contract_errors.AuditIntegrityError("EXECUTE close lacks actual Task ownership")
            self._execution_obligation.bind_lifecycle_task(actual_task)
            self._close_task = actual_task
            try:
                await self._close_execution()
            except BaseException as original:
                self._execution_obligation.record_lifecycle_outcome(original)
                raise
            else:
                self._execution_obligation.record_lifecycle_outcome(None)
            return
        owned_errors: tuple[BaseException, ...] = ()
        release_error: BaseException | None = None
        custody_error: BaseException | None = None
        try:
            owned_errors = await self._join_owned_tasks()
            try:
                await self._stop_and_join_renewal()
                if self._required_work is not None:
                    self._required_work.assert_completed()
            except BaseException as error:
                custody_error = error
                self._disposition = SessionOperationLeaseDisposition.UNKNOWN
            if custody_error is None and self._renewal_error is None:
                try:
                    await self._release_current()
                except BaseException as error:
                    release_error = error
            elif self._renewal_error is not None:
                self._disposition = SessionOperationLeaseDisposition.LOST
        finally:
            self._closed = True

        # Priority order is unchanged: renewal loss, then release, then owned
        # children.  What changed is that no failure behind the primary is
        # discarded: integrity failures escape as instances, the rest as notes.
        failures = [failure for failure in (self._renewal_error, custody_error, release_error, *owned_errors) if failure is not None]
        if not failures:
            return
        raise _preserve_failures(
            failures[0],
            failures[1:],
            note_prefix="Session-operation close",
            group_message="Session operation close failed",
        )

    async def _close_execution(self) -> None:
        obligation = self._execution_obligation
        if type(obligation) is not ExecutionAcquisitionObligation:
            raise contract_errors.AuditIntegrityError("EXECUTE close lost registered lifecycle ownership")
        cancellations: list[asyncio.CancelledError] = []
        failures: list[BaseException] = []
        tasks = tuple(self._owned_tasks)
        observed: set[asyncio.Task[Any]] = set()
        try:
            # Observe each completed child independently. Cancellation of this
            # close owner never cancels a child or substitutes wrapper outcome.
            while len(observed) < len(tasks):
                for task in tasks:
                    if task in observed or not task.done():
                        continue
                    try:
                        task.result()
                    except BaseException as original:
                        if all(original is not earlier for earlier in failures):
                            failures.append(original)
                    observed.add(task)
                if len(observed) < len(tasks):
                    try:
                        await asyncio.sleep(0.01)
                    except asyncio.CancelledError as original:
                        _retain_cancellation(cancellations, original)
            self._owned_tasks.clear()
            self._stop_renewal.set()
            while self._renewal_task is None or not self._renewal_task.done():
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError as original:
                    _retain_cancellation(cancellations, original)
            renewal_join_failure: BaseException | None = None
            try:
                self._renewal_task.result()
            except BaseException as original:
                renewal_join_failure = original
                if all(original is not earlier for earlier in failures):
                    failures.append(original)
            if self._renewal_error is not None:
                if all(self._renewal_error is not earlier for earlier in failures):
                    failures.append(self._renewal_error)
                self._disposition = SessionOperationLeaseDisposition.LOST
            elif renewal_join_failure is not None:
                self._disposition = SessionOperationLeaseDisposition.UNKNOWN
            else:
                outcome = await run_execution_lease_sql_finish_once(obligation.issue_release(self._context))
                for delivered_cancellation in outcome.deferred_cancellations:
                    _retain_cancellation(cancellations, delivered_cancellation)
                if type(outcome) is RequiredSQLRaised:
                    failures.append(outcome.error)
                    self._disposition = (
                        SessionOperationLeaseDisposition.LOST
                        if isinstance(outcome.error, SessionOperationFenceLost)
                        else SessionOperationLeaseDisposition.UNKNOWN
                    )
                else:
                    self._disposition = SessionOperationLeaseDisposition.RELEASED
        finally:
            self._closed = True
        for retained_failure in failures:
            obligation.registry.record_failure(retained_failure)
        _raise_lifecycle_originals(cancellations, failures)

    async def _run_archive_action(self) -> None:
        action_task = asyncio.create_task(
            run_sync_in_worker(self._authority.archive_delete, self._context),
            name="session-operation-archive-delete",
        )
        try:
            await asyncio.shield(action_task)
        except asyncio.CancelledError:
            await _join_shielded_task_after_cancellation(action_task)
            raise

    async def _run_archive_reconciliation(self) -> ArchiveDeleteReconciliation:
        reconcile_task = asyncio.create_task(
            run_sync_in_worker(self._authority.reconcile_archive_delete, self._context),
            name="session-operation-archive-reconcile",
        )
        try:
            return await asyncio.shield(reconcile_task)
        except asyncio.CancelledError:
            return await _join_shielded_task_after_cancellation(reconcile_task)

    async def _capture_archive_action_error(self) -> BaseException | None:
        try:
            await self._run_archive_action()
        except BaseException as error:
            return error
        return None

    async def _capture_archive_reconciliation(
        self,
    ) -> tuple[ArchiveDeleteReconciliation | object | None, BaseException | None]:
        try:
            return await self._run_archive_reconciliation(), None
        except BaseException as error:
            return None, error

    async def _capture_release_error(self) -> BaseException | None:
        try:
            await self._release_current()
        except BaseException as error:
            return error
        return None

    async def _run_archive_lifecycle_callback(
        self,
        callback: ArchiveLifecycleCallback,
        *,
        task_name: str,
    ) -> None:
        async def invoke_once() -> None:
            await callback()

        callback_task = asyncio.create_task(invoke_once(), name=task_name)
        try:
            await asyncio.shield(callback_task)
        except asyncio.CancelledError:
            await _join_shielded_task_after_cancellation(callback_task)
            raise

    async def _restore_archive_current(
        self,
        restore_current: ArchiveLifecycleCallback,
        *,
        primary_error: BaseException,
    ) -> None:
        try:
            await self._run_archive_lifecycle_callback(
                restore_current,
                task_name="session-operation-archive-restore-current",
            )
        except BaseException as restore_error:
            if isinstance(restore_error, SessionOperationFenceLost):
                self._disposition = SessionOperationLeaseDisposition.LOST
            else:
                self._disposition = SessionOperationLeaseDisposition.UNKNOWN
            escaping = _preserve_failures(
                primary_error,
                (restore_error,),
                note_prefix="Archive restore-current compensation",
                group_message="Archive action and restore-current compensation failed",
            )
            del restore_error
        else:
            return
        raise escaping from None

    @staticmethod
    def _unknown_terminal_error(*, primary_type: str, terminal_type: str, phase: str) -> SessionOperationTerminalOutcomeUnknown:
        error = SessionOperationTerminalOutcomeUnknown()
        error.add_note(f"Archive action failed with {primary_type}.")
        error.add_note(f"Archive {phase} failed with {terminal_type}.")
        return error

    @staticmethod
    def _terminal_with_integrity(
        terminal: BaseException,
        originals: Iterable[BaseException | None],
    ) -> BaseException:
        """Attach any Tier-1 original to a sanitized terminal classification.

        The terminal error already names every original by class; ordinary
        originals therefore add nothing, but an integrity failure must not be
        reduced to that name.
        """
        return _preserve_failures(
            terminal,
            originals,
            note_prefix=None,
            group_message="Archive terminal outcome with integrity failures",
        )

    @staticmethod
    def _sanitized_fence_loss(
        *,
        reason: FenceLossReason,
        primary_type: str,
        phase: str,
    ) -> SessionOperationFenceLost:
        error = SessionOperationFenceLost(reason)
        error.add_note(f"Archive action failed with {primary_type}.")
        error.add_note(f"Archive {phase} lost authority with reason {reason.value}.")
        return error

    async def _consume_archive(
        self,
        *,
        restore_current: ArchiveLifecycleCallback,
        finalize_consumed: ArchiveLifecycleCallback,
    ) -> None:
        try:
            owned_errors = await self._join_owned_tasks()
            await self._stop_and_join_renewal()
            if self._renewal_error is not None:
                self._disposition = SessionOperationLeaseDisposition.LOST
                raise _preserve_failures(
                    self._renewal_error,
                    owned_errors,
                    note_prefix="Session-operation archive child",
                    group_message="Session operation archive refused after renewal loss",
                )
            if owned_errors:
                release_error = await self._capture_release_error()
                failures = [failure for failure in (release_error, *owned_errors) if failure is not None]
                del release_error
                raise _preserve_failures(
                    failures[0],
                    failures[1:],
                    note_prefix="Session-operation archive close",
                    group_message="Session operation archive children failed",
                )

            primary_error = await self._capture_archive_action_error()
            if primary_error is None:
                self._disposition = SessionOperationLeaseDisposition.CONSUMED
                await self._run_archive_lifecycle_callback(
                    finalize_consumed,
                    task_name="session-operation-archive-finalize-consumed",
                )
                return

            primary_type = type(primary_error).__name__
            reconciliation, reconciliation_error = await self._capture_archive_reconciliation()
            if isinstance(reconciliation_error, SessionOperationFenceLost):
                reason = reconciliation_error.reason
                self._disposition = SessionOperationLeaseDisposition.LOST
                escaping = self._terminal_with_integrity(
                    self._sanitized_fence_loss(
                        reason=reason,
                        primary_type=primary_type,
                        phase="reconciliation",
                    ),
                    (primary_error,),
                )
                del primary_error, reconciliation_error
                raise escaping from None
            if reconciliation_error is not None:
                reconciliation_type = type(reconciliation_error).__name__
                self._disposition = SessionOperationLeaseDisposition.UNKNOWN
                escaping = self._terminal_with_integrity(
                    self._unknown_terminal_error(
                        primary_type=primary_type,
                        terminal_type=reconciliation_type,
                        phase="reconciliation",
                    ),
                    (primary_error, reconciliation_error),
                )
                del primary_error, reconciliation_error
                raise escaping from None
            if type(reconciliation) is not ArchiveDeleteReconciliation:
                reconciliation_type = type(reconciliation).__name__
                self._disposition = SessionOperationLeaseDisposition.UNKNOWN
                escaping = self._terminal_with_integrity(
                    self._unknown_terminal_error(
                        primary_type=primary_type,
                        terminal_type=reconciliation_type,
                        phase="reconciliation",
                    ),
                    (primary_error,),
                )
                del primary_error, reconciliation
                raise escaping from None
            if reconciliation is ArchiveDeleteReconciliation.CONSUMED:
                self._disposition = SessionOperationLeaseDisposition.CONSUMED
                # Confirmed deletion resolves commit uncertainty, not an
                # integrity failure. Finish consumed-resource cleanup, then
                # preserve the invariant violation as an operation failure.
                retain_primary_error = isinstance(primary_error, contract_errors.TIER_1_ERRORS)
                try:
                    await self._run_archive_lifecycle_callback(
                        finalize_consumed,
                        task_name="session-operation-archive-finalize-consumed",
                    )
                except BaseException as finalization_error:
                    if not retain_primary_error:
                        finalization_error.add_note(f"Archive action failed with {primary_type}.")
                        raise
                    escaping = _preserve_failures(
                        primary_error,
                        (finalization_error,),
                        note_prefix="Consumed archive finalization",
                        group_message="Archive action and consumed finalization failed",
                    )
                    del finalization_error
                    raise escaping from None
                if retain_primary_error:
                    raise primary_error
                return

            await self._restore_archive_current(
                restore_current,
                primary_error=primary_error,
            )
            release_error = await self._capture_release_error()
            if isinstance(release_error, SessionOperationFenceLost):
                reason = release_error.reason
                self._disposition = SessionOperationLeaseDisposition.LOST
                escaping = self._terminal_with_integrity(
                    self._sanitized_fence_loss(
                        reason=reason,
                        primary_type=primary_type,
                        phase="rollback release",
                    ),
                    (primary_error,),
                )
                del primary_error, release_error
                raise escaping from None
            if release_error is not None:
                release_type = type(release_error).__name__
                self._disposition = SessionOperationLeaseDisposition.UNKNOWN
                escaping = self._terminal_with_integrity(
                    self._unknown_terminal_error(
                        primary_type=primary_type,
                        terminal_type=release_type,
                        phase="rollback release",
                    ),
                    (primary_error, release_error),
                )
                del primary_error, release_error
                raise escaping from None
            raise primary_error
        finally:
            self._closed = True

    async def _join_finish_task(self, task: asyncio.Task[None]) -> None:
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Cancellation delays observation; it does not replace the owned
            # task's failure. Drain it and let its original exception escape.
            await _join_shielded_task_after_cancellation(task)
            raise

    async def close(self) -> None:
        """Join children, stop renewal, and release only known-current authority."""
        obligation = self._execution_obligation
        cancellations: list[asyncio.CancelledError] = []
        allocation_failures: list[BaseException] = []
        if self._close_task is None:
            self._finish_mode = "close"
            if obligation is None:
                self._close_task = asyncio.create_task(self._close(), name="session-operation-close")
            elif not obligation.lifecycle_required:
                obligation.declare_lifecycle_close()
                try:
                    self._close_task = asyncio.create_task(self._close(), name="session-operation-close")
                except BaseException as original:
                    # May have allocated before throwing. Only the actual
                    # close coroutine entry can resolve this ownership gap.
                    allocation_failures.append(original)
                    obligation.registry.record_failure(original)
            if obligation is not None:
                while self._close_task is None:
                    try:
                        await asyncio.sleep(0.01)
                    except asyncio.CancelledError as original:
                        _retain_cancellation(cancellations, original)
        close_task = self._close_task
        if close_task is None:
            raise contract_errors.AuditIntegrityError("Lease close lacks its retained Task")
        if obligation is None:
            await self._join_finish_task(close_task)
            return
        while not close_task.done():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError as original:
                _retain_cancellation(cancellations, original)
        failures: list[BaseException] = allocation_failures
        obligation.observe_lifecycle()
        if not obligation.lifecycle_outcome_recorded:
            failure = contract_errors.AuditIntegrityError("EXECUTE close Task lacks its original producer outcome")
            obligation.registry.record_failure(failure)
            _raise_lifecycle_originals(cancellations, [*failures, failure])
        if obligation.lifecycle_original_error is not None:
            failures.append(obligation.lifecycle_original_error)
        _raise_lifecycle_originals(cancellations, failures)

    async def consume_archive(
        self,
        *,
        restore_current: ArchiveLifecycleCallback = _noop_archive_lifecycle_callback,
        finalize_consumed: ArchiveLifecycleCallback = _noop_archive_lifecycle_callback,
    ) -> None:
        """Consume an ARCHIVE lease through one cancellation-safe terminal task."""
        if self._context.operation_kind is not SessionOperationKind.ARCHIVE:
            raise RuntimeError("consume_archive requires an ARCHIVE operation context")
        if self._close_task is None:
            self._finish_mode = "consume"
            self._close_task = asyncio.create_task(
                self._consume_archive(
                    restore_current=restore_current,
                    finalize_consumed=finalize_consumed,
                ),
                name="session-operation-archive-consume",
            )
        elif self._finish_mode != "consume":
            raise RuntimeError("archive consume cannot begin after normal close")
        await self._join_finish_task(self._close_task)

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
    ) -> Literal[False]:
        del exc_type, traceback
        try:
            await self.close()
        except BaseException as cleanup_error:
            if exc_value is None:
                raise
            if (
                self._execution_obligation is None
                and isinstance(exc_value, asyncio.CancelledError)
                and isinstance(cleanup_error, asyncio.CancelledError)
                and self._close_task is not None
                and self._close_task.done()
                and not self._close_task.cancelled()
                and self._close_task.exception() is None
            ):
                # A second cancellation interrupted only the waiter. The
                # owned close finished; preserve the body's original signal.
                return False
            if cleanup_error is not exc_value:
                raise BaseExceptionGroup(
                    "Session operation body and cleanup both failed",
                    [exc_value, cleanup_error],
                ) from None
        return False
