"""The live Continuation post-batch owner (U-40, runtime design §2.2).

An issued batch's P4 publication has been applied. This owner runs the shared
loop's post-batch phase on the restored live runtime: the batch's outcome
fold, the EARLY Advisor checkpoint, the anti-anchor hint and turn accounting
(``_classify_and_budget_turn``). Then either:

* the request continues: the Compose frame returns to ``action_ready`` and
  the next authoring transition is the provider's own PRIMARY call; or
* the post-batch phase ended the request itself (source-proof stop or the
  last-chance reply): the result is published through the shared finalizer.

The phase may call the Advisor, so it runs only after one ``advance:`` effect
is recorded ``dispatched``.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_initialization import _registered
from elspeth.web.composer.continuation_iteration_transitions import IterationTransition, applied_batch_cut, retain_iteration_transition
from elspeth.web.composer.continuation_runtime import capture_continuation_runtime_owners
from elspeth.web.composer.continuation_selection import SelectedContinuationState, load_selected_continuation
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.provider_quota import composer_quota_scope
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_end import publish_continuation_result

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
    from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime
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
) -> SelectedContinuationState:
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
    await phases.current(following)
    return following


async def advance_continuation_iteration(
    phases: ContinuationLivePhases, services: ComposerAppServices, selected: SelectedContinuationState
) -> ComposerOperationRecord | None:
    from elspeth.web.composer.no_tool_policy import state_is_structurally_empty
    from elspeth.web.composer.service import _ELIDE_ADVISOR_EXCHANGE_AT_FINALIZE, _record_advisor_repair_mutations

    applied_batch_cut(selected.stack.frames)
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
    await phases.current(selected)
    current = await _retain(phases, selected, "iteration_review_dispatched")
    runtime, _inventory = await phases.current(current)
    service, live, bindings = phases.service, runtime.live, runtime.bindings
    context = bindings.session_operation_context
    if context is None:
        raise AuditIntegrityError("post-batch phase lost its actual COMPOSE operation context")
    # The batch outcome fold of the shared loop (state, mutation history and
    # the Advisor repair record). Per-call state already moved with each call.
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
    with composer_quota_scope(service._require_sessions_service(), context), prompt_guard_user_scope(bindings.message):
        if state_is_structurally_empty(pre_batch_state) and not state_is_structurally_empty(dispatch.state):
            # The EARLY advisory checkpoint runs on the empty -> non-empty
            # transition. Advisory only; its findings, if any, are context.
            before = len(live.llm_messages)
            await service._advisor_checkpoint._maybe_run_early_checkpoint(
                state=dispatch.state,
                prev_state=pre_batch_state,
                session_id=bindings.session_id,
                llm_messages=live.llm_messages,
                recorder=live.recorder,
                progress=bindings.progress,
                deadline=bindings.deadline,
                session_operation_context=context,
                provider_owner=bindings.provider_owner,
                turn_evidence=live.turn_evidence,
            )
            if len(live.llm_messages) > before:
                live.advisor_repair_context_introduced = True
        if live.pending_advisor_elision_indices and dispatch.mutation_success_observed and _ELIDE_ADVISOR_EXCHANGE_AT_FINALIZE:
            for elision_index in sorted(live.pending_advisor_elision_indices, reverse=True):
                del live.llm_messages[elision_index]
            live.pending_advisor_elision_indices = []
        classify = await service._classify_and_budget_turn(
            dispatch=dispatch,
            turn_evidence=live.turn_evidence,
            session_operation_context=context,
            persist=persist,
            llm_messages=live.llm_messages,
            tools=live.tools,
            recorder=live.recorder,
            anti_anchor=live.anti_anchor,
            progress=bindings.progress,
            message=bindings.message,
            initial_version=live.initial_version,
            deadline=bindings.deadline,
            runtime_preflight_cache=live.runtime_preflight_cache,
            session_scope=bindings.session_scope,
            session_id=bindings.session_id,
            user_id=bindings.user_id,
            mutation_success_seen=live.mutation_success_seen,
            repair_turns_used=live.repair_turns_used,
            composition_turns_used=live.composition_turns_used,
            discovery_turns_used=live.discovery_turns_used,
            advisor_checkpoint_passes_used=live.advisor_checkpoint_passes_used,
            plugin_snapshot=bindings.plugin_snapshot,
            advisor_review_state=live.advisor_review_state,
            completion_gates=bindings.completion_gates,
            provider_owner=bindings.provider_owner,
        )
        live.composition_turns_used += classify.composition_turns_delta
        live.discovery_turns_used += classify.discovery_turns_delta
        live.advisor_checkpoint_passes_used += classify.advisor_passes_delta
        live.repair_turns_used += classify.repair_turns_delta
        if classify.action == "return":
            if classify.result is None:
                raise AuditIntegrityError("post-batch return lost its actual result")
            public_result = classify.result
            if public_result.graph_recovery_intent is None:
                if live.advisor_repair_context_introduced:
                    public_result = await service._completion._qualified_advisor_repair_public_result(
                        public_result,
                        user_id=bindings.user_id,
                        session_id=bindings.session_id,
                        session_operation_context=context,
                        cache=live.runtime_preflight_cache,
                        initial_version=live.initial_version,
                        session_scope=bindings.session_scope,
                        plugin_snapshot=bindings.plugin_snapshot,
                        publication_binding=classify.publication_binding,
                        turn_evidence=live.turn_evidence,
                        user_message=bindings.message,
                    )
                public_result = service._completion._append_checkpoint_receipt(public_result, turn_evidence=live.turn_evidence)
            phases.finished_batch = None
            return await publish_continuation_result(phases, services, current, runtime, public_result)
    live.turn_evidence.transcript.observe_messages(live.llm_messages)
    captured = await capture_continuation_runtime_owners(runtime, selected=current, inventory=runtime.materialization.inventory)
    phases.finished_batch = None
    await _retain(phases, current, "iteration_advanced", captured)
    return None
