"""The live V2 owner: real phase actions, Stop and verified disposition.

Selected DATA never replaces current SQL authority. Every action enters its
source owner again; every disposition uses the existing transaction body.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Coroutine
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.advisor_output import ReadContinuationAction
from elspeth.web.composer.continuation import AdvisorFrame, ComposeFrame, EndFrame, InitializeFrame
from elspeth.web.composer.continuation_action_fit_yield import ActionFitSuccessorPlan, plan_action_fit_successor
from elspeth.web.composer.continuation_checkpoint_end_dispositions import (
    EndCheckpointNoSdkTerminal,
    EndCheckpointPaidOverflowTerminal,
    EndCheckpointUnboundRefusalTerminal,
)
from elspeth.web.composer.continuation_checkpoint_end_terminal import EndCheckpointPaidIncompleteTerminal
from elspeth.web.composer.continuation_clock import ContinuationActionClock, ContinuationTimingObservation, SegmentFit, admit_action
from elspeth.web.composer.continuation_composition_initialization import initialize_continuation_composition
from elspeth.web.composer.continuation_control_transitions import StoredContinuationControlIntent, read_pending_control_on_connection
from elspeth.web.composer.continuation_env_inventory import read_disposition_env_names
from elspeth.web.composer.continuation_initialization import _registered, adopt_recompose_original, prepare_pretitle_bootstrap
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_next_action import (
    _has_committed_primary,
    _is_first_compose_iteration,
    is_consumed_semantic_stop_cut,
    iteration_stage_action_policy,
    next_continuation_action_policy,
)
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy
from elspeth.web.composer.continuation_primary import run_continuation_primary
from elspeth.web.composer.continuation_progress import decode_prescribed_continuation_action
from elspeth.web.composer.continuation_selection import SelectedContinuationState, load_selected_continuation
from elspeth.web.composer.continuation_semantic_advisor_phase import (
    SemanticAdvisorNoSdkTerminal,
    SemanticAdvisorPaidIncompleteTerminal,
    SemanticAdvisorPaidTerminal,
    run_continuation_semantic_advisor,
    run_prescribed_semantic_read,
)
from elspeth.web.composer.continuation_semantic_advisor_return import (
    ObservedClassifierReplyDeadline,
    require_original_classifier_reply_deadline,
)
from elspeth.web.composer.continuation_semantic_advisor_review import review_prescribed_continuation_end
from elspeth.web.composer.continuation_title_application import apply_current_title_success
from elspeth.web.composer.continuation_title_provider import (
    TitleNoSdkTerminalResult,
    TitlePaidIncompleteResult,
    prepare_current_title_provider,
    run_prepared_title_provider,
    settle_current_title_result,
)
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer_watch_reads import ComposerOperationWatchReader
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.coordination.lifecycle import ContinuationStage, SessionOperationLease
from elspeth.web.required_work import (
    ComposerFailureReceipt,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
    RequiredWorkSource,
    RequiredWorkTicket,
    reduce_composer_failures,
)
from elspeth.web.sessions.composer_app_services import ComposerAppServices
from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error, request_cancelled_error, worker_lost_error
from elspeth.web.sessions.composer_operations import (
    COMPOSER_CANCEL_REQUESTED,
    COMPOSER_DEADLINE,
    COMPOSER_LEASE_LOST,
    COMPOSER_SHUTDOWN,
    ComposerOperationCancelledDuringTurn,
    ComposerOperationError,
    ComposerOperationFenceLost,
    ComposerOperationRecord,
    ComposerOperationRunning,
)
from elspeth.web.sessions.continuation import (
    ComposerSegmentRecoveryReceipt,
    ComposerSegmentSuspendObservation,
    ComposerSegmentYieldReceipt,
    StoredContinuationSnapshot,
    V3UnboundedTailRefusal,
    _require_current_continuation_running,
    read_v3_retained_tail_refusal_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_end_retry import (
    VerifiedEndFormatRetry,
    read_retry_ready_action_source_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_no_sdk_terminal import (
    VerifiedCheckpointNoSdkTerminal,
    read_v4_checkpoint_no_sdk_terminal_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_paid_terminal import (
    VerifiedCheckpointPaidOverflowTerminal,
    read_v4_checkpoint_paid_incomplete_verified_on_connection,
    read_v4_checkpoint_paid_overflow_terminal_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_unbound_refusal import (
    VerifiedCheckpointUnboundRefusal,
    read_v4_checkpoint_unbound_refusal_on_connection,
)
from elspeth.web.sessions.continuation_classifier_primary import (
    VerifiedClassifierPaidIncompleteTerminal,
    VerifiedClassifierPaidOverflowTerminal,
)
from elspeth.web.sessions.continuation_classifier_primary_incomplete_terminal import (
    read_v4_classifier_paid_incomplete_terminal_on_connection,
)
from elspeth.web.sessions.continuation_classifier_primary_no_sdk import (
    ClassifierNoSdkRefusalReceipt,
    read_classifier_no_sdk_refusal_on_connection,
)
from elspeth.web.sessions.continuation_classifier_primary_terminal import (
    VerifiedClassifierPaidOverflowCold,
    read_v4_classifier_paid_overflow_terminal_on_connection,
)
from elspeth.web.sessions.continuation_end import enter_continuation_end, primary_issued_no_tools, publish_semantic_continuation_result
from elspeth.web.sessions.continuation_ingress import ContinuationIngressCheckpoint
from elspeth.web.sessions.continuation_iteration_return import (
    IterationStageData,
    read_iteration_stage_on_connection,
    retain_iteration_reply_deadline_ready,
)
from elspeth.web.sessions.continuation_next_iteration import advance_continuation_iteration, is_applied_batch_cut
from elspeth.web.sessions.continuation_paid_incomplete_terminal import VerifiedPaidIncompleteTerminal
from elspeth.web.sessions.continuation_primary import (
    PrimaryNoSdkRefusalReceipt,
    PrimaryPaidIncompleteTerminal,
    PrimaryPaidOverflowTerminal,
    PrimaryProviderResult,
)
from elspeth.web.sessions.continuation_primary_paid_incomplete_terminal import read_v4_primary_paid_incomplete_verified_on_connection
from elspeth.web.sessions.continuation_primary_paid_overflow_terminal import (
    VerifiedPrimaryPaidOverflowTerminal,
    read_v4_primary_paid_overflow_terminal_on_connection,
)
from elspeth.web.sessions.continuation_progress_owner import ContinuationProgressOwner
from elspeth.web.sessions.continuation_sdk_tail import SdkTerminalFamily
from elspeth.web.sessions.continuation_sdk_terminal_cold import read_v4_sdk_terminal_verified_on_connection
from elspeth.web.sessions.continuation_sdk_terminal_result import (
    PrimarySdkTerminal,
    SdkTerminalFailure,
    SdkTerminalResult,
    VerifiedSdkTerminal,
)
from elspeth.web.sessions.continuation_semantic_paid_incomplete_terminal import read_v4_semantic_paid_incomplete_terminal_on_connection
from elspeth.web.sessions.continuation_semantic_paid_overflow_terminal import (
    VerifiedSemanticPaidOverflowTerminal,
    read_v4_semantic_paid_overflow_terminal_on_connection,
)
from elspeth.web.sessions.continuation_title_no_sdk_terminal import (
    VerifiedTitleNoSdkTerminal,
    read_v4_title_no_sdk_terminal_on_connection,
)
from elspeth.web.sessions.continuation_title_paid_incomplete_terminal import read_v4_title_paid_incomplete_terminal_on_connection
from elspeth.web.sessions.continuation_title_paid_overflow_terminal import (
    VerifiedTitlePaidOverflowTerminal,
    read_v4_title_paid_overflow_terminal_on_connection,
)
from elspeth.web.sessions.continuation_tool_failure import ContinuationToolFailureRead, RetainedToolFailureSource
from elspeth.web.sessions.continuation_tool_no_execution import ToolNoExecutionRefusal, read_v3_tool_no_execution_refusal_on_connection
from elspeth.web.sessions.locking import locked_session_transaction, session_data_read_transaction
from elspeth.web.sessions.schemas import SendMessageRequest

if TYPE_CHECKING:
    from elspeth.web.sessions.composer_async_worker import ComposerAsyncWorker
    from elspeth.web.sessions.continuation_tool_failure import CommittedContinuationFailure


@dataclass(frozen=True, slots=True)
class ContinuationSegmentYielded:
    receipt: ComposerSegmentYieldReceipt


@dataclass(frozen=True, slots=True)
class ContinuationSegmentHandedOver:
    receipt: ComposerSegmentRecoveryReceipt


@dataclass(frozen=True, slots=True)
class ContinuationSegmentSettled:
    terminal: ComposerOperationRecord


@dataclass(frozen=True, slots=True)
class _TitlePaidTerminalResult:
    verified: VerifiedTitlePaidOverflowTerminal
    names: frozenset[str]


@dataclass(frozen=True, slots=True)
class _TitlePaidIncompleteResult:
    verified: VerifiedPaidIncompleteTerminal
    names: frozenset[str]
    deferred_cancellations: tuple[asyncio.CancelledError, ...]


@dataclass(frozen=True, slots=True)
class _TitleNoSdkResult:
    verified: VerifiedTitleNoSdkTerminal
    names: frozenset[str]
    deferred_cancellations: tuple[asyncio.CancelledError, ...]


@dataclass(frozen=True, slots=True)
class _EndRetryReadyRead:
    proof: VerifiedEndFormatRetry | None


type ContinuationSegmentOutcome = ContinuationSegmentYielded | ContinuationSegmentHandedOver | ContinuationSegmentSettled
type _ActionResult = (
    ComposerOperationRecord
    | _TitlePaidTerminalResult
    | _TitlePaidIncompleteResult
    | _TitleNoSdkResult
    | SemanticAdvisorPaidTerminal
    | SemanticAdvisorPaidIncompleteTerminal
    | EndCheckpointPaidIncompleteTerminal
    | EndCheckpointPaidOverflowTerminal
    | EndCheckpointNoSdkTerminal
    | EndCheckpointUnboundRefusalTerminal
    | VerifiedClassifierPaidIncompleteTerminal
    | VerifiedClassifierPaidOverflowTerminal
    | ClassifierNoSdkRefusalReceipt
    | PrimaryNoSdkRefusalReceipt
    | PrimaryPaidOverflowTerminal
    | PrimaryPaidIncompleteTerminal
    | PrimarySdkTerminal
    | SdkTerminalResult
    | SdkTerminalFailure
    | SemanticAdvisorNoSdkTerminal
    | V3UnboundedTailRefusal
    | ToolNoExecutionRefusal
    | ObservedClassifierReplyDeadline
    | None
)


@dataclass(slots=True)
class _ActionExpiry:
    phases: ContinuationLivePhases
    producer: RequiredWorkTicket
    signal: RequiredWorkTicket
    deadline: float
    task: asyncio.Task[_ActionResult] | None = None
    timer: asyncio.TimerHandle | None = None
    completed_at: float | None = None
    expired: bool = False
    interrupted: bool = False
    cause: TimeoutError = field(default_factory=lambda: TimeoutError("Continuation action exceeded its original allowance"))
    callback_errors: list[BaseException] = field(default_factory=list)

    def _interrupt(self, *, deadline: bool = True) -> None:
        self.interrupted = True
        reason = COMPOSER_DEADLINE if deadline else COMPOSER_LEASE_LOST
        try:
            self.phases.cancel_business(reason)
        except BaseException as error:
            self.callback_errors.append(error)
        finally:
            task = self.task
            if task is not None and not task.done():
                try:
                    task.cancel(reason)
                except BaseException as error:
                    self.callback_errors.append(error)

    def fire(self) -> None:
        if self.expired or (self.completed_at is not None and self.completed_at <= self.deadline):
            return
        if self.completed_at is None and time.monotonic() < self.deadline:
            self.arm()
            return
        self.expired = True
        try:
            self.signal.complete_owned(self.cause)
        except BaseException as error:
            self.callback_errors.append(error)
        if self.completed_at is None:
            self._interrupt()

    def arm(self) -> None:
        try:
            self.timer = asyncio.get_running_loop().call_later(max(0.0, self.deadline - time.monotonic()), self.fire)
        except BaseException as error:
            self.callback_errors.append(error)
            self._interrupt(deadline=False)

    def close(self, original: BaseException | None, *, observe_expiry: bool = True) -> tuple[BaseException, ...]:
        if self.timer is not None:
            try:
                self.timer.cancel()
            except BaseException as error:
                self.callback_errors.append(error)
        if observe_expiry and self.completed_at is not None and self.completed_at > self.deadline:
            self.fire()
        failures = tuple(self.callback_errors)
        body_error = original
        if failures:
            body_error = BaseExceptionGroup(
                "Continuation action observation failed", ([original] if original is not None else []) + list(failures)
            )
        try:
            self.producer.complete_owned(body_error)
        except BaseException as error:
            failures = (*failures, error)
        if not self.signal.complete:
            try:
                self.signal.complete_owned(self.cause if self.expired else None)
            except BaseException as error:
                failures = (*failures, error)
        return failures


@dataclass(frozen=True, slots=True)
class _HandledReplyDeadline:
    """A committed ready source handles only this original deadline signal."""

    signal: RequiredWorkTicket
    cause: TimeoutError
    observed: ObservedClassifierReplyDeadline
    ready: StoredContinuationSnapshot

    def handles(self, receipt: ComposerFailureReceipt) -> bool:
        return (
            receipt.key == self.signal.key
            and receipt.original_root is self.cause
            and len(receipt.original_category_witnesses) == 1
            and receipt.original_category_witnesses[0] is self.cause
        )


class _SegmentDriver:
    def __init__(
        self, worker: ComposerAsyncWorker, services: ComposerAppServices, running: ComposerOperationRunning, lease: SessionOperationLease
    ):
        if type(services.composer_service) is not ComposerServiceImpl or type(lease.required_work) is not RequiredWorkCoordinator:
            raise AuditIntegrityError("V2 driver requires its actual Composer and required-work owners")
        if lease.context is not running.session_operation_context:
            raise AuditIntegrityError("V2 driver changed its exact started context")
        self.worker, self.services, self.running, self.lease = worker, services, running, lease
        self.service = services.composer_service
        self.authority = services.composer_operation_authority
        self.work = RequiredWorkBinding(lease.required_work, 0, 0, RequiredWorkRole.TURN, running)
        self.provider = ProviderInvocationOwner(service=services.session_service, required_work=self.work)
        self.phases = ContinuationLivePhases(self.service, self.authority, self.work, self.provider, lease)
        sql_deadline = lease.required_work.sql_deadline_owner
        if sql_deadline is None:
            raise AuditIntegrityError("V2 driver lost its actual SQL timing owner")
        self.sql_deadline = sql_deadline
        self._consumed_native: list[BaseException] = []
        self._committed_failure: CommittedContinuationFailure | None = None
        self._action_ordinal = 0
        self.progress_owner: ContinuationProgressOwner | None = None
        self._progress_cleanup_failures: list[BaseException] = []
        self._iteration_stage: IterationStageData | None = None
        self._action_clock: ContinuationActionClock | None = None
        self._handled_reply_deadlines: list[_HandledReplyDeadline] = []

    async def start_progress(self, record: ComposerOperationRecord) -> None:
        if self.progress_owner is not None:
            return
        if self.phases.runtime is not None:
            raise AuditIntegrityError("segment progress cannot replace an already bound runtime sink")
        owner = ContinuationProgressOwner(
            registry=self.services.progress_registry,
            running=self.running,
            lease=self.lease,
            record=record,
            renew_interval_seconds=self.services.progress_registry.renew_interval_seconds,
            hard_expires_at=self.sql_deadline.hard_expires_at,
            drain_deadline=lambda: self.worker.drain_deadline,
        )
        # Retain partial start custody before its first await so the outer
        # physical cleanup always joins this exact owner.
        self.progress_owner = owner
        self.phases.progress = owner.sink
        await owner.start()

    async def close_progress(self, *, retain_failure: bool = False) -> None:
        if self.progress_owner is not None:
            try:
                await self.progress_owner.close()
            except BaseException as error:
                if not retain_failure:
                    raise
                if not any(error is previous for previous in self._progress_cleanup_failures):
                    self._progress_cleanup_failures.append(error)

    def join_failure(self, error: BaseException, *additional: BaseException) -> BaseException:
        from elspeth.web.composer.continuation_tool_failure import combine_failure_objects
        from elspeth.web.required_sql_outcomes import RequiredSQLRaised

        values = list(self._consumed_native)
        custody = self.phases.failure_custody
        if custody is not None:
            if custody.original is not None:
                values.append(custody.original)
            if custody.wrapper is not None:
                values.append(custody.wrapper)
            values.extend(custody.errors)
            if custody.observation.feedback is not None and (custody.original is not None or custody.errors):
                values.append(custody.observation.feedback)
        observation = None if self._committed_failure is None else self._committed_failure.observation
        while observation is not None:
            values.extend(observation.original_errors)
            values.extend(observation.deferred_cancellations)
            for actual in observation.physical:
                if type(actual) is RequiredSQLRaised:
                    values.append(actual.error)
                values.extend(actual.deferred_cancellations)
            observation = observation.prior
        values.extend((*additional, error))
        return combine_failure_objects(values[0], *values[1:])

    async def record(self) -> ComposerOperationRecord:
        def read_record() -> ComposerOperationRecord:
            record = self.authority.get_for_turn(session_id=self.running.claim.session_id, operation_id=self.running.claim.operation_id)
            if record is None:
                raise ComposerOperationFenceLost(
                    session_id=self.running.claim.session_id,
                    operation_id=self.running.claim.operation_id,
                    attempt=self.running.claim.attempt,
                )
            return record

        record = await _registered(
            work=self.work,
            source=RequiredWorkSource.CONTINUATION_DISPOSITION_READ_SQL,
            operation=read_record,
            expected=ComposerOperationRecord,
        )
        fence = self.running.session_operation_context.fence
        if (
            record.attempt,
            record.current_segment_sequence,
            record.ownership_generation,
            record.session_operation_id,
            record.session_operation_epoch,
        ) != (
            self.running.claim.attempt,
            self.running.claim.segment_sequence,
            self.running.claim.ownership_generation,
            fence.operation_id,
            fence.operation_epoch,
        ):
            raise ComposerOperationFenceLost(
                session_id=record.session_id, operation_id=record.operation_id, attempt=self.running.claim.attempt
            )
        return record

    async def selected(self) -> tuple[StoredContinuationSnapshot, SelectedContinuationState]:
        stored = await _registered(
            work=self.work,
            source=RequiredWorkSource.CONTINUATION_READ_SQL,
            operation=partial(self.authority.load_continuation_snapshot, self.running),
            expected=StoredContinuationSnapshot,
        )
        selected = await load_selected_continuation(
            authority=self.authority, running=self.running, selected=stored, required_work=self.work
        )
        return stored, selected

    async def consume_sdk_terminal(self, result: PrimarySdkTerminal | SdkTerminalResult | SdkTerminalFailure) -> ContinuationSegmentSettled:
        """Verify a committed SDK outcome before any running-only read."""
        from elspeth.web.composer.provider_sdk_terminal import require_sdk_terminal_failure
        from elspeth.web.sessions.composer_async_worker import _StartedSettlement

        original: BaseException | None = None
        expected: VerifiedSdkTerminal | None = None
        if type(result) is SdkTerminalFailure:
            require_sdk_terminal_failure(result)
            original = result.original
            expected = result.verified
            family = result.family
            names = expected.env_ref_names
            receipt, snapshot = expected.receipt, expected.terminal
        elif type(result) is SdkTerminalResult:
            expected = result.verified
            family = result.family
            names = expected.env_ref_names
            receipt, snapshot = expected.receipt, expected.terminal
        elif type(result) is PrimarySdkTerminal:
            family, names = SdkTerminalFamily.PRIMARY, frozenset()
            receipt, snapshot = result.receipt, result.snapshot
        else:
            raise AuditIntegrityError("SDK disposition lost its nominal committed result")
        if original is not None and not any(error is original for error in self._consumed_native):
            self._consumed_native.append(original)

        def verify() -> VerifiedSdkTerminal:
            with locked_session_transaction(self.authority._engine, str(self.running.claim.session_id)) as conn:
                actual = read_v4_sdk_terminal_verified_on_connection(
                    conn,
                    session_id=self.running.claim.session_id,
                    operation_id=self.running.claim.operation_id,
                    effect_key=receipt.identity.effect_key,
                    family=family,
                    names=names,
                )
            context = self.running.session_operation_context
            if (
                actual is None
                or (expected is not None and actual != expected)
                or actual.receipt != receipt
                or actual.terminal != snapshot
                or actual.provider_attempt_id != receipt.identity.attempt_id
                or actual.hold_state != "consumed"
                or actual.source.session_id != str(self.running.claim.session_id)
                or actual.source.operation_id != self.running.claim.operation_id
                or actual.source.creator_sequence != self.running.claim.segment_sequence
                or actual.source.creator_generation != self.running.claim.ownership_generation
                or actual.source.claim_attempt != self.running.claim.attempt
                or actual.source.session_operation_id != context.fence.operation_id
                or actual.source.session_operation_epoch != context.fence.operation_epoch
            ):
                raise AuditIntegrityError("SDK disposition lost its exact original cold terminal and current owner")
            return actual

        self.sql_deadline.begin_settlement()
        try:
            await _registered(
                work=self.work,
                source=RequiredWorkSource.CONTINUATION_DISPOSITION_READ_SQL,
                operation=verify,
                expected=VerifiedSdkTerminal,
            )
            terminal = await self.record()
            if terminal.status != "failed" or (terminal.checkpoint_id, terminal.journal_cursor) != (
                snapshot.checkpoint_id,
                snapshot.cursor,
            ):
                raise AuditIntegrityError("SDK disposition lost its actual committed failed parent")
            await self.worker._verify_terminal_before_release(
                self.running,
                self.lease,
                terminal,
                _StartedSettlement(terminal_receipt=terminal, deferred_cancellations=list(result.deferred_cancellations)),
            )
        except BaseException as error:
            if original is None:
                raise
            combined = self.join_failure(error)
            raise combined from combined.__cause__
        if original is not None:
            raise original from original.__cause__
        return ContinuationSegmentSettled(terminal)

    async def settle(
        self,
        record: ComposerOperationRecord,
        original: BaseException | None = None,
        *,
        expired_failure: ComposerOperationError | None = None,
    ) -> ContinuationSegmentSettled:
        from elspeth.web.composer.continuation_tool_failure import combine_failure_objects
        from elspeth.web.sessions.composer_async_worker import _StartedSettlement, _terminal_completion_unknown

        if original is not None and not any(original is item for item in self._consumed_native):
            self._consumed_native.append(original)
        self.sql_deadline.begin_settlement()
        try:
            names = await read_disposition_env_names(
                advisor=self.service._advisor_checkpoint, authority=self.authority, required_work=self.work
            )
            failure_read = await self.read_failure(names)
        except BaseException as error:
            combined = self.join_failure(error)
            if combined is error:
                raise
            raise combined from combined.__cause__
        source, custody = failure_read.source, self.phases.failure_custody
        p4 = self.phases.p4
        p4_writer_observation = None if p4 is None else p4.writer_observation
        from elspeth.web.sessions.continuation_p4 import terminal_service_failure
        from elspeth.web.sessions.continuation_p4_application_store import StoredTerminalP4Application, StoredTerminalP4Preparation
        from elspeth.web.sessions.service import ComposerTerminalSQLCompletionUnknown

        if type(source) is StoredTerminalP4Preparation:
            unresolved = ComposerTerminalSQLCompletionUnknown("terminal P4 preparation has no actual committed result")
            combined = self.join_failure(unresolved)
            raise combined from combined.__cause__
        if type(source) is StoredTerminalP4Application:
            failure = (
                request_cancelled_error(request_id=record.request_id)
                if record.cancel_requested_at is not None
                else project_composer_operation_error(original, request_id=record.request_id)
                if original is not None
                else terminal_service_failure(source, request_id=record.request_id)
            )
            terminal = await _registered(
                work=self.work,
                source=RequiredWorkSource.CONTINUATION_STATE_SQL,
                operation=partial(
                    self.authority.settle_continuation_owner,
                    self.running,
                    outcome=failure,
                    env_ref_names=names,
                    deadline=self.worker.drain_deadline,
                ),
                expected=ComposerOperationRecord,
            )
            await self.worker._verify_terminal_before_release(
                self.running, self.lease, terminal, _StartedSettlement(terminal_receipt=terminal)
            )
            return ContinuationSegmentSettled(terminal)
        if source is not None and (not isinstance(source, RetainedToolFailureSource) or type(source) is not RetainedToolFailureSource):
            raise AuditIntegrityError("continuation settlement lost its closed failure source owner")
        owns_failure = source is not None or (custody is not None and (custody.completed or original is not None))
        if owns_failure:
            if source is not None and not source.applied:
                try:
                    await self.phases.finish_owed_prefix(source.selected)
                except BaseException as error:
                    if _terminal_completion_unknown(error):
                        combined = self.join_failure(error)
                        if combined is error:
                            raise
                        raise combined from combined.__cause__
                    original = error if original is None else combine_failure_objects(original, error)
            # Re-enter the current source in the terminal SQL body. No earlier
            # source DTO, native pointer or cancellation flag grants a write.
            try:
                projection = self.work.coordinator.reserve(RequiredWorkSource.TERMINAL_FAILURE_PROJECTION)
                committed = await self.services.session_service.fail_continuation_tool_operation(
                    self.running,
                    required_work=self.work.coordinator,
                    failure_projection_work=projection,
                    env_ref_names=names,
                    original=original,
                    custody=custody,
                    p4_writer_observation=p4_writer_observation,
                    p4_live_owner=p4,
                )
            except BaseException as error:
                combined = self.join_failure(error)
                if combined is error:
                    raise
                raise combined from combined.__cause__
            self._committed_failure = committed
            settlement = _StartedSettlement(
                terminal_receipt=committed.terminal, deferred_cancellations=list(committed.observation.deferred_cancellations)
            )
            try:
                await self.worker._verify_terminal_before_release(self.running, self.lease, committed.terminal, settlement)
            except BaseException as error:
                combined = self.join_failure(error)
                if combined is error:
                    raise
                raise combined from combined.__cause__
            return ContinuationSegmentSettled(committed.terminal)
        if record.cancel_requested_at is not None:
            _stored, selected = await self.selected()
            await self.phases.finish_owed_prefix(selected)
            failure = request_cancelled_error(request_id=record.request_id)
        elif expired_failure is not None:
            failure = expired_failure
        elif original is None or isinstance(original, asyncio.CancelledError):
            failure = worker_lost_error(request_id=record.request_id)
        else:
            failure = project_composer_operation_error(original, request_id=record.request_id)
        terminal = await _registered(
            work=self.work,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL,
            operation=partial(
                self.authority.settle_continuation_owner,
                self.running,
                outcome=failure,
                env_ref_names=names,
                deadline=self.worker.drain_deadline,
            ),
            expected=ComposerOperationRecord,
        )
        await self.worker._verify_terminal_before_release(self.running, self.lease, terminal, _StartedSettlement(terminal_receipt=terminal))
        return ContinuationSegmentSettled(terminal)

    async def settle_expired(self, original: BaseException | None, *, timeout_seconds: float) -> ContinuationSegmentSettled:
        from elspeth.web.sessions.composer_async_worker import _StartedSettlement

        self.sql_deadline.begin_settlement()
        current = await self.record()
        if current.status in ("completed", "failed"):
            # The committing SQL owner wins over delayed caller delivery. Its
            # staged candidate or a merely finished Future is insufficient.
            await self.worker._verify_terminal_before_release(
                self.running, self.lease, current, _StartedSettlement(terminal_receipt=current)
            )
            return ContinuationSegmentSettled(current)
        receipts = tuple(
            receipt
            for receipt in self.work.coordinator.settlement_failure_receipts()
            if not any(handled.handles(receipt) for handled in self._handled_reply_deadlines)
        )
        reduction = reduce_composer_failures(receipts)
        failure = reduction.project(request_id=current.request_id, timeout_seconds=timeout_seconds)
        return await self.settle(current, original, expired_failure=failure)

    async def read_failure(self, names: frozenset[str]) -> ContinuationToolFailureRead:
        try:
            return await _registered(
                work=self.work,
                source=RequiredWorkSource.CONTINUATION_DISPOSITION_READ_SQL,
                operation=lambda: ContinuationToolFailureRead(
                    self.authority.read_continuation_tool_failure(self.running, env_ref_names=names)
                ),
                expected=ContinuationToolFailureRead,
            )
        except BaseException as error:
            custody = self.phases.failure_custody
            if custody is not None and custody.completed:
                combined = self.join_failure(error)
                if combined is error:
                    raise
                raise combined from combined.__cause__
            raise

    async def retain_reply_deadline(
        self,
        observed: ObservedClassifierReplyDeadline,
        *,
        expiry: _ActionExpiry,
        stage: ContinuationStage[_ActionResult],
        clock: ContinuationActionClock,
    ) -> None:
        """Retain only the original returned no-SDK boundary after its stage joins."""
        if (
            type(observed) is not ObservedClassifierReplyDeadline
            or expiry.interrupted
            or expiry.callback_errors
            or expiry.completed_at is None
            or stage.original is not None
            or not stage.returned
            or stage.task is not expiry.task
            or stage.task is None
            or not stage.task.done()
            or stage.task.result() is not observed
            or self.phases.cancellation_requested.is_set()
            or self._action_clock is not clock
            or not expiry.producer.complete
            or not expiry.signal.complete
            or any(handled.signal is expiry.signal for handled in self._handled_reply_deadlines)
        ):
            raise AuditIntegrityError("reply deadline disposition lost its original joined action")
        original = require_original_classifier_reply_deadline(observed, phases=self.phases, running=self.running, action_clock=clock)
        sampled = original.token.observation.observed_monotonic
        if sampled is None or sampled > expiry.completed_at:
            raise AuditIntegrityError("reply deadline observation followed its original producer completion")
        self.sql_deadline.require_action_clock(clock)
        self.lease.raise_if_lost()
        self.sql_deadline.begin_settlement()
        ready = await _registered(
            work=self.work,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL,
            operation=partial(
                retain_iteration_reply_deadline_ready,
                self.authority,
                self.running,
                selected=original.selected,
                inventory=original.inventory,
                observed=observed,
                action_clock=clock,
                phases=self.phases,
            ),
            expected=StoredContinuationSnapshot,
        )
        if expiry.expired:
            handled = _HandledReplyDeadline(expiry.signal, expiry.cause, observed, ready)
            if not any(handled.handles(receipt) for receipt in expiry.signal.receipts()):
                raise AuditIntegrityError("reply deadline disposition lost its original expiry cause")
            self._handled_reply_deadlines.append(handled)

    async def consume_tool_refusal(self, expected: ToolNoExecutionRefusal) -> ContinuationSegmentSettled:
        from elspeth.web.sessions.composer_async_worker import _StartedSettlement

        observed = self.phases.tool_refusal
        if (
            observed is None
            or observed.receipt != expected
            or observed.inventory.running is not self.running
            or observed.inventory.selection.checkpoint_id != expected.source_checkpoint_id
            or observed.inventory.selection.cursor != expected.source_cursor
        ):
            raise AuditIntegrityError("V3 tool refusal lost its original committed observation and inventory")

        def verify_tool_refusal() -> ToolNoExecutionRefusal:
            with locked_session_transaction(self.authority._engine, str(self.running.claim.session_id)) as conn:
                actual = read_v3_tool_no_execution_refusal_on_connection(
                    conn,
                    session_id=self.running.claim.session_id,
                    operation_id=self.running.claim.operation_id,
                    effect_key=expected.effect_key,
                    env_ref_names=observed.inventory.names,
                )
            if actual is None or actual != expected:
                raise AuditIntegrityError("V3 tool refusal lost its exact original cold source")
            if (
                actual.session_id != str(self.running.claim.session_id)
                or actual.operation_id != self.running.claim.operation_id
                or actual.creator_sequence != self.running.claim.segment_sequence
                or actual.creator_generation != self.running.claim.ownership_generation
                or actual.claim_attempt != self.running.claim.attempt
                or actual.operation_epoch != self.running.session_operation_context.fence.operation_epoch
            ):
                raise AuditIntegrityError("V3 tool refusal changed its actual original claim")
            return actual

        self.sql_deadline.begin_settlement()
        await _registered(
            work=self.work,
            source=RequiredWorkSource.CONTINUATION_DISPOSITION_READ_SQL,
            operation=verify_tool_refusal,
            expected=ToolNoExecutionRefusal,
        )
        terminal = await self.record()
        if terminal.status != "failed" or terminal.journal_cursor != expected.source_cursor + 2:
            raise AuditIntegrityError("V3 tool refusal lost its actual committed failed terminal")
        await self.worker._verify_terminal_before_release(self.running, self.lease, terminal, _StartedSettlement(terminal_receipt=terminal))
        return ContinuationSegmentSettled(terminal)

    async def consume_preview_terminal(self) -> ContinuationSegmentSettled:
        from hashlib import sha256

        from elspeth.web.sessions.composer_async_worker import _StartedSettlement
        from elspeth.web.sessions.continuation_preview_result import (
            PreviewValidationIncompleteTerminal,
            read_preview_validation_incomplete_on_connection,
        )

        expected = self.phases.preview_terminal
        if type(expected) is not PreviewValidationIncompleteTerminal or self.phases.latest_snapshot != expected.snapshot:
            raise AuditIntegrityError("Preview terminal lost its actual committing observation")
        source = expected.receipt.source
        fence = self.running.session_operation_context.fence
        if (source.session_id, source.operation_id, source.sequence, source.generation, source.claim_attempt) != (
            str(self.running.claim.session_id),
            self.running.claim.operation_id,
            self.running.claim.segment_sequence,
            self.running.claim.ownership_generation,
            self.running.claim.attempt,
        ) or (source.creator_operation_id, source.creator_operation_epoch, source.lease_sha256) != (
            fence.operation_id,
            fence.operation_epoch,
            sha256(fence.lease_token.encode("utf-8")).hexdigest(),
        ):
            raise AuditIntegrityError("Preview terminal changed its actual original claim")

        def verify() -> PreviewValidationIncompleteTerminal:
            with locked_session_transaction(self.authority._engine, source.session_id) as conn:
                actual = read_preview_validation_incomplete_on_connection(
                    conn,
                    session_id=source.session_id,
                    operation_id=source.operation_id,
                    effect_key=f"tool:{source.response.digest}:{source.tool_index}",
                    env_ref_names=expected.env_ref_names,
                )
            if actual is None or actual != expected:
                raise AuditIntegrityError("Preview terminal lost its exact full cold source")
            return actual

        self.sql_deadline.begin_settlement()
        await _registered(
            work=self.work,
            source=RequiredWorkSource.CONTINUATION_DISPOSITION_READ_SQL,
            operation=verify,
            expected=PreviewValidationIncompleteTerminal,
        )
        terminal = await self.record()
        if terminal.status != "failed" or (terminal.checkpoint_id, terminal.journal_cursor) != (
            expected.snapshot.checkpoint_id,
            expected.snapshot.cursor,
        ):
            raise AuditIntegrityError("Preview terminal lost its actual sealed parent")
        await self.worker._verify_terminal_before_release(self.running, self.lease, terminal, _StartedSettlement(terminal_receipt=terminal))
        return ContinuationSegmentSettled(terminal)

    async def dispose(self, original: BaseException | None = None) -> ContinuationSegmentOutcome:
        if original is not None and not any(original is item for item in self._consumed_native):
            self._consumed_native.append(original)
        await self.close_progress(retain_failure=True)
        self.sql_deadline.begin_settlement()
        record = await self.record()
        if record.status == "failed" and self.phases.preview_terminal is not None:
            return await self.consume_preview_terminal()
        if record.status == "failed" and self.phases.tool_refusal is not None:
            return await self.consume_tool_refusal(self.phases.tool_refusal.receipt)
        if record.cancel_requested_at is not None:
            return await self.settle(record, original)
        names = await read_disposition_env_names(
            advisor=self.service._advisor_checkpoint, authority=self.authority, required_work=self.work
        )
        if (await self.read_failure(names)).source is not None:
            return await self.settle(record, original)
        disposition = await _registered(
            work=self.work,
            source=RequiredWorkSource.CONTINUATION_DISPOSITION_READ_SQL,
            operation=partial(
                self.authority.classify_continuation_owner, self.running, env_ref_names=names, deadline=self.worker.drain_deadline
            ),
            expected=str,
        )
        if disposition == "unresolved":
            return await self.settle(record, original)
        if disposition != "handover":
            raise AuditIntegrityError("V2 driver has no landed cross-sequence lineage owner")
        observation = ComposerSegmentSuspendObservation(self.running)

        async def verify_handover(
            committed: ComposerSegmentRecoveryReceipt | None, original_failure: BaseException | None
        ) -> ComposerSegmentRecoveryReceipt:
            if original_failure is not None:
                # The SQL producer recorded this candidate before its commit
                # boundary. Only immutable readback may promote an ambiguous
                # raised outcome; the original remains in the SQL work ticket.
                if observation.candidate is None:
                    raise original_failure
                recovered = await _registered(
                    work=self.work,
                    source=RequiredWorkSource.CONTINUATION_HANDOFF_READBACK_SQL,
                    operation=partial(
                        self.services.session_service.session_operation_authority.read_suspend_outcome,
                        observation,
                        deadline=self.worker.drain_deadline,
                    ),
                    expected=ComposerSegmentRecoveryReceipt,
                )
                return recovered
            if (
                type(committed) is not ComposerSegmentRecoveryReceipt
                or committed is not observation.candidate
                or committed.released_context is not self.running.session_operation_context
            ):
                raise AuditIntegrityError("V2 handover lost its actual committing release receipt")

            def verify_receipt() -> ComposerSegmentRecoveryReceipt:
                self.services.session_service.session_operation_authority.verify_recovery(committed, deadline=self.worker.drain_deadline)
                return committed

            verified = await _registered(
                work=self.work,
                source=RequiredWorkSource.CONTINUATION_HANDOFF_READBACK_SQL,
                operation=verify_receipt,
                expected=ComposerSegmentRecoveryReceipt,
            )
            if verified is not committed:
                raise AuditIntegrityError("V2 handover verification replaced its actual committing receipt")
            return committed

        try:
            receipt = await self.lease.consume_continuation_handoff(
                partial(
                    self.services.session_service.session_operation_authority.suspend_composer_segment,
                    self.running,
                    observation=observation,
                    deadline=self.worker.drain_deadline,
                ),
                deadline=self.worker.drain_deadline,
                verify_outcome=verify_handover,
            )
        except ComposerOperationCancelledDuringTurn:
            return await self.settle(await self.record(), original)
        return ContinuationSegmentHandedOver(receipt)

    async def advance(self, stored: StoredContinuationSnapshot, selected: SelectedContinuationState) -> _ActionResult:
        root, top = selected.stack, selected.stack.frames[-1]
        if type(top) is InitializeFrame:
            if top.ingress_phase == "reserved":
                if root.logical.kind != "compose_message":
                    await adopt_recompose_original(service=self.service, selected=stored, required_work=self.work)
                    return None
                request = SendMessageRequest.model_validate_json(top.request_json)
                sql, projection = self.work.reserve_pair(RequiredWorkSource.INGRESS_SQL, RequiredWorkSource.INGRESS_PROJECTION)
                try:
                    ingress_result = await self.services.session_service.add_message_with_transcript(
                        self.running.claim.session_id,
                        "user",
                        request.content,
                        operation_id=UUID(self.running.claim.operation_id),
                        requested_state_id=request.state_id,
                        composition_state_id=request.state_id,
                        writer_principal="route_user_message",
                        session_operation_context=self.running.session_operation_context,
                        running=self.running,
                        required_work=sql,
                        continuation=stored,
                    )
                    if type(ingress_result) is not ContinuationIngressCheckpoint:
                        raise AuditIntegrityError("V2 ingress returned another actual owner")
                except BaseException as original:
                    projection.complete_owned(original)
                    raise
                projection.complete_owned()
                return None
            if top.ingress_phase == "inserted":
                await prepare_pretitle_bootstrap(service=self.service, authority=self.authority, selected=stored, required_work=self.work)
                return None
            if top.ingress_phase == "title_pending":
                if top.provider_effect_key is None:
                    prepared = await prepare_current_title_provider(
                        service=self.service, authority=self.authority, selected=stored, required_work=self.work
                    )
                    if isinstance(prepared, V3UnboundedTailRefusal):
                        return prepared
                    title_result = await run_prepared_title_provider(prepared=prepared, invocation_owner=self.provider)
                    if isinstance(title_result, (SdkTerminalResult, SdkTerminalFailure)):
                        return title_result
                    if type(title_result) is VerifiedTitlePaidOverflowTerminal:
                        return _TitlePaidTerminalResult(title_result, prepared.names)
                    if type(title_result) is TitlePaidIncompleteResult:
                        return _TitlePaidIncompleteResult(title_result.verified, prepared.names, title_result.deferred_cancellations)
                    if type(title_result) is TitleNoSdkTerminalResult:
                        return _TitleNoSdkResult(title_result.verified, prepared.names, title_result.deferred_cancellations)
                else:
                    current = await settle_current_title_result(service=self.service, running=self.running, required_work=self.work)
                    await apply_current_title_success(service=self.service, selected=current.selected, required_work=self.work)
                return None
            runtime = await initialize_continuation_composition(
                service=self.service,
                authority=self.authority,
                selected=stored,
                required_work=self.work,
                provider_owner=self.provider,
                progress=self.phases.progress,
            )
            self.phases.bind_initial(runtime)
            return None
        if type(top) is ComposeFrame and top.phase == "action_ready":
            if (
                (not _is_first_compose_iteration(top) or _has_committed_primary(root))
                and root.active_action_json is None
                and root.active_action_source is None
            ):
                return await run_continuation_semantic_advisor(self.phases, selected)
            if self.phases.runtime is None and _is_first_compose_iteration(top) and not _has_committed_primary(root):
                runtime = await initialize_continuation_composition(
                    service=self.service,
                    authority=self.authority,
                    selected=stored,
                    required_work=self.work,
                    provider_owner=self.provider,
                    progress=self.phases.progress,
                )
                self.phases.bind_initial(runtime)
            else:
                # Later published cuts restore their retained runtime sources;
                # first initialization keeps its original stricter contract.
                runtime, _inventory = await self.phases.current(selected)
            policy = runtime.source.policy
            # This packet owns delivery preflight only. The current SQL-backed
            # PRIMARY factory separately admits its original producer messages.
            _messages, _tools, delivery, _delivery_input = self.service._prepare_primary_delivery(
                messages=runtime.live.llm_messages,
                tools=runtime.live.tools,
                recorder=runtime.live.recorder,
                turn_evidence=runtime.live.turn_evidence,
                state=runtime.live.state,
                failed_turn=runtime.live.failed_turn,
            )
            primary_result = await run_continuation_primary(
                service=self.service,
                authority=self.authority,
                selected=runtime.source.selected,
                producers=runtime.materialization,
                messages=runtime.live.llm_messages,
                tools=runtime.live.tools,
                recorder=runtime.live.recorder,
                drained=runtime.drained,
                required_work=self.work,
                provider_owner=self.provider,
                timeout=policy.actions.primary.total,
                on_sdk_entry=self.progress_owner.notify_sdk_entry if self.progress_owner is not None else None,
            )
            if isinstance(
                primary_result,
                (
                    PrimaryNoSdkRefusalReceipt,
                    PrimaryPaidOverflowTerminal,
                    PrimaryPaidIncompleteTerminal,
                    PrimarySdkTerminal,
                    SdkTerminalFailure,
                    V3UnboundedTailRefusal,
                ),
            ):
                return primary_result
            if delivery is not None:
                runtime.live.turn_evidence.confirm_delivery(delivery, runtime.live.recorder.llm_calls)
            self.phases.accept_primary(primary_result)
            if type(primary_result) is PrimaryProviderResult:
                if len(primary_result.deferred_cancellations) == 1:
                    raise primary_result.deferred_cancellations[0]
                if primary_result.deferred_cancellations:
                    raise BaseExceptionGroup(
                        "PRIMARY complete result and original cancellations", list(primary_result.deferred_cancellations)
                    )
            return None
        if type(top) is AdvisorFrame:
            if (
                top.phase == "decision_ready"
                and root.active_action_json is not None
                and type(decode_prescribed_continuation_action(root.active_action_json)) is ReadContinuationAction
            ):
                await run_prescribed_semantic_read(self.phases, selected)
                return None
            return await run_continuation_semantic_advisor(self.phases, selected)
        if is_applied_batch_cut(selected):
            stage = self._iteration_stage
            if stage is not None and stage.current != stored:
                raise AuditIntegrityError("iteration stage changed its exact selected action source")
            if stage is not None and stage.stage == "returned":
                # This route preserves the actual applied root. The semantic
                # producer must independently replay the complete return;
                # entering the post-batch owner again would replay work.
                return await run_continuation_semantic_advisor(self.phases, selected)
            if stage is not None and stage.stage == "publication_ready":
                return await publish_semantic_continuation_result(self.phases, self.services, selected)
            return await advance_continuation_iteration(self.phases, self.services, selected, iteration_stage=stage)
        if type(top) is EndFrame:
            if type(self.sql_deadline.policy) is ContinuationAdmissionPolicyV4 and root.pending_settlement_ids:
                # The warm Review entry would replay PRIMARY over a paid H.
                # Only independently rederived first usable consequences can
                # consume this cut; unsupported H dispositions fail closed.
                from elspeth.web.composer.continuation_checkpoint_end_cold_resume import resume_first_cold_end_repair

                await resume_first_cold_end_repair(phases=self.phases, selected=selected)
                return None
            if type(self.sql_deadline.policy) is ContinuationAdmissionPolicyV4:
                from elspeth.web.composer.continuation_checkpoint_end_format_retry import restore_checkpoint_end_format_retry_source
                from elspeth.web.composer.continuation_checkpoint_end_live import run_v4_checkpoint_end_live
                from elspeth.web.composer.continuation_checkpoint_end_terminal import (
                    EndCheckpointCompleteReview,
                    EndCheckpointFormatRetryReady,
                    EndCheckpointRepairContinued,
                    observe_end_checkpoint_terminal,
                )
                from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
                from elspeth.web.composer.provider_quota import composer_quota_scope

                retry = await restore_checkpoint_end_format_retry_source(phases=self.phases, selected=selected)
                if retry is not None:
                    bindings = retry.phase_owner.runtime.bindings
                    context = bindings.session_operation_context
                    if context is None:
                        raise AuditIntegrityError("END retry lost its original current operation context")
                    with composer_quota_scope(self.service._require_sessions_service(), context), prompt_guard_user_scope(bindings.message):
                        outcome = await observe_end_checkpoint_terminal(
                            run_v4_checkpoint_end_live(
                                advisor=self.service._advisor_checkpoint,
                                phase_owner=retry.phase_owner,
                                gate_locals=retry.gate_locals,
                                deadline=bindings.deadline,
                                format_retry=retry,
                            )
                        )
                    if type(outcome) is SdkTerminalResult or type(outcome) is SdkTerminalFailure:
                        return outcome
                    if type(outcome) is EndCheckpointCompleteReview:
                        self.phases.latest_snapshot = outcome.review.completed
                        return None
                    if type(outcome) is EndCheckpointRepairContinued:
                        self.phases.latest_snapshot = outcome.completed
                        return None
                    if type(outcome) is EndCheckpointFormatRetryReady:
                        raise AuditIntegrityError("END retry cannot issue a third physical action")
                    if isinstance(
                        outcome,
                        (
                            EndCheckpointPaidIncompleteTerminal,
                            EndCheckpointPaidOverflowTerminal,
                            EndCheckpointNoSdkTerminal,
                            EndCheckpointUnboundRefusalTerminal,
                        ),
                    ):
                        return outcome
                    raise AuditIntegrityError("END retry returned an unfamiliar committed consequence")
            if root.active_action_json is not None:
                return await review_prescribed_continuation_end(self.phases, self.services, selected)
            if is_consumed_semantic_stop_cut(selected):
                return await publish_semantic_continuation_result(self.phases, self.services, selected)
            return await run_continuation_semantic_advisor(self.phases, selected)
        phase_read = None
        if type(top) is ComposeFrame and top.phase == "response_retained":
            phase_read = await self.phases._current_phase_read(selected)
        if (
            type(top) is ComposeFrame
            and top.phase == "response_retained"
            and await primary_issued_no_tools(self.phases, selected, phase_read=phase_read)
        ):
            await enter_continuation_end(self.phases, selected, phase_read=phase_read)
            return None
        return await self.service.advance_continuation_phase(
            owner=self.phases,
            authority=self.authority,
            selected=selected,
            work=self.work,
            provider_owner=self.provider,
            phase_read=phase_read,
        )

    async def pending_control(self, selected: SelectedContinuationState) -> StoredContinuationControlIntent | None:
        top = selected.stack.frames[-1]
        if type(top) is not ComposeFrame or top.phase != "action_ready" or not _has_committed_primary(selected.stack):
            return None
        _runtime, inventory = await self.phases.current(selected)

        def read() -> tuple[StoredContinuationControlIntent | None]:
            with locked_session_transaction(self.authority._engine, str(self.running.claim.session_id)) as conn:
                return (read_pending_control_on_connection(conn, self.running, selected=selected, inventory=inventory),)

        (intent,) = await _registered(work=self.work, source=RequiredWorkSource.CONTINUATION_READ_SQL, operation=read, expected=tuple)
        if intent is not None and type(intent) is not StoredContinuationControlIntent:
            raise AuditIntegrityError("model yield lost its actual durable intent DATA")
        return intent

    async def end_retry_ready(
        self, stored: StoredContinuationSnapshot, selected: SelectedContinuationState
    ) -> VerifiedEndFormatRetry | None:
        if (
            type(self.sql_deadline.policy) is not ContinuationAdmissionPolicyV4
            or type(selected.stack.frames[-1]) is not EndFrame
            or selected.stack.pending_settlement_ids
        ):
            return None

        def read() -> _EndRetryReadyRead:
            with session_data_read_transaction(self.authority._engine, str(self.running.claim.session_id)) as conn:
                _require_current_continuation_running(conn, self.running)
                return _EndRetryReadyRead(
                    read_retry_ready_action_source_on_connection(
                        conn, session_id=str(self.running.claim.session_id), operation_id=self.running.claim.operation_id, selected=stored
                    )
                )

        observed = await _registered(
            work=self.work, source=RequiredWorkSource.CONTINUATION_READ_SQL, operation=read, expected=_EndRetryReadyRead
        )
        return observed.proof

    async def iteration_stage(self, stored: StoredContinuationSnapshot, selected: SelectedContinuationState) -> IterationStageData | None:
        if not is_applied_batch_cut(selected):
            return None

        def read() -> tuple[IterationStageData | None]:
            with session_data_read_transaction(self.authority._engine, str(self.running.claim.session_id)) as conn:
                return (read_iteration_stage_on_connection(conn, self.running, selected=stored),)

        (stage,) = await _registered(work=self.work, source=RequiredWorkSource.CONTINUATION_READ_SQL, operation=read, expected=tuple)
        if stage is None:
            return None
        if not isinstance(stage, IterationStageData) or stage.current != stored:
            raise AuditIntegrityError("iteration stage read changed its exact current source DATA")
        if stage.stage in ("early_dispatched", "classify_dispatched", "end_dispatched", "reply_refusal_dispatched"):
            raise AuditIntegrityError("iteration stage cannot replay unresolved paid work")
        return stage

    async def yield_control(
        self,
        stored: StoredContinuationSnapshot,
        selected: SelectedContinuationState,
        *,
        reason: Literal["model_request", "action_fit"],
        action_fit: ActionFitSuccessorPlan | None = None,
    ) -> ContinuationSegmentOutcome:
        """Yield only after the actual phase and watcher cohorts have joined."""
        await self.close_progress(retain_failure=True)
        self.sql_deadline.begin_settlement()
        top = selected.stack.frames[-1]
        if type(top) is not ComposeFrame or top.phase != "action_ready" or not selected.stack.is_handoff_safe:
            raise AuditIntegrityError("successor yield lost its actual quiescent Compose cut")
        expected_head = None if top.current_state_id is None else UUID(top.current_state_id)
        if (reason == "model_request" and action_fit is not None) or (
            reason == "action_fit" and (type(action_fit) is not ActionFitSuccessorPlan or action_fit.expected_head != expected_head)
        ):
            raise AuditIntegrityError("successor yield lost its actual action-fit plan")
        repository = self.services.session_service.session_operation_authority

        def readback() -> tuple[ComposerSegmentYieldReceipt | None]:
            value = repository.read_yield_outcome(self.running)
            if value is not None:
                repository.verify_yield(value)
            return (value,)

        async def readback_owned() -> ComposerSegmentYieldReceipt | None:
            # The registered immutable readback retains the same original
            # hard/drain lifecycle clock after the committing owner releases.
            values = await _registered(
                work=self.work,
                source=RequiredWorkSource.CONTINUATION_HANDOFF_READBACK_SQL,
                operation=readback,
                expected=tuple,
            )
            if len(values) != 1 or (values[0] is not None and type(values[0]) is not ComposerSegmentYieldReceipt):
                raise AuditIntegrityError("model yield lost its actual immutable readback outcome")
            return values[0]

        async def verify_yield(
            committed: ComposerSegmentYieldReceipt | None, original: BaseException | None
        ) -> ComposerSegmentYieldReceipt:
            if original is not None and not any(original is item for item in self._consumed_native):
                self._consumed_native.append(original)
            observed = await readback_owned()
            if observed is None:
                if original is not None:
                    raise original
                raise AuditIntegrityError("model yield has no actual immutable committed outcome")
            if (
                (observed.session_id, observed.operation_id) != (self.running.claim.session_id, self.running.claim.operation_id)
                or (observed.predecessor_sequence, observed.predecessor_generation)
                != (self.running.claim.segment_sequence, self.running.claim.ownership_generation)
                or (observed.successor_sequence, observed.successor_generation)
                != (self.running.claim.segment_sequence + 1, self.running.claim.ownership_generation + 1)
                or observed.released_epoch != self.running.session_operation_context.fence.operation_epoch
                or observed.cursor != stored.cursor + 1
            ):
                raise AuditIntegrityError("model yield lost its actual atomic successor/release receipt")
            if committed is not None and observed != committed:
                raise AuditIntegrityError("model yield readback differs from its actual committing receipt")
            return observed if committed is None else committed

        try:
            receipt = await self.lease.consume_continuation_handoff(
                partial(
                    repository.commit_checkpoint_and_successor,
                    self.running,
                    expected_head=expected_head,
                    expected_checkpoint_id=stored.checkpoint_id,
                    expected_cursor=stored.cursor,
                    snapshot_json=stored.payload_json,
                    reason=reason,
                ),
                deadline=self.worker.drain_deadline,
                verify_outcome=verify_yield,
            )
        except ComposerOperationCancelledDuringTurn:
            return await self.settle(await self.record())
        return ContinuationSegmentYielded(receipt)

    async def watch_stage(self, stage: asyncio.Task[_ActionResult], reader: ComposerOperationWatchReader) -> None:
        key = (self.running.claim.session_id, self.running.claim.operation_id)
        stop_delivered = False
        try:
            while not stage.done():
                self.lease.raise_if_lost()
                if self.worker._local_cancels[key].is_set() and not stop_delivered:
                    self.phases.observe_stop()
                    stage.cancel(COMPOSER_CANCEL_REQUESTED)
                    stop_delivered = True
                current, _now = await reader.read()
                if current is None or (current.attempt, current.current_segment_sequence, current.ownership_generation) != (
                    self.running.claim.attempt,
                    self.running.claim.segment_sequence,
                    self.running.claim.ownership_generation,
                ):
                    self.phases.cancel_business(COMPOSER_LEASE_LOST)
                    stage.cancel(COMPOSER_LEASE_LOST)
                    return
                if current.cancel_requested_at is not None and not stop_delivered:
                    self.phases.observe_stop()
                    stage.cancel(COMPOSER_CANCEL_REQUESTED)
                    stop_delivered = True
                deadline = self.worker.drain_deadline
                if deadline is not None and time.monotonic() >= deadline.expires_at - self.sql_deadline.drain_finish_reserve_seconds:
                    self.phases.cancel_business(COMPOSER_SHUTDOWN)
                    stage.cancel(COMPOSER_SHUTDOWN)
                    return
                await reader.wait_watch_cadence()
        except asyncio.CancelledError as original:
            if reader.owns_internal_cleanup_observation(original):
                return
            self.phases.cancel_business(COMPOSER_LEASE_LOST)
            stage.cancel(COMPOSER_LEASE_LOST)
            raise
        except BaseException:
            self.phases.cancel_business(COMPOSER_LEASE_LOST)
            stage.cancel(COMPOSER_LEASE_LOST)
            raise

    def _worker_is_draining(self) -> bool:
        """Read live drain state afresh across asynchronous phase boundaries."""
        return self.worker.drain_deadline is not None

    async def run(self, adoption_failure: BaseException | None) -> ContinuationSegmentOutcome:
        from elspeth.web.sessions.composer_async_worker import _finish_operation_watcher, _OperationWatchObservation, _StartedSettlement

        if adoption_failure is not None:
            return await self.dispose(adoption_failure)
        while True:
            self.sql_deadline.begin_settlement()
            self.lease.raise_if_lost()
            if self._worker_is_draining():
                return await self.dispose()
            record = await self.record()
            if record.cancel_requested_at is not None:
                return await self.settle(record)
            names = await read_disposition_env_names(
                advisor=self.service._advisor_checkpoint, authority=self.authority, required_work=self.work
            )
            if (await self.read_failure(names)).source is not None:
                return await self.settle(record)
            if self._worker_is_draining():
                return await self.dispose()
            stored, selected = await self.selected()
            # A cold control preflight may restore and bind the runtime. Give
            # it this segment's original progress sink before that first read.
            await self.start_progress(record)
            if await self.pending_control(selected) is not None:
                return await self.yield_control(stored, selected, reason="model_request")
            policy = decode_continuation_policy(selected.stack.admission_policy_json, selected.stack.policy_hash)
            if policy != self.sql_deadline.policy:
                raise AuditIntegrityError("driver action changed its original SQL owner's admitted policy")
            # Preserve the original action objects as well as their values;
            # result-disposition facets bind that exact begun policy member.
            policy = self.sql_deadline.policy
            # This is a current DATA observation, followed by the fresh timing
            # observation and actual stage writer admission. Each paid stage
            # returns here before the next original scalar action begins.
            self._iteration_stage = await self.iteration_stage(stored, selected)
            retry_ready = await self.end_retry_ready(stored, selected)
            started_read = time.monotonic()
            timing = await _registered(
                work=self.work,
                source=RequiredWorkSource.CONTINUATION_READ_SQL,
                operation=partial(self.authority.read_continuation_timing, self.running, deadline=self.worker.drain_deadline),
                expected=ContinuationTimingObservation,
            )
            elapsed = time.monotonic() - started_read
            if is_applied_batch_cut(selected):
                action = iteration_stage_action_policy(selected=stored, stage=self._iteration_stage, policy=policy)
            else:
                action = next_continuation_action_policy(selected=selected, policy=policy, retry_ready=retry_ready)
            hard_remaining = max(0.0, timing.hard_remaining_seconds - elapsed)
            cooperative_remaining = max(0.0, timing.cooperative_remaining_seconds - elapsed)
            if (
                admit_action(
                    action,
                    policy.segment,
                    hard_remaining=hard_remaining,
                    cooperative_remaining=cooperative_remaining,
                    fresh=False,
                )
                is SegmentFit.YIELD
            ):
                if not self._worker_is_draining() and not is_applied_batch_cut(selected):
                    action_fit = plan_action_fit_successor(
                        selected=selected,
                        policy=policy,
                        action=action,
                        sequence=self.running.claim.segment_sequence,
                        hard_remaining=hard_remaining,
                    )
                    if action_fit is not None:
                        return await self.yield_control(stored, selected, reason="action_fit", action_fit=action_fit)
                return await self.dispose()
            if self._worker_is_draining():
                return await self.dispose()
            action_clock = ContinuationActionClock(action)
            self._action_clock = action_clock
            self.sql_deadline.begin_action(action, started_at=action_clock.started_monotonic)
            self.phases.bind_action_clock(action_clock)
            self._action_ordinal += 1
            producer: RequiredWorkTicket | None = None
            signal: RequiredWorkTicket | None = None
            expiry: _ActionExpiry | None = None
            stage_owner: ContinuationStage[_ActionResult] | None = None
            stage_coroutine: Coroutine[object, object, _ActionResult] | None = None
            try:
                producer = self.work.coordinator.reserve(
                    RequiredWorkSource.REQUIRED_CONTINUATION_PRODUCER,
                    transition_ordinal=selected.stack.cursor,
                    recurrence_ordinal=self._action_ordinal,
                )
                signal = self.work.coordinator.reserve(
                    RequiredWorkSource.DEADLINE_SIGNAL,
                    transition_ordinal=selected.stack.cursor,
                    recurrence_ordinal=self._action_ordinal,
                )
                now = time.monotonic()
                active_expiry = _ActionExpiry(self.phases, producer, signal, now + self.sql_deadline.remaining_dispatch(monotonic_now=now))
                expiry = active_expiry

                async def advance_recorded(
                    current_stored: StoredContinuationSnapshot = stored,
                    current_selected: SelectedContinuationState = selected,
                    current_expiry: _ActionExpiry = active_expiry,
                ) -> _ActionResult:
                    try:
                        return await self.advance(current_stored, current_selected)
                    finally:
                        current_expiry.completed_at = time.monotonic()

                stage_coroutine = advance_recorded()
                stage_owner = self.lease.create_continuation_stage(stage_coroutine, name="continuation-phase")
                stage = stage_owner.task
                if stage is None:
                    raise AuditIntegrityError("Continuation phase allocation lacks its actual Task")
                expiry.task = stage
                expiry.arm()
                reader = ComposerOperationWatchReader.for_segment(self.worker, self.running, self.lease)
                observation = _OperationWatchObservation(reader=reader)
                watch_coroutine = self.watch_stage(stage, reader)
                try:
                    watch = asyncio.create_task(watch_coroutine, name="continuation-stage-watch")
                except BaseException as watch_setup:
                    try:
                        watch_coroutine.close()
                    except BaseException as close_error:
                        raise BaseExceptionGroup("Watcher allocation and close failed", [watch_setup, close_error]) from None
                    raise
            except BaseException as setup_error:
                cleanup_errors: list[BaseException] = []
                if expiry is not None and expiry.timer is not None:
                    try:
                        expiry.timer.cancel()
                        expiry.timer = None
                    except BaseException as error:
                        cleanup_errors.append(error)
                if stage_owner is not None and stage_owner.task is not None:
                    try:
                        self.phases.cancel_business(COMPOSER_LEASE_LOST)
                    except BaseException as error:
                        cleanup_errors.append(error)
                    try:
                        stage_owner.task.cancel(COMPOSER_LEASE_LOST)
                    except BaseException as error:
                        cleanup_errors.append(error)
                    try:
                        await self.lease.join_continuation_stage(stage_owner)
                    except BaseException as error:
                        cleanup_errors.append(error)
                elif stage_coroutine is not None:
                    try:
                        stage_coroutine.close()
                    except BaseException as error:
                        cleanup_errors.append(error)
                combined = self.join_failure(setup_error, *cleanup_errors)
                if expiry is not None:
                    try:
                        cleanup_errors.extend(expiry.close(combined, observe_expiry=False))
                    except BaseException as error:
                        cleanup_errors.append(error)
                else:
                    for ticket, failure in ((producer, combined), (signal, None)):
                        if ticket is not None and not ticket.complete:
                            try:
                                ticket.complete_owned(failure)
                            except BaseException as error:
                                cleanup_errors.append(error)
                combined = self.join_failure(setup_error, *cleanup_errors)
                if combined is setup_error:
                    raise
                raise combined from combined.__cause__
            original: BaseException | None = None
            try:
                result = await self.lease.join_continuation_stage(stage_owner)
            except (SessionOperationFenceLost, ComposerOperationFenceLost) as error:
                original = error
                raise
            except BaseException as error:
                original = error
                if not any(error is item for item in self._consumed_native):
                    self._consumed_native.append(error)
                result = None
            finally:
                action_failures = expiry.close(original)
                failures = (*action_failures, *await _finish_operation_watcher(watch, observation))
                if failures:
                    raise BaseExceptionGroup(
                        "Continuation stage observation failed", ([original] if original is not None else []) + list(failures)
                    )
            if (
                type(result) is ObservedClassifierReplyDeadline
                and original is None
                and not expiry.interrupted
                and not self.phases.cancellation_requested.is_set()
                and not self._worker_is_draining()
            ):
                await self.retain_reply_deadline(result, expiry=expiry, stage=stage_owner, clock=action_clock)
                # The old action is closed. The loop re-reads current ownership
                # and independently admits the retained SQL refusal tail.
                continue
            if isinstance(result, (PrimarySdkTerminal, SdkTerminalResult, SdkTerminalFailure)):
                return await self.consume_sdk_terminal(result)
            if self.phases.preview_terminal is not None:
                return await self.consume_preview_terminal()
            if expiry.expired:
                from elspeth.web.sessions.composer_async_worker import _terminal_completion_unknown

                if original is not None and _terminal_completion_unknown(original):
                    raise original
                return await self.settle_expired(original, timeout_seconds=action.total)
            if type(result) is ObservedClassifierReplyDeadline:
                # A watcher Stop/drain observation wins over an otherwise
                # returned no-SDK boundary; do not reopen its business stage.
                return await self.dispose(original)
            if original is not None:
                from elspeth.web.sessions.composer_async_worker import _terminal_completion_unknown

                if _terminal_completion_unknown(original):
                    raise original
                if isinstance(original, asyncio.CancelledError) or self.worker.drain_deadline is not None:
                    return await self.dispose(original)
                return await self.settle(await self.record(), original)
            if result is not None:
                if type(result) is VerifiedClassifierPaidIncompleteTerminal:
                    classifier_incomplete_result = result

                    def verify_classifier_paid_incomplete(
                        expected_terminal: VerifiedClassifierPaidIncompleteTerminal = classifier_incomplete_result,
                    ) -> VerifiedPaidIncompleteTerminal:
                        with locked_session_transaction(self.authority._engine, str(self.running.claim.session_id)) as conn:
                            actual = read_v4_classifier_paid_incomplete_terminal_on_connection(
                                conn,
                                session_id=self.running.claim.session_id,
                                operation_id=self.running.claim.operation_id,
                                effect_key=expected_terminal.verified.receipt.identity.effect_key,
                            )
                        if actual is None or actual != expected_terminal.verified or actual.hold_state != "consumed":
                            raise AuditIntegrityError("Classifier paid incomplete lost its exact original cold terminal")
                        return actual

                    self.sql_deadline.begin_settlement()
                    await _registered(
                        work=self.work,
                        source=RequiredWorkSource.CONTINUATION_DISPOSITION_READ_SQL,
                        operation=verify_classifier_paid_incomplete,
                        expected=VerifiedPaidIncompleteTerminal,
                    )
                    terminal = await self.record()
                    if terminal.status != "failed" or (terminal.checkpoint_id, terminal.journal_cursor) != (
                        result.verified.terminal.checkpoint_id,
                        result.verified.terminal.cursor,
                    ):
                        raise AuditIntegrityError("Classifier paid incomplete lost its actual committed failed terminal")
                    await self.worker._verify_terminal_before_release(
                        self.running,
                        self.lease,
                        terminal,
                        _StartedSettlement(terminal_receipt=terminal, deferred_cancellations=list(result.deferred_cancellations)),
                    )
                    return ContinuationSegmentSettled(terminal)
                if type(result) is VerifiedClassifierPaidOverflowTerminal:
                    classifier_paid_result = result

                    def verify_classifier_paid_overflow(
                        expected_terminal: VerifiedClassifierPaidOverflowTerminal = classifier_paid_result,
                    ) -> VerifiedClassifierPaidOverflowCold:
                        with locked_session_transaction(self.authority._engine, str(self.running.claim.session_id)) as conn:
                            actual = read_v4_classifier_paid_overflow_terminal_on_connection(
                                conn,
                                session_id=self.running.claim.session_id,
                                operation_id=self.running.claim.operation_id,
                                effect_key=expected_terminal.receipt.identity.effect_key,
                            )
                        if (
                            actual is None
                            or actual.receipt != expected_terminal.receipt
                            or actual.terminal != expected_terminal.snapshot
                            or actual.provider_attempt_id != expected_terminal.receipt.identity.attempt_id
                            or actual.hold_state != "consumed"
                        ):
                            raise AuditIntegrityError("Classifier paid overflow lost its exact original cold terminal")
                        return actual

                    self.sql_deadline.begin_settlement()
                    await _registered(
                        work=self.work,
                        source=RequiredWorkSource.CONTINUATION_DISPOSITION_READ_SQL,
                        operation=verify_classifier_paid_overflow,
                        expected=VerifiedClassifierPaidOverflowCold,
                    )
                    terminal = await self.record()
                    if terminal.status != "failed" or (terminal.checkpoint_id, terminal.journal_cursor) != (
                        result.snapshot.checkpoint_id,
                        result.snapshot.cursor,
                    ):
                        raise AuditIntegrityError("Classifier paid overflow lost its actual committed failed terminal")
                    await self.worker._verify_terminal_before_release(
                        self.running,
                        self.lease,
                        terminal,
                        _StartedSettlement(terminal_receipt=terminal, deferred_cancellations=list(result.deferred_cancellations)),
                    )
                    return ContinuationSegmentSettled(terminal)
                if type(result) is ClassifierNoSdkRefusalReceipt:
                    classifier_refusal_result = result

                    def verify_classifier_no_sdk(
                        expected_refusal: ClassifierNoSdkRefusalReceipt = classifier_refusal_result,
                    ) -> ClassifierNoSdkRefusalReceipt:
                        with locked_session_transaction(self.authority._engine, str(self.running.claim.session_id)) as conn:
                            actual = read_classifier_no_sdk_refusal_on_connection(
                                conn,
                                session_id=self.running.claim.session_id,
                                operation_id=self.running.claim.operation_id,
                                effect_key=expected_refusal.effect_key,
                            )
                        if actual is None or actual != expected_refusal:
                            raise AuditIntegrityError("Classifier no-SDK refusal lost its exact original cold terminal")
                        return actual

                    self.sql_deadline.begin_settlement()
                    await _registered(
                        work=self.work,
                        source=RequiredWorkSource.CONTINUATION_DISPOSITION_READ_SQL,
                        operation=verify_classifier_no_sdk,
                        expected=ClassifierNoSdkRefusalReceipt,
                    )
                    terminal = await self.record()
                    if terminal.status != "failed" or terminal != result.terminal:
                        raise AuditIntegrityError("Classifier no-SDK refusal lost its actual committed failed terminal")
                    await self.worker._verify_terminal_before_release(
                        self.running,
                        self.lease,
                        terminal,
                        _StartedSettlement(terminal_receipt=terminal, deferred_cancellations=list(result.deferred_cancellations)),
                    )
                    return ContinuationSegmentSettled(terminal)
                if type(result) is EndCheckpointPaidOverflowTerminal:
                    end_overflow_result = result

                    def verify_end_paid_overflow(
                        expected_terminal: EndCheckpointPaidOverflowTerminal = end_overflow_result,
                        expected_names: frozenset[str] = names,
                    ) -> VerifiedCheckpointPaidOverflowTerminal:
                        with locked_session_transaction(self.authority._engine, str(self.running.claim.session_id)) as conn:
                            actual = read_v4_checkpoint_paid_overflow_terminal_on_connection(
                                conn,
                                session_id=self.running.claim.session_id,
                                operation_id=self.running.claim.operation_id,
                                effect_key=expected_terminal.verified.source.effect_key,
                                names=expected_names,
                            )
                        if actual is None or actual != expected_terminal.verified or actual.hold_state != "consumed":
                            raise AuditIntegrityError("END paid overflow lost its exact original cold terminal")
                        return actual

                    self.sql_deadline.begin_settlement()
                    await _registered(
                        work=self.work,
                        source=RequiredWorkSource.CONTINUATION_DISPOSITION_READ_SQL,
                        operation=verify_end_paid_overflow,
                        expected=VerifiedCheckpointPaidOverflowTerminal,
                    )
                    terminal = await self.record()
                    if terminal.status != "failed" or (terminal.checkpoint_id, terminal.journal_cursor) != (
                        result.verified.terminal.checkpoint_id,
                        result.verified.terminal.cursor,
                    ):
                        raise AuditIntegrityError("END paid overflow lost its actual committed failed terminal")
                    await self.worker._verify_terminal_before_release(
                        self.running,
                        self.lease,
                        terminal,
                        _StartedSettlement(terminal_receipt=terminal, deferred_cancellations=list(result.deferred_cancellations)),
                    )
                    return ContinuationSegmentSettled(terminal)
                if type(result) is EndCheckpointNoSdkTerminal:
                    end_no_sdk_result = result

                    def verify_end_no_sdk(
                        expected_terminal: EndCheckpointNoSdkTerminal = end_no_sdk_result,
                        expected_names: frozenset[str] = names,
                    ) -> VerifiedCheckpointNoSdkTerminal:
                        with locked_session_transaction(self.authority._engine, str(self.running.claim.session_id)) as conn:
                            actual = read_v4_checkpoint_no_sdk_terminal_on_connection(
                                conn,
                                session_id=self.running.claim.session_id,
                                operation_id=self.running.claim.operation_id,
                                effect_key=expected_terminal.verified.source.effect_key,
                                names=expected_names,
                            )
                        if actual is None or actual != expected_terminal.verified or actual.hold_state != "consumed":
                            raise AuditIntegrityError("END no-SDK refusal lost its exact original cold terminal")
                        return actual

                    self.sql_deadline.begin_settlement()
                    await _registered(
                        work=self.work,
                        source=RequiredWorkSource.CONTINUATION_DISPOSITION_READ_SQL,
                        operation=verify_end_no_sdk,
                        expected=VerifiedCheckpointNoSdkTerminal,
                    )
                    terminal = await self.record()
                    if terminal.status != "failed" or (terminal.checkpoint_id, terminal.journal_cursor) != (
                        result.verified.terminal.checkpoint_id,
                        result.verified.terminal.cursor,
                    ):
                        raise AuditIntegrityError("END no-SDK refusal lost its actual committed failed terminal")
                    await self.worker._verify_terminal_before_release(
                        self.running,
                        self.lease,
                        terminal,
                        _StartedSettlement(terminal_receipt=terminal, deferred_cancellations=list(result.deferred_cancellations)),
                    )
                    return ContinuationSegmentSettled(terminal)
                if type(result) is EndCheckpointUnboundRefusalTerminal:
                    end_unbound_result = result

                    def verify_end_unbound_refusal(
                        expected_terminal: EndCheckpointUnboundRefusalTerminal = end_unbound_result,
                        expected_names: frozenset[str] = names,
                    ) -> VerifiedCheckpointUnboundRefusal:
                        with locked_session_transaction(self.authority._engine, str(self.running.claim.session_id)) as conn:
                            actual = read_v4_checkpoint_unbound_refusal_on_connection(
                                conn,
                                session_id=self.running.claim.session_id,
                                operation_id=self.running.claim.operation_id,
                                names=expected_names,
                            )
                        if actual is None or actual != expected_terminal.verified:
                            raise AuditIntegrityError("END unbound refusal lost its exact original cold terminal")
                        return actual

                    self.sql_deadline.begin_settlement()
                    await _registered(
                        work=self.work,
                        source=RequiredWorkSource.CONTINUATION_DISPOSITION_READ_SQL,
                        operation=verify_end_unbound_refusal,
                        expected=VerifiedCheckpointUnboundRefusal,
                    )
                    terminal = await self.record()
                    if terminal.status != "failed" or (terminal.checkpoint_id, terminal.journal_cursor) != (
                        result.verified.terminal.checkpoint_id,
                        result.verified.terminal.cursor,
                    ):
                        raise AuditIntegrityError("END unbound refusal lost its actual committed failed terminal")
                    await self.worker._verify_terminal_before_release(
                        self.running,
                        self.lease,
                        terminal,
                        _StartedSettlement(terminal_receipt=terminal, deferred_cancellations=list(result.deferred_cancellations)),
                    )
                    return ContinuationSegmentSettled(terminal)
                if type(result) is EndCheckpointPaidIncompleteTerminal:
                    end_incomplete_result = result

                    def verify_end_paid_incomplete(
                        expected_terminal: EndCheckpointPaidIncompleteTerminal = end_incomplete_result,
                        expected_names: frozenset[str] = names,
                    ) -> VerifiedPaidIncompleteTerminal:
                        with locked_session_transaction(self.authority._engine, str(self.running.claim.session_id)) as conn:
                            actual = read_v4_checkpoint_paid_incomplete_verified_on_connection(
                                conn,
                                session_id=self.running.claim.session_id,
                                operation_id=self.running.claim.operation_id,
                                effect_key=expected_terminal.verified.receipt.identity.effect_key,
                                names=expected_names,
                            )
                        if actual is None or actual != expected_terminal.verified or actual.hold_state != "consumed":
                            raise AuditIntegrityError("END paid incomplete lost its exact original cold terminal")
                        return actual

                    self.sql_deadline.begin_settlement()
                    await _registered(
                        work=self.work,
                        source=RequiredWorkSource.CONTINUATION_DISPOSITION_READ_SQL,
                        operation=verify_end_paid_incomplete,
                        expected=VerifiedPaidIncompleteTerminal,
                    )
                    terminal = await self.record()
                    if terminal.status != "failed" or (terminal.checkpoint_id, terminal.journal_cursor) != (
                        result.verified.terminal.checkpoint_id,
                        result.verified.terminal.cursor,
                    ):
                        raise AuditIntegrityError("END paid incomplete lost its actual committed failed terminal")
                    await self.worker._verify_terminal_before_release(
                        self.running,
                        self.lease,
                        terminal,
                        _StartedSettlement(terminal_receipt=terminal, deferred_cancellations=list(result.deferred_cancellations)),
                    )
                    return ContinuationSegmentSettled(terminal)
                if type(result) is SemanticAdvisorPaidIncompleteTerminal:
                    semantic_incomplete_result = result

                    def verify_semantic_paid_incomplete(
                        expected_terminal: SemanticAdvisorPaidIncompleteTerminal = semantic_incomplete_result,
                    ) -> VerifiedPaidIncompleteTerminal:
                        with locked_session_transaction(self.authority._engine, str(self.running.claim.session_id)) as conn:
                            actual = read_v4_semantic_paid_incomplete_terminal_on_connection(
                                conn,
                                session_id=self.running.claim.session_id,
                                operation_id=self.running.claim.operation_id,
                                effect_key=expected_terminal.verified.receipt.identity.effect_key,
                            )
                        if actual is None or actual != expected_terminal.verified or actual.hold_state != "consumed":
                            raise AuditIntegrityError("Semantic paid incomplete lost its exact original cold terminal")
                        return actual

                    self.sql_deadline.begin_settlement()
                    await _registered(
                        work=self.work,
                        source=RequiredWorkSource.CONTINUATION_DISPOSITION_READ_SQL,
                        operation=verify_semantic_paid_incomplete,
                        expected=VerifiedPaidIncompleteTerminal,
                    )
                    terminal = await self.record()
                    if terminal.status != "failed" or (terminal.checkpoint_id, terminal.journal_cursor) != (
                        result.verified.terminal.checkpoint_id,
                        result.verified.terminal.cursor,
                    ):
                        raise AuditIntegrityError("Semantic paid incomplete lost its actual committed failed terminal")
                    await self.worker._verify_terminal_before_release(
                        self.running,
                        self.lease,
                        terminal,
                        _StartedSettlement(terminal_receipt=terminal, deferred_cancellations=list(result.deferred_cancellations)),
                    )
                    return ContinuationSegmentSettled(terminal)
                if type(result) is SemanticAdvisorPaidTerminal:
                    semantic_paid_result = result

                    def verify_semantic_paid_overflow(
                        expected_terminal: SemanticAdvisorPaidTerminal = semantic_paid_result,
                    ) -> VerifiedSemanticPaidOverflowTerminal:
                        with locked_session_transaction(self.authority._engine, str(self.running.claim.session_id)) as conn:
                            actual = read_v4_semantic_paid_overflow_terminal_on_connection(
                                conn,
                                session_id=self.running.claim.session_id,
                                operation_id=self.running.claim.operation_id,
                                effect_key=expected_terminal.verified.receipt.identity.effect_key,
                            )
                        if actual is None or actual != expected_terminal.verified or actual.hold_state != "consumed":
                            raise AuditIntegrityError("Semantic paid overflow lost its exact original cold terminal")
                        return actual

                    self.sql_deadline.begin_settlement()
                    await _registered(
                        work=self.work,
                        source=RequiredWorkSource.CONTINUATION_DISPOSITION_READ_SQL,
                        operation=verify_semantic_paid_overflow,
                        expected=VerifiedSemanticPaidOverflowTerminal,
                    )
                    terminal = await self.record()
                    if terminal.status != "failed" or (terminal.checkpoint_id, terminal.journal_cursor) != (
                        result.verified.terminal.checkpoint_id,
                        result.verified.terminal.cursor,
                    ):
                        raise AuditIntegrityError("Semantic paid overflow lost its actual committed failed terminal")
                    await self.worker._verify_terminal_before_release(
                        self.running,
                        self.lease,
                        terminal,
                        _StartedSettlement(terminal_receipt=terminal, deferred_cancellations=list(result.deferred_cancellations)),
                    )
                    return ContinuationSegmentSettled(terminal)
                if type(result) is _TitlePaidTerminalResult:
                    title_paid_result = result

                    def verify_title_paid_overflow(
                        expected_terminal: _TitlePaidTerminalResult = title_paid_result,
                    ) -> VerifiedTitlePaidOverflowTerminal:
                        with locked_session_transaction(self.authority._engine, str(self.running.claim.session_id)) as conn:
                            actual = read_v4_title_paid_overflow_terminal_on_connection(
                                conn,
                                session_id=self.running.claim.session_id,
                                operation_id=self.running.claim.operation_id,
                                names=expected_terminal.names,
                            )
                        if actual is None or actual != expected_terminal.verified or actual.hold_state != "consumed":
                            raise AuditIntegrityError("TITLE paid overflow lost its exact original cold terminal")
                        return actual

                    self.sql_deadline.begin_settlement()
                    await _registered(
                        work=self.work,
                        source=RequiredWorkSource.CONTINUATION_DISPOSITION_READ_SQL,
                        operation=verify_title_paid_overflow,
                        expected=VerifiedTitlePaidOverflowTerminal,
                    )
                    terminal = await self.record()
                    if terminal.status != "failed" or (terminal.checkpoint_id, terminal.journal_cursor) != (
                        result.verified.terminal.checkpoint_id,
                        result.verified.terminal.cursor,
                    ):
                        raise AuditIntegrityError("TITLE paid overflow lost its actual committed failed terminal")
                    await self.worker._verify_terminal_before_release(
                        self.running, self.lease, terminal, _StartedSettlement(terminal_receipt=terminal)
                    )
                    return ContinuationSegmentSettled(terminal)
                if type(result) is _TitleNoSdkResult:
                    title_no_sdk_result = result

                    def verify_title_no_sdk(
                        expected_terminal: _TitleNoSdkResult = title_no_sdk_result,
                    ) -> VerifiedTitleNoSdkTerminal:
                        with locked_session_transaction(self.authority._engine, str(self.running.claim.session_id)) as conn:
                            actual = read_v4_title_no_sdk_terminal_on_connection(
                                conn,
                                session_id=self.running.claim.session_id,
                                operation_id=self.running.claim.operation_id,
                                names=expected_terminal.names,
                            )
                        if actual is None or actual != expected_terminal.verified or actual.hold_state != "consumed":
                            raise AuditIntegrityError("TITLE no-SDK refusal lost its exact original cold terminal")
                        return actual

                    self.sql_deadline.begin_settlement()
                    await _registered(
                        work=self.work,
                        source=RequiredWorkSource.CONTINUATION_DISPOSITION_READ_SQL,
                        operation=verify_title_no_sdk,
                        expected=VerifiedTitleNoSdkTerminal,
                    )
                    terminal = await self.record()
                    if terminal.status != "failed" or (terminal.checkpoint_id, terminal.journal_cursor) != (
                        result.verified.terminal.checkpoint_id,
                        result.verified.terminal.cursor,
                    ):
                        raise AuditIntegrityError("TITLE no-SDK refusal lost its actual committed failed terminal")
                    await self.worker._verify_terminal_before_release(
                        self.running,
                        self.lease,
                        terminal,
                        _StartedSettlement(terminal_receipt=terminal, deferred_cancellations=list(result.deferred_cancellations)),
                    )
                    return ContinuationSegmentSettled(terminal)
                if type(result) is _TitlePaidIncompleteResult:
                    title_incomplete_result = result

                    def verify_title_paid_incomplete(
                        expected_terminal: _TitlePaidIncompleteResult = title_incomplete_result,
                    ) -> VerifiedPaidIncompleteTerminal:
                        with locked_session_transaction(self.authority._engine, str(self.running.claim.session_id)) as conn:
                            actual = read_v4_title_paid_incomplete_terminal_on_connection(
                                conn,
                                session_id=self.running.claim.session_id,
                                operation_id=self.running.claim.operation_id,
                                names=expected_terminal.names,
                            )
                        if actual is None or actual != expected_terminal.verified or actual.hold_state != "consumed":
                            raise AuditIntegrityError("TITLE paid incomplete lost its exact original cold terminal")
                        return actual

                    self.sql_deadline.begin_settlement()
                    await _registered(
                        work=self.work,
                        source=RequiredWorkSource.CONTINUATION_DISPOSITION_READ_SQL,
                        operation=verify_title_paid_incomplete,
                        expected=VerifiedPaidIncompleteTerminal,
                    )
                    terminal = await self.record()
                    if terminal.status != "failed" or (terminal.checkpoint_id, terminal.journal_cursor) != (
                        result.verified.terminal.checkpoint_id,
                        result.verified.terminal.cursor,
                    ):
                        raise AuditIntegrityError("TITLE paid incomplete lost its actual committed failed terminal")
                    await self.worker._verify_terminal_before_release(
                        self.running,
                        self.lease,
                        terminal,
                        _StartedSettlement(terminal_receipt=terminal, deferred_cancellations=list(result.deferred_cancellations)),
                    )
                    return ContinuationSegmentSettled(terminal)
                if type(result) is PrimaryPaidIncompleteTerminal:
                    primary_incomplete_result = result

                    def verify_paid_incomplete(
                        expected_terminal: PrimaryPaidIncompleteTerminal = primary_incomplete_result,
                    ) -> VerifiedPaidIncompleteTerminal:
                        with locked_session_transaction(self.authority._engine, str(self.running.claim.session_id)) as conn:
                            actual = read_v4_primary_paid_incomplete_verified_on_connection(
                                conn,
                                session_id=self.running.claim.session_id,
                                operation_id=self.running.claim.operation_id,
                                effect_key=expected_terminal.receipt.identity.effect_key,
                            )
                        if (
                            actual is None
                            or actual.receipt != expected_terminal.receipt
                            or actual.terminal != expected_terminal.snapshot
                            or actual.provider_attempt_id != expected_terminal.receipt.identity.attempt_id
                            or actual.hold_state != "consumed"
                        ):
                            raise AuditIntegrityError("PRIMARY paid incomplete lost its exact original cold terminal")
                        return actual

                    self.sql_deadline.begin_settlement()
                    await _registered(
                        work=self.work,
                        source=RequiredWorkSource.CONTINUATION_DISPOSITION_READ_SQL,
                        operation=verify_paid_incomplete,
                        expected=VerifiedPaidIncompleteTerminal,
                    )
                    terminal = await self.record()
                    if terminal.status != "failed" or (terminal.checkpoint_id, terminal.journal_cursor) != (
                        result.snapshot.checkpoint_id,
                        result.snapshot.cursor,
                    ):
                        raise AuditIntegrityError("PRIMARY paid incomplete lost its actual committed failed terminal")
                    await self.worker._verify_terminal_before_release(
                        self.running,
                        self.lease,
                        terminal,
                        _StartedSettlement(terminal_receipt=terminal, deferred_cancellations=list(result.deferred_cancellations)),
                    )
                    return ContinuationSegmentSettled(terminal)
                if type(result) is PrimaryPaidOverflowTerminal:
                    primary_paid_result = result

                    def verify_paid_overflow(
                        expected_terminal: PrimaryPaidOverflowTerminal = primary_paid_result,
                    ) -> VerifiedPrimaryPaidOverflowTerminal:
                        with locked_session_transaction(self.authority._engine, str(self.running.claim.session_id)) as conn:
                            actual = read_v4_primary_paid_overflow_terminal_on_connection(
                                conn,
                                session_id=self.running.claim.session_id,
                                operation_id=self.running.claim.operation_id,
                                effect_key=expected_terminal.receipt.identity.effect_key,
                            )
                        if (
                            actual is None
                            or actual.receipt != expected_terminal.receipt
                            or actual.terminal != expected_terminal.snapshot
                            or actual.hold_state != "consumed"
                        ):
                            raise AuditIntegrityError("PRIMARY paid overflow lost its exact original cold terminal")
                        return actual

                    self.sql_deadline.begin_settlement()
                    await _registered(
                        work=self.work,
                        source=RequiredWorkSource.CONTINUATION_DISPOSITION_READ_SQL,
                        operation=verify_paid_overflow,
                        expected=VerifiedPrimaryPaidOverflowTerminal,
                    )
                    terminal = await self.record()
                    if terminal.status != "failed" or (terminal.checkpoint_id, terminal.journal_cursor) != (
                        result.snapshot.checkpoint_id,
                        result.snapshot.cursor,
                    ):
                        raise AuditIntegrityError("PRIMARY paid overflow lost its actual committed failed terminal")
                    await self.worker._verify_terminal_before_release(
                        self.running,
                        self.lease,
                        terminal,
                        _StartedSettlement(terminal_receipt=terminal, deferred_cancellations=list(result.deferred_cancellations)),
                    )
                    return ContinuationSegmentSettled(terminal)
                if type(result) is ToolNoExecutionRefusal:
                    return await self.consume_tool_refusal(result)
                if isinstance(result, V3UnboundedTailRefusal):
                    refusal = result

                    def verify_refusal(expected_refusal: V3UnboundedTailRefusal = refusal) -> V3UnboundedTailRefusal:
                        with locked_session_transaction(self.authority._engine, str(self.running.claim.session_id)) as conn:
                            actual = read_v3_retained_tail_refusal_on_connection(
                                conn, session_id=self.running.claim.session_id, operation_id=self.running.claim.operation_id
                            )
                        if actual is None or actual != expected_refusal:
                            raise AuditIntegrityError("V3 unpaid refusal lost its exact original cold source")
                        return actual

                    self.sql_deadline.begin_settlement()
                    await _registered(
                        work=self.work,
                        source=RequiredWorkSource.CONTINUATION_DISPOSITION_READ_SQL,
                        operation=verify_refusal,
                        expected=V3UnboundedTailRefusal,
                    )
                    terminal = await self.record()
                    if terminal.status != "failed" or (terminal.checkpoint_id, terminal.journal_cursor) != (
                        result.terminal.checkpoint_id,
                        result.terminal.cursor,
                    ):
                        raise AuditIntegrityError("V3 unpaid refusal lost its actual committed failed terminal")
                    await self.worker._verify_terminal_before_release(
                        self.running, self.lease, terminal, _StartedSettlement(terminal_receipt=terminal)
                    )
                    return ContinuationSegmentSettled(terminal)
                if type(result) is SemanticAdvisorNoSdkTerminal:
                    if result.terminal.status != "failed":
                        raise AuditIntegrityError("Semantic no-SDK close lost its actual failed terminal")
                    self._consumed_native.append(result.original_error)
                    await self.worker._verify_terminal_before_release(
                        self.running,
                        self.lease,
                        result.terminal,
                        _StartedSettlement(terminal_receipt=result.terminal, deferred_cancellations=list(result.deferred_cancellations)),
                    )
                    return ContinuationSegmentSettled(result.terminal)
                if type(result) is PrimaryNoSdkRefusalReceipt:
                    from elspeth.web.sessions.composer_async_worker import _StartedSettlement

                    if result.terminal.status != "failed":
                        raise AuditIntegrityError("PRIMARY no-SDK refusal lost its failed terminal")
                    await self.worker._verify_terminal_before_release(
                        self.running,
                        self.lease,
                        result.terminal,
                        _StartedSettlement(deferred_cancellations=list(result.deferred_cancellations), terminal_receipt=result.terminal),
                    )
                    return ContinuationSegmentSettled(result.terminal)
                # The END owner already committed this terminal together with
                # its assistant row and audit cohort (the shared R2 composite).
                if type(result) is not ComposerOperationRecord or result.status != "completed":
                    raise AuditIntegrityError("Continuation publication lost its actual committed terminal")
                await self.worker._verify_terminal_before_release(
                    self.running, self.lease, result, _StartedSettlement(terminal_receipt=result)
                )
                return ContinuationSegmentSettled(result)


async def run_continuation_segment(
    worker: ComposerAsyncWorker,
    services: ComposerAppServices,
    running: ComposerOperationRunning,
    lease: SessionOperationLease,
    *,
    adoption_failure: BaseException | None = None,
) -> ContinuationSegmentOutcome:
    """Run one real owner and join exactly its retained lease finish task."""
    from elspeth.web.composer.continuation_tool_failure import combine_failure_objects

    original: BaseException | None = None
    driver: _SegmentDriver | None = None
    try:
        # Keep native custody alive through the one retained physical close,
        # including a verified failed operation whose stage is now consumed.
        driver = _SegmentDriver(worker, services, running, lease)
        return await driver.run(adoption_failure)
    except BaseException as failure:
        original = failure
        if driver is not None:
            original = driver.join_failure(failure)
        elif adoption_failure is not None:
            original = combine_failure_objects(adoption_failure, failure)
        if original is failure:
            raise
        raise original from original.__cause__
    finally:
        if lease.required_work is not None and lease.required_work.sql_deadline_owner is not None:
            lease.required_work.sql_deadline_owner.begin_settlement()
        cleanup_errors = list(driver._progress_cleanup_failures) if driver is not None else []
        if driver is not None:
            try:
                await driver.close_progress()
            except BaseException as cleanup:
                cleanup_errors.append(cleanup)
        try:
            await lease.close(deadline=worker.drain_deadline)
        except BaseException as cleanup:
            cleanup_errors.append(cleanup)
        if cleanup_errors:
            cleanup_failure = cleanup_errors[0]
            additional = (*cleanup_errors[1:], *((original,) if original is not None else ()))
            if driver is not None:
                combined = driver.join_failure(cleanup_failure, *additional)
                if combined is cleanup_failure:
                    raise cleanup_failure
                raise combined from combined.__cause__
            combined = combine_failure_objects(cleanup_failure, *additional)
            if combined is cleanup_failure:
                raise cleanup_failure
            raise combined from combined.__cause__
