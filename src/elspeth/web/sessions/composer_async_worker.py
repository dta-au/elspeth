"""App-owned durable composer queue, cancellation watch and recovery."""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections.abc import Awaitable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from functools import partial
from uuid import UUID

import structlog
from starlette.applications import Starlette

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.async_workers import (
    QueuedClaimRelease,
    run_claim_release_in_worker,
    run_required_sql_finish_once,
    run_sync_in_worker,
)
from elspeth.web.composer_watch_reads import ComposerOperationWatchReader
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption, SessionOperationLease
from elspeth.web.coordination.repository import SessionOperationConflictError
from elspeth.web.process_recovery import ProcessRecovery
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkCoordinator,
    RequiredWorkIncomplete,
    RequiredWorkSource,
    required_failure_leaves,
)
from elspeth.web.sessions.composer_app_services import composer_app_services
from elspeth.web.sessions.composer_drain import ComposerDrainDeadline, ComposerDrainSignal
from elspeth.web.sessions.composer_operations import (
    COMPOSER_SHUTDOWN,
    ComposerOperationClaim,
    ComposerOperationRecord,
    ComposerOperationRunning,
)
from elspeth.web.sessions.continuation import ComposerSegmentStartObservation
from elspeth.web.sessions.continuation_driver import run_continuation_segment
from elspeth.web.sessions.continuation_recovery import reap_continuations_once
from elspeth.web.sessions.continuation_sql_deadline import ContinuationSQLDeadlineOwner
from elspeth.web.sessions.service import ComposerTerminalSQLCompletionUnknown

slog = structlog.get_logger()


@dataclass(slots=True)
class _StartedSettlement:
    deferred_cancellations: list[asyncio.CancelledError] = field(default_factory=list)
    terminal_receipt: ComposerOperationRecord | None = None
    verified_terminal: ComposerOperationRecord | None = None


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
class _OperationWatchObservation:
    failure: BaseException | None = None
    reader: ComposerOperationWatchReader | None = None


