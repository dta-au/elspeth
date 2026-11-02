"""The live Continuation END owner (U-40, runtime design §2.2).

END starts at a committed PRIMARY response that issued no tool calls. It runs
the shared terminal gates (proof, interpretation, preflight repair and the END
Advisor review) on the restored live runtime, then publishes through the one
shared finalization owner, ``settle_composer_result``. Nothing here writes a
reply, a graph or a proposal: the published words are the provider's own, and
every gate and review that can change the outcome is the existing one.

The END review makes provider calls, so it runs only after one ``end:``
effect is recorded ``dispatched``. A lost owner therefore settles that effect
as unknown instead of re-running the review; a successful publication commits
it inside the terminal transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING
from uuid import UUID

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer._compose_loop_carriers import _EndCompletionDisposition
from elspeth.web.composer.continuation import ComposeFrame, EndFrame
from elspeth.web.composer.continuation_bootstrap import bootstrap_values, decode_continuation_bootstrap
from elspeth.web.composer.continuation_checkpoint_end_dispositions import (
    EndCheckpointNoSdkTerminal,
    EndCheckpointPaidOverflowTerminal,
    EndCheckpointUnboundRefusalTerminal,
)
from elspeth.web.composer.continuation_checkpoint_end_terminal import (
    EndCheckpointCompleteReview,
    EndCheckpointPaidIncompleteTerminal,
    EndCheckpointRepairContinued,
    observe_end_checkpoint_terminal,
)
from elspeth.web.composer.continuation_end_transitions import (
    EndTransition,
    end_candidate_for,
    recovery_candidate_for,
    retain_end_transition,
)
from elspeth.web.composer.continuation_end_values import encode_end_candidate
from elspeth.web.composer.continuation_initialization import _registered
from elspeth.web.composer.continuation_runtime import capture_continuation_runtime_owners
from elspeth.web.composer.continuation_selection import SelectedContinuationState, load_selected_continuation
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.provider_quota import composer_quota_scope
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_primary import PrimaryProviderResult, RetainedPrimaryPhaseData

if TYPE_CHECKING:
    from elspeth.web.composer._compose_loop_carriers import _TerminateOutcome
    from elspeth.web.composer.continuation_codec import PrivateChunk
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
    from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime, RestoredContinuationRuntime, _CurrentPhaseRead
    from elspeth.web.composer.protocol import ComposerResult
    from elspeth.web.sessions.composer_app_services import ComposerAppServices
    from elspeth.web.sessions.composer_operations import ComposerOperationRecord


@dataclass(frozen=True, slots=True)
class EndReviewExecution:
    """The real shared END gates returned a result that has not been published."""

    current: SelectedContinuationState
    runtime: RestoredContinuationRuntime
    public_result: ComposerResult
    publication_binding: str | None
    disposition: _EndCompletionDisposition = _EndCompletionDisposition.BLOCKED


@dataclass(frozen=True, slots=True)
class EndReviewContinued:
    """The real shared END gates retained the next provider turn instead."""

    current: SelectedContinuationState
    runtime: RestoredContinuationRuntime


def is_end_cut(selected: SelectedContinuationState) -> bool:
    """A cut END owns once its PRIMARY response is known to issue no tools."""
    top = selected.stack.frames[-1]
    return type(top) is EndFrame or (type(top) is ComposeFrame and top.phase == "response_retained")


async def _primary(
    phases: ContinuationLivePhases, selected: SelectedContinuationState, *, phase_read: _CurrentPhaseRead | None = None
) -> tuple[RestoredContinuationRuntime, PrimaryProviderResult | RetainedPrimaryPhaseData]:
    if phase_read is None:
        runtime, inventory = await phases.current(selected)
    else:
        current = phases._require_phase_read(selected, phase_read)
        runtime, inventory = current.runtime, current.inventory
    result = await phases._primary_for_phase(runtime, inventory, selected)
    return runtime, result


async def primary_issued_no_tools(
    phases: ContinuationLivePhases, selected: SelectedContinuationState, *, phase_read: _CurrentPhaseRead | None = None
) -> bool:
    _runtime, result = await _primary(phases, selected, phase_read=phase_read)
    return not result.outcome.completion.tool_batch.calls


async def _retain(
    phases: ContinuationLivePhases,
    selected: SelectedContinuationState,
    transition: EndTransition,
    candidate: PrivateChunk | None = None,
    captured: CapturedContinuationRuntime | None = None,
) -> SelectedContinuationState:
    runtime = phases.runtime
    if runtime is None or runtime.source.selected is not selected:
        raise AuditIntegrityError("END transition lost its actual current runtime selection")
    inventory = runtime.materialization.inventory
    running = phases.work.running
    if running is None or inventory.running is not running:
        raise AuditIntegrityError("END transition lost its actual running owner")
    stored = await _registered(
        work=phases.work,
        source=RequiredWorkSource.CONTINUATION_STATE_SQL,
        operation=partial(
            retain_end_transition,
            phases.authority,
            running,
            selected=selected,
            inventory=inventory,
            transition=transition,
            candidate=candidate,
            captured=captured,
        ),
        expected=StoredContinuationSnapshot,
    )
    following = await load_selected_continuation(authority=phases.authority, running=running, selected=stored, required_work=phases.work)
    await phases.current(following)
    return following


async def enter_continuation_end(
    phases: ContinuationLivePhases, selected: SelectedContinuationState, *, phase_read: _CurrentPhaseRead | None = None
) -> None:
    """Retain the provider's no-tool reply as the END candidate (no provider call)."""
    _runtime, result = await _primary(phases, selected, phase_read=phase_read)
    completion = result.outcome.completion
    if completion.tool_batch.calls:
        raise AuditIntegrityError("END entry requires a PRIMARY response that issued no tool calls")
    compose = selected.stack.frames[-1]
    if type(compose) is not ComposeFrame:
        raise AuditIntegrityError("END entry lost its actual Compose owner")
    candidate = encode_end_candidate(end_candidate_for(compose, candidate_text=completion.message.content or ""))
    await _retain(phases, selected, "end_entered", candidate)


