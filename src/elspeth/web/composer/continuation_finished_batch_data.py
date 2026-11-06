"""Historical applied-P4 batch DATA for the current post-batch owner.

The caller supplies separately proved current SQL and PRIMARY results. This
projection never reconstructs a P4 writer outcome, checkpoint witness, or
execution authority from retained bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from elspeth.contracts.errors import AuditIntegrityError, FailedTurnMetadata
from elspeth.web.composer._compose_loop_carriers import _DispatchOutcome
from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_batch_data import RestoredBatchData
from elspeth.web.composer.continuation_codec import encode_continuation_snapshot
from elspeth.web.composer.continuation_iteration_transitions import applied_batch_cut
from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tool_batch import ToolBatchContext, _finish_continuation_batch_live
from elspeth.web.sessions.continuation_iteration_return import IterationStageData
from elspeth.web.sessions.continuation_p4 import ReadmittedP4Result, decode_p4_receipt
from elspeth.web.sessions.continuation_primary import RetainedPrimaryPhaseData


@dataclass(frozen=True, slots=True)
class ColdPostBatchPersistData:
    """Only the historical publication facts consumed after an applied P4."""

    current_state_id: str | None
    persisted_assistant_message_id: str
    persisted_assistant_content: str
    persisted_tool_call_turn: Literal[True]
    failed_turn: FailedTurnMetadata
    effect_verification_failure: None = None
    authority_revocation: None = None


@dataclass(frozen=True, slots=True)
class ColdFinishedBatchData:
    """Complete cold dispatch and publication DATA, with no warm writer."""

    dispatch: _DispatchOutcome
    persist: ColdPostBatchPersistData
    pre_batch_state: CompositionState


def project_cold_finished_batch_data(
    *,
    runtime: RestoredContinuationRuntime,
    selected: SelectedContinuationState,
    primary: RetainedPrimaryPhaseData,
    p4_result: ReadmittedP4Result,
    iteration_stage: IterationStageData | None,
    context: ToolBatchContext,
) -> ColdFinishedBatchData:
    """Project one clean applied batch after the caller's current SQL proof."""
    if (
        type(runtime) is not RestoredContinuationRuntime
        or type(selected) is not SelectedContinuationState
        or type(primary) is not RetainedPrimaryPhaseData
        or type(p4_result) is not ReadmittedP4Result
        or (iteration_stage is not None and type(iteration_stage) is not IterationStageData)
        or type(context) is not ToolBatchContext
        or runtime.source.selected is not selected
        or context.recorder is not runtime.live.recorder
        or context.session_operation_context is not runtime.bindings.session_operation_context
    ):
        raise AuditIntegrityError("cold finished batch changed its actual current source owners")
    compose, frame = applied_batch_cut(selected.stack.frames)
    data = runtime.graph.batch
    if (
        type(compose) is not ComposeFrame
        or type(data) is not RestoredBatchData
        or data.batch.phase != "complete"
        or data.batch.p4 is not None
        or data.batch.p4_receipt_json is None
        or data.batch.p4_input_json is None
        or data.batch.p4_verification_failed
        or runtime.live.state is not data.batch.state
        or runtime.graph.recorder is not runtime.live.recorder
        or primary.response.reference != frame.response
        or primary.outcome.completion.tool_batch.calls is not data.calls
        or data.batch.current_call is not None
        or data.batch.retained_failure is not None
        or data.batch.plugin_crash is not None
        or data.batch.plugin_crash_cause is not None
        or data.batch.advisor_failure is not None
        or data.batch.service_failure is not None
        or data.batch.advisor_compose_timeout is not None
    ):
        raise AuditIntegrityError("cold finished batch lacks one clean applied original")
    applications = runtime.graph.applications
    effect_key = f"batch:{frame.response.digest}:p4"
    if applications is None or not applications.applications or applications.applications[-1].effect_key != effect_key:
        raise AuditIntegrityError("cold finished batch lost its selected applied P4 journal")
    receipt, outcome = decode_p4_receipt(data.batch.p4_receipt_json, env_ref_names=runtime.env_ref_names)
    if (
        receipt != p4_result.receipt
        or outcome != p4_result.outcome
        or receipt.effect_key != effect_key
        or receipt.prepared_p4_json != data.batch.p4_input_json
        or outcome.unwind_audit_failed
        or outcome.authority_revocation_json is not None
        or receipt.assistant_id is None
        or outcome.assistant_id != str(receipt.assistant_id)
        or outcome.published_assistant_content is None
        or len(receipt.invocation_ids) != len(data.batch.tool_outcomes)
        or outcome.current_state_id != runtime.live.current_state_id
    ):
        raise AuditIntegrityError("cold finished batch changed its complete P4 receipt or publication")
    failed_turn = FailedTurnMetadata(
        assistant_message_id=outcome.assistant_id,
        tool_calls_attempted=len(data.calls),
        tool_responses_persisted=len(receipt.invocation_ids),
    )
    # The raw applied-P4 snapshot still carries the Compose frame from before
    # publication. Only an authenticated iteration stage proves that the later
    # fold captured the P4 assistant and failed-turn fields in that frame.
    if iteration_stage is not None:
        current = iteration_stage.current
        if (
            current.checkpoint_id != selected.checkpoint_id
            or current.payload_json != encode_continuation_snapshot(selected.stack)
            or runtime.live.persisted_assistant_message_id != outcome.assistant_id
            or runtime.live.persisted_assistant_content != outcome.published_assistant_content
            or not runtime.live.persisted_tool_call_turn
            or runtime.live.failed_turn != failed_turn
        ):
            raise AuditIntegrityError("cold finished batch changed its proved post-P4 fold")
    dispatch, _advisor_calls = _finish_continuation_batch_live(
        call_model=primary.outcome,
        ctx=context,
        acc=data.accumulator,
        batch=data.batch,
    )
    if dispatch.authority_revocation is not None or data.accumulator.authority_revocation is not None:
        raise AuditIntegrityError("cold finished batch cannot treat a revoked graph as clean")
    persist = ColdPostBatchPersistData(
        current_state_id=outcome.current_state_id,
        persisted_assistant_message_id=outcome.assistant_id,
        persisted_assistant_content=outcome.published_assistant_content,
        persisted_tool_call_turn=True,
        failed_turn=failed_turn,
    )
    return ColdFinishedBatchData(dispatch, persist, data.batch.entry_state)