async def _finish_operation_watcher[T](watcher: asyncio.Task[T], observation: _OperationWatchObservation) -> tuple[BaseException, ...]:
    """Join the exact watcher before settlement, preserving caller originals."""
    cleanup_marker = object()
    errors: list[BaseException] = []
    if not watcher.done() and observation.failure is None:
        if observation.reader is not None:
            observation.reader.request_internal_cleanup(cleanup_marker)
        watcher.cancel(cleanup_marker)
    while not watcher.done():
        try:
            await asyncio.sleep(0.01)
        except asyncio.CancelledError as cancellation:
            # This catch observes cancellation delivered to the joining caller,
            # never a watcher result.
            if all(cancellation is not earlier for earlier in errors):
                errors.append(cancellation)
    try:
        result = watcher.result()
        if isinstance(result, BaseException):
            errors.append(result)
    except asyncio.CancelledError as cancellation:
        if observation.reader is not None:
            known_private_close = observation.reader.owns_internal_cleanup_observation(cancellation)
        else:
            known_private_close = len(cancellation.args) == 1 and cancellation.args[0] is cleanup_marker
        if not known_private_close:
            errors.append(cancellation)
    except BaseException as failure:
        errors.append(failure)
    if observation.failure is not None and all(observation.failure is not earlier for earlier in errors):
        errors.append(observation.failure)
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
        drain_signal: ComposerDrainSignal | None = None,
    ) -> None:
        if (
            not 1 <= concurrency <= 16
            or scan_interval_seconds <= 0
            or isinstance(drain_seconds, bool)
            or not math.isfinite(drain_seconds)
            or drain_seconds <= 0
        ):
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
        if drain_signal is not None and (type(drain_signal) is not ComposerDrainSignal or drain_signal.draining is not instance_draining):
            raise AuditIntegrityError("Composer worker changed its actual process drain signal")
        self._drain_signal = drain_signal
        self._required_coordinators: dict[tuple[UUID, str], RequiredWorkCoordinator] = {}
        self._stopping = False
        self._drain_deadline: ComposerDrainDeadline | None = None
        self._continuation_adoptions: dict[tuple[UUID, str], ContinuationLeaseAdoption] = {}
        self._continuation_starts: dict[tuple[UUID, str], ComposerSegmentStartObservation] = {}
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._jobs: dict[tuple[UUID, str], asyncio.Task[None]] = {}
        self._local_cancels: dict[tuple[UUID, str], asyncio.Event] = {}

    def notify_work(self) -> None:
        self._wake.set()

    @property
    def drain_deadline(self) -> ComposerDrainDeadline | None:
        return self._drain_deadline

    def stop_claiming(self) -> None:
        """Latch process admission refusal."""
        self._instance_draining.set()
        self._wake.set()

    def begin_drain(self, *, started_at: float | None = None) -> ComposerDrainDeadline:
        """Latch claim refusal and one deadline on the owning event-loop thread.

        The signal handler supplies its monotonic start, so loop scheduling
        delay consumes the budget. Repeated calls retain the first deadline.
        """
        if self._drain_deadline is None:
            self._drain_deadline = ComposerDrainDeadline.from_start(
                started_at=time.monotonic() if started_at is None else started_at, drain_seconds=self._drain_seconds
            )
        self.stop_claiming()
        return self._drain_deadline

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
        if self._drain_deadline is not None and not task.cancelled() and failure is None:
            return
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
        excluded_operations = frozenset(self._jobs)
        claims = await _owned(run_sync_in_worker(self._authority.claim_segment, limit=capacity, excluded_operations=excluded_operations))
        if self._stopping or self._instance_draining.is_set():
            for claim in claims:
                await self._release_unstarted_claim(claim)
            return 0
        admitted = 0
        for claim in claims:
            key = (claim.session_id, claim.operation_id)
            if key in self._jobs or len(self._jobs) >= self._concurrency:
                # Another local caller can install work while SQL selection
                # awaits. Hand back only this exact new unstarted claim;
                # never replace the predecessor's Task/Stop/coordinator.
                await self._release_unstarted_claim(claim)
                continue
            self._local_cancels[key] = asyncio.Event()
            task = asyncio.create_task(self._job(claim), name="composer-operation")
            self._jobs[key] = task
            task.add_done_callback(partial(self._job_finished, key))
            admitted += 1
        return admitted

    def _job_finished(self, key: tuple[UUID, str], task: asyncio.Task[None]) -> None:
        del self._jobs[key]
        del self._local_cancels[key]
        failure = None if task.cancelled() else task.exception()
        coordinator = self._required_coordinators.get(key)
        adoption = self._continuation_adoptions.get(key)
        if adoption is not None:
            if adoption.lease is not None and adoption.lease.continuation_finished and (coordinator is None or coordinator.all_completed):
                del self._continuation_adoptions[key]
                self._continuation_starts.pop(key, None)
            else:
                self._process_recovery.request_shutdown()
        elif key in self._continuation_starts:
            self._process_recovery.request_shutdown()
        if coordinator is not None and coordinator.all_completed:
            del self._required_coordinators[key]
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
        if self._drain_deadline is not None:
            # HTTP shutdown has already spent part of this same budget. Jobs
            # own cancellation at their stage cutoff; do not cancel at H.
            remaining = self._drain_deadline.remaining_seconds(now=time.monotonic())
            if tasks and remaining > 0:
                await _owned(asyncio.wait(tasks, timeout=remaining))
            for task in tasks:
                if not task.done():
                    task.cancel(COMPOSER_SHUTDOWN)
            # Expiry leaves the actual jobs, physical SQL and finish tasks in
            # custody. It does not grant a second relative drain interval.
            return
        for task in tasks:
            task.cancel(COMPOSER_SHUTDOWN)
        if tasks:
            # asyncio.wait does not cancel unfinished children on timeout.
            await _owned(asyncio.wait(tasks, timeout=self._drain_seconds))
            # Remaining children retain task/fence/SQL custody. Their peer reaper
            # may settle only once the COMPOSE lease has actually lapsed.

    def assert_shutdown_complete(self) -> None:
        if (
            not self._stopping
            or self._jobs
            or self._continuation_adoptions
            or self._continuation_starts
            or (self._loop_task is not None and not self._loop_task.done())
        ):
            raise AuditIntegrityError("Composer worker still owns unfinished shutdown work")
        if self._loop_task is not None and not self._loop_task.cancelled():
            self._loop_task.exception()
        for coordinator in self._required_coordinators.values():
            coordinator.assert_completed()

    async def _release_unstarted_claim(self, claim: ComposerOperationClaim) -> None:
        # Process drain refuses ticketless shared-pool work, and an unstarted
        # claim has no context to hold a drain ticket. Its hand-back is the one
        # drain-admitted submission (U-37; container-grace note R2).
        await _owned(run_claim_release_in_worker(QueuedClaimRelease(self._authority.release_claim, claim)))

    async def _job(self, claim: ComposerOperationClaim) -> None:
        services = composer_app_services(self._app)
        lock = await services.compose_locks.get_lock(str(claim.session_id))
        acquired = False
        running: ComposerOperationRunning | None = None
        context: SessionOperationContext | None = None
        adoption: ContinuationLeaseAdoption | None = None
        cancellations: list[asyncio.CancelledError] = []
        key = (claim.session_id, claim.operation_id)
        known_unstarted = True
        started_failure: BaseException | None = None
        next_renewal = time.monotonic() + self._claim_lease_seconds / 3
        try:
            while not acquired:
                if self._instance_draining.is_set():
                    # Drain refuses the segment-claim read itself. An unstarted
                    # segment cannot start under drain, so hand it back; a
                    # durable Stop marker stays for the next claimer.
                    await self._release_unstarted_claim(claim)
                    return
                current = await _owned(run_sync_in_worker(self._authority.read_segment_claim, claim))
                if current is None:
                    return
                draining = self._stopping or self._instance_draining.is_set()
                expired = self._drain_deadline is not None and self._drain_deadline.remaining_seconds(now=time.monotonic()) <= 0
                if expired or (draining and current.cancel_requested_at is None):
                    await self._release_unstarted_claim(claim)
                    return
                try:
                    await asyncio.wait_for(lock.acquire(), timeout=min(0.25, self._claim_lease_seconds / 3))
                    acquired = True
                except TimeoutError:
                    if time.monotonic() >= next_renewal:
                        await _owned(run_sync_in_worker(self._authority.renew_claim, claim))
                        next_renewal = time.monotonic() + self._claim_lease_seconds / 3
            observation = ComposerSegmentStartObservation(claim)
            self._continuation_starts[key] = observation
            known_unstarted = False
            start_deadline = self._drain_deadline or ComposerDrainDeadline.from_start(
                started_at=time.monotonic(), drain_seconds=self._drain_seconds
            )
            try:
                context = await _owned(
                    run_sync_in_worker(
                        services.session_service.session_operation_authority.start_composer_segment,
                        claim,
                        owner_instance_id=self._owner_instance_id,
                        lease_seconds=services.session_service.session_operation_lease_seconds,
                        auth_provider_type=services.settings.auth_provider,
                        observation=observation,
                        deadline=start_deadline,
                    ),
                    cancellation_observations=cancellations,
                )
            except BaseException as original:
                started_failure = original
                try:
                    context = await _owned(
                        run_sync_in_worker(
                            services.session_service.session_operation_authority.reconcile_composer_segment_start,
                            observation,
                            deadline=self._drain_deadline or start_deadline,
                        ),
                        cancellation_observations=cancellations,
                    )
                except BaseException as readback:
                    raise _combine_operation_failures(original, (readback,)) from None
                if context is None:
                    known_unstarted = True
                    self._continuation_starts.pop(key)
                    raise original
            running = ComposerOperationRunning(claim=claim, session_operation_context=context)
            if observation.policy is None or observation.hard_expires_at is None or self._drain_signal is None:
                raise AuditIntegrityError("segment start lost its actual retained timing/process signal owners")
            sql_deadline = ContinuationSQLDeadlineOwner(
                running,
                policy=observation.policy,
                drain_signal=self._drain_signal,
                drain_seconds=self._drain_seconds,
                hard_expires_at=observation.hard_expires_at,
            )
            coordinator = RequiredWorkCoordinator(
                RequiredWorkAuthority(RequiredAuthorityKind.DURABLE_COMPOSE, context, claim.operation_id, claim.attempt),
                sql_deadline_owner=sql_deadline,
            )
            self._required_coordinators[key] = coordinator
            adoption = ContinuationLeaseAdoption(
                services.session_service.session_operation_authority,
                context,
                lease_seconds=services.session_service.session_operation_lease_seconds,
                required_work=coordinator,
            )
            self._continuation_adoptions[key] = adoption
            adoption.cancellations.extend(cancellations)
            try:
                lease = await adoption.join()
            except BaseException as original:
                retained_lease = adoption.lease
                if retained_lease is None:
                    raise
                # This owner has not implicitly released the fence.
                # The driver sees the original failure and can hand over only
                # after current SQL custody/Stop and actual CAS were observed.
                await run_continuation_segment(self, services, running, retained_lease, adoption_failure=original)
                raise
            await run_continuation_segment(self, services, running, lease, adoption_failure=started_failure)
        except SessionOperationConflictError:
            if running is not None or not known_unstarted:
                raise
            await self._release_unstarted_claim(claim)
        except BaseException:
            if running is None and known_unstarted:
                await self._release_unstarted_claim(claim)
            raise
        finally:
            if acquired:
                lock.release()

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

    async def reap_once(self) -> int:
        return await reap_continuations_once(self)


def _failure_leaves(exc: BaseException) -> tuple[BaseException, ...]:
    return required_failure_leaves(exc)


def _terminal_completion_unknown(exc: BaseException) -> bool:
    return any(isinstance(leaf, ComposerTerminalSQLCompletionUnknown) for leaf in _failure_leaves(exc))