async def enter_continuation_recovery_end(
    phases: ContinuationLivePhases, selected: SelectedContinuationState, *, phase_read: _CurrentPhaseRead | None = None
) -> None:
    """Retain one paid ordinary PRIMARY recovery as an END source for paid Stop."""
    from elspeth.web.composer.recovery_controls import parse_recovery_tool_batch

    _runtime, result = await _primary(phases, selected, phase_read=phase_read)
    try:
        intent = parse_recovery_tool_batch(result.outcome.completion.tool_batch)
    except (TypeError, ValueError) as error:
        raise AuditIntegrityError("ordinary recovery requires its sole provider-authored control") from error
    compose = selected.stack.frames[-1]
    if intent is None or type(compose) is not ComposeFrame:
        raise AuditIntegrityError("ordinary recovery lost its original paid Compose source")
    candidate = encode_end_candidate(recovery_candidate_for(compose, message=intent.message))
    await _retain(phases, selected, "recovery_entered", candidate)


async def review_continuation_end(
    phases: ContinuationLivePhases, services: ComposerAppServices, selected: SelectedContinuationState
) -> (
    ComposerOperationRecord
    | EndCheckpointPaidIncompleteTerminal
    | EndCheckpointPaidOverflowTerminal
    | EndCheckpointNoSdkTerminal
    | EndCheckpointUnboundRefusalTerminal
    | None
):
    """Run the shared terminal gates; publish, or return the next turn to the provider."""
    if selected.stack.active_action_json is not None:
        raise AuditIntegrityError("prescribed END review requires its owned action consumer")
    execution = await execute_continuation_end_review(phases, selected)
    if isinstance(
        execution,
        (
            EndCheckpointPaidIncompleteTerminal,
            EndCheckpointPaidOverflowTerminal,
            EndCheckpointNoSdkTerminal,
            EndCheckpointUnboundRefusalTerminal,
        ),
    ):
        return execution
    if type(execution) is EndReviewContinued:
        return None
    if not isinstance(execution, EndReviewExecution):
        raise AuditIntegrityError("END review lost its actual gate execution")
    return await publish_continuation_result(phases, services, execution.current, execution.runtime, execution.public_result)


