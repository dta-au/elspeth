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

from functools import partial
from typing import TYPE_CHECKING
from uuid import UUID

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import ComposeFrame, EndFrame
from elspeth.web.composer.continuation_bootstrap import bootstrap_values, decode_continuation_bootstrap
from elspeth.web.composer.continuation_end_transitions import EndTransition, end_candidate_for, retain_end_transition
from elspeth.web.composer.continuation_end_values import encode_end_candidate
from elspeth.web.composer.continuation_initialization import _registered
from elspeth.web.composer.continuation_selection import SelectedContinuationState, load_selected_continuation
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.provider_quota import composer_quota_scope
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_primary import PrimaryProviderResult, RetainedPrimaryPhaseData

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_codec import PrivateChunk
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
    from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime
    from elspeth.web.sessions.composer_app_services import ComposerAppServices
    from elspeth.web.sessions.composer_operations import ComposerOperationRecord


def is_end_cut(selected: SelectedContinuationState) -> bool:
    """A cut END owns once its PRIMARY response is known to issue no tools."""
    top = selected.stack.frames[-1]
    return type(top) is EndFrame or (type(top) is ComposeFrame and top.phase == "response_retained")


async def _primary(
    phases: ContinuationLivePhases, selected: SelectedContinuationState
) -> tuple[RestoredContinuationRuntime, PrimaryProviderResult | RetainedPrimaryPhaseData]:
    runtime, inventory = await phases.current(selected)
    result = await phases._primary_for_phase(runtime, inventory, selected)
    return runtime, result


async def primary_issued_no_tools(phases: ContinuationLivePhases, selected: SelectedContinuationState) -> bool:
    _runtime, result = await _primary(phases, selected)
    return not result.outcome.completion.tool_batch.calls


async def _retain(
    phases: ContinuationLivePhases,
    selected: SelectedContinuationState,
    transition: EndTransition,
    candidate: PrivateChunk | None = None,
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
        ),
        expected=StoredContinuationSnapshot,
    )
    following = await load_selected_continuation(authority=phases.authority, running=running, selected=stored, required_work=phases.work)
    await phases.current(following)
    return following


async def enter_continuation_end(phases: ContinuationLivePhases, selected: SelectedContinuationState) -> None:
    """Retain the provider's no-tool reply as the END candidate (no provider call)."""
    _runtime, result = await _primary(phases, selected)
    completion = result.outcome.completion
    if completion.tool_batch.calls:
        raise AuditIntegrityError("END entry requires a PRIMARY response that issued no tool calls")
    compose = selected.stack.frames[-1]
    if type(compose) is not ComposeFrame:
        raise AuditIntegrityError("END entry lost its actual Compose owner")
    candidate = encode_end_candidate(end_candidate_for(compose, candidate_text=completion.message.content or ""))
    await _retain(phases, selected, "end_entered", candidate)


async def review_continuation_end(
    phases: ContinuationLivePhases, services: ComposerAppServices, selected: SelectedContinuationState
) -> ComposerOperationRecord:
    """Run the shared terminal gates and publish through the shared owner."""
    from elspeth.web.sessions.composer_finalization import (
        ComposerFinalizationInputs,
        ContinuationFinalizationBudget,
        settle_composer_result,
    )
    from elspeth.web.sessions.composer_operations import ComposerOperationRecord
    from elspeth.web.sessions.composer_turn import _RECOMPOSE_LABELS, _SEND_LABELS, ComposerTurnObservation, _discard_progress

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
    context = bindings.session_operation_context
    if context is None:
        raise AuditIntegrityError("END review lost its actual COMPOSE operation context")
    # The shared gates read the request's quota scope and genuine user input
    # from task-local scope, exactly as the shared loop binds them.
    with composer_quota_scope(service._require_sessions_service(), context), prompt_guard_user_scope(bindings.message):
        terminate = await service._completion._try_terminate_no_tools(
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
        )
    if terminate.advisor_review_state is not None:
        live.advisor_review_state = terminate.advisor_review_state
    if terminate.action != "return":
        raise AuditIntegrityError("END repair continuation has no landed next-iteration owner")
    if superseded_reply_index is not None:
        del live.llm_messages[superseded_reply_index]
    if terminate.result is None:
        raise AuditIntegrityError("END return lost its actual result")
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

    running = phases.work.running
    owner = phases.work.coordinator.sql_deadline_owner
    if running is None or owner is None:
        raise AuditIntegrityError("END publication lost its actual running and SQL timing owners")
    compose = current.stack.frames[0]
    if type(compose) is not ComposeFrame:
        raise AuditIntegrityError("END publication lost its actual Compose owner")
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
        raise AuditIntegrityError("END publication lost its actual terminal record")
    return record
