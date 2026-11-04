"""One original V4 END physical call and its first complete returned H cut.

The caller still owns the shared END gate. This source does not classify a
malformed complete response or grant a second call; the bounded format-retry
owner must decide that separately after this original response is durable.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import advisor_policy
from elspeth.web.composer.content_scope import composition_scope_for_operation
from elspeth.web.composer.continuation_checkpoint_end_dispositions import EndCheckpointPaidOverflowTerminal
from elspeth.web.composer.continuation_checkpoint_end_physical import (
    CheckpointEndPhysicalSource,
    checkpoint_end_physical_custody,
    observe_checkpoint_end_physical_return,
    observe_checkpoint_end_sdk_companion,
    prepare_checkpoint_end_physical_source,
)
from elspeth.web.composer.continuation_checkpoint_observation import (
    CheckpointIncompleteResponseObservation,
    CheckpointReturnedResponseObservation,
)
from elspeth.web.composer.continuation_initialization import _registered
from elspeth.web.composer.provider_quota import V4SdkTerminalCommitted
from elspeth.web.continuation_retention_memo import v4_preparation_memo_scope
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions.continuation_checkpoint_end_owed import CheckpointEndOwedAdmission
from elspeth.web.sessions.continuation_checkpoint_paid import PreparedCheckpointProvider, V4CheckpointProviderAdmission
from elspeth.web.sessions.continuation_checkpoint_paid_terminal import VerifiedCheckpointPaidOverflowTerminal
from elspeth.web.sessions.continuation_retained_capacity import ContinuationRetainedCapacityExceeded
from elspeth.web.sessions.continuation_sdk_tail import SdkTerminalFamily
from elspeth.web.sessions.continuation_sdk_terminal_result import SdkTerminalFailure, SdkTerminalResult
from elspeth.web.sessions.service import SessionServiceImpl

if TYPE_CHECKING:
    from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
    from elspeth.web.composer.audit import BufferingRecorder
    from elspeth.web.composer.continuation_checkpoint_end_format_retry import CheckpointEndFormatRetrySource
    from elspeth.web.composer.continuation_checkpoint_end_locals import CheckpointEndGateLocals
    from elspeth.web.composer.continuation_checkpoint_phase import CheckpointPhaseOwner
    from elspeth.web.composer.provider_quota import ProviderInvocationOwner


@dataclass(frozen=True, slots=True)
class V4EndFirstRetained:
    physical: CheckpointEndPhysicalSource
    prepared: PreparedCheckpointProvider
    admission: V4CheckpointProviderAdmission
    owed: CheckpointEndOwedAdmission
    returned: CheckpointReturnedResponseObservation
    format_retry: CheckpointEndFormatRetrySource | None = None


async def run_v4_checkpoint_end_first_return(
    *,
    advisor: AdvisorCheckpointOwner,
    phase_owner: CheckpointPhaseOwner,
    gate_locals: CheckpointEndGateLocals,
    recorder: BufferingRecorder,
    provider_owner: ProviderInvocationOwner,
    timeout: float | None,
    format_retry: CheckpointEndFormatRetrySource | None = None,
) -> V4EndFirstRetained | EndCheckpointPaidOverflowTerminal | SdkTerminalResult | SdkTerminalFailure:
    """Use the exact original paid owner, then retain raw complete H once."""
    clock = phase_owner.phases.action_clock
    if clock is None:
        raise AuditIntegrityError("END first call lost its original preparation clock")
    with v4_preparation_memo_scope(clock):
        return await _run_v4_checkpoint_end_first_return(
            advisor=advisor,
            phase_owner=phase_owner,
            gate_locals=gate_locals,
            recorder=recorder,
            provider_owner=provider_owner,
            timeout=timeout,
            format_retry=format_retry,
        )


async def _run_v4_checkpoint_end_first_return(
    *,
    advisor: AdvisorCheckpointOwner,
    phase_owner: CheckpointPhaseOwner,
    gate_locals: CheckpointEndGateLocals,
    recorder: BufferingRecorder,
    provider_owner: ProviderInvocationOwner,
    timeout: float | None,
    format_retry: CheckpointEndFormatRetrySource | None,
) -> V4EndFirstRetained | EndCheckpointPaidOverflowTerminal | SdkTerminalResult | SdkTerminalFailure:
    phases = phase_owner.phases
    running = phases.work.running
    sessions = phases.service._sessions_service
    if (
        type(sessions) is not SessionServiceImpl
        or running is None
        or advisor is not phases.service._advisor_checkpoint
        or provider_owner is not phases.provider_owner
        or recorder is not phase_owner.runtime.live.recorder
    ):
        raise AuditIntegrityError("END first call changed its original physical invocation owner")
    bindings = phase_owner.runtime.bindings
    if bindings.session_id is None or bindings.session_operation_context is None:
        raise AuditIntegrityError("END first call lost its original session authority")
    names = await advisor._current_env_ref_names(bindings.session_id)
    if names != phase_owner.runtime.env_ref_names:
        raise AuditIntegrityError("END first call changed its original credential inventory")
    if format_retry is None:
        if phase_owner.phase_source.completed_children:
            raise AuditIntegrityError("END retry requires its original retained format-reprompt source")
    else:
        from elspeth.web.composer.continuation_checkpoint_end_format_retry import require_original_checkpoint_end_format_retry

        require_original_checkpoint_end_format_retry(format_retry)
        if format_retry.phase_owner is not phase_owner or format_retry.gate_locals is not gate_locals:
            raise AuditIntegrityError("END format re-prompt changed its original next gate")
    scope = composition_scope_for_operation(
        phase_owner.runtime.live.state,
        session_id=bindings.session_id,
        session_operation_context=bindings.session_operation_context,
        session_operation_authority=sessions.session_operation_authority,
    )
    arguments = advisor._build_checkpoint_arguments(
        phase="end",
        state=phase_owner.runtime.live.state,
        content_scope=scope,
        env_ref_names=names,
        user_message=bindings.message,
        advisor_review_state=gate_locals.review_state,
        publication_review=gate_locals.publication_review,
    )
    advisor._require_complete_graph_summary(arguments["_elspeth_advisor_summary_parts"])
    if format_retry is not None:
        arguments = advisor_policy.advisor_arguments_with_format_reprompt(
            arguments, schema_valid=format_retry.proof.marker.first_attempt_schema_valid is True
        )
    source = await prepare_checkpoint_end_physical_source(phase_owner=phase_owner, gate_locals=gate_locals)
    try:
        await advisor._call_advisor_with_audit(
            arguments,
            recorder=recorder,
            timeout=timeout,
            structured_output=True,
            provider_owner=provider_owner,
            _outcome=source.capture,
            checkpoint_end_source=source,
            checkpoint_end_retry=format_retry,
        )
    except V4SdkTerminalCommitted as control:
        custody = checkpoint_end_physical_custody(source)
        if custody.v4_sdk_terminal_result is not control.finalization:
            raise AuditIntegrityError("END SDK terminal changed its original committed custody") from control
        finalized = control.finalization
        return SdkTerminalResult(finalized.verified, SdkTerminalFamily.CHECKPOINT_END, finalized.deferred_cancellations)
    except BaseException as failure:
        exceptional = observe_checkpoint_end_sdk_companion(source, failure)
        if exceptional is not None:
            if exceptional.family is not SdkTerminalFamily.CHECKPOINT_END:
                raise AuditIntegrityError("END SDK companion changed its original family") from failure
            return exceptional
        raise
    observed = observe_checkpoint_end_physical_return(source)
    custody = checkpoint_end_physical_custody(source)
    prepared = custody.continuation_checkpoint_v4
    admission = custody.continuation_checkpoint_v4_admission
    if type(prepared) is not PreparedCheckpointProvider or type(admission) is not V4CheckpointProviderAdmission:
        raise AuditIntegrityError("END first return lost its original admitted physical attempt")
    if type(observed) is CheckpointIncompleteResponseObservation:
        settled = await _registered(
            work=phases.work,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL,
            operation=partial(sessions.complete_v4_checkpoint_result_sync, prepared=prepared, admission=admission, observed=observed),
            expected=VerifiedCheckpointPaidOverflowTerminal,
        )
        return EndCheckpointPaidOverflowTerminal(settled)
    if type(observed) is not CheckpointReturnedResponseObservation:
        raise AuditIntegrityError("END first return lost its complete or measured-incomplete result")
    try:
        owed = await _registered(
            work=phases.work,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL,
            operation=partial(sessions.retain_v4_checkpoint_end_owed_sync, prepared=prepared, admission=admission, returned=observed),
            expected=CheckpointEndOwedAdmission,
        )
    except ContinuationRetainedCapacityExceeded as excess:
        settled = await _registered(
            work=phases.work,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL,
            operation=partial(
                sessions.complete_v4_checkpoint_result_sync,
                prepared=prepared,
                admission=admission,
                observed=observed,
                overflow=excess,
            ),
            expected=VerifiedCheckpointPaidOverflowTerminal,
        )
        return EndCheckpointPaidOverflowTerminal(settled)
    return V4EndFirstRetained(source, prepared, admission, owed, observed, format_retry)