async def execute_continuation_end_review(
    phases: ContinuationLivePhases, selected: SelectedContinuationState
) -> (
    EndReviewExecution
    | EndReviewContinued
    | EndCheckpointCompleteReview
    | EndCheckpointRepairContinued
    | EndCheckpointPaidIncompleteTerminal
    | EndCheckpointPaidOverflowTerminal
    | EndCheckpointNoSdkTerminal
    | EndCheckpointUnboundRefusalTerminal
):
    """Execute the one shared review without granting publication authority.

    A prescribed Review must retain its actual action result and coverage before
    a later whole-request Stop can authorize publication. The ordinary END owner
    publishes the same result through ``review_continuation_end``.
    """
    top = selected.stack.frames[-1]
    if type(top) is not EndFrame or top.phase != "review_needed":
        raise AuditIntegrityError("END review has no landed owner for this END phase")
    await _primary(phases, selected)
    current = await _retain(phases, selected, "end_review_dispatched")
    runtime, result = await _primary(phases, current)
    service = phases.service
    live, bindings = runtime.live, runtime.bindings
    completion = result.outcome.completion
    if completion.tool_batch.calls:
        raise AuditIntegrityError("END review requires a PRIMARY response that issued no tool calls")
    if (
        service._terminal_recovery_result(completion, live.state, live.recorder, prior_tools_persisted=live.persisted_tool_call_turn)
        is not None
    ):
        raise AuditIntegrityError("END reply cannot carry a recovery control")
    # The same provider-context discipline as the shared loop: the reply is in
    # the provider context before a repair gate may answer it.
    superseded_reply = completion.message.content or ""
    superseded_reply_index: int | None = None
    if superseded_reply.strip():
        superseded_reply_index = len(live.llm_messages)
        live.llm_messages.append({"role": "assistant", "content": superseded_reply})
    checkpoint_phase_owner = None
    from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4
    from elspeth.web.composer.service import _ELIDE_ADVISOR_EXCHANGE_AT_FINALIZE

    if type(runtime.source.policy) is ContinuationAdmissionPolicyV4:
        from elspeth.web.composer.continuation_checkpoint_phase import prepare_checkpoint_phase_owner

        checkpoint_phase_owner = await prepare_checkpoint_phase_owner(
            phases,
            runtime,
            current,
            superseded_reply_index=superseded_reply_index,
            elide_advisor_exchange=_ELIDE_ADVISOR_EXCHANGE_AT_FINALIZE,
        )
    context = bindings.session_operation_context
    if context is None:
        raise AuditIntegrityError("END review lost its actual COMPOSE operation context")
    # The shared gates read the request's quota scope and genuine user input
    # from task-local scope, exactly as the shared loop binds them.
    with composer_quota_scope(service._require_sessions_service(), context), prompt_guard_user_scope(bindings.message):
        terminate = await observe_end_checkpoint_terminal(
            service._completion._try_terminate_no_tools(
                assistant_message=completion.message,
                turn_evidence=live.turn_evidence,
                session_operation_context=bindings.session_operation_context,
                message=bindings.message,
                llm_messages=live.llm_messages,
                state=live.state,
                session_id=bindings.session_id,
                current_state_id=live.current_state_id,
                initial_version=live.initial_version,
                user_id=bindings.user_id,
                last_runtime_preflight=live.last_runtime_preflight,
                runtime_preflight_cache=live.runtime_preflight_cache,
                session_scope=bindings.session_scope,
                mutation_success_seen=live.mutation_success_seen,
                recorder=live.recorder,
                progress=bindings.progress,
                repair_turns_used=live.repair_turns_used,
                persisted_assistant_message_id=live.persisted_assistant_message_id,
                persisted_assistant_content=live.persisted_assistant_content,
                persisted_tool_call_turn=live.persisted_tool_call_turn,
                advisor_checkpoint_passes_used=live.advisor_checkpoint_passes_used,
                plugin_snapshot=bindings.plugin_snapshot,
                advisor_review_state=live.advisor_review_state,
                deadline=bindings.deadline,
                composition_turns_used=live.composition_turns_used,
                discovery_turns_used=live.discovery_turns_used,
                failed_turn=live.failed_turn,
                completion_gates=bindings.completion_gates,
                provider_owner=bindings.provider_owner,
                checkpoint_phase_owner=checkpoint_phase_owner,
            )
        )
    if isinstance(
        terminate,
        (
            EndCheckpointPaidIncompleteTerminal,
            EndCheckpointCompleteReview,
            EndCheckpointRepairContinued,
            EndCheckpointPaidOverflowTerminal,
            EndCheckpointNoSdkTerminal,
            EndCheckpointUnboundRefusalTerminal,
        ),
    ):
        return terminate
    return await continue_end_after_terminal_review(
        phases,
        current=current,
        runtime=runtime,
        terminate=terminate,
        superseded_reply=superseded_reply,
        superseded_reply_index=superseded_reply_index,
        elide_advisor_exchange=_ELIDE_ADVISOR_EXCHANGE_AT_FINALIZE,
    )


