"""The live Continuation post-batch owner (U-40, runtime design §2.2).

An issued batch's P4 publication has been applied. This owner runs the shared
loop's post-batch phase on the restored live runtime: the batch's outcome
fold, the EARLY Advisor checkpoint, the anti-anchor hint and turn accounting
(``_classify_and_budget_turn``). Then either:

* the request continues: the Compose frame returns to ``action_ready`` and
  the next authoring transition is the provider's own PRIMARY call; or
* the post-batch phase ended the request itself (source-proof stop or the
  last-chance reply): the result is published through the shared finalizer.

The fold, optional EARLY Advisor and classifier return to the driver between
actions. Each paid stage has its own original allowance and durable effect;
the complete returned result is retained before a later semantic assessment.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Literal

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer._compose_loop_carriers import _ClassifyOutcome
from elspeth.web.composer.continuation_checkpoint_end_dispositions import (
    EndCheckpointNoSdkTerminal,
    EndCheckpointPaidOverflowTerminal,
    EndCheckpointUnboundRefusalTerminal,
)
from elspeth.web.composer.continuation_checkpoint_end_terminal import EndCheckpointPaidIncompleteTerminal
from elspeth.web.composer.continuation_classifier_handoff import ClassifyPostPrimary, ClassifySourceProofStop
from elspeth.web.composer.continuation_classifier_owned_source import ClassifyReplyRefusalReady
from elspeth.web.composer.continuation_codec import encode_continuation_snapshot
from elspeth.web.composer.continuation_initialization import _registered
from elspeth.web.composer.continuation_iteration_transitions import IterationTransition, applied_batch_cut, retain_iteration_transition
from elspeth.web.composer.continuation_runtime import capture_continuation_runtime_owners
from elspeth.web.composer.continuation_selection import SelectedContinuationState, load_selected_continuation
from elspeth.web.composer.continuation_semantic_advisor_return import (
    ObservedClassification,
    ObservedClassifierReplyDeadline,
    capture_original_classification_pending,
    complete_continuation_iteration_return,
    observe_continuation_classifier_end,
    observe_continuation_classifier_end_input,
    observe_continuation_classifier_reply_refusal,
    observe_continuation_iteration_classification,
    observe_continuation_iteration_early,
    refresh_original_classification_source,
)
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_classifier_primary import (
    VerifiedClassifierPaidIncompleteTerminal,
    VerifiedClassifierPaidOverflowTerminal,
)
from elspeth.web.sessions.continuation_classifier_primary_no_sdk import ClassifierNoSdkRefusalReceipt
from elspeth.web.sessions.continuation_iteration_return import (
    IterationPendingEndData,
    IterationStageData,
    begin_iteration_paid_stage,
    begin_iteration_reply_refusal,
    read_iteration_pending_end,
    read_iteration_reply_refusal,
    retain_iteration_early_completed,
    retain_iteration_pending_end,
    retain_iteration_prepared,
    retain_iteration_returned,
)
from elspeth.web.sessions.continuation_sdk_terminal_result import SdkTerminalFailure, SdkTerminalResult

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
    from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime, _CurrentPhaseRead
    from elspeth.web.sessions.composer_app_services import ComposerAppServices
    from elspeth.web.sessions.composer_operations import ComposerOperationRecord


def is_applied_batch_cut(selected: SelectedContinuationState) -> bool:
    try:
        applied_batch_cut(selected.stack.frames)
    except AuditIntegrityError:
        return False
    return True


async def _retain(
    phases: ContinuationLivePhases,
    selected: SelectedContinuationState,
    transition: IterationTransition,
    captured: CapturedContinuationRuntime | None = None,
) -> _CurrentPhaseRead:
    runtime = phases.runtime
    if runtime is None or runtime.source.selected is not selected:
        raise AuditIntegrityError("post-batch transition lost its actual current runtime selection")
    inventory = runtime.materialization.inventory
    running = phases.work.running
    if running is None or inventory.running is not running:
        raise AuditIntegrityError("post-batch transition lost its actual running owner")
    stored = await _registered(
        work=phases.work,
        source=RequiredWorkSource.CONTINUATION_STATE_SQL,
        operation=partial(
            retain_iteration_transition,
            phases.authority,
            running,
            selected=selected,
            inventory=inventory,
            transition=transition,
            captured=captured,
        ),
        expected=StoredContinuationSnapshot,
    )
    following = await load_selected_continuation(authority=phases.authority, running=running, selected=stored, required_work=phases.work)
    return await phases._current_phase_read(following)


async def advance_continuation_iteration(
    phases: ContinuationLivePhases,
    services: ComposerAppServices,
    selected: SelectedContinuationState,
    *,
    iteration_stage: IterationStageData | None = None,
) -> (
    ComposerOperationRecord
    | ObservedClassifierReplyDeadline
    | VerifiedClassifierPaidOverflowTerminal
    | VerifiedClassifierPaidIncompleteTerminal
    | ClassifierNoSdkRefusalReceipt
    | SdkTerminalResult
    | SdkTerminalFailure
    | EndCheckpointPaidIncompleteTerminal
    | EndCheckpointPaidOverflowTerminal
    | EndCheckpointNoSdkTerminal
    | EndCheckpointUnboundRefusalTerminal
    | None
):
    from elspeth.web.composer.service import _ELIDE_ADVISOR_EXCHANGE_AT_FINALIZE, _record_advisor_repair_mutations

    applied_batch_cut(selected.stack.frames)
    finished = phases.finished_batch
    phase_read = None
    if finished is None:
        phase_read = await phases.restore_finished_batch(selected)
        finished = phases.finished_batch
    if finished is None:
        raise AuditIntegrityError("post-batch phase has no landed cold source for this applied batch")
    dispatch, persist, pre_batch_state = finished
    if (
        dispatch.plugin_crash is not None
        or dispatch.advisor_failure is not None
        or dispatch.service_failure is not None
        or dispatch.advisor_compose_timeout is not None
        or persist.effect_verification_failure is not None
    ):
        raise AuditIntegrityError("post-batch phase reached an unsettled batch failure")
    if persist.authority_revocation is not None:
        from elspeth.web.composer.protocol import ComposerMutationAuthorityRevoked

        runtime = phases.runtime
        if runtime is None:
            raise AuditIntegrityError("post-batch revocation lost its actual runtime")
        raise ComposerMutationAuthorityRevoked(
            revocation=persist.authority_revocation,
            llm_calls=runtime.live.recorder.llm_calls,
            failed_turn=persist.failed_turn,
            state=pre_batch_state,
            initial_version=runtime.live.initial_version,
        )
    if phase_read is None:
        phase_read = await phases._current_phase_read(selected)
    phases._require_phase_read(selected, phase_read)
    runtime, _inventory = phase_read.runtime, phase_read.inventory
    if iteration_stage is not None and (
        type(iteration_stage) is not IterationStageData
        or iteration_stage.current.checkpoint_id != selected.checkpoint_id
        or iteration_stage.current.cursor != selected.stack.cursor
        or iteration_stage.current.payload_json != encode_continuation_snapshot(selected.stack)
    ):
        raise AuditIntegrityError("post-batch caller substituted its current admitted stage")
    live, bindings = runtime.live, runtime.bindings
    context = bindings.session_operation_context
    if context is None:
        raise AuditIntegrityError("post-batch phase lost its actual COMPOSE operation context")
    running = phases.work.running
    if running is None or runtime.materialization.inventory.running is not running:
        raise AuditIntegrityError("post-batch stage lost its actual running claim")

    async def retain(operation: Callable[[], StoredContinuationSnapshot]) -> StoredContinuationSnapshot:
        return await _registered(
            work=phases.work,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL,
            operation=operation,
            expected=StoredContinuationSnapshot,
        )

    if iteration_stage is None:
        # This fold is pure; the driver returns before either paid stage.
        live.state = dispatch.state
        live.last_validation = dispatch.last_validation
        live.last_runtime_preflight = dispatch.last_runtime_preflight
        if dispatch.mutation_success_observed:
            live.mutation_success_seen = True
        live.advisor_review_state = _record_advisor_repair_mutations(live.advisor_review_state, dispatch.tool_outcomes)
        live.current_state_id = persist.current_state_id
        live.persisted_assistant_message_id = persist.persisted_assistant_message_id
        live.persisted_assistant_content = persist.persisted_assistant_content
        live.persisted_tool_call_turn = persist.persisted_tool_call_turn
        live.failed_turn = persist.failed_turn
        live.turn_evidence.transcript.observe_messages(live.llm_messages)
        captured = await capture_continuation_runtime_owners(runtime, selected=selected, inventory=runtime.materialization.inventory)
        await retain(
            partial(
                retain_iteration_prepared,
                phases.authority,
                running,
                selected=selected,
                inventory=runtime.materialization.inventory,
                captured=captured,
            )
        )
        return None
    if iteration_stage.stage not in ("early_ready", "classify_ready", "end_ready", "reply_refusal_ready"):
        raise AuditIntegrityError("post-batch paid stage cannot replay a dispatched or completed owner")
    stage: Literal["early", "classify", "end", "reply_refusal"] = (
        "early"
        if iteration_stage.stage == "early_ready"
        else "end"
        if iteration_stage.stage == "end_ready"
        else "reply_refusal"
        if iteration_stage.stage == "reply_refusal_ready"
        else "classify"
    )
    end_input = None
    if stage == "end":
        ready_input = await _registered(
            work=phases.work,
            source=RequiredWorkSource.CONTINUATION_READ_SQL,
            operation=partial(
                read_iteration_pending_end,
                phases.authority,
                running,
                selected=selected,
                inventory=runtime.materialization.inventory,
            ),
            expected=IterationPendingEndData,
        )
        end_input = observe_continuation_classifier_end_input(
            phases,
            runtime,
            selected=selected,
            pending=ready_input.pending,
            dispatch=dispatch,
            persist=persist,
            primary_response=ready_input.primary_response,
        )
    if stage == "reply_refusal":
        stored = await retain(
            partial(
                begin_iteration_reply_refusal,
                phases.authority,
                running,
                selected=selected,
                inventory=runtime.materialization.inventory,
            )
        )
    else:
        stored = await retain(
            partial(
                begin_iteration_paid_stage,
                phases.authority,
                running,
                selected=selected,
                inventory=runtime.materialization.inventory,
                stage=stage,
                end_input=end_input,
            )
        )
    current = await load_selected_continuation(authority=phases.authority, running=running, selected=stored, required_work=phases.work)
    phase_read = await phases._current_phase_read(current)
    if phase_read.finish_read is None:
        phases._require_phase_read(current, phase_read)
        runtime = phase_read.runtime
    else:
        runtime, _inventory = await phases.current(current)
    live = runtime.live
    if stage == "early":
        observed_early, captured = await observe_continuation_iteration_early(
            phases, runtime, selected=current, dispatch=dispatch, persist=persist, pre_batch_state=pre_batch_state
        )
        await retain(
            partial(
                retain_iteration_early_completed,
                phases.authority,
                running,
                selected=current,
                inventory=runtime.materialization.inventory,
                observed=observed_early,
                captured=captured,
            )
        )
        return None
    if (
        stage == "classify"
        and live.pending_advisor_elision_indices
        and dispatch.mutation_success_observed
        and _ELIDE_ADVISOR_EXCHANGE_AT_FINALIZE
    ):
        for elision_index in sorted(live.pending_advisor_elision_indices, reverse=True):
            del live.llm_messages[elision_index]
        live.pending_advisor_elision_indices = []
    observed: (
        ObservedClassification
        | ObservedClassifierReplyDeadline
        | VerifiedClassifierPaidOverflowTerminal
        | VerifiedClassifierPaidIncompleteTerminal
        | ClassifierNoSdkRefusalReceipt
        | SdkTerminalResult
        | SdkTerminalFailure
        | EndCheckpointPaidIncompleteTerminal
        | EndCheckpointPaidOverflowTerminal
        | EndCheckpointNoSdkTerminal
        | EndCheckpointUnboundRefusalTerminal
    )
    if stage == "reply_refusal":
        refusal_pending = await _registered(
            work=phases.work,
            source=RequiredWorkSource.CONTINUATION_READ_SQL,
            operation=partial(
                read_iteration_reply_refusal, phases.authority, running, selected=current, inventory=runtime.materialization.inventory
            ),
            expected=ClassifyReplyRefusalReady,
        )
        observed = await observe_continuation_classifier_reply_refusal(
            phases,
            runtime,
            selected=current,
            pending=refusal_pending,
            dispatch=dispatch,
            persist=persist,
        )
    elif stage == "end":
        pending_source = await _registered(
            work=phases.work,
            source=RequiredWorkSource.CONTINUATION_READ_SQL,
            operation=partial(
                read_iteration_pending_end, phases.authority, running, selected=current, inventory=runtime.materialization.inventory
            ),
            expected=IterationPendingEndData,
        )
        observed = await observe_continuation_classifier_end(
            phases,
            runtime,
            selected=current,
            pending=pending_source.pending,
            dispatch=dispatch,
            persist=persist,
            primary_response=pending_source.primary_response,
            owned_source_json=pending_source.owned_source_json,
        )
    else:
        observed = await observe_continuation_iteration_classification(
            phases, runtime, selected=current, dispatch=dispatch, persist=persist
        )
    if isinstance(
        observed,
        (
            EndCheckpointPaidIncompleteTerminal,
            EndCheckpointPaidOverflowTerminal,
            EndCheckpointNoSdkTerminal,
            EndCheckpointUnboundRefusalTerminal,
        ),
    ):
        return observed
    if isinstance(
        observed, (VerifiedClassifierPaidOverflowTerminal, VerifiedClassifierPaidIncompleteTerminal, ClassifierNoSdkRefusalReceipt)
    ):
        return observed
    if isinstance(observed, (SdkTerminalResult, SdkTerminalFailure)):
        if type(observed) not in (SdkTerminalResult, SdkTerminalFailure):
            raise AuditIntegrityError("iteration SDK result requires its original nominal terminal")
        return observed
    if isinstance(observed, ObservedClassifierReplyDeadline):
        return observed
    classify = observed.outcome
    if isinstance(classify, (ClassifyPostPrimary, ClassifySourceProofStop)):
        captured, current = await capture_original_classification_pending(observed, phases, runtime)
        runtime = phases.runtime
        if runtime is None or runtime.source.selected is not current:
            raise AuditIntegrityError("pending END lost its actual refreshed provider/runtime source")
        await retain(
            partial(
                retain_iteration_pending_end,
                phases.authority,
                running,
                selected=current,
                inventory=runtime.materialization.inventory,
                observed=observed,
                captured=captured,
            )
        )
        return None
    if type(classify) is not _ClassifyOutcome:
        raise AuditIntegrityError("post-batch phase lost its actual nominal classifier result")
    runtime, current = await refresh_original_classification_source(observed, phases, runtime)
    live = runtime.live
    live.composition_turns_used += classify.composition_turns_delta
    live.discovery_turns_used += classify.discovery_turns_delta
    live.advisor_checkpoint_passes_used += classify.advisor_passes_delta
    live.repair_turns_used += classify.repair_turns_delta
    if classify.action == "return":
        returned, captured = await complete_continuation_iteration_return(observed, phases, runtime)
        await retain(
            partial(
                retain_iteration_returned,
                phases.authority,
                running,
                selected=current,
                inventory=runtime.materialization.inventory,
                observed=returned,
                captured=captured,
            )
        )
        phases.finished_batch = None
        return None
    live.turn_evidence.transcript.observe_messages(live.llm_messages)
    captured = await capture_continuation_runtime_owners(runtime, selected=current, inventory=runtime.materialization.inventory)
    await _retain(phases, current, "iteration_advanced", captured)
    phases.finished_batch = None
    return None
