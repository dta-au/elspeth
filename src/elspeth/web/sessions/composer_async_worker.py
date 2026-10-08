"""App-owned durable composer queue, cancellation watch and recovery."""

from __future__ import annotations

import asyncio
import errno
import threading
import time
from collections.abc import Awaitable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import partial
from uuid import UUID

import structlog
from sqlalchemy.exc import SQLAlchemyError
from starlette.applications import Starlette

from elspeth.contracts.errors import AuditIntegrityError, ComposerOwnedSettlementFailure
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.async_workers import AsyncWorkerAdmissionTimeoutError, run_required_sql_finish_once, run_sync_in_worker
from elspeth.web.composer_watch_reads import ComposerOperationWatchReader
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.repository import SessionOperationConflictError
from elspeth.web.process_recovery import ProcessRecovery
from elspeth.web.required_executor import RequiredGenerationUnavailable
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import (
    ComposerFailureReceipt,
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkCoordinator,
    RequiredWorkIncomplete,
    RequiredWorkSource,
    RequiredWorkTicket,
    reduce_composer_failures,
    required_failure_leaves,
)
from elspeth.web.sessions.composer_app_services import ComposerAppServices, composer_app_services
from elspeth.web.sessions.composer_operation_errors import (
    deadline_expired_error,
    project_composer_operation_error,
    request_cancelled_error,
    worker_lost_error,
)
from elspeth.web.sessions.composer_operations import (
    COMPOSER_CANCEL_REQUESTED,
    COMPOSER_DEADLINE,
    COMPOSER_LEASE_LOST,
    COMPOSER_SHUTDOWN,
    ComposerOperationCancelledBeforeStart,
    ComposerOperationClaim,
    ComposerOperationError,
    ComposerOperationFenceLost,
    ComposerOperationPreconditionRefused,
    ComposerOperationRecord,
    ComposerOperationRunning,
    ComposerTurnDeadlineExpired,
)
from elspeth.web.sessions.composer_turn import (
    ComposerBudgetAnchor,
    ComposerTurnInput,
    ComposerTurnObservation,
    persist_cancelled_turn_audit,
    run_composer_turn,
)
from elspeth.web.sessions.routes._helpers import composer_request_lifecycle
from elspeth.web.sessions.schemas import RecomposeRequest, SendMessageRequest
from elspeth.web.sessions.service import ComposerTerminalSQLCompletionUnknown
from elspeth.web.sessions.time_normalization import restore_utc

slog = structlog.get_logger()


@dataclass(slots=True)
class _StartedSettlement:
    deferred_cancellations: list[asyncio.CancelledError] = field(default_factory=list)
    failure_attempted: bool = False
    terminal_receipt: ComposerOperationRecord | None = None
    verified_terminal: ComposerOperationRecord | None = None


@dataclass(frozen=True, slots=True)
class _AdoptionSettlementHandoff:
    """Actual finished job result carrying every live adoption observation."""

    terminal: ComposerOperationRecord
    cancellations: tuple[asyncio.CancelledError, ...]


def _owned_deadline_failure(
    original: BaseException,
    *,
    timeout_scope: asyncio.Timeout | None,
    record: ComposerOperationRecord,
    anchor: ComposerBudgetAnchor,
) -> BaseException:
    """Classify only expiry of this worker's actual owned turn budget."""
    if type(original) is not TimeoutError or timeout_scope is None or not timeout_scope.expired():
        return original
    deadline = ComposerTurnDeadlineExpired(
        session_id=record.session_id,
        operation_id=record.operation_id,
        remaining_seconds=anchor.remaining_seconds(monotonic_now=time.monotonic()),
        budget_seconds_at_running=anchor.remaining_at_running_seconds,
    )
    deadline.__cause__ = original
    return deadline


async def _join_owned[T](task: asyncio.Task[T], *, cancellation_observations: list[asyncio.CancelledError] | None = None) -> T:
    """Join actual work, retaining caller originals separately from its outcome."""
    observed: list[asyncio.CancelledError] = []
    owner = asyncio.current_task()
    while not task.done():
        cancelling_before = owner.cancelling() if owner is not None else 0
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as cancelled:
            # A self-cancelled child is retrieved through its actual result below.
            if not task.cancelled() or (owner is not None and owner.cancelling() > cancelling_before):
                observed.append(cancelled)
        except BaseException:
            break
    if cancellation_observations is not None:
        cancellation_observations.extend(observed)
    try:
        result = task.result()
    except BaseException as child_failure:
        if observed:
            combined = _combine_operation_failures(child_failure, tuple(observed))
            raise combined from combined.__cause__
        raise
    # Admission/reaper callers retain their existing successful-result handoff.
    # Live-lease callers explicitly retain originals through terminal proof.
    return result


async def _owned[T](awaitable: Awaitable[T], *, cancellation_observations: list[asyncio.CancelledError] | None = None) -> T:
    return await _join_owned(asyncio.ensure_future(awaitable), cancellation_observations=cancellation_observations)


@dataclass(slots=True)
class _OperationLossWatchOwner:
    entered: asyncio.Event
    task: asyncio.Task[BaseException] | None = None
    conflicting_tasks: list[asyncio.Task[BaseException]] = field(default_factory=list)

    def bind_returned_task(self, task: asyncio.Task[BaseException]) -> None:
        if type(task) is not asyncio.Task:
            raise AuditIntegrityError("Loss watcher allocation returned a foreign owner")
        if self.task is not None and self.task is not task:
            if all(task is not original for original in self.conflicting_tasks):
                self.conflicting_tasks.append(task)
            raise AuditIntegrityError("Loss watcher allocation replaced its actual producer")
        self.task = task

    def observe_producer_entry(self) -> None:
        task = asyncio.current_task()
        if type(task) is not asyncio.Task:
            raise AuditIntegrityError("Loss watcher producer lacks its actual Task")
        self.bind_returned_task(task)
        self.entered.set()


@dataclass(slots=True)
class _OperationWatchObservation:
    failure: BaseException | None = None
    reader: ComposerOperationWatchReader | None = None
    child_loop_cancellations: list[asyncio.CancelledError] | None = None
    child_owner: _OperationLossWatchOwner | None = None


async def _finish_operation_watcher[T](
    watcher: asyncio.Task[T],
    observation: _OperationWatchObservation,
    *,
    caller_reader: ComposerOperationWatchReader | None = None,
) -> tuple[BaseException, ...]:
    """Join the exact watcher before settlement, preserving caller originals."""
    cleanup_marker = object()
    errors: list[BaseException] = []
    child_owner = observation.child_owner
    if child_owner is not None:
        if child_owner.task is not watcher:
            raise AuditIntegrityError("Loss watcher cleanup replaced its actual producer")
        # Publication occurs at actual producer entry immediately before the
        # canonical Event-wait call. A foreign pre-entry cancellation instead
        # makes this exact Task done and remains its business result below.
        while not child_owner.entered.is_set() and not watcher.done():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError as cancellation:
                if caller_reader is not None:
                    caller_reader.retain_join_loop_delivery(cancellation)
                    if caller_reader.owns_internal_cleanup_observation(cancellation):
                        continue
                if all(cancellation is not earlier for earlier in errors):
                    errors.append(cancellation)
    if not watcher.done() and observation.failure is None:
        if observation.reader is not None:
            observation.reader.request_internal_cleanup(cleanup_marker)
        watcher.cancel(cleanup_marker)
    while not watcher.done():
        try:
            await asyncio.sleep(0.01)
        except asyncio.CancelledError as cancellation:
            # This catch observes cancellation delivered to the joining caller,
            # never a loss-child result. Transfer only that exact provenance.
            if caller_reader is not None:
                caller_reader.retain_join_loop_delivery(cancellation)
                if caller_reader.owns_internal_cleanup_observation(cancellation):
                    continue
            if all(cancellation is not earlier for earlier in errors):
                errors.append(cancellation)
    try:
        result = watcher.result()
        if isinstance(result, BaseException):
            errors.append(result)
    except asyncio.CancelledError as cancellation:
        if observation.reader is not None:
            known_private_close = observation.reader.owns_internal_cleanup_observation(cancellation)
        elif observation.child_loop_cancellations is not None:
            known_private_close = (
                any(cancellation is original for original in observation.child_loop_cancellations)
                and len(cancellation.args) == 1
                and cancellation.args[0] is cleanup_marker
            )
        else:
            # Preserve the existing generic helper for unselected callers.
            known_private_close = len(cancellation.args) == 1 and cancellation.args[0] is cleanup_marker
        if not known_private_close:
            errors.append(cancellation)
    except BaseException as failure:
        errors.append(failure)
    if observation.failure is not None and all(observation.failure is not earlier for earlier in errors):
        errors.append(observation.failure)
    if child_owner is not None:
        # A malformed allocation return can disagree with the producer's
        # actual Task. Retain both actual owners and observe the other one
        # without privately canceling work that was never our loss producer.
        for conflicting_task in child_owner.conflicting_tasks:
            while not conflicting_task.done():
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError as cancellation:
                    if caller_reader is not None:
                        caller_reader.retain_join_loop_delivery(cancellation)
                        if caller_reader.owns_internal_cleanup_observation(cancellation):
                            continue
                    if all(cancellation is not earlier for earlier in errors):
                        errors.append(cancellation)
            try:
                conflicting_task.result()
            except BaseException as conflict_original:
                if all(conflict_original is not earlier for earlier in errors):
                    errors.append(conflict_original)
    return tuple(errors)