async def continue_end_after_terminal_review(
    phases: ContinuationLivePhases,
    *,
    current: SelectedContinuationState,
    runtime: RestoredContinuationRuntime,
    terminate: _TerminateOutcome,
    superseded_reply: str,
    superseded_reply_index: int | None,
    elide_advisor_exchange: bool,
) -> EndReviewExecution | EndReviewContinued:
    """Finish the original END caller without reappending its reply or invoking SDK."""
    service = phases.service
    live, bindings = runtime.live, runtime.bindings
    context = bindings.session_operation_context
    if context is None:
        raise AuditIntegrityError("END continuation lost its original operation context")
    if superseded_reply_index is not None and (
        not 0 <= superseded_reply_index < len(live.llm_messages)
        or live.llm_messages[superseded_reply_index] != {"role": "assistant", "content": superseded_reply}
    ):
        raise AuditIntegrityError("END continuation changed its original superseded reply position")
    if terminate.advisor_review_state is not None:
        live.advisor_review_state = terminate.advisor_review_state
    if terminate.action != "return":
        # A repair or flagged review asked the model to keep working. The
        # next authoring transition is the provider's next PRIMARY call.
        live.repair_turns_used += terminate.repair_turns_delta
        live.advisor_checkpoint_passes_used += terminate.advisor_passes_delta
        if superseded_reply_index is not None:
            # The gate superseded this reply: never published, kept as audit.
            await service._completion._persist_withheld_reply(
                "repair_gate_superseded",
                superseded_reply,
                session_id=bindings.session_id,
                session_operation_context=context,
            )
        if terminate.advisor_injection_index is not None:
            live.advisor_repair_context_introduced = True
            if elide_advisor_exchange:
                live.pending_advisor_elision_indices.append(terminate.advisor_injection_index)
                if superseded_reply_index is not None:
                    live.pending_advisor_elision_indices.append(superseded_reply_index)
        live.turn_evidence.transcript.observe_messages(live.llm_messages)
        captured = await capture_continuation_runtime_owners(runtime, selected=current, inventory=runtime.materialization.inventory)
        following = await _retain(phases, current, "end_repair_continued", captured=captured)
        following_runtime = phases.runtime
        if following_runtime is None or following_runtime.source.selected is not following:
            raise AuditIntegrityError("continued END review lost its actual retained runtime")
        return EndReviewContinued(current=following, runtime=following_runtime)
    if superseded_reply_index is not None:
        del live.llm_messages[superseded_reply_index]
    if terminate.result is None:
        raise AuditIntegrityError("END return lost its actual result")
    live.advisor_checkpoint_passes_used += terminate.advisor_passes_delta
    public_result = terminate.result
    if live.advisor_repair_context_introduced:
        public_result = await service._completion._qualified_advisor_repair_public_result(
            public_result,
            user_id=bindings.user_id,
            session_id=bindings.session_id,
            session_operation_context=bindings.session_operation_context,
            cache=live.runtime_preflight_cache,
            initial_version=live.initial_version,
            session_scope=bindings.session_scope,
            plugin_snapshot=bindings.plugin_snapshot,
            publication_binding=terminate.publication_binding,
            turn_evidence=live.turn_evidence,
            user_message=bindings.message,
        )
    public_result = service._completion._append_checkpoint_receipt(public_result, turn_evidence=live.turn_evidence)
    return EndReviewExecution(
        current=current,
        runtime=runtime,
        public_result=public_result,
        publication_binding=terminate.publication_binding,
        disposition=terminate.end_disposition,
    )


