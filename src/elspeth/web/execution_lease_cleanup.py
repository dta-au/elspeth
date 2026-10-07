"""Application-owned EXECUTE acquisition and exact release custody."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from types import MethodType
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.application_finalizers import ApplicationFinalizerCapability, ApplicationFinalizerKind, ApplicationFinalizerOwner
from elspeth.web.required_executor import (
    InvocationGate,
    InvocationReservation,
    RequiredExecutorGenerationCustodian,
    RequiredGenerationUnavailable,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from elspeth.web.coordination.lifecycle import SessionOperationLease
    from elspeth.web.coordination.repository import _SessionOperationAuthorityRepository
    from elspeth.web.process_recovery import ProcessRecovery
    from elspeth.web.sessions.protocol import SessionOperationAuthority


_ISSUANCE_SEAL = object()
_EXECUTION_SQL_BRIDGE_ISSUER_SEAL = object()


@dataclass(frozen=True, slots=True)
class _ExecutionRegistration:
    registry: ExecutionLeaseReleaseRegistry
    obligation: ExecutionAcquisitionObligation


@dataclass(frozen=True, slots=True)
class _ExecutionRetirement:
    registration: _ExecutionRegistration
    submission: ExecutionLeaseSQLSubmission


class _ExecutionObservationPhase(Enum):
    ACQUISITION = "acquisition"
    RELEASE = "release"
    COMPLETION = "completion"
    LIFECYCLE = "lifecycle"


class _ExecutionSQLArm(Enum):
    ACQUIRE = "acquire"
    RELEASE = "release"


class ExecutionLeaseSQLSubmission:
    """One registry-issued invocation; its dispatch accepts no replacement SQL."""

    def __init__(self, seal: object, obligation: ExecutionAcquisitionObligation, arm: _ExecutionSQLArm) -> None:
        if seal is not _ISSUANCE_SEAL or type(obligation) is not ExecutionAcquisitionObligation or type(arm) is not _ExecutionSQLArm:
            raise AuditIntegrityError("EXECUTE SQL submission lacks lifecycle issuance")
        self.obligation = obligation
        self.arm = arm
        self.future: Future[SessionOperationContext | None] | None = None
        self.reservation: InvocationReservation | None = None
        self.original_error: BaseException | None = None
        self.source_value: SessionOperationContext | None = None
        self.domain_refusal: BaseException | None = None
        self.observed = False
        self.no_submission = False
        self.callback_return_observed = False
        self.projection_failure: BaseException | None = None

    def assert_canonical_dispatch(self) -> None:
        """Refuse a known substituted authority before acquiring admission."""
        from elspeth.web.coordination.repository import _SessionOperationAuthorityRepository

        obligation = self.obligation
        with obligation.registry._lock:
            obligation.registry._require_submission(self)
        acquire, release = obligation._acquire, obligation._release
        if (
            type(acquire) is not MethodType
            or acquire.__self__ is not obligation.authority
            or acquire.__func__ is not _SessionOperationAuthorityRepository.acquire
            or type(release) is not MethodType
            or release.__self__ is not obligation.authority
            or release.__func__ is not _SessionOperationAuthorityRepository.release
        ):
            raise AuditIntegrityError("EXECUTE SQL replaced its captured authority dispatch")
        if self.arm is _ExecutionSQLArm.RELEASE and (
            type(obligation.context) is not SessionOperationContext
            or obligation.context is not obligation.acquire_submission.source_value
            or not obligation.acquire_submission.observed
        ):
            raise AuditIntegrityError("EXECUTE release replaced its exact acquired context")

    def invoke(self) -> SessionOperationContext | None:
        from elspeth.web.coordination.contracts import SessionOperationFenceLost
        from elspeth.web.coordination.repository import SessionOperationConflictError, _SessionOperationAuthorityRepository

        obligation = self.obligation
        acquire, release = obligation._acquire, obligation._release
        if (
            type(acquire) is not MethodType
            or acquire.__self__ is not obligation.authority
            or acquire.__func__ is not _SessionOperationAuthorityRepository.acquire
            or type(release) is not MethodType
            or release.__self__ is not obligation.authority
            or release.__func__ is not _SessionOperationAuthorityRepository.release
        ):
            raise AuditIntegrityError("EXECUTE SQL lost its captured canonical authority dispatch")
        if self.arm is _ExecutionSQLArm.ACQUIRE:
            try:
                value: SessionOperationContext = acquire(
                    session_id=obligation.session_id,
                    operation_kind=SessionOperationKind.EXECUTE,
                    owner_instance_id=obligation.owner_instance_id,
                    lease_seconds=obligation.lease_seconds,
                )
            except (SessionOperationConflictError, SessionOperationFenceLost) as original:
                # Only this captured canonical authority invocation can record
                # its source-proven transaction/domain refusal.
                self.domain_refusal = original
                raise
            self.source_value = value
            return value
        context = obligation.context
        if (
            type(context) is not SessionOperationContext
            or context is not obligation.acquire_submission.source_value
            or not obligation.acquire_submission.observed
            or context.operation_kind is not SessionOperationKind.EXECUTE
            or context.fence.session_id != str(obligation.session_id)
        ):
            raise AuditIntegrityError("EXECUTE release lacks exact joined acquired context")
        release(context)
        return None

    @property
    def permits_process_drain(self) -> bool:
        return self.arm is _ExecutionSQLArm.RELEASE and self.obligation.registry.owns_submission(self)

    @contextmanager
    def submission_decision(self) -> Iterator[None]:
        """Caller holds generation lock; retain registry lock through submit."""
        registry = self.obligation.registry
        with registry._lock:
            registry._require_submission(self)
            if self.observed or self.future is not None or self.reservation is not None:
                raise AuditIntegrityError("EXECUTE SQL invocation was reused")
            if self.arm is _ExecutionSQLArm.ACQUIRE and registry._sealed:
                raise RequiredGenerationUnavailable("EXECUTE acquisition lifecycle is sealed")
            yield

    def bind_reservation(self, reservation: InvocationReservation) -> None:
        with self.obligation.registry._lock:
            self.obligation.registry._require_submission(self)
            if self.reservation is not None or reservation._registering_generation is not self.obligation.generation:
                raise AuditIntegrityError("EXECUTE reservation lacks exact registered generation")
            self.reservation = reservation

    def observe_no_submission(self, seal: object, original: BaseException) -> None:
        """Only bridge pre-submit branches may publish this exact original."""
        with self.obligation.registry._lock:
            self.obligation.registry._require_submission(self)
            reservation = self.reservation
            if (
                seal is not _EXECUTION_SQL_BRIDGE_ISSUER_SEAL
                or self.observed
                or self.future is not None
                or (
                    reservation is not None
                    and (
                        reservation.future is not None
                        or reservation.submission_error is not None
                        or reservation._registering_generation is not None
                    )
                )
            ):
                raise AuditIntegrityError("EXECUTE refusal cannot erase accepted submission custody")
            self.original_error = original
            self.no_submission = self.observed = True
            if self.arm is _ExecutionSQLArm.ACQUIRE:
                self.obligation.registry._retire_no_resource(self.obligation)

    def observe_aborted_submission(self, seal: object, original: BaseException) -> None:
        registry = self.obligation.registry
        with registry._lock:
            registry._require_submission(self)
            reservation = self.reservation
            if seal is not _EXECUTION_SQL_BRIDGE_ISSUER_SEAL or reservation is None:
                raise AuditIntegrityError("EXECUTE aborted submission lacks issued physical custody")
            if self.projection_failure is not None:
                # Failed callback custody remains Unknown, even if a later
                # generation join observes that the SQL was never entered.
                return
            if not reservation.released or not (self.callback_return_observed or self.obligation.generation.joined.is_set()):
                return
            trace = reservation.witness.snapshot()
            if trace.impossible or not (
                trace.valid_aborted_exit
                or (self.future is None and trace.gate is InvocationGate.ABORTED and self.obligation.generation.joined.is_set())
            ):
                raise AuditIntegrityError("EXECUTE aborted submission lacks no-SQL physical proof")
            # A no-return reservation is released only by actual aborted exit or
            # actual generation shutdown(join), never its async wrapper.
            self.original_error = original
            self.observed = True
            if self.arm is _ExecutionSQLArm.ACQUIRE:
                registry._retire_no_resource(self.obligation)
        registry.record_failure(original)

    def bind_future(self, future: Future[SessionOperationContext | None], reservation: InvocationReservation) -> None:
        registry = self.obligation.registry
        with registry._lock:
            registry._require_submission(self)
            if self.future is not None or self.reservation is not reservation:
                raise AuditIntegrityError("EXECUTE SQL Future was already bound")
            if reservation.future is not future or reservation._registering_generation is not self.obligation.generation:
                raise AuditIntegrityError("EXECUTE SQL Future has foreign physical custody")
            self.future = future
            self.reservation = reservation

    def observe_callback_return(self, seal: object, actual: InvocationReservation) -> None:
        """Issued only after the actual physical release callback returns."""
        registry = self.obligation.registry
        with registry._lock:
            registry._require_submission(self)
            if (
                seal is not _EXECUTION_SQL_BRIDGE_ISSUER_SEAL
                or actual is not self.reservation
                or not actual.released
                or actual.semantic_observation_error is not None
            ):
                raise AuditIntegrityError("EXECUTE callback lacks its actual successful release return")
            self.callback_return_observed = True

    def observe_future(self, actual: Future[SessionOperationContext | None]) -> None:
        registry = self.obligation.registry
        failed_cleanup: BaseException | None = None
        if self.projection_failure is not None:
            return
        try:
            with registry._lock:
                registry._require_submission(self)
                if actual is not self.future or not actual.done() or self.reservation is None:
                    raise AuditIntegrityError("EXECUTE observation lacks its completed actual Future")
                if self.observed:
                    return
                # Future.done precedes callback completion. A pending release
                # remains an observation pending state, never an integrity fault
                # or a resource retirement receipt.
                if not self.reservation.released or not self.callback_return_observed:
                    return
                trace = self.reservation.witness.snapshot()
                if trace.valid_aborted_exit and (self.reservation.setup_error is not None or self.reservation.submission_error is not None):
                    # Only the issued bridge can publish the no-entry setup
                    # receipt. A census before that handoff is merely pending.
                    return
                if trace.impossible or not trace.exited or not trace.callable_finished:
                    raise AuditIntegrityError("EXECUTE SQL outcome has no completed invocation witness")
                try:
                    value = actual.result()
                except BaseException as original:
                    self.original_error = original
                    self.observed = True
                    if self.arm is _ExecutionSQLArm.ACQUIRE and original is self.domain_refusal:
                        registry._retire_no_resource(self.obligation)
                    else:
                        registry._retain_failure(original)
                        failed_cleanup = original
                else:
                    if self.arm is _ExecutionSQLArm.ACQUIRE:
                        if (
                            type(value) is not SessionOperationContext
                            or value is not self.source_value
                            or value.operation_kind is not SessionOperationKind.EXECUTE
                            or value.fence.session_id != str(self.obligation.session_id)
                        ):
                            raise AuditIntegrityError("EXECUTE acquired context changed its actual authority")
                        self.obligation.context = value
                    elif value is not None:
                        raise AuditIntegrityError("EXECUTE release returned an unexpected value")
                    self.observed = True
                    if self.arm is _ExecutionSQLArm.RELEASE:
                        self.obligation.release_succeeded = True
                        registry._retire_success_if_complete(self.obligation)
            if failed_cleanup is not None:
                registry.record_failure(failed_cleanup)
        except BaseException as original:
            self.projection_failure = original
            registry.record_failure(original)


class ExecutionAcquisitionObligation:
    def __init__(
        self,
        seal: object,
        registry: ExecutionLeaseReleaseRegistry,
        authority: _SessionOperationAuthorityRepository,
        session_id: UUID,
        owner_instance_id: str,
        lease_seconds: int,
        executor: ThreadPoolExecutor,
        generation: RequiredExecutorGenerationCustodian,
    ) -> None:
        if seal is not _ISSUANCE_SEAL:
            raise AuditIntegrityError("EXECUTE obligation is lifecycle-issued")
        self.registry = registry
        self._registration: _ExecutionRegistration | None = None
        self._retirement: _ExecutionRetirement | None = None
        self.authority = authority
        self.session_id = session_id
        self.owner_instance_id = owner_instance_id
        self.lease_seconds = lease_seconds
        self.executor = executor
        self.generation = generation
        self._acquire = authority.acquire
        self._release = authority.release
        self.acquire_submission = ExecutionLeaseSQLSubmission(_ISSUANCE_SEAL, self, _ExecutionSQLArm.ACQUIRE)
        self.release_submission: ExecutionLeaseSQLSubmission | None = None
        self.context: SessionOperationContext | None = None
        self.lease: SessionOperationLease | None = None
        self.construction_error: BaseException | None = None
        self.renewal_task: asyncio.Task[None] | None = None
        self.renewal_allocation_declared = False
        self.release_succeeded = False
        self.no_resource = False
        self.retired = False
        self.completion: Future[None] | None = None
        self.completion_task: asyncio.Task[None] | None = None
        self.completion_required = False
        self.completion_observed = False
        self.completion_outcome_recorded = False
        self.completion_original_error: BaseException | None = None
        self.pipeline: Future[Literal["graceful_shutdown_handled"] | None] | None = None
        self.lifecycle_task: asyncio.Task[None] | None = None
        self.lifecycle_required = False
        self.lifecycle_observed = False
        self.lifecycle_outcome_recorded = False
        self.lifecycle_original_error: BaseException | None = None
        self.pipeline_submission_unknown: BaseException | None = None
        self.unknown_pipeline_cleanup_declared = False
        self.observation_failures: dict[_ExecutionObservationPhase, BaseException] = {}

    def validate_acquisition_request(self, authority: object, *, session_id: UUID, owner_instance_id: str, lease_seconds: int) -> None:
        with self.registry._lock:
            self.registry._require_obligation(self)
            if (
                authority is not self.authority
                or session_id != self.session_id
                or owner_instance_id != self.owner_instance_id
                or lease_seconds != self.lease_seconds
            ):
                raise AuditIntegrityError("EXECUTE acquisition changed its pre-admitted authority")

    def retain_lease_construction(self, lease: SessionOperationLease) -> None:
        from elspeth.web.coordination.lifecycle import SessionOperationLease

        with self.registry._lock:
            self.registry._require_obligation(self)
            if type(lease) is not SessionOperationLease or self.lease is not None or self.context is None:
                raise AuditIntegrityError("EXECUTE lease construction lacks exact retained acquisition")
            self.lease = lease

    def declare_renewal_allocation(self, lease: SessionOperationLease) -> None:
        with self.registry._lock:
            self.registry._require_obligation(self)
            if lease is not self.lease or self.renewal_allocation_declared:
                raise AuditIntegrityError("EXECUTE renewal allocation changed construction ownership")
            self.renewal_allocation_declared = True

    def bind_actual_renewal_task(self, lease: SessionOperationLease, task: asyncio.Task[None]) -> None:
        with self.registry._lock:
            self.registry._require_obligation(self)
            if (
                not isinstance(task, asyncio.Task)
                or lease is not self.lease
                or not self.renewal_allocation_declared
                or (self.renewal_task is not None and self.renewal_task is not task)
            ):
                raise AuditIntegrityError("EXECUTE renewal task changed actual construction owner")
            self.renewal_task = task

    def record_construction_failure(self, original: BaseException) -> None:
        self.construction_error = original
        self.registry.record_failure(original)

    def assert_business_dispatch(self, context: SessionOperationContext) -> None:
        with self.registry._lock:
            self.registry._require_obligation(self)
            if context is not self.context or not self.acquire_submission.observed:
                raise AuditIntegrityError("EXECUTE business dispatch lost exact acquired context")
            if self.registry._sealed or self.registry.recovery.instance_draining.is_set():
                raise RequiredGenerationUnavailable("EXECUTE application is draining")

    @contextmanager
    def pipeline_submission_decision(self, context: SessionOperationContext) -> Iterator[None]:
        from elspeth.web.async_workers import _assert_execution_generation

        with self.generation.submission_lock:
            _assert_execution_generation(self.acquire_submission)
            with self.registry._lock:
                self.assert_business_dispatch(context)
                self.completion_required = True
                yield

    def record_pipeline_submission_unknown(self, original: BaseException) -> None:
        with self.registry._lock:
            self.registry._require_obligation(self)
            if not self.completion_required or self.pipeline is not None:
                raise AuditIntegrityError("EXECUTE unknown pipeline submit lost declared ownership")
            self.pipeline_submission_unknown = original
        self.registry.record_failure(original)

    def declare_unknown_pipeline_cleanup(self) -> None:
        if not self.registry.executor_join_succeeded:
            raise AuditIntegrityError("EXECUTE unknown pipeline cleanup lacks actual executor join")
        with self.registry._lock:
            self.registry._require_obligation(self)
            if (
                self.pipeline_submission_unknown is None
                or self.pipeline is not None
                or self.completion is not None
                or self.completion_task is not None
                or self.unknown_pipeline_cleanup_declared
            ):
                raise AuditIntegrityError("EXECUTE unknown pipeline cleanup ownership changed or was reused")
            self.unknown_pipeline_cleanup_declared = True

    def bind_pipeline_future(self, future: Future[Literal["graceful_shutdown_handled"] | None]) -> None:
        with self.registry._lock:
            self.registry._require_obligation(self)
            if not isinstance(future, Future) or not self.completion_required or self.pipeline is not None:
                raise AuditIntegrityError("EXECUTE pipeline Future lacks actual dispatch ownership")
            self.pipeline = future

    def declare_lifecycle_close(self) -> None:
        with self.registry._lock:
            self.registry._require_obligation(self)
            self.lifecycle_required = True

    def bind_lifecycle_task(self, task: asyncio.Task[None]) -> None:
        with self.registry._lock:
            self.registry._require_obligation(self)
            if (
                not isinstance(task, asyncio.Task)
                or not self.lifecycle_required
                or (self.lifecycle_task is not None and self.lifecycle_task is not task)
            ):
                raise AuditIntegrityError("EXECUTE lifecycle task replaced declared cleanup owner")
            self.lifecycle_task = task

    def record_lifecycle_outcome(self, original: BaseException | None) -> None:
        with self.registry._lock:
            self.registry._require_obligation(self)
            if asyncio.current_task() is not self.lifecycle_task or self.lifecycle_outcome_recorded:
                raise AuditIntegrityError("EXECUTE lifecycle outcome lacks exact producer Task")
            self.lifecycle_original_error = original
            self.lifecycle_outcome_recorded = True

    def observe_lifecycle(self) -> None:
        failure: BaseException | None = None
        with self.registry._lock:
            self.registry._require_obligation(self)
            task = self.lifecycle_task
            if task is None or not task.done() or self.lifecycle_observed or not self.lifecycle_outcome_recorded:
                return
            try:
                task.result()
            except BaseException as actual:
                original = self.lifecycle_original_error
                if original is None or (
                    actual is not original
                    and not (isinstance(actual, asyncio.CancelledError) and isinstance(original, asyncio.CancelledError))
                ):
                    raise AuditIntegrityError("EXECUTE lifecycle producer and actual Task outcome disagree") from actual
                failure = original
            else:
                if self.lifecycle_original_error is not None:
                    raise AuditIntegrityError(
                        "EXECUTE lifecycle Task erased its original producer failure"
                    ) from self.lifecycle_original_error
            self.lifecycle_observed = True
            self.registry._retire_success_if_complete(self)
        if failure is not None:
            self.registry.record_failure(failure)

    def bind_completion_task(self, task: asyncio.Task[None]) -> None:
        with self.registry._lock:
            self.registry._require_obligation(self)
            if not isinstance(task, asyncio.Task) or (self.completion_task is not None and self.completion_task is not task):
                raise AuditIntegrityError("EXECUTE completion task replaced actual cleanup owner")
            self.completion_task = task

    def bind_completion_future(self, future: Future[None]) -> None:
        with self.registry._lock:
            self.registry._require_obligation(self)
            if not isinstance(future, Future) or self.completion is not None:
                raise AuditIntegrityError("EXECUTE completion Future already registered or foreign")
            self.completion = future

    def record_completion_outcome(self, original: BaseException | None) -> None:
        with self.registry._lock:
            self.registry._require_obligation(self)
            if asyncio.current_task() is not self.completion_task or self.completion_outcome_recorded:
                raise AuditIntegrityError("EXECUTE completion outcome lacks exact producer Task")
            self.completion_original_error = original
            self.completion_outcome_recorded = True

    def observe_completion(self) -> None:
        failure: BaseException | None = None
        with self.registry._lock:
            self.registry._require_obligation(self)
            task = self.completion_task
            if task is None or not task.done() or self.completion_observed or not self.completion_outcome_recorded:
                return
            try:
                task.result()
            except BaseException as actual:
                original = self.completion_original_error
                if original is None or (
                    actual is not original
                    and not (isinstance(actual, asyncio.CancelledError) and isinstance(original, asyncio.CancelledError))
                ):
                    raise AuditIntegrityError("EXECUTE completion producer and actual Task outcome disagree") from actual
                failure = original
            else:
                if self.completion_original_error is not None:
                    raise AuditIntegrityError(
                        "EXECUTE completion Task erased original producer failure"
                    ) from self.completion_original_error
            self.completion_observed = True
            if self.release_succeeded:
                self.registry._retire_success_if_complete(self)
        if failure is not None:
            self.registry.record_failure(failure)

    def issue_release(self, context: SessionOperationContext) -> ExecutionLeaseSQLSubmission:
        with self.registry._lock:
            self.registry._require_obligation(self)
            if context is not self.context or not self.acquire_submission.observed:
                raise AuditIntegrityError("EXECUTE release lacks its joined exact acquisition")
            if self.release_submission is None:
                self.release_submission = ExecutionLeaseSQLSubmission(_ISSUANCE_SEAL, self, _ExecutionSQLArm.RELEASE)
            return self.release_submission


class ExecutionLeaseReleaseRegistry:
    def __init__(self, *, owner: ApplicationFinalizerOwner, recovery: ProcessRecovery, loop: asyncio.AbstractEventLoop) -> None:
        from elspeth.web.process_recovery import ProcessRecovery

        if type(owner) is not ApplicationFinalizerOwner or type(recovery) is not ProcessRecovery:
            raise AuditIntegrityError("EXECUTE cleanup lacks actual selected application ownership")
        self.owner = owner
        self._executor_finalizer: ApplicationFinalizerCapability | None = None
        self._executor_allocation_declared = False
        self._execution_executor: ThreadPoolExecutor | None = None
        self._executor_join_returned = False
        self.recovery = recovery
        self.loop = loop
        self._lock = threading.RLock()
        self._sealed = False
        self._pending: dict[int, ExecutionAcquisitionObligation] = {}
        self._failures: list[BaseException] = []
        self._recovery_requested = False

    @property
    def executor_finalizer(self) -> ApplicationFinalizerCapability | None:
        with self._lock:
            return self._executor_finalizer

    def bind_executor_finalizer(self, capability: ApplicationFinalizerCapability) -> None:
        # Owner identity check completes before taking registry lock. There
        # is no owner-lock -> registry-lock nesting and no new claim right.
        if (
            type(capability) is not ApplicationFinalizerCapability
            or capability.kind is not ApplicationFinalizerKind.EXECUTION_EXECUTOR_JOIN
            or not self.owner.owns_registered(capability)
        ):
            raise AuditIntegrityError("EXECUTE executor finalizer lost actual registered ownership")
        with self._lock:
            if self._sealed or self._executor_finalizer is not None:
                raise AuditIntegrityError("EXECUTE executor finalizer registration closed or repeated")
            self._executor_finalizer = capability

    def declare_executor_allocation(self, capability: ApplicationFinalizerCapability) -> None:
        with self._lock:
            if capability is not self._executor_finalizer or self._executor_allocation_declared or self._sealed:
                raise AuditIntegrityError("EXECUTE executor allocation lost preowned finalizer")
            self._executor_allocation_declared = True

    def bind_execution_executor(self, capability: ApplicationFinalizerCapability, executor: ThreadPoolExecutor) -> None:
        with self._lock:
            if (
                capability is not self._executor_finalizer
                or not self._executor_allocation_declared
                or self._execution_executor is not None
                or type(executor) is not ThreadPoolExecutor
            ):
                raise AuditIntegrityError("EXECUTE allocated executor changed its actual owner")
            self._execution_executor = executor

    def record_executor_join_return(self, capability: ApplicationFinalizerCapability, executor: ThreadPoolExecutor | None) -> None:
        with self._lock:
            if capability is not self._executor_finalizer or self._executor_join_returned:
                raise AuditIntegrityError("EXECUTE executor join return is foreign or reused")
            if self._executor_allocation_declared:
                if executor is None or executor is not self._execution_executor:
                    raise AuditIntegrityError("EXECUTE executor allocation remains physically unknown")
            elif executor is not None:
                raise AuditIntegrityError("EXECUTE executor join has undeclared allocation")
            self._executor_join_returned = True

    @property
    def executor_join_succeeded(self) -> bool:
        with self._lock:
            capability = self._executor_finalizer
            returned = self._executor_join_returned
        # Do not hold registry lock while taking owner/capability locks.
        if capability is not None and not self.owner.owns_registered(capability):
            raise AuditIntegrityError("EXECUTE executor join capability lost actual registration")
        return capability is not None and returned and capability.physical_completion_succeeded

    @property
    def executor_join_physically_observed(self) -> bool:
        """Actual executor join and shared custody closed, with failures retained."""
        with self._lock:
            capability = self._executor_finalizer
            returned = self._executor_join_returned
        if capability is not None and not self.owner.owns_registered(capability):
            raise AuditIntegrityError("EXECUTE executor physical observation lost actual registration")
        return (
            capability is not None
            and returned
            and (
                capability.physical_completion_succeeded
                or capability.physical_failure_completion_known
                or capability.physical_submission_failure_completion_known
            )
        )

    @property
    def executor_join_failures(self) -> tuple[BaseException, ...]:
        with self._lock:
            capability = self._executor_finalizer
        if capability is None:
            return ()
        if not self.owner.owns_registered(capability):
            raise AuditIntegrityError("EXECUTE executor join capability lost registration identity")
        return capability.physical_failure_originals

    def seal(self) -> None:
        with self._lock:
            self._sealed = True

    def admit(
        self, authority: SessionOperationAuthority, *, session_id: UUID, owner_instance_id: str, lease_seconds: int
    ) -> ExecutionAcquisitionObligation:
        from elspeth.web.async_workers import _APPLICATION_FINALIZER_OWNER, _INSTANCE_DRAINING, _generation_for, _get_shared_executor
        from elspeth.web.coordination.repository import PostgresSessionOperationRepository, _SessionOperationAuthorityRepository
        from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority

        if (
            not isinstance(authority, _SessionOperationAuthorityRepository)
            or type(authority) not in (SQLiteLocalSessionOperationAuthority, PostgresSessionOperationRepository)
            or type(session_id) is not UUID
            or type(owner_instance_id) is not str
            or not owner_instance_id.strip()
            or type(lease_seconds) is not int
            or lease_seconds < 1
        ):
            raise AuditIntegrityError("EXECUTE acquisition lacks exact application authority")
        acquire, release = authority.acquire, authority.release
        if (
            type(acquire) is not MethodType
            or acquire.__self__ is not authority
            or acquire.__func__ is not _SessionOperationAuthorityRepository.acquire
            or type(release) is not MethodType
            or release.__self__ is not authority
            or release.__func__ is not _SessionOperationAuthorityRepository.release
        ):
            raise AuditIntegrityError("EXECUTE acquisition replaced its canonical SQL dispatch")
        if _APPLICATION_FINALIZER_OWNER is not self.owner or _INSTANCE_DRAINING is not self.recovery.instance_draining:
            raise AuditIntegrityError("EXECUTE acquisition lost selected application ownership")
        executor = _get_shared_executor()
        generation = _generation_for(executor)
        if generation.instance_draining is not self.recovery.instance_draining:
            raise AuditIntegrityError("EXECUTE acquisition crossed application generation ownership")
        with self._lock:
            if self._sealed or self.recovery.instance_draining.is_set():
                raise RequiredGenerationUnavailable("EXECUTE acquisition lifecycle is sealed")
            obligation = ExecutionAcquisitionObligation(
                _ISSUANCE_SEAL, self, authority, session_id, owner_instance_id, lease_seconds, executor, generation
            )
            obligation._registration = _ExecutionRegistration(self, obligation)
            self._pending[id(obligation)] = obligation
            return obligation

    def _require_obligation(self, obligation: ExecutionAcquisitionObligation) -> None:
        if type(obligation) is not ExecutionAcquisitionObligation or obligation.registry is not self:
            raise AuditIntegrityError("EXECUTE cleanup obligation is foreign")
        registration = obligation._registration
        if registration is None or registration.registry is not self or registration.obligation is not obligation:
            raise AuditIntegrityError("EXECUTE cleanup obligation lacks actual registration identity")
        retained = self._pending.get(id(obligation))
        retirement = obligation._retirement
        if retained is obligation:
            return
        if (
            retained is not None
            or retirement is None
            or retirement.registration is not registration
            or retirement.submission.obligation is not obligation
        ):
            raise AuditIntegrityError("EXECUTE cleanup obligation is unregistered or replaced")

    def _require_submission(self, submission: ExecutionLeaseSQLSubmission) -> None:
        if type(submission) is not ExecutionLeaseSQLSubmission or type(submission.arm) is not _ExecutionSQLArm:
            raise AuditIntegrityError("EXECUTE SQL submission is foreign")
        obligation = submission.obligation
        self._require_obligation(obligation)
        expected = obligation.acquire_submission if submission.arm is _ExecutionSQLArm.ACQUIRE else obligation.release_submission
        if submission is not expected:
            raise AuditIntegrityError("EXECUTE SQL submission replaced its issued invocation")

    def owns_submission(self, submission: ExecutionLeaseSQLSubmission) -> bool:
        with self._lock:
            self._require_submission(submission)
            return not submission.obligation.retired

    def _retire_no_resource(self, obligation: ExecutionAcquisitionObligation) -> None:
        self._require_obligation(obligation)
        registration = obligation._registration
        if registration is None:
            raise AuditIntegrityError("EXECUTE retirement lost actual registration")
        obligation._retirement = _ExecutionRetirement(registration, obligation.acquire_submission)
        obligation.no_resource = obligation.retired = True
        del self._pending[id(obligation)]

    def _retire_success_if_complete(self, obligation: ExecutionAcquisitionObligation) -> None:
        self._require_obligation(obligation)
        if (
            not obligation.retired
            and obligation.release_succeeded
            and (not obligation.completion_required or obligation.completion_observed)
            and (not obligation.lifecycle_required or obligation.lifecycle_observed)
        ):
            registration = obligation._registration
            if registration is None or obligation.release_submission is None:
                raise AuditIntegrityError("EXECUTE retirement lost actual release registration")
            obligation._retirement = _ExecutionRetirement(registration, obligation.release_submission)
            obligation.retired = True
            del self._pending[id(obligation)]

    def _retain_failure(self, original: BaseException) -> None:
        if all(original is not earlier for earlier in self._failures):
            self._failures.append(original)
        self._sealed = True

    def record_failure(self, original: BaseException) -> None:
        with self._lock:
            self._retain_failure(original)
            schedule = not self._recovery_requested
            self._recovery_requested = True
        if schedule:
            try:
                self.loop.call_soon_threadsafe(self.recovery.request_shutdown)
            except BaseException as scheduling_failure:
                with self._lock:
                    self._retain_failure(scheduling_failure)

    def assert_completed(self) -> None:
        executor_joined = self.executor_join_succeeded
        with self._lock:
            if (
                not self._sealed
                or self._pending
                or self._failures
                or (self._executor_finalizer is not None and not executor_joined)
                or (self._executor_allocation_declared and self._executor_finalizer is None)
            ):
                failure = AuditIntegrityError("EXECUTE lifecycle has unresolved or failed cleanup")
                if self._failures:
                    raise BaseExceptionGroup("EXECUTE lifecycle retained cleanup failures", [failure, *self._failures])
                raise failure

    def joined_pipeline_submission_failures(self) -> tuple[ExecutionAcquisitionObligation, ...]:
        if not self.executor_join_physically_observed:
            raise AuditIntegrityError("EXECUTE missing pipeline Future still has physical executor custody")
        with self._lock:
            return tuple(
                obligation
                for obligation in self._pending.values()
                if obligation.pipeline_submission_unknown is not None
                and obligation.pipeline is None
                and obligation.completion is None
                and obligation.completion_task is None
                and not obligation.unknown_pipeline_cleanup_declared
            )

    def observe_ready(self) -> tuple[BaseException, ...]:
        """Observe independent owners; failure does not erase unknown custody."""
        with self._lock:
            obligations = tuple(self._pending.values())
        for obligation in obligations:
            for phase in _ExecutionObservationPhase:
                if phase in obligation.observation_failures:
                    continue
                try:
                    if phase is _ExecutionObservationPhase.ACQUISITION:
                        acquisition = obligation.acquire_submission
                        reservation = acquisition.reservation
                        if (
                            reservation is not None
                            and reservation.released
                            and acquisition.future is None
                            and reservation.submission_error is not None
                            and not acquisition.observed
                            and (reservation.witness.snapshot().valid_aborted_exit or obligation.generation.joined.is_set())
                        ):
                            acquisition.observe_aborted_submission(_EXECUTION_SQL_BRIDGE_ISSUER_SEAL, reservation.submission_error)
                        if acquisition.future is not None and acquisition.future.done() and not acquisition.observed:
                            acquisition.observe_future(acquisition.future)
                    elif phase is _ExecutionObservationPhase.RELEASE:
                        release = obligation.release_submission
                        if release is not None and release.future is not None and release.future.done() and not release.observed:
                            release.observe_future(release.future)
                    elif phase is _ExecutionObservationPhase.COMPLETION:
                        obligation.observe_completion()
                    else:
                        obligation.observe_lifecycle()
                except BaseException as original:
                    # Bounded one original per immutable phase proof failure;
                    # leave that owner's pending receipt unobserved. Never
                    # regenerate errors on every census or skip other phases.
                    obligation.observation_failures[phase] = original
                    self.record_failure(original)
        with self._lock:
            return tuple(self._failures)

    def has_pending_physical_owners(self) -> bool:
        with self._lock:
            return bool(self._pending)

    async def join_all(self) -> None:
        """Keep observing independent actual owners even if one remains unknown."""
        cancellations: list[asyncio.CancelledError] = []
        from elspeth.web.async_workers import _raise_lifecycle_originals, _retain_cancellation

        while True:
            self.observe_ready()
            with self._lock:
                pending = bool(self._pending)
                failures = list(self._failures)
            if not pending:
                _raise_lifecycle_originals(cancellations, failures)
                return
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError as original:
                _retain_cancellation(cancellations, original)
