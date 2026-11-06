"""One-action EARLY caller and original complete-return handoff.

The driver owns caller selection, committed graph adoption and a separate retry
action. Source-owned SQL completion projects paid cost before graph adoption.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from elspeth.contracts.chargeable_admission import ChargeableAdmissionRefused
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import freeze_fields
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer.advisor_policy import advisor_arguments_with_format_reprompt
from elspeth.web.composer.content_scope import composition_scope_for_operation
from elspeth.web.composer.continuation_checkpoint_early_physical import (
    CheckpointEarlyPhysicalSource,
    checkpoint_early_detached_runtime,
    checkpoint_early_format_retry,
    checkpoint_early_physical_custody,
    checkpoint_early_physical_no_sdk_issuer,
    checkpoint_early_physical_preparation,
    observe_checkpoint_early_physical_return,
    observe_checkpoint_early_sdk_companion,
    require_original_checkpoint_early_physical_source,
)
from elspeth.web.composer.continuation_checkpoint_no_sdk import CheckpointNoSdkRefusalReason
from elspeth.web.composer.continuation_checkpoint_observation import (
    CheckpointIncompleteResponseObservation,
    CheckpointReturnedResponseObservation,
    require_original_v4_checkpoint_returned_response,
)
from elspeth.web.composer.continuation_checkpoint_projection import (
    CheckpointPassAccounting,
    ProjectedEarlyCheckpoint,
    ProjectedEarlyCheckpointOverflow,
    project_early_checkpoint_result,
)
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.progress import advisor_checkpoint_progress_event, emit_progress
from elspeth.web.composer.prompt_guard import PromptGuardRefused, prompt_guard_user_scope
from elspeth.web.composer.provider_cost import CommonProviderCostRefused
from elspeth.web.composer.provider_gateway import _MalformedLLMResponseError
from elspeth.web.composer.provider_quota import V4PaidIncompleteCommitted, V4SdkTerminalCommitted, composer_quota_scope
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_checkpoint_early_retry import VerifiedEarlyFormatRetry
from elspeth.web.sessions.continuation_checkpoint_early_return import EarlyCompleteReturnAdmission
from elspeth.web.sessions.continuation_checkpoint_early_sql import (
    EarlyCheckpointAdvanced,
    retain_early_complete_return_sync,
)
from elspeth.web.sessions.continuation_checkpoint_no_sdk_terminal import (
    VerifiedCheckpointNoSdkTerminal,
    read_v4_checkpoint_no_sdk_terminal_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_paid import PreparedCheckpointProvider, V4CheckpointProviderAdmission
from elspeth.web.sessions.continuation_checkpoint_paid_terminal import VerifiedCheckpointPaidOverflowTerminal
from elspeth.web.sessions.continuation_paid_incomplete_terminal import VerifiedPaidIncompleteTerminal
from elspeth.web.sessions.continuation_retained_capacity import ContinuationRetainedCapacityExceeded
from elspeth.web.sessions.continuation_sdk_tail import SdkTerminalFamily
from elspeth.web.sessions.continuation_sdk_terminal_result import SdkTerminalFailure, SdkTerminalResult
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.service import SessionServiceImpl

if TYPE_CHECKING:
    from elspeth.web.composer._compose_loop_carriers import _DispatchOutcome, _PersistOutcome
    from elspeth.web.composer.continuation_finished_batch_data import ColdPostBatchPersistData
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
    from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime
    from elspeth.web.composer.continuation_selection import SelectedContinuationState
    from elspeth.web.composer.state import CompositionState


def _original_admission(source: CheckpointEarlyPhysicalSource) -> tuple[PreparedCheckpointProvider, V4CheckpointProviderAdmission]:
    require_original_checkpoint_early_physical_source(source)
    custody = checkpoint_early_physical_custody(source)
    prepared, admission = custody.continuation_checkpoint_v4, custody.continuation_checkpoint_v4_admission
    if (
        type(prepared) is not PreparedCheckpointProvider
        or type(admission) is not V4CheckpointProviderAdmission
        or prepared.physical_source() is not source
        or prepared.clock is not source.clock
        or prepared.prepared_source.phase != "early"
        or admission.original_source is not prepared.prepared_source
    ):
        raise AuditIntegrityError("EARLY live result lost its original admitted physical source")
    return prepared, admission


def _admitted(source: CheckpointEarlyPhysicalSource) -> tuple[PreparedCheckpointProvider, V4CheckpointProviderAdmission]:
    prepared, admission = _original_admission(source)
    if checkpoint_early_physical_custody(source).attempt is not admission.attempt:
        raise AuditIntegrityError("EARLY live result lost its original active physical attempt")
    return prepared, admission


def _require_return(source: CheckpointEarlyPhysicalSource, returned: CheckpointReturnedResponseObservation) -> None:
    prepared, admission = _admitted(source)
    if (
        type(returned) is not CheckpointReturnedResponseObservation
        or returned.phase != "early"
        or returned.attempt is not admission.attempt
        or returned.audit_call is not checkpoint_early_physical_custody(source).call
        or source.capture.snapshot().audit_call is not returned.audit_call
    ):
        raise AuditIntegrityError("EARLY live return changed its original complete physical observation")
    require_original_v4_checkpoint_returned_response(
        returned, attempt_id=admission.attempt.attempt_id, request_sha256=prepared.request_sha256
    )


def _sessions(prepared: PreparedCheckpointProvider) -> SessionServiceImpl:
    sessions = prepared.composer._require_sessions_service()
    if type(sessions) is not SessionServiceImpl:
        raise AuditIntegrityError("EARLY live SQL requires its original concrete sessions owner")
    return sessions


@dataclass(frozen=True, slots=True)
class EarlyCheckpointLiveAdvanced:
    """Original paid handoff; independent SQL adoption remains mandatory."""

    source: CheckpointEarlyPhysicalSource
    advanced: EarlyCheckpointAdvanced
    returned: CheckpointReturnedResponseObservation

    def __post_init__(self) -> None:
        _require_return(self.source, self.returned)
        prepared, admission = _admitted(self.source)
        if (
            type(self.advanced) is not EarlyCheckpointAdvanced
            or type(self.advanced.completed) is not StoredContinuationSnapshot
            or self.advanced.completed.cursor != admission.selected.cursor + 3
        ):
            raise AuditIntegrityError("EARLY live handoff lost its original completed next-stage data")
        retry = self.advanced.retry
        if retry is not None and (
            type(retry) is not VerifiedEarlyFormatRetry
            or prepared.retry_ordinal != 0
            or retry.selected != self.advanced.completed
            or retry.marker.session_id != str(prepared.running.claim.session_id)
            or retry.marker.operation_id != prepared.running.claim.operation_id
            or retry.marker.attempt_id != admission.attempt.attempt_id
            or retry.marker.effect_key != admission.effect_key
            or retry.marker.response_sha256 != self.returned.response.reference.digest
        ):
            raise AuditIntegrityError("EARLY live retry handoff changed its original completed first physical child")


@dataclass(frozen=True, slots=True)
class EarlyCheckpointPaidOverflowTerminal:
    verified: VerifiedCheckpointPaidOverflowTerminal
    deferred_cancellations: tuple[asyncio.CancelledError, ...] = ()

    def __post_init__(self) -> None:
        freeze_fields(self, "deferred_cancellations")
        if (
            type(self.verified) is not VerifiedCheckpointPaidOverflowTerminal
            or self.verified.phase != "early"
            or self.verified.hold_state != "consumed"
        ):
            raise AuditIntegrityError("EARLY overflow requires its original settled checkpoint terminal")


@dataclass(frozen=True, slots=True)
class EarlyCheckpointOwedOverflowTerminal(EarlyCheckpointPaidOverflowTerminal):
    """Post-H overflow; the driver must use the owed-result cold proof."""


@dataclass(frozen=True, slots=True)
class EarlyCheckpointNoSdkTerminal:
    verified: VerifiedCheckpointNoSdkTerminal
    deferred_cancellations: tuple[asyncio.CancelledError, ...] = ()

    def __post_init__(self) -> None:
        freeze_fields(self, "deferred_cancellations")
        if (
            type(self.verified) is not VerifiedCheckpointNoSdkTerminal
            or self.verified.phase != "early"
            or self.verified.hold_state != "consumed"
        ):
            raise AuditIntegrityError("EARLY no-SDK requires its original admitted and cancelled checkpoint terminal")


@dataclass(frozen=True, slots=True)
class EarlyCheckpointPaidIncompleteTerminal:
    source: CheckpointEarlyPhysicalSource
    verified: VerifiedPaidIncompleteTerminal
    deferred_cancellations: tuple[asyncio.CancelledError, ...] = ()

    def __post_init__(self) -> None:
        freeze_fields(self, "deferred_cancellations")
        _, admission = _original_admission(self.source)
        custody = checkpoint_early_physical_custody(self.source)
        finalized = custody.v4_returned_incomplete_result
        if (
            type(self.verified) is not VerifiedPaidIncompleteTerminal
            or self.verified.receipt.producer != "checkpoint_advisor"
            or self.verified.source != admission.hold
            or self.verified.provider_attempt_id != admission.attempt.attempt_id
            or not custody.has_v4_returned_incomplete_terminal()
            or finalized is None
            or finalized.verified is not self.verified
            or finalized.deferred_cancellations != self.deferred_cancellations
        ):
            raise AuditIntegrityError("EARLY incomplete terminal lost its original custody and phase")


type EarlyCheckpointLiveTerminal = (
    EarlyCheckpointPaidOverflowTerminal
    | EarlyCheckpointOwedOverflowTerminal
    | EarlyCheckpointNoSdkTerminal
    | EarlyCheckpointPaidIncompleteTerminal
    | SdkTerminalResult
    | SdkTerminalFailure
)
type EarlyCheckpointLiveOutcome = EarlyCheckpointLiveAdvanced | EarlyCheckpointLiveTerminal


def _begin_settlement(source: CheckpointEarlyPhysicalSource) -> None:
    owner = source.phase_owner.phases.work.coordinator.sql_deadline_owner
    if owner is None:
        raise AuditIntegrityError("EARLY live settlement lost its original SQL deadline")
    owner.require_action_clock(source.clock)
    owner.begin_settlement()


async def _delivered_sql[T](
    source: CheckpointEarlyPhysicalSource, *, operation: Callable[[], T], expected: type[T]
) -> RequiredSQLReturned[T] | RequiredSQLRaised:
    """Keep the original SQL result and cancellation until its owner disposes it."""
    work = source.phase_owner.phases.work
    _begin_settlement(source)
    sql, projection = work.reserve_pair(RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(sql, operation)
        work.coordinator.validate_continuation_sql_outcome(
            ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_STATE_SQL, actual_outcome=outcome
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    if type(outcome) is RequiredSQLRaised:
        projection.complete_without_submission()
        return outcome
    if type(outcome) is not RequiredSQLReturned:
        projection.complete_without_submission()
        raise AuditIntegrityError("EARLY terminal lost its original registered SQL outcome")
    projection.begin_projection()
    try:
        if type(outcome.value) is not expected:
            raise AuditIntegrityError("EARLY terminal lost its original nominal SQL result")
    except BaseException as error:
        projection.complete_owned(error)
        raise
    projection.complete_owned()
    return outcome


async def _terminal_sql[T](
    source: CheckpointEarlyPhysicalSource, *, operation: Callable[[], T], expected: type[T]
) -> tuple[T, tuple[asyncio.CancelledError, ...]]:
    """Return committed terminal DATA before surfacing deferred cancellation."""
    outcome = await _delivered_sql(source, operation=operation, expected=expected)
    if isinstance(outcome, RequiredSQLRaised):
        if outcome.deferred_cancellations:
            raise BaseExceptionGroup("EARLY terminal SQL and deferred cancellation", [outcome.error, *outcome.deferred_cancellations])
        raise outcome.error
    return outcome.value, outcome.deferred_cancellations


async def _returned_overflow(
    source: CheckpointEarlyPhysicalSource,
    returned: CheckpointReturnedResponseObservation | CheckpointIncompleteResponseObservation,
    *,
    overflow: ContinuationRetainedCapacityExceeded | None = None,
) -> EarlyCheckpointPaidOverflowTerminal:
    prepared, admission = _admitted(source)
    sessions = _sessions(prepared)
    verified, deferred = await _terminal_sql(
        source,
        operation=partial(
            sessions.complete_v4_checkpoint_result_sync, prepared=prepared, admission=admission, observed=returned, overflow=overflow
        ),
        expected=VerifiedCheckpointPaidOverflowTerminal,
    )
    return EarlyCheckpointPaidOverflowTerminal(verified, deferred)


def _no_sdk_reason(error: BaseException) -> CheckpointNoSdkRefusalReason | None:
    if type(error) is PromptGuardRefused:
        return CheckpointNoSdkRefusalReason.GUARD_REFUSED
    if type(error) is CommonProviderCostRefused:
        return CheckpointNoSdkRefusalReason.COST_REFUSED
    if type(error) is ChargeableAdmissionRefused:
        return CheckpointNoSdkRefusalReason.CHARGEABLE_REFUSED
    if type(error) is TimeoutError:
        return CheckpointNoSdkRefusalReason.PRE_SDK_DEADLINE
    if type(error) is asyncio.CancelledError:
        return CheckpointNoSdkRefusalReason.CANCELLED_BEFORE_SDK
    return None


async def _no_sdk(
    source: CheckpointEarlyPhysicalSource, error: BaseException, reason: CheckpointNoSdkRefusalReason
) -> EarlyCheckpointNoSdkTerminal:
    prepared, admission = _admitted(source)
    issuer = checkpoint_early_physical_no_sdk_issuer(source)
    permit = issuer.reserve_refusal(reason=reason, original_failure=error)
    sessions = _sessions(prepared)

    def close() -> VerifiedCheckpointNoSdkTerminal:
        sessions.refuse_v4_checkpoint_no_sdk_sync(permit=permit)
        with locked_session_transaction(sessions._engine, str(prepared.running.claim.session_id)) as conn:
            verified = read_v4_checkpoint_no_sdk_terminal_on_connection(
                conn,
                session_id=prepared.running.claim.session_id,
                operation_id=prepared.running.claim.operation_id,
                effect_key=admission.effect_key,
                names=prepared.env_ref_names,
            )
        if verified is None:
            raise AuditIntegrityError("EARLY no-SDK lost its original committed cold proof")
        return verified

    verified, deferred = await _terminal_sql(source, operation=close, expected=VerifiedCheckpointNoSdkTerminal)
    if type(error) is asyncio.CancelledError and all(error is not cancellation for cancellation in deferred):
        deferred = (error, *deferred)
    return EarlyCheckpointNoSdkTerminal(verified, deferred)


async def _call_early(source: CheckpointEarlyPhysicalSource) -> CheckpointReturnedResponseObservation | EarlyCheckpointLiveTerminal:
    from elspeth.web.composer.advisor_checkpoint import _compose_deadline_time
    from elspeth.web.composer.continuation_semantic_advisor_return import _original_action_deadline

    owner = source.phase_owner
    phases, runtime = owner.phases, owner.runtime
    advisor = phases.service._advisor_checkpoint
    bindings = runtime.bindings
    if bindings.session_id is None or bindings.session_operation_context is None:
        raise AuditIntegrityError("EARLY live call lost its original operation context")
    names = await advisor._current_env_ref_names(bindings.session_id)
    if names != runtime.env_ref_names:
        raise AuditIntegrityError("EARLY live call changed its original environment inventory")
    scope = composition_scope_for_operation(
        runtime.live.state,
        session_id=bindings.session_id,
        session_operation_context=bindings.session_operation_context,
        session_operation_authority=phases.service._require_sessions_service().session_operation_authority,
    )
    arguments = advisor._build_checkpoint_arguments(
        phase="early",
        state=runtime.live.state,
        content_scope=scope,
        env_ref_names=names,
        advisor_transcript=runtime.live.turn_evidence.transcript.snapshot(),
    )
    advisor._require_complete_graph_summary(arguments["_elspeth_advisor_summary_parts"])
    retry = checkpoint_early_format_retry(source)
    if retry is not None and retry.marker.format_reprompt_required:
        arguments = advisor_arguments_with_format_reprompt(arguments, schema_valid=retry.marker.first_attempt_schema_valid is True)
    remaining = _original_action_deadline(phases, runtime, source.clock) - _compose_deadline_time()
    if remaining <= 0:
        raise TimeoutError("EARLY live call exhausted its original dispatch allowance")
    try:
        await advisor._call_advisor_with_audit(
            arguments,
            recorder=runtime.live.recorder,
            provider_owner=phases.provider_owner,
            timeout=remaining,
            structured_output=True,
            _outcome=source.capture,
            checkpoint_early_source=source,
        )
    except V4SdkTerminalCommitted as control:
        custody = checkpoint_early_physical_custody(source)
        if custody.v4_sdk_terminal_result is not control.finalization:
            raise AuditIntegrityError("EARLY SDK terminal changed its original custody") from control
        return SdkTerminalResult(
            control.finalization.verified, SdkTerminalFamily.CHECKPOINT_EARLY, control.finalization.deferred_cancellations
        )
    except V4PaidIncompleteCommitted as control:
        finalized = checkpoint_early_physical_custody(source).require_v4_paid_incomplete_committed(control)
        return EarlyCheckpointPaidIncompleteTerminal(source, finalized.verified, finalized.deferred_cancellations)
    except BaseException as failure:
        companion = observe_checkpoint_early_sdk_companion(source, failure)
        if companion is not None:
            if companion.family is not SdkTerminalFamily.CHECKPOINT_EARLY:
                raise AuditIntegrityError("EARLY SDK companion changed its original family") from failure
            return companion
        # A physically complete unsupported response keeps its malformed audit
        # and must pass the original return observer before format retry exists.
        if type(failure) is not _MalformedLLMResponseError:
            reason = _no_sdk_reason(failure)
            if reason is not None and source.capture.snapshot().attempt is not None:
                return await _no_sdk(source, failure, reason)
            raise
    observed = observe_checkpoint_early_physical_return(source)
    if isinstance(observed, CheckpointIncompleteResponseObservation):
        if type(observed) is not CheckpointIncompleteResponseObservation:
            raise AuditIntegrityError("EARLY live call received a foreign incomplete observation")
        return await _returned_overflow(source, observed)
    if type(observed) is not CheckpointReturnedResponseObservation:
        raise AuditIntegrityError("EARLY live call lost its exact complete response observation")
    return observed


async def complete_v4_checkpoint_early_return(
    source: CheckpointEarlyPhysicalSource, *, returned: CheckpointReturnedResponseObservation
) -> EarlyCheckpointLiveAdvanced | EarlyCheckpointPaidOverflowTerminal:
    """Project current D, then retain H and atomically complete C/K or C/R."""
    from elspeth.web.sessions.continuation_checkpoint_early_owed_overflow import (
        EarlyCheckpointCompletionDeliveryError,
        complete_early_checkpoint_or_overflow,
    )
    from elspeth.web.sessions.continuation_checkpoint_early_projected_overflow import settle_early_projected_overflow_sync

    _require_return(source, returned)
    prepared, admission = _admitted(source)
    phases, runtime = source.phase_owner.phases, source.phase_owner.runtime
    _begin_settlement(source)
    selected = await load_selected_continuation(
        authority=phases.authority, running=prepared.running, selected=admission.selected, required_work=phases.work
    )
    inventory = await materialize_continuation_env_inventory(
        advisor=phases.service._advisor_checkpoint, authority=phases.authority, selected=selected, required_work=phases.work
    )
    retry = checkpoint_early_format_retry(source)
    accounting = (
        CheckpointPassAccounting(0, 1, None, None, False)
        if retry is None
        else CheckpointPassAccounting(
            0,
            2,
            retry.marker.first_attempt_schema_valid,
            retry.child.accounting.first_attempt_accepted,
            retry.marker.format_reprompt_required,
        )
    )
    projected = await project_early_checkpoint_result(
        prepared=prepared,
        admission=admission,
        detached=checkpoint_early_detached_runtime(source),
        original=runtime,
        selected=selected,
        inventory=inventory,
        custody=checkpoint_early_physical_custody(source),
        returned=returned,
        accounting=accounting,
        physical_source=source,
    )
    if isinstance(projected, ProjectedEarlyCheckpointOverflow):
        if type(projected) is not ProjectedEarlyCheckpointOverflow:
            raise AuditIntegrityError("EARLY live call received a foreign projection overflow")
        verified, deferred = await _terminal_sql(
            source,
            operation=partial(
                settle_early_projected_overflow_sync,
                service=_sessions(prepared),
                prepared=prepared,
                admission=admission,
                returned=returned,
                projected=projected,
            ),
            expected=VerifiedCheckpointPaidOverflowTerminal,
        )
        return EarlyCheckpointPaidOverflowTerminal(verified, deferred)
    if type(projected) is not ProjectedEarlyCheckpoint:
        raise AuditIntegrityError("EARLY live call lost its original detached complete projection")
    sessions = _sessions(prepared)
    retained = await _delivered_sql(
        source,
        operation=partial(retain_early_complete_return_sync, service=sessions, prepared=prepared, admission=admission, returned=returned),
        expected=EarlyCompleteReturnAdmission,
    )
    deferred = retained.deferred_cancellations
    if isinstance(retained, RequiredSQLRaised):
        if isinstance(retained.error, ContinuationRetainedCapacityExceeded):
            try:
                overflow = await _returned_overflow(source, returned, overflow=retained.error)
            except BaseException as failure:
                if deferred:
                    raise BaseExceptionGroup("EARLY H overflow settlement and cancellation", [failure, *deferred]) from None
                raise
            return EarlyCheckpointPaidOverflowTerminal(overflow.verified, (*deferred, *overflow.deferred_cancellations))
        if deferred:
            raise BaseExceptionGroup("EARLY H retention and cancellation", [retained.error, *deferred])
        raise retained.error
    owed = retained.value
    try:
        completed = await complete_early_checkpoint_or_overflow(
            service=sessions, prepared=prepared, owed=owed, returned=returned, projected=projected
        )
    except EarlyCheckpointCompletionDeliveryError as delivery:
        originals = delivery.original_group.exceptions
        if all(isinstance(error, asyncio.CancelledError) for error in originals):
            # Completion has proved/projected its committed head and cost onto
            # the original phase owner. Dispatch the original cancellation via
            # dispose; its cause must not contain the same exception recursively.
            first = originals[0]
            others = [error for error in (*originals[1:], *deferred) if error is not first]
            if others:
                if first.__cause__ is not None:
                    others.insert(0, first.__cause__)
                raise first from BaseExceptionGroup("EARLY other completion cancellations", others)
            raise first from first.__cause__
        if deferred:
            raise BaseExceptionGroup("EARLY completion and retained-return cancellation", [delivery, *deferred]) from None
        raise
    except BaseException as failure:
        if deferred:
            raise BaseExceptionGroup("EARLY completion and retained-return cancellation", [failure, *deferred]) from None
        raise
    if type(completed) is EarlyCheckpointOwedOverflowTerminal:
        return EarlyCheckpointOwedOverflowTerminal(completed.verified, (*deferred, *completed.deferred_cancellations))
    if deferred:
        # H and its paid completion are delivered before cancellation. The
        # committed head/cost are already projected; business adoption is skipped.
        first = deferred[0]
        others = [error for error in deferred[1:] if error is not first]
        if others:
            if first.__cause__ is not None:
                others.insert(0, first.__cause__)
            raise first from BaseExceptionGroup("EARLY other retained-return cancellations", others)
        raise first
    return completed


async def run_v4_checkpoint_early_live(
    phases: ContinuationLivePhases,
    runtime: RestoredContinuationRuntime,
    *,
    selected: SelectedContinuationState,
    dispatch: _DispatchOutcome,
    persist: _PersistOutcome | ColdPostBatchPersistData,
    pre_batch_state: CompositionState,
) -> EarlyCheckpointLiveOutcome:
    """One original R0/R1 call; the driver owns the next independently fitted action."""
    context = runtime.bindings.session_operation_context
    if context is None:
        raise AuditIntegrityError("EARLY live action requires its original session context")
    with composer_quota_scope(phases.service._require_sessions_service(), context), prompt_guard_user_scope(runtime.bindings.message):
        async with checkpoint_early_physical_preparation(
            phases, runtime, selected=selected, dispatch=dispatch, persist=persist, pre_batch_state=pre_batch_state
        ) as source:
            await emit_progress(runtime.bindings.progress, advisor_checkpoint_progress_event("early"))
            returned = await _call_early(source)
            if isinstance(returned, CheckpointReturnedResponseObservation):
                return await complete_v4_checkpoint_early_return(source, returned=returned)
            return returned