async def publish_semantic_continuation_result(
    phases: ContinuationLivePhases, services: ComposerAppServices, selected: SelectedContinuationState
) -> ComposerOperationRecord:
    """Derive the actual cold-reviewed result; the final transaction re-proves it."""
    from elspeth.web.composer.continuation_codec import encode_continuation_snapshot
    from elspeth.web.composer.protocol import ComposerResult
    from elspeth.web.sessions.continuation import load_snapshot
    from elspeth.web.sessions.continuation_semantic_actions import require_semantic_publication_on_connection
    from elspeth.web.sessions.continuation_semantic_advisor import (
        require_semantic_iteration_publication_on_connection,
        require_semantic_ordinary_recovery_publication_on_connection,
        require_semantic_owned_iteration_publication_on_connection,
    )
    from elspeth.web.sessions.locking import locked_session_transaction

    runtime, _inventory = await phases.current(selected)
    running = phases.work.running
    if running is None or runtime.bindings.session_operation_context is not running.session_operation_context:
        raise AuditIntegrityError("semantic publication lost its actual runtime/claim owners")

    def read_result() -> ComposerResult:
        with locked_session_transaction(phases.authority._engine, str(running.claim.session_id)) as conn:
            stored = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
            if stored.checkpoint_id != selected.checkpoint_id or stored.payload_json != encode_continuation_snapshot(selected.stack):
                raise AuditIntegrityError("semantic publication changed its selected source before result preparation")
            owned = require_semantic_owned_iteration_publication_on_connection(conn, running, selected=stored)
            iteration = require_semantic_iteration_publication_on_connection(conn, running, selected=stored) if owned is None else None
            recovery = (
                require_semantic_ordinary_recovery_publication_on_connection(conn, running, selected=stored)
                if owned is None and iteration is None
                else None
            )
            if owned is not None:
                original = owned.returned.result
                audit_call = owned.admission.paid.audit_call
            elif iteration is not None:
                original = iteration.returned.result
                audit_call = iteration.admission.paid.audit_call
            elif recovery is not None:
                original = recovery.result
                audit_call = recovery.admission.paid.audit_call
            else:
                review = require_semantic_publication_on_connection(conn, running, selected=stored)
                if review is None:
                    raise AuditIntegrityError("semantic publication requires its actual retained paid Stop")
                original = review.review_result.result
                audit_call = review.admission.paid.audit_call
            return replace(original, llm_calls=(*original.llm_calls, audit_call))

    public_result = await _registered(
        work=phases.work,
        source=RequiredWorkSource.CONTINUATION_STATE_SQL,
        operation=read_result,
        expected=ComposerResult,
    )
    return await publish_continuation_result(phases, services, selected, runtime, public_result)


async def publish_continuation_result(
    phases: ContinuationLivePhases,
    services: ComposerAppServices,
    current: SelectedContinuationState,
    runtime: RestoredContinuationRuntime,
    public_result: ComposerResult,
) -> ComposerOperationRecord:
    """Publish one provider-authored result through the one shared finalizer.

    The terminal composite (assistant row, audit cohort, proposals and the
    Continuation terminal) commits in one transaction; this owner never builds the
    reply, graph or proposal itself.
    """
    from elspeth.web.sessions.composer_finalization import (
        ComposerFinalizationInputs,
        ContinuationFinalizationBudget,
        settle_composer_result,
    )
    from elspeth.web.sessions.composer_operations import ComposerOperationRecord
    from elspeth.web.sessions.composer_turn import _RECOMPOSE_LABELS, _SEND_LABELS, ComposerTurnObservation, _discard_progress

    service, bindings = phases.service, runtime.bindings
    running = phases.work.running
    owner = phases.work.coordinator.sql_deadline_owner
    if running is None or owner is None:
        raise AuditIntegrityError("publication lost its actual running and SQL timing owners")
    compose = current.stack.frames[0]
    if type(compose) is not ComposeFrame:
        raise AuditIntegrityError("publication lost its actual Compose owner")
    bootstrap = decode_continuation_bootstrap(next(chunk for chunk in current.chunks if chunk.reference == compose.bootstrap))
    values = bootstrap_values(bootstrap)
    sessions = service._require_sessions_service()
    session = await sessions.get_session(UUID(current.stack.logical.session_id))
    base = values.recovery_context.turn_start_state_id
    base_id = UUID(base) if base is not None else None
    state_record = await sessions.get_state(base_id) if base_id is not None else None
    observation = ComposerTurnObservation(required_work=phases.work.coordinator, compose_base_state_id=base_id)
    record = await settle_composer_result(
        ComposerFinalizationInputs(
            service=sessions,
            session=session,
            recovery_context=values.recovery_context,
            lease=phases.lease,
            compose_base_state_id=base_id,
            running=running,
            observation=observation,
            progress_sink=bindings.progress if bindings.progress is not None else _discard_progress,
            state=values.initial_state,
            chat_ingress={} if bindings.ingress is None else bindings.ingress,
            chat_ingress_inputs=() if bindings.chat_ingress_inputs is None else bindings.chat_ingress_inputs,
            prior_completion_gates_facts=values.completion_gates,
            state_record=state_record,
            budget_anchor=ContinuationFinalizationBudget(owner=owner),
            services=services,
            turn=bootstrap,
            labels=_SEND_LABELS if current.stack.logical.kind == "compose_message" else _RECOMPOSE_LABELS,
            provider_binding=phases.work,
            settings=services.settings,
            plugin_snapshot=bindings.plugin_snapshot,
            profile_registry=services.operator_profile_registry,
            required_work=phases.work.coordinator,
        ),
        public_result,
    )
    if type(record) is not ComposerOperationRecord:
        raise AuditIntegrityError("publication lost its actual terminal record")
    return record