def _combine_operation_failures(original: BaseException, additional: tuple[BaseException, ...]) -> BaseException:
    errors: list[BaseException] = [original]
    for error in additional:
        if all(error is not earlier for earlier in errors):
            errors.append(error)
    return original if len(errors) == 1 else BaseExceptionGroup("Composer operation retained original outcomes", errors)


class ComposerAsyncWorker:
    def __init__(
        self,
        *,
        app: Starlette,
        authority: ComposerAsyncOperationAuthority,
        concurrency: int,
        scan_interval_seconds: float,
        claim_lease_seconds: int,
        drain_seconds: float,
        owner_instance_id: str,
        process_recovery: ProcessRecovery,
        instance_draining: threading.Event,
    ) -> None:
        if not 1 <= concurrency <= 16 or scan_interval_seconds <= 0 or drain_seconds <= 0:
            raise ValueError("Invalid composer worker resource limits")
        self._app = app
        self._authority = authority
        self._concurrency = concurrency
        self._scan_interval = scan_interval_seconds
        self._claim_lease_seconds = claim_lease_seconds
        self._drain_seconds = drain_seconds
        self._owner_instance_id = owner_instance_id
        self._process_recovery = process_recovery
        self._instance_draining = instance_draining
        self._required_coordinators: dict[tuple[UUID, str], RequiredWorkCoordinator] = {}
        self._recovery_coordinators: dict[tuple[UUID, str], list[RequiredWorkCoordinator]] = {}
        self._adoption_recovery_pending: set[tuple[UUID, str]] = set()
        self._adoption_recovery_cancellations: dict[tuple[UUID, str], list[asyncio.CancelledError]] = {}
        self._stopping = False
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._jobs: dict[tuple[UUID, str], asyncio.Task[_AdoptionSettlementHandoff | None]] = {}
        self._local_cancels: dict[tuple[UUID, str], asyncio.Event] = {}

    def notify_work(self) -> None:
        self._wake.set()

    def start(self) -> None:
        if self._loop_task is not None or self._stopping:
            raise RuntimeError("Composer worker cannot be started twice")
        self._loop_task = asyncio.create_task(self._loop(), name="composer-async-worker")
        self._loop_task.add_done_callback(self._loop_finished)

    def _loop_finished(self, task: asyncio.Task[None]) -> None:
        if self._stopping:
            return
        # Consume the exception without logging provider/request content.
        failure = None if task.cancelled() else task.exception()
        slog.error("composer_operation.worker_loop_lost", exc_class=type(failure).__name__)
        self._process_recovery.request_shutdown()

    async def _loop(self) -> None:
        while not self._stopping and not self._instance_draining.is_set():
            self._wake.clear()
            await self.reap_once()
            await self._claim_once()
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._scan_interval)

    async def _claim_once(self) -> int:
        capacity = self._concurrency - len(self._jobs)
        if capacity <= 0 or self._stopping or self._instance_draining.is_set():
            return 0
        claims = await _owned(run_sync_in_worker(self._authority.claim_next, limit=capacity))
        if self._stopping or self._instance_draining.is_set():
            for claim in claims:
                await _owned(run_sync_in_worker(self._authority.release_claim, claim))
            return 0
        for claim in claims:
            key = (claim.session_id, claim.operation_id)
            if key in self._jobs:
                raise AuditIntegrityError("Composer claim duplicates locally owned work")
            self._local_cancels[key] = asyncio.Event()
            task = asyncio.create_task(self._job(claim), name="composer-operation")
            self._jobs[key] = task
            task.add_done_callback(partial(self._job_finished, key))
        return len(claims)

    def _job_finished(self, key: tuple[UUID, str], task: asyncio.Task[_AdoptionSettlementHandoff | None]) -> None:
        del self._jobs[key]
        del self._local_cancels[key]
        failure = None if task.cancelled() else task.exception()
        result = task.result() if failure is None and not task.cancelled() else None
        coordinator = self._required_coordinators.get(key)
        recovery = self._recovery_coordinators.get(key)
        if key in self._adoption_recovery_pending:
            cancellations = self._adoption_recovery_cancellations.get(key)
            proved_handoff = (
                type(result) is _AdoptionSettlementHandoff
                and type(result.terminal) is ComposerOperationRecord
                and result.terminal.session_id == key[0]
                and result.terminal.operation_id == key[1]
                and result.terminal.status in ("completed", "failed")
                and cancellations is not None
                and len(result.cancellations) == len(cancellations)
                and all(observed is retained for observed, retained in zip(result.cancellations, cancellations, strict=True))
            )
            if (
                proved_handoff
                and coordinator is not None
                and coordinator.all_completed
                and (recovery is None or all(item.all_completed for item in recovery))
            ):
                self._adoption_recovery_pending.remove(key)
                self._adoption_recovery_cancellations.pop(key, None)
            else:
                self._process_recovery.request_shutdown()
        elif not self._adoption_recovery_cancellations.get(key):
            # A successful adoption installed a temporary empty observation
            # list before its first await. It never entered recovery custody.
            self._adoption_recovery_cancellations.pop(key, None)
        else:
            self._process_recovery.request_shutdown()
        if key not in self._adoption_recovery_pending:
            if coordinator is not None and coordinator.all_completed:
                del self._required_coordinators[key]
            if recovery is not None and all(item.all_completed for item in recovery):
                del self._recovery_coordinators[key]
        if failure is not None:
            slog.error("composer_operation.owner_unsettled", exc_class=type(failure).__name__)
        self._wake.set()

    async def run_until_idle(self) -> None:
        """Drive ordinary claims inline for local fake-provider tests."""
        while not self._stopping:
            await self.reap_once()
            claimed = await self._claim_once()
            tasks = tuple(self._jobs.values())
            if not tasks and claimed == 0:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    def signal_local_cancel(self, *, session_id: UUID, operation_id: str) -> bool:
        event = self._local_cancels.get((session_id, operation_id))
        if event is None:
            return False
        event.set()
        return True

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        tasks = tuple(self._jobs.values())
        if self._loop_task is not None:
            tasks = (*tasks, self._loop_task)
        for task in tasks:
            task.cancel(COMPOSER_SHUTDOWN)
        if tasks:
            # asyncio.wait does not cancel unfinished children on timeout.
            await _owned(asyncio.wait(tasks, timeout=self._drain_seconds))
            # Remaining children retain task/fence/SQL custody. Their peer reaper
            # may settle only once the COMPOSE lease has actually lapsed.

    def assert_shutdown_complete(self) -> None:
        if not self._stopping or self._jobs or (self._loop_task is not None and not self._loop_task.done()):
            raise AuditIntegrityError("Composer worker still owns unfinished shutdown work")
        if self._loop_task is not None and not self._loop_task.cancelled():
            self._loop_task.exception()
        for coordinator in self._required_coordinators.values():
            coordinator.assert_completed()
        for recovery in self._recovery_coordinators.values():
            for coordinator in recovery:
                coordinator.assert_completed()

    async def _queued_outcome(self, claim: ComposerOperationClaim, record: ComposerOperationRecord) -> bool:
        current, now = await _owned(
            run_sync_in_worker(self._authority.get_with_database_now, session_id=claim.session_id, operation_id=claim.operation_id)
        )
        if current is None or current.status != "queued":
            return True
        failure = None
        if current.cancel_requested_at is not None:
            failure = request_cancelled_error(request_id=current.request_id)
        elif restore_utc(current.deadline_at) <= now:
            failure = deadline_expired_error(
                request_id=current.request_id,
                timeout_seconds=(restore_utc(current.deadline_at) - restore_utc(current.created_at)).total_seconds(),
            )
        if failure is not None:
            await _owned(
                run_sync_in_worker(
                    self._authority.settle_unstarted, claim, session_id=claim.session_id, operation_id=claim.operation_id, failure=failure
                )
            )
            return True
        return False

    async def _job(self, claim: ComposerOperationClaim) -> _AdoptionSettlementHandoff | None:
        services = composer_app_services(self._app)
        lock = await services.compose_locks.get_lock(str(claim.session_id))
        record = await _owned(
            run_sync_in_worker(self._authority.get_for_turn, session_id=claim.session_id, operation_id=claim.operation_id)
        )
        if record is None:
            raise AuditIntegrityError("Claimed composer operation disappeared")
        acquired = False
        running: ComposerOperationRunning | None = None
        started_handoff = False
        next_renewal_at = asyncio.get_running_loop().time() + self._claim_lease_seconds / 3
        try:
            # A claim is renewed while waiting; no provider work has started.
            while not acquired:
                if await self._queued_outcome(claim, record):
                    return None
                if self._stopping or self._instance_draining.is_set():
                    await _owned(run_sync_in_worker(self._authority.release_claim, claim))
                    return None
                try:
                    await asyncio.wait_for(lock.acquire(), timeout=min(0.25, self._claim_lease_seconds / 3))
                    acquired = True
                except TimeoutError:
                    if asyncio.get_running_loop().time() >= next_renewal_at:
                        await _owned(run_sync_in_worker(self._authority.renew_claim, claim))
                        next_renewal_at = asyncio.get_running_loop().time() + self._claim_lease_seconds / 3
            context = await _owned(
                run_sync_in_worker(
                    services.session_service.session_operation_authority.start_composer_async_operation,
                    claim,
                    owner_instance_id=self._owner_instance_id,
                    lease_seconds=services.session_service.session_operation_lease_seconds,
                    auth_provider_type=services.settings.auth_provider,
                )
            )
            running = ComposerOperationRunning(claim=claim, session_operation_context=context)
            coordinator = RequiredWorkCoordinator(
                RequiredWorkAuthority(RequiredAuthorityKind.DURABLE_COMPOSE, context, claim.operation_id, claim.attempt)
            )
            adoption_key = (claim.session_id, claim.operation_id)
            self._required_coordinators[adoption_key] = coordinator
            adoption_cancellations = self._adoption_recovery_cancellations.setdefault(adoption_key, [])
            lease = await SessionOperationLease.adopt(
                services.session_service.session_operation_authority,
                context,
                lease_seconds=services.session_service.session_operation_lease_seconds,
                required_work=coordinator,
                cancellation_observations=adoption_cancellations,
            )
            started_handoff = True
            await self._run_started(services, running, lease)
        except SessionOperationConflictError:
            await _owned(run_sync_in_worker(self._authority.release_claim, claim))
        except ComposerOperationPreconditionRefused as exc:
            await _owned(
                run_sync_in_worker(
                    self._authority.settle_unstarted, claim, session_id=claim.session_id, operation_id=claim.operation_id, failure=exc.error
                )
            )
        except ComposerOperationCancelledBeforeStart:
            await _owned(
                run_sync_in_worker(
                    self._authority.settle_unstarted,
                    claim,
                    session_id=claim.session_id,
                    operation_id=claim.operation_id,
                    failure=request_cancelled_error(request_id=record.request_id),
                )
            )
        except asyncio.CancelledError as exc:
            if running is None:
                await _owned(run_sync_in_worker(self._authority.release_claim, claim))
            elif not started_handoff:
                observations = self._begin_adoption_failure_custody(running, exc)
                terminal = await self._settle_failure(services, running, exc, adoption_failed=True, cancellation_observations=observations)
                return _AdoptionSettlementHandoff(terminal, tuple(observations))
            raise
        except BaseException as exc:
            if running is None:
                await _owned(
                    run_sync_in_worker(
                        self._authority.settle_unstarted,
                        claim,
                        session_id=claim.session_id,
                        operation_id=claim.operation_id,
                        failure=project_composer_operation_error(exc, request_id=record.request_id),
                        authoritative_failure=_authoritative_failure(exc),
                    )
                )
            elif not started_handoff:
                observations = self._begin_adoption_failure_custody(running, exc)
                terminal = await self._settle_failure(services, running, exc, adoption_failed=True, cancellation_observations=observations)
                return _AdoptionSettlementHandoff(terminal, tuple(observations))
            else:
                raise
        finally:
            if acquired:
                lock.release()
        return None

    async def _run_started(self, services: ComposerAppServices, running: ComposerOperationRunning, lease: SessionOperationLease) -> None:
        if type(lease) is not SessionOperationLease:
            raise AuditIntegrityError("Started composer operation requires an owned lease")
        settlement = _StartedSettlement()
        coordinator = lease.required_work
        if coordinator is not None and type(coordinator) is not RequiredWorkCoordinator:
            raise AuditIntegrityError("Started composer operation requires an owned coordinator")
        setup_ticket = coordinator.reserve(RequiredWorkSource.OWNED_TURN_SETUP_PRODUCER) if coordinator is not None else None
        try:
            async with lease:
                try:
                    terminal = await self._run_started_under_lease(services, running, lease, settlement, setup_ticket)
                    await self._verify_terminal_before_release(running, lease, terminal, settlement)
                except BaseException as exc:
                    if setup_ticket is not None and not setup_ticket.complete:
                        setup_ticket.complete_owned(exc)
                    if settlement.verified_terminal is not None:
                        raise
                    if _terminal_completion_unknown(exc):
                        raise
                    if settlement.terminal_receipt is not None:
                        try:
                            await self._verify_terminal_before_release(running, lease, settlement.terminal_receipt, settlement)
                        except BaseException as readback_failure:
                            combined = _combine_operation_failures(exc, (readback_failure,))
                            raise combined from combined.__cause__
                        raise
                    if _terminal_completion_unknown(exc) or settlement.failure_attempted:
                        raise
                    settlement.failure_attempted = True
                    try:
                        terminal = await self._settle_failure(
                            services, running, exc, cancellation_observations=settlement.deferred_cancellations
                        )
                    except BaseException as settlement_failure:
                        combined = _combine_operation_failures(exc, (settlement_failure,))
                        raise combined from combined.__cause__
                    settlement.terminal_receipt = terminal
                    try:
                        await self._verify_terminal_before_release(running, lease, terminal, settlement)
                    except BaseException as readback_failure:
                        combined = _combine_operation_failures(exc, (readback_failure,))
                        raise combined from combined.__cause__
                    raise
        except BaseException as exc:
            retained = _combine_operation_failures(exc, tuple(settlement.deferred_cancellations))
            if settlement.verified_terminal is not None:
                slog.warning("composer_operation.lease_close_failed_after_settle", exc_class=type(exc).__name__)
            # Never admit new terminal SQL after lease closure/sealing. A verified
            # terminal still wins publicly; the Task retains the exact late root.
            raise retained from retained.__cause__

    async def _verify_terminal_before_release(
        self,
        running: ComposerOperationRunning,
        lease: SessionOperationLease,
        terminal: object,
        settlement: _StartedSettlement,
    ) -> None:
        coordinator = lease.required_work
        if coordinator is None:
            raise AuditIntegrityError("Owned terminal verification has no required-work authority")
        failure_projections = [
            ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.TERMINAL_FAILURE_PROJECTION
        ]
        if type(terminal) is not ComposerOperationRecord:
            missing = AuditIntegrityError("Owned composer turn returned without a nominal terminal receipt")
            if failure_projections:
                raise ComposerTerminalSQLCompletionUnknown("Failed publication returned no terminal receipt") from missing
            # A setup producer's known missing return has not started a writer
            # read and remains eligible for its one live-lease failure write.
            raise missing
        sql, projection = coordinator.reserve_pair(
            RequiredWorkSource.TERMINAL_WRITER_READ_SQL,
            RequiredWorkSource.TERMINAL_WRITER_READ_PROJECTION,
            transition_ordinal=0,
            semantic_ordinal=1,
        )
        try:
            outcome = await run_required_sql_finish_once(
                sql,
                self._authority.get,
                session_id=running.claim.session_id,
                operation_id=running.claim.operation_id,
            )
        except BaseException as read_failure:
            raise ComposerTerminalSQLCompletionUnknown("Terminal writer read has no actual completed outcome") from read_failure
        try:
            coordinator.validate_joined_lifecycle_sql_outcome(
                ticket=sql,
                expected_source=RequiredWorkSource.TERMINAL_WRITER_READ_SQL,
                actual_outcome=outcome,
            )
        except (AuditIntegrityError, RequiredWorkIncomplete) as invalid_receipt:
            unknown = ComposerTerminalSQLCompletionUnknown("Terminal writer read has no verified physical receipt")
            unknown.__cause__ = invalid_receipt
            receipt_cancellations = (
                outcome.deferred_cancellations if (type(outcome) is RequiredSQLReturned or type(outcome) is RequiredSQLRaised) else ()
            )
            escaping = _combine_operation_failures(unknown, receipt_cancellations)
            raise escaping from escaping.__cause__
        errors: list[BaseException] = []
        if isinstance(outcome, RequiredSQLRaised):
            errors.append(outcome.error)
        else:
            projection.begin_projection()
            try:
                committed = outcome.value
                if type(committed) is not ComposerOperationRecord:
                    raise AuditIntegrityError("Terminal writer read returned a nonnominal row")
                try:
                    terminal = replace(terminal)
                    committed = replace(committed)
                except (TypeError, ValueError) as invalid_terminal:
                    raise AuditIntegrityError("Owned terminal receipt failed strict DTO validation") from invalid_terminal
                fence = running.session_operation_context.fence
                if (
                    terminal.session_id != running.claim.session_id
                    or terminal.operation_id != running.claim.operation_id
                    or terminal.attempt != running.claim.attempt
                    or terminal.session_operation_id != fence.operation_id
                    or terminal.session_operation_epoch != fence.operation_epoch
                    or terminal.status not in ("completed", "failed")
                    or committed.session_id != running.claim.session_id
                    or committed.operation_id != running.claim.operation_id
                    or committed.attempt != running.claim.attempt
                    or committed.session_operation_id != fence.operation_id
                    or committed.session_operation_epoch != fence.operation_epoch
                    or committed.status != terminal.status
                    or committed.settled_at != terminal.settled_at
                    or committed.failure_code != terminal.failure_code
                    or committed.settled_by != terminal.settled_by
                    or committed.result_schema != terminal.result_schema
                    or committed.result_json != terminal.result_json
                    or committed.result_sha256 != terminal.result_sha256
                    or committed != terminal
                ):
                    raise AuditIntegrityError("Terminal receipt changed the exact running authority or immutable result")
                if len(failure_projections) > 1:
                    raise AuditIntegrityError("Failed terminal has more than one sealed projection owner")
                if failure_projections:
                    coordinator.validate_terminal_failure_work(projection_ticket=failure_projections[0])
            except BaseException as invalid_terminal:
                errors.append(invalid_terminal)
            else:
                projection.complete_owned()
                if failure_projections:
                    failure_projections[0].begin_projection()
                    failure_projections[0].complete_owned()
                settlement.verified_terminal = committed
        if errors:
            unknown = ComposerTerminalSQLCompletionUnknown("Terminal writer projection has no authoritative terminal evidence")
            unknown.__cause__ = errors[0]
            escaping = _combine_operation_failures(
                unknown, tuple(errors[1:]) + tuple(settlement.deferred_cancellations) + outcome.deferred_cancellations
            )
            # Neither a real SQL failure nor a completed invalid projection
            # proves a terminal; source36 and any38 remain pending.
            raise escaping from escaping.__cause__
        cancellations = list(settlement.deferred_cancellations)
        for cancelled in outcome.deferred_cancellations:
            if all(cancelled is not earlier for earlier in cancellations):
                cancellations.append(cancelled)
        if len(cancellations) == 1:
            raise cancellations[0]
        if cancellations:
            raise BaseExceptionGroup("Terminal readback retained original cancellations", cancellations)

    async def _run_started_under_lease(
        self,
        services: ComposerAppServices,
        running: ComposerOperationRunning,
        lease: SessionOperationLease,
        settlement: _StartedSettlement,
        setup_ticket: RequiredWorkTicket | None,
    ) -> ComposerOperationRecord:
        if type(lease) is not SessionOperationLease:
            raise AuditIntegrityError("Started composer operation requires an owned lease")
        coordinator = lease.required_work
        if coordinator is not None and type(coordinator) is not RequiredWorkCoordinator:
            raise AuditIntegrityError("Started composer operation requires an owned coordinator")
        record = await _owned(
            run_sync_in_worker(self._authority.get_for_turn, session_id=running.claim.session_id, operation_id=running.claim.operation_id),
            cancellation_observations=settlement.deferred_cancellations,
        )
        current, now = await _owned(
            run_sync_in_worker(
                self._authority.get_with_database_now, session_id=running.claim.session_id, operation_id=running.claim.operation_id
            ),
            cancellation_observations=settlement.deferred_cancellations,
        )
        if settlement.deferred_cancellations:
            if len(settlement.deferred_cancellations) == 1:
                raise settlement.deferred_cancellations[0]
            raise BaseExceptionGroup("Composer setup retained original cancellations", settlement.deferred_cancellations)
        if record is None or current is None or record.request_json is None:
            raise AuditIntegrityError("Started composer operation has no admitted request")
        budget = max(0.0, (restore_utc(record.deadline_at) - now).total_seconds())
        if budget <= 0:
            if setup_ticket is not None:
                setup_ticket.complete_owned()
            settlement.failure_attempted = True
            return await self._settle_failure(
                services,
                running,
                ComposerTurnDeadlineExpired(
                    session_id=record.session_id,
                    operation_id=record.operation_id,
                    remaining_seconds=budget,
                    budget_seconds_at_running=budget,
                ),
                cancellation_observations=settlement.deferred_cancellations,
            )
        anchor = ComposerBudgetAnchor(remaining_at_running_seconds=budget, monotonic_at_running=time.monotonic())
        request = (SendMessageRequest if record.kind == "compose_message" else RecomposeRequest).model_validate_json(record.request_json)
        turn = ComposerTurnInput(
            session_id=record.session_id,
            operation_id=record.operation_id,
            kind=record.kind,
            actor_user_id=record.actor_user_id,
            request=request,
            request_id=record.request_id,
            budget_seconds=budget,
        )
        if setup_ticket is not None:
            setup_ticket.complete_owned()
        observation = ComposerTurnObservation(required_work=coordinator)
        owner = asyncio.current_task()
        if owner is None:
            raise RuntimeError("Composer worker requires a task owner")
        watcher: asyncio.Task[None] | None = None
        watch_observation = _OperationWatchObservation()
        failure: BaseException | None = None
        unhandled_primary: BaseException | None = None
        try:
            async with composer_request_lifecycle(
                services.progress_registry, session_id=str(record.session_id), user_id=record.actor_user_id, owner_task=owner
            ) as lifecycle:
                watcher = asyncio.create_task(
                    self._watch(running, lease, owner, anchor, watch_observation), name="composer-operation-watch"
                )
                turn_ticket = (
                    coordinator.reserve(RequiredWorkSource.REQUIRED_CONTINUATION_PRODUCER, semantic_ordinal=1)
                    if coordinator is not None
                    else None
                )
                timeout_scope: asyncio.Timeout | None = None
                try:
                    if self._stopping or self._instance_draining.is_set():
                        raise asyncio.CancelledError(COMPOSER_SHUTDOWN)
                    async with asyncio.timeout(budget) as timeout_scope:
                        if coordinator is None:
                            raise AuditIntegrityError("Detached composer turn has no owned required-work coordinator")
                        terminal = await run_composer_turn(
                            services,
                            turn,
                            lease=lease,
                            running=running,
                            request_lifecycle=lifecycle,
                            observation=observation,
                            budget_anchor=anchor,
                        )
                    if turn_ticket is not None:
                        turn_ticket.complete_owned()
                except BaseException as exc:
                    if turn_ticket is not None:
                        turn_ticket.complete_owned(exc)
                    if _terminal_completion_unknown(exc):
                        raise
                    failure = _owned_deadline_failure(exc, timeout_scope=timeout_scope, record=record, anchor=anchor)
                    if watcher is not None:
                        watcher_failures = await _finish_operation_watcher(watcher, watch_observation)
                        watcher = None
                        failure = _combine_operation_failures(failure, watcher_failures)
                    renewal_failures = await lease.observe_current_renewal_attempt()
                    failure = _combine_operation_failures(failure, renewal_failures)
                    if lease.renewal_error is not None:
                        failure = _combine_operation_failures(failure, (lease.renewal_error,))
                    try:
                        await _owned(
                            persist_cancelled_turn_audit(services, running, observation),
                            cancellation_observations=settlement.deferred_cancellations,
                        )
                    except BaseException as audit_failure:
                        failure = _combine_operation_failures(failure, (audit_failure,))
                    settlement.failure_attempted = True
                    try:
                        terminal = await self._settle_failure(
                            services, running, failure, cancellation_observations=settlement.deferred_cancellations
                        )
                    except BaseException as settlement_failure:
                        combined = _combine_operation_failures(failure, (settlement_failure,))
                        raise combined from combined.__cause__
                    settlement.terminal_receipt = terminal
                    lifecycle.durable_completed = terminal.status == "completed"
                    lifecycle.durable_terminal_status = (
                        "completed"
                        if terminal.status == "completed"
                        else "cancelled"
                        if terminal.failure_code == "request_cancelled"
                        else "timed_out"
                        if terminal.failure_code == "deadline_expired"
                        else "failed"
                    )
                    if terminal.status == "completed":
                        raise failure from failure.__cause__
        except BaseException as primary:
            unhandled_primary = primary
            raise
        finally:
            if watcher is not None:
                watcher_failures = await _finish_operation_watcher(watcher, watch_observation)
                if watcher_failures:
                    if unhandled_primary is not None:
                        raise _combine_operation_failures(unhandled_primary, watcher_failures)
                    if len(watcher_failures) == 1:
                        raise watcher_failures[0]
                    raise BaseExceptionGroup("Composer watcher cleanup retained original outcomes", watcher_failures)

        return terminal

    async def _watch(
        self,
        running: ComposerOperationRunning,
        lease: SessionOperationLease,
        owner: asyncio.Task[object],
        anchor: ComposerBudgetAnchor,
        observation: _OperationWatchObservation,
    ) -> None:
        reader = ComposerOperationWatchReader.for_operation(self, running, lease, anchor)
        observation.reader = reader
        loss_owner = _OperationLossWatchOwner(asyncio.Event())
        lost_observation = _OperationWatchObservation(child_loop_cancellations=[], child_owner=loss_owner)

        async def owned_loss_watch() -> BaseException:
            loss_owner.observe_producer_entry()
            return await SessionOperationLease.wait_until_lost(lease, cancellation_observations=lost_observation.child_loop_cancellations)

        loss_coroutine = owned_loss_watch()
        unhandled_primary: BaseException | None = None
        try:
            allocation_failed = False
            try:
                returned_loss = asyncio.create_task(loss_coroutine, name="composer-operation-loss-watch")
                loss_owner.bind_returned_task(returned_loss)
            except BaseException as allocation_original:
                allocation_failed = True
                observation.failure = allocation_original
                # Allocation may have scheduled the actual coroutine before
                # raising. Its entry binds that exact Task; absence is Unknown,
                # never proof of no allocation or permission to retry/close it.
                try:
                    self._process_recovery.request_shutdown()
                except BaseException as recovery_original:
                    observation.failure = _combine_operation_failures(allocation_original, (recovery_original,))
                while loss_owner.task is None:
                    try:
                        await asyncio.sleep(0.01)
                    except asyncio.CancelledError as cancellation:
                        reader.retain_join_loop_delivery(cancellation)
                        if not reader.owns_internal_cleanup_observation(cancellation):
                            observation.failure = _combine_operation_failures(observation.failure, (cancellation,))
            if allocation_failed:
                failure = observation.failure
                if failure is None:
                    raise AuditIntegrityError("Loss watcher allocation failure lost its original")
                raise failure
            lost = loss_owner.task
            if lost is None:
                raise AuditIntegrityError("Loss watcher allocation lacks its actual Task")
            while True:
                if lost.done():
                    observation.failure = lost.result()
                    raise observation.failure
                current, now = await reader.read()
                if current is None:
                    owner.cancel(COMPOSER_LEASE_LOST)
                    return
                if current.status in ("completed", "failed"):
                    return
                if current.cancel_requested_at is not None:
                    owner.cancel(COMPOSER_CANCEL_REQUESTED)
                    return
                if restore_utc(current.deadline_at) <= now or anchor.remaining_seconds(monotonic_now=time.monotonic()) <= 0:
                    owner.cancel(COMPOSER_DEADLINE)
                    return
                if self._stopping or self._instance_draining.is_set():
                    owner.cancel(COMPOSER_SHUTDOWN)
                    return
                await reader.wait_watch_cadence()
        except asyncio.CancelledError as cancellation:
            unhandled_primary = cancellation
            if reader.owns_internal_cleanup_observation(cancellation):
                # Private close is cleanup bookkeeping. Join the loss child in
                # finally, then return normally rather than store a canceled
                # Task whose later result() can synthesize a different object.
                return
            observation.failure = cancellation
            if cancellation.args and cancellation.args[0] in (COMPOSER_LEASE_LOST, COMPOSER_SHUTDOWN):
                owner.cancel(cancellation.args[0])
            else:
                owner.cancel(COMPOSER_LEASE_LOST)
            raise
        except BaseException as failure:
            unhandled_primary = failure
            observation.failure = failure
            # A failed watch cannot leave work running without authority
            # observation. The joined watcher error keeps nominal priority.
            owner.cancel(COMPOSER_LEASE_LOST)
            raise
        finally:
            # A normal return, failed pre-entry Task or hidden allocation all
            # join the same producer. Unknown allocation stays in its owner
            # wait above under recovery and cannot reach this release path.
            actual_loss = loss_owner.task
            if actual_loss is None:
                raise AuditIntegrityError("Loss watcher cleanup lacks its actual Task")
            lost_failures = await _finish_operation_watcher(actual_loss, lost_observation, caller_reader=reader)
            if lost_failures:
                primary_is_private_close = isinstance(
                    unhandled_primary, asyncio.CancelledError
                ) and reader.owns_internal_cleanup_observation(unhandled_primary)
                if unhandled_primary is not None and not primary_is_private_close:
                    raise _combine_operation_failures(unhandled_primary, lost_failures)
                if len(lost_failures) == 1:
                    raise lost_failures[0]
                raise BaseExceptionGroup("Composer loss watch retained original outcomes", lost_failures)

    async def _settle_failure(
        self,
        services: ComposerAppServices,
        running: ComposerOperationRunning,
        exc: BaseException,
        *,
        cancellation_observations: list[asyncio.CancelledError] | None = None,
        adoption_failed: bool = False,
    ) -> ComposerOperationRecord:
        coordinator = self._required_coordinators.get((running.claim.session_id, running.claim.operation_id))
        try:
            current, now = await _owned(
                run_sync_in_worker(
                    self._authority.get_with_database_now,
                    session_id=running.claim.session_id,
                    operation_id=running.claim.operation_id,
                ),
                cancellation_observations=cancellation_observations,
            )
            if type(current) is not ComposerOperationRecord:
                raise AuditIntegrityError("Failed composer operation has no nominal authoritative row")
            if current.session_id != running.claim.session_id or current.operation_id != running.claim.operation_id:
                raise AuditIntegrityError("Failed composer operation changed its exact job binding")
            if current.status in ("completed", "failed"):
                return current
            failure = self._failure_for(current, now, exc)
        except BaseException as selection_failure:
            if coordinator is not None:
                try:
                    self._reserve_failed_terminal_projection(coordinator)
                except BaseException as barrier_failure:
                    original = _combine_operation_failures(selection_failure, (barrier_failure,))
                    raise ComposerTerminalSQLCompletionUnknown(
                        "Failed publication selection could not retain its exact handoff"
                    ) from original
                raise ComposerTerminalSQLCompletionUnknown(
                    "Failed publication selection has no authoritative terminal evidence"
                ) from selection_failure
            raise
        failure_projection = self._reserve_failed_terminal_projection(coordinator) if coordinator is not None else None
        if failure.diagnostic_id is not None:
            slog.error("composer_operation.failure", diagnostic_id=failure.diagnostic_id, exc_class=type(exc).__name__)
        try:
            terminal = await _owned(
                services.session_service.fail_composer_async_operation(
                    running,
                    failure=failure,
                    authoritative_failure=_authoritative_failure(exc),
                    required_work=coordinator,
                    failure_projection_work=failure_projection,
                ),
                cancellation_observations=cancellation_observations,
            )
        except (ComposerOperationFenceLost, SessionOperationFenceLost) as fence_failure:
            if coordinator is not None:
                if not adoption_failed:
                    raise ComposerTerminalSQLCompletionUnknown(
                        "Owned failed publication requires separate fresh-authority recovery"
                    ) from fence_failure
                if failure_projection is None:
                    raise AuditIntegrityError("Owned failed publication lost its projection handoff") from fence_failure
                recovery_key = (current.session_id, current.operation_id)
                self._adoption_recovery_pending.add(recovery_key)
                self._adoption_recovery_cancellations.setdefault(recovery_key, [])
                terminal = await self._recover_failed_adoption(services, running, current, coordinator, failure_projection, exc)
            else:
                try:
                    terminal = await _owned(
                        run_sync_in_worker(
                            self._authority.settle_own_lapsed,
                            session_id=current.session_id,
                            operation_id=current.operation_id,
                            owner_instance_id=self._owner_instance_id,
                            failure=failure,
                            authoritative_failure=_authoritative_failure(exc),
                        )
                    )
                except ComposerOperationFenceLost:
                    # A proved pre-write refusal can follow joined adoption
                    # cleanup's exact release. Recover only under a fresh COMPOSE
                    # fence and the existing newer-epoch lost-operation CAS.
                    recovery = await SessionOperationLease.acquire(
                        services.session_service.session_operation_authority,
                        session_id=current.session_id,
                        operation_kind=SessionOperationKind.COMPOSE,
                        owner_instance_id=self._owner_instance_id,
                        lease_seconds=services.session_service.session_operation_lease_seconds,
                    )
                    try:
                        async with recovery:
                            terminal = await _owned(
                                run_sync_in_worker(
                                    self._authority.settle_lost,
                                    session_operation_context=recovery.context,
                                    session_id=current.session_id,
                                    operation_id=current.operation_id,
                                    failure=failure,
                                    authoritative_failure=_authoritative_failure(exc),
                                )
                            )
                    except BaseException:
                        committed = await _owned(
                            run_sync_in_worker(self._authority.get, session_id=current.session_id, operation_id=current.operation_id)
                        )
                        if committed is None or committed.status not in ("completed", "failed"):
                            raise
                        terminal = committed
        except BaseExceptionGroup as grouped_failure:
            if not adoption_failed or coordinator is None:
                raise
            grouped_refusal = self._grouped_prewrite_fence_refusal(grouped_failure)
            if grouped_refusal is None or failure_projection is None:
                raise
            if cancellation_observations is None:
                raise AuditIntegrityError("Adoption recovery lost its cancellation owner") from grouped_failure
            self._retain_recovery_cancellations(cancellation_observations, grouped_failure)
            recovery_key = (current.session_id, current.operation_id)
            self._adoption_recovery_pending.add(recovery_key)
            self._adoption_recovery_cancellations.setdefault(recovery_key, cancellation_observations)
            terminal = await self._recover_failed_adoption(services, running, current, coordinator, failure_projection, exc)
        slog.info(
            "composer_operation.settled",
            status=terminal.status,
            failure_code=terminal.failure_code,
            settled_by=terminal.settled_by,
            diagnostic_id=failure.diagnostic_id,
        )
        return terminal

    async def _recover_failed_adoption(
        self,
        services: ComposerAppServices,
        running: ComposerOperationRunning,
        current: ComposerOperationRecord,
        original_work: RequiredWorkCoordinator,
        original_projection: RequiredWorkTicket,
        original: BaseException,
    ) -> ComposerOperationRecord:
        key = (current.session_id, current.operation_id)
        cancellations = self._adoption_recovery_cancellations.setdefault(key, [])
        try:
            return await self._reconcile_failed_adoption(services, running, current, original_work, original_projection, original)
        except BaseException as escaping:
            self._retain_recovery_cancellations(cancellations, escaping)
            raise

    def _begin_adoption_failure_custody(self, running: ComposerOperationRunning, original: BaseException) -> list[asyncio.CancelledError]:
        key = (running.claim.session_id, running.claim.operation_id)
        self._adoption_recovery_pending.add(key)
        cancellations = self._adoption_recovery_cancellations.setdefault(key, [])
        self._retain_recovery_cancellations(cancellations, original)
        return cancellations

    @staticmethod
    def _grouped_prewrite_fence_refusal(
        grouped: BaseExceptionGroup,
    ) -> ComposerOperationFenceLost | SessionOperationFenceLost | None:
        pending: list[BaseException] = [grouped]
        fences: list[ComposerOperationFenceLost | SessionOperationFenceLost] = []
        while pending:
            original = pending.pop()
            if isinstance(original, BaseExceptionGroup):
                pending.extend(original.exceptions)
            elif isinstance(original, (ComposerOperationFenceLost, SessionOperationFenceLost)):
                fences.append(original)
            elif not isinstance(original, asyncio.CancelledError):
                return None
        return fences[0] if len(fences) == 1 else None

    @staticmethod
    def _retain_recovery_cancellations(cancellations: list[asyncio.CancelledError], failure: BaseException) -> None:
        pending = [failure]
        visited: set[int] = set()
        while pending:
            current = pending.pop()
            if id(current) in visited:
                continue
            visited.add(id(current))
            if isinstance(current, asyncio.CancelledError) and all(current is not earlier for earlier in cancellations):
                cancellations.append(current)
            if isinstance(current, BaseExceptionGroup):
                pending.extend(current.exceptions)
            if current.__cause__ is not None:
                pending.append(current.__cause__)

    async def _reconcile_failed_adoption(
        self,
        services: ComposerAppServices,
        running: ComposerOperationRunning,
        current: ComposerOperationRecord,
        original_work: RequiredWorkCoordinator,
        original_projection: RequiredWorkTicket,
        original: BaseException,
    ) -> ComposerOperationRecord:
        """Reconcile the actual writer, then recover only a proved running job."""
        original_work.validate_terminal_failure_work(projection_ticket=original_projection)
        if (
            original_work.authority.context != running.session_operation_context
            or original_work.authority.durable_operation_id != current.operation_id
            or original_work.authority.claim_attempt != running.claim.attempt
        ):
            raise AuditIntegrityError("Adoption recovery changed its original required-work authority")
        key = (current.session_id, current.operation_id)
        if self._required_coordinators.get(key) is not original_work:
            raise AuditIntegrityError("Adoption recovery lost its registered original owner")
        self._adoption_recovery_pending.add(key)
        cancellations = self._adoption_recovery_cancellations.setdefault(key, [])
        original_projection.begin_projection()
        deadline = asyncio.get_running_loop().time() + min(self._drain_seconds, 30.0)
        last_failure: BaseException | None = None
        for attempt in range(3):
            # A peer may have committed while joined adoption cleanup released
            # the old fence. Read the authoritative writer before another CAS.
            witness = original_work.reserve(RequiredWorkSource.TERMINAL_WRITER_READ_SQL, recurrence_ordinal=2 + attempt)
            observed = await _owned(
                run_required_sql_finish_once(
                    witness, self._authority.get_with_database_now, session_id=current.session_id, operation_id=current.operation_id
                ),
                cancellation_observations=cancellations,
            )
            original_work.validate_joined_lifecycle_sql_outcome(
                ticket=witness, expected_source=RequiredWorkSource.TERMINAL_WRITER_READ_SQL, actual_outcome=observed
            )
            cancellations.extend(observed.deferred_cancellations)
            if type(observed) is RequiredSQLRaised:
                last_failure = observed.error
            elif type(observed) is RequiredSQLReturned:
                if type(observed.value) is not tuple or len(observed.value) != 2:
                    raise AuditIntegrityError("Adoption recovery read changed its nominal operation result")
                seen, database_now = observed.value
                if type(seen) is not ComposerOperationRecord or type(database_now) is not datetime:
                    raise AuditIntegrityError("Adoption recovery read changed its nominal operation record")
                if seen.session_id != current.session_id or seen.operation_id != current.operation_id:
                    raise AuditIntegrityError("Adoption recovery read crossed its exact job")
                if seen.status in ("completed", "failed"):
                    self._complete_reconciled_projection(original_projection, cancellations)
                    original_work.assert_completed()
                    return seen
                if seen.status != "running":
                    raise AuditIntegrityError("Adoption recovery found an unsupported operation state")
                receipts = original_work.recovery_failure_receipts(projection_ticket=original_projection)
                reduction = reduce_composer_failures(receipts) if receipts else None
                selected_failure = self._failure_for(seen, database_now, original, recovery_receipts=receipts)
                selected_authoritative = _authoritative_failure(original) or (reduction is not None and reduction.category_rank <= 51)
                try:
                    recovery = await _owned(
                        SessionOperationLease.acquire(
                            services.session_service.session_operation_authority,
                            session_id=current.session_id,
                            operation_kind=SessionOperationKind.COMPOSE,
                            owner_instance_id=self._owner_instance_id,
                            lease_seconds=services.session_service.session_operation_lease_seconds,
                        ),
                        cancellation_observations=cancellations,
                    )
                except (SessionOperationConflictError, asyncio.CancelledError) as acquisition_failure:
                    last_failure = acquisition_failure
                    if isinstance(acquisition_failure, asyncio.CancelledError):
                        self._retain_recovery_cancellations(cancellations, acquisition_failure)
                else:
                    body_failure: BaseException | None = None
                    try:
                        if recovery.context.fence.operation_epoch <= running.session_operation_context.fence.operation_epoch:
                            raise AuditIntegrityError("Adoption recovery did not acquire a newer fence")
                        recovered_work = RequiredWorkCoordinator(
                            RequiredWorkAuthority(
                                RequiredAuthorityKind.DURABLE_COMPOSE, recovery.context, current.operation_id, running.claim.attempt
                            )
                        )
                        recovery.bind_required_work(recovered_work)
                        self._recovery_coordinators.setdefault(key, []).append(recovered_work)
                        recovered_projection = self._reserve_failed_terminal_projection(recovered_work)
                        recovered_projection.begin_projection()
                        sql_ticket = recovered_work.reserve(RequiredWorkSource.TERMINAL_FAILURE_SQL)
                        sql_outcome = await _owned(
                            run_required_sql_finish_once(
                                sql_ticket,
                                self._authority.settle_lost,
                                session_operation_context=recovery.context,
                                session_id=current.session_id,
                                operation_id=current.operation_id,
                                failure=selected_failure,
                                authoritative_failure=selected_authoritative,
                            ),
                            cancellation_observations=cancellations,
                        )
                        recovered_work.validate_recovery_terminal_sql_outcome(ticket=sql_ticket, actual_outcome=sql_outcome)
                        cancellations.extend(sql_outcome.deferred_cancellations)
                        read_ticket = recovered_work.reserve(RequiredWorkSource.TERMINAL_WRITER_READ_SQL)
                        read_outcome = await _owned(
                            run_required_sql_finish_once(
                                read_ticket, self._authority.get, session_id=current.session_id, operation_id=current.operation_id
                            ),
                            cancellation_observations=cancellations,
                        )
                        recovered_work.validate_joined_lifecycle_sql_outcome(
                            ticket=read_ticket, expected_source=RequiredWorkSource.TERMINAL_WRITER_READ_SQL, actual_outcome=read_outcome
                        )
                        cancellations.extend(read_outcome.deferred_cancellations)
                        if type(read_outcome) is RequiredSQLRaised:
                            raise ComposerTerminalSQLCompletionUnknown("Adoption recovery readback failed") from read_outcome.error
                        if type(read_outcome) is RequiredSQLReturned:
                            terminal = read_outcome.value
                        else:
                            raise AuditIntegrityError("Adoption recovery readback returned an unsupported nominal SQL outcome")
                        if type(terminal) is not ComposerOperationRecord:
                            raise AuditIntegrityError("Adoption recovery readback changed its nominal record")
                        if terminal.session_id != current.session_id or terminal.operation_id != current.operation_id:
                            raise AuditIntegrityError("Adoption recovery readback crossed its exact job")
                        if type(sql_outcome) is RequiredSQLReturned and sql_outcome.value != terminal:
                            raise AuditIntegrityError("Adoption recovery SQL result disagreed with its independent readback")
                        if terminal.status in ("completed", "failed"):
                            secondary = (sql_outcome.error,) if type(sql_outcome) is RequiredSQLRaised else ()
                            self._complete_reconciled_projection(recovered_projection, (*secondary, *cancellations))
                            self._complete_reconciled_projection(original_projection, cancellations)
                            original_work.assert_completed()
                            return terminal
                        if terminal.status != "running" or type(sql_outcome) is not RequiredSQLRaised:
                            raise ComposerTerminalSQLCompletionUnknown("Adoption recovery has no proved terminal or retry state")
                        # The attempted bundle is sealed. A running readback
                        # does not complete publication or authorize release of
                        # this fresh fence through a replacement coordinator.
                        raise ComposerTerminalSQLCompletionUnknown("Adoption recovery has no committed terminal") from sql_outcome.error
                    except BaseException as failure_during_recovery:
                        body_failure = failure_during_recovery
                        raise
                    finally:
                        try:
                            # This outer Task owns the physical close. Its
                            # shielded join observes every caller cancellation
                            # without cancelling the inner lifecycle join.
                            await _owned(recovery.close(), cancellation_observations=cancellations)
                        except BaseException as close_failure:
                            if body_failure is not None and close_failure is not body_failure:
                                raise _combine_operation_failures(body_failure, (close_failure,)) from None
                            raise
            else:
                raise AuditIntegrityError("Adoption recovery read returned an unsupported nominal SQL outcome")
            if attempt < 2 and asyncio.get_running_loop().time() < deadline:
                await _owned(asyncio.sleep(min(self._scan_interval, 0.05)), cancellation_observations=cancellations)
        raise ComposerTerminalSQLCompletionUnknown("Adoption recovery exhausted its bounded writer reconciliation") from last_failure

    @staticmethod
    def _complete_reconciled_projection(ticket: RequiredWorkTicket, errors: Iterable[BaseException]) -> None:
        unique: list[BaseException] = []
        for error in errors:
            if all(error is not earlier for earlier in unique):
                unique.append(error)
        if not unique:
            ticket.complete_owned()
        elif len(unique) == 1:
            ticket.complete_owned(unique[0])
        else:
            ticket.complete_owned(BaseExceptionGroup("Adoption recovery retained original outcomes", unique))

    @staticmethod
    def _reserve_failed_terminal_projection(coordinator: RequiredWorkCoordinator) -> RequiredWorkTicket:
        if type(coordinator) is not RequiredWorkCoordinator:
            raise AuditIntegrityError("Failed terminal projection requires an owned coordinator")
        existing = [ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.TERMINAL_FAILURE_PROJECTION]
        if len(existing) > 1:
            raise AuditIntegrityError("Failed publication has more than one projection handoff")
        if existing:
            projection = existing[0]
        else:
            projection = coordinator.reserve(
                RequiredWorkSource.TERMINAL_FAILURE_PROJECTION,
                transition_ordinal=0,
                semantic_ordinal=0,
                recurrence_ordinal=0,
            )
        coordinator.validate_terminal_failure_work(projection_ticket=projection)
        return projection

    def _failure_for(
        self,
        current: ComposerOperationRecord,
        now: datetime,
        exc: BaseException,
        *,
        recovery_receipts: tuple[ComposerFailureReceipt, ...] | None = None,
    ) -> ComposerOperationError:
        leaves = _failure_leaves(exc)
        coordinator = self._required_coordinators.get((current.session_id, current.operation_id))
        reduction = None
        if coordinator is not None:
            receipts = coordinator.settlement_failure_receipts() if recovery_receipts is None else recovery_receipts
            if receipts:
                reduction = reduce_composer_failures(receipts)
        if reduction is not None and reduction.category_rank <= 51:
            return reduction.project(
                request_id=current.request_id,
                timeout_seconds=(restore_utc(current.deadline_at) - restore_utc(current.created_at)).total_seconds(),
            )
        if _authoritative_failure(exc):
            return project_composer_operation_error(exc, request_id=current.request_id)
        if current.cancel_requested_at is not None:
            return request_cancelled_error(request_id=current.request_id)
        if restore_utc(current.deadline_at) <= now or any(isinstance(leaf, ComposerTurnDeadlineExpired) for leaf in leaves):
            return deadline_expired_error(
                request_id=current.request_id,
                timeout_seconds=(restore_utc(current.deadline_at) - restore_utc(current.created_at)).total_seconds(),
            )
        if any(isinstance(leaf, (asyncio.CancelledError, SessionOperationFenceLost, ComposerOperationFenceLost)) for leaf in leaves):
            return worker_lost_error(request_id=current.request_id)
        if reduction is not None:
            return reduction.project(
                request_id=current.request_id,
                timeout_seconds=(restore_utc(current.deadline_at) - restore_utc(current.created_at)).total_seconds(),
            )
        return project_composer_operation_error(exc, request_id=current.request_id)

    async def reap_once(self) -> int:
        services = composer_app_services(self._app)
        count = 0
        queued = await _owned(run_sync_in_worker(self._authority.list_expired_queued, limit=self._concurrency))
        for record in queued:
            failure = (
                request_cancelled_error(request_id=record.request_id)
                if record.cancel_requested_at is not None
                else deadline_expired_error(
                    request_id=record.request_id,
                    timeout_seconds=(restore_utc(record.deadline_at) - restore_utc(record.created_at)).total_seconds(),
                )
            )
            try:
                await _owned(
                    run_sync_in_worker(
                        self._authority.settle_unstarted,
                        None,
                        session_id=record.session_id,
                        operation_id=record.operation_id,
                        failure=failure,
                    )
                )
                count += 1
            except ComposerOperationFenceLost:
                pass
        expired = await _owned(run_sync_in_worker(self._authority.list_expired_running, limit=self._concurrency))
        for record in expired:
            if (record.session_id, record.operation_id) in self._jobs:
                continue
            if (record.session_id, record.operation_id) in self._adoption_recovery_pending:
                self._process_recovery.request_shutdown()
                continue
            failure = worker_lost_error(request_id=record.request_id)
            try:
                lease = await SessionOperationLease.acquire(
                    services.session_service.session_operation_authority,
                    session_id=record.session_id,
                    operation_kind=SessionOperationKind.COMPOSE,
                    owner_instance_id=self._owner_instance_id,
                    lease_seconds=services.session_service.session_operation_lease_seconds,
                )
                async with lease:
                    await _owned(
                        run_sync_in_worker(
                            self._authority.settle_lost,
                            session_operation_context=lease.context,
                            session_id=record.session_id,
                            operation_id=record.operation_id,
                            failure=failure,
                        )
                    )
                count += 1
            except SessionOperationConflictError:
                if record.claim_owner_instance_id == self._owner_instance_id:
                    try:
                        await _owned(
                            run_sync_in_worker(
                                self._authority.settle_own_lapsed,
                                session_id=record.session_id,
                                operation_id=record.operation_id,
                                owner_instance_id=self._owner_instance_id,
                                failure=failure,
                            )
                        )
                        count += 1
                    except ComposerOperationFenceLost:
                        pass
            except SessionOperationFenceLost as exc:
                if exc.reason is FenceLossReason.OWNER_INACTIVE:
                    await _owned(
                        run_sync_in_worker(
                            self._authority.settle_lost_inactive_session,
                            session_id=record.session_id,
                            operation_id=record.operation_id,
                            failure=failure,
                        )
                    )
                    count += 1
                elif exc.reason is not FenceLossReason.MISSING:
                    raise
            except ComposerOperationFenceLost:
                pass
        return count


def _failure_leaves(exc: BaseException) -> tuple[BaseException, ...]:
    return required_failure_leaves(exc)


def _authoritative_failure(exc: BaseException) -> bool:
    return any(
        isinstance(
            leaf,
            (
                AuditIntegrityError,
                ComposerOwnedSettlementFailure,
                SQLAlchemyError,
                RequiredGenerationUnavailable,
                AsyncWorkerAdmissionTimeoutError,
            ),
        )
        or (isinstance(leaf, OSError) and leaf.errno in (errno.EIO, errno.ENOSPC, errno.EROFS))
        for leaf in _failure_leaves(exc)
    )


def _terminal_completion_unknown(exc: BaseException) -> bool:
    return any(isinstance(leaf, ComposerTerminalSQLCompletionUnknown) for leaf in _failure_leaves(exc))
