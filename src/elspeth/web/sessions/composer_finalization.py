"""One production owner for Composer response, review and terminal settlement.

Inputs retain the actual live owners and provider outcome alongside detached
provenance values. Current SQL fences and phase owners authorize every write.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4

from fastapi import HTTPException

from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus
from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.composer_progress import ComposerProgressEvent
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw, freeze_fields
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.core.canonical import canonical_json
from elspeth.web.composer.advisor_audit import AdvisorGraphReviewFact
from elspeth.web.composer.advisor_decision import AdvisorGateBlocked, AdvisorGatePassed
from elspeth.web.composer.audit_storage import redacted_tool_invocation_content_and_envelope
from elspeth.web.composer.no_tool_policy import compose_graph_recovery_refusal_message
from elspeth.web.composer.pipeline_commit import PipelineDispatchAuditBinding
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.protocol import (
    PIPELINE_STAGED_REVIEW_MESSAGE,
    ComposerResult,
    ComposerRuntimePreflightError,
    GraphRecoveryResponse,
)
from elspeth.web.composer.visible_tool_event import visible_response_gap
from elspeth.web.execution.completion_gates import (
    completion_gate_decision_changes,
    completion_gates_meta_from_facts,
    project_committed_graph_review,
    resolve_completion_gate_facts,
)
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions._persist_payload import AuditMessageDraft
from elspeth.web.sessions.composer_operations import ComposerOperationAssistantWrite
from elspeth.web.sessions.routes._helpers import (
    _composition_proposal_response,
    _handle_runtime_preflight_failure,
    _join_shielded_task_after_cancellation,
    _llm_calls_from_exception,
    _message_response,
    _publish_progress,
    _state_data_from_composer_state,
    _state_response,
    composer_turn_end_assistant_row,
    llm_call_audit_envelope,
    llm_call_audit_summary,
    merge_composer_meta_updates,
)
from elspeth.web.sessions.routes.composer.pipeline_settlement import PipelineRouteSettlement, settle_auto_commit_intent
from elspeth.web.sessions.schemas import MessageWithStateResponse

if TYPE_CHECKING:
    from elspeth.contracts.composer_progress import ComposerProgressSink
    from elspeth.web.compartments import ChatIngressInput, CompositionIngressRecord
    from elspeth.web.composer.continuation_bootstrap import ContinuationBootstrapSnapshot
    from elspeth.web.composer.graph_recovery import GraphRecoveryContext
    from elspeth.web.composer.state import CompositionState, ValidationSummary
    from elspeth.web.config import WebSettings
    from elspeth.web.coordination.lifecycle import SessionOperationLease
    from elspeth.web.execution.completion_gates import CompletionGateFacts
    from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
    from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry
    from elspeth.web.required_work import RequiredWorkBinding, RequiredWorkCoordinator
    from elspeth.web.sessions.composer_app_services import ComposerAppServices
    from elspeth.web.sessions.composer_operations import ComposerOperationRecord, ComposerOperationRunning
    from elspeth.web.sessions.composer_turn import (
        ComposerBudgetAnchor,
        ComposerPostComposeUpdates,
        ComposerTurnInput,
        ComposerTurnObservation,
        _ComposerTurnLabels,
    )
    from elspeth.web.sessions.continuation_sql_deadline import ContinuationSQLDeadlineOwner
    from elspeth.web.sessions.protocol import (
        ChatMessageRecord,
        CompositionProposalRecord,
        CompositionStateRecord,
        SessionRecord,
        SessionServiceProtocol,
    )
    from elspeth.web.sessions.schemas import CompositionStateResponse


@dataclass(frozen=True, slots=True)
class ContinuationFinalizationBudget:
    """Observe the live V2 action/drain clock without creating a new budget."""

    owner: ContinuationSQLDeadlineOwner

    def remaining_seconds(self, *, monotonic_now: float) -> float:
        if type(monotonic_now) is not float or not math.isfinite(monotonic_now):
            raise ValueError("Continuation finalization requires a finite monotonic observation")
        return self.owner.remaining_action(monotonic_now=monotonic_now)


@dataclass(frozen=True, slots=True)
class ComposerFinalizationInputs:
    """Actual source inputs retained by the live turn or continuation owner."""

    service: SessionServiceProtocol
    session: SessionRecord
    recovery_context: GraphRecoveryContext
    lease: SessionOperationLease
    compose_base_state_id: UUID | None
    running: ComposerOperationRunning
    observation: ComposerTurnObservation
    progress_sink: ComposerProgressSink
    state: CompositionState | None
    chat_ingress: Mapping[str, object]
    chat_ingress_inputs: Sequence[Mapping[str, object]]
    prior_completion_gates_facts: CompletionGateFacts | None
    state_record: CompositionStateRecord | None
    budget_anchor: ComposerBudgetAnchor | ContinuationFinalizationBudget
    services: ComposerAppServices
    turn: ComposerTurnInput | ContinuationBootstrapSnapshot
    labels: _ComposerTurnLabels
    provider_binding: RequiredWorkBinding
    settings: WebSettings
    plugin_snapshot: PluginAvailabilitySnapshot
    profile_registry: OperatorProfileRegistry
    required_work: RequiredWorkCoordinator

    def __post_init__(self) -> None:
        freeze_fields(self, "chat_ingress", "chat_ingress_inputs")


def _message_with_state_response(
    assistant: ChatMessageRecord,
    proposals: tuple[CompositionProposalRecord, ...],
    current_state: CompositionStateRecord | None,
    *,
    state_response: CompositionStateResponse | None,
    live_validation: ValidationSummary | None,
) -> MessageWithStateResponse:
    """The route's success DTO (``messages.py:551-556``), built inside the terminal transaction."""
    if state_response is not None and (current_state is None or str(current_state.id) != state_response.id):
        raise AuditIntegrityError("Composer terminal state changed before validation projection")
    return MessageWithStateResponse(
        message=_message_response(assistant),
        state=_state_response(current_state, live_validation=live_validation)
        if state_response is not None and current_state is not None
        else None,
        proposals=[_composition_proposal_response(proposal) for proposal in proposals if proposal.status == "pending"],
    )


def _turn_audit_cohort_drafts(
    tool_invocations: tuple[ComposerToolInvocation, ...],
    llm_calls: tuple[ComposerLLMCall, ...],
    *,
    tool_composition_state_id: UUID | None,
    llm_composition_state_id: UUID | None,
    parent_assistant_id: UUID | None,
    session_operation_context: SessionOperationContext,
) -> tuple[tuple[AuditMessageDraft, ...], tuple[PipelineDispatchAuditBinding, ...]]:
    """Preserve the production cohort redaction, locators and binding shapes."""
    drafts: list[AuditMessageDraft] = []
    bindings: list[PipelineDispatchAuditBinding] = []
    for invocation in tool_invocations:
        from elspeth.web.composer.audit_storage import private_diagnostic_drafts, private_tool_invocation_draft

        drafts.extend(
            private_diagnostic_drafts(
                invocation,
                context=session_operation_context,
                composition_state_id=str(tool_composition_state_id) if tool_composition_state_id is not None else None,
            )
        )

        drafts.append(
            private_tool_invocation_draft(
                invocation,
                session_id=session_operation_context.fence.session_id,
                session_operation_context=session_operation_context,
                composition_state_id=str(tool_composition_state_id) if tool_composition_state_id is not None else None,
            )
        )
        content, envelope = redacted_tool_invocation_content_and_envelope(invocation)
        drafts.append(
            AuditMessageDraft(
                role="tool" if parent_assistant_id is not None else "audit",
                content=content,
                tool_calls=(envelope,),
                tool_call_id=invocation.tool_call_id if parent_assistant_id is not None else None,
                parent_assistant_id=str(parent_assistant_id) if parent_assistant_id is not None else None,
                composition_state_id=str(tool_composition_state_id) if tool_composition_state_id is not None else None,
            )
        )
        if invocation.tool_name == "set_pipeline" and invocation.status is ComposerToolStatus.SUCCESS:
            bindings.append(PipelineDispatchAuditBinding.from_persisted_envelope(envelope))
    for call in llm_calls:
        from elspeth.web.composer.audit_storage import private_diagnostic_drafts

        drafts.extend(
            private_diagnostic_drafts(
                call,
                context=session_operation_context,
                composition_state_id=str(llm_composition_state_id) if llm_composition_state_id is not None else None,
            )
        )
        public_call = llm_call_audit_envelope(call)
        drafts.append(
            AuditMessageDraft(
                role="audit",
                content=llm_call_audit_summary(call),
                tool_calls=(public_call, visible_response_gap(call, session_operation_context, public_call)),
                composition_state_id=str(llm_composition_state_id) if llm_composition_state_id is not None else None,
            )
        )
    return tuple(drafts), tuple(bindings)


async def settle_composer_result(
    inputs: ComposerFinalizationInputs, result: ComposerResult | GraphRecoveryResponse
) -> ComposerOperationRecord:
    from elspeth.web.sessions.composer_turn import (
        _SETTLEMENT_TIMEOUT_DETAIL,
        _graph_recovery_response,
        _required_audit,
    )

    if type(inputs.budget_anchor) is ContinuationFinalizationBudget:
        owner = inputs.budget_anchor.owner
        if (
            owner.running is not inputs.running
            or owner.running.session_operation_context is not inputs.lease.context
            or inputs.required_work.sql_deadline_owner is not owner
            or inputs.observation.required_work is not inputs.required_work
        ):
            raise AuditIntegrityError("Continuation finalization changed its actual live SQL/budget owners")

    service = inputs.service
    session = inputs.session
    recovery_context = inputs.recovery_context
    lease = inputs.lease
    compose_base_state_id = inputs.compose_base_state_id
    running = inputs.running
    observation = inputs.observation
    progress_sink = inputs.progress_sink
    state = inputs.state
    chat_ingress = inputs.chat_ingress
    chat_ingress_inputs = inputs.chat_ingress_inputs
    prior_completion_gates_facts = inputs.prior_completion_gates_facts
    state_record = inputs.state_record
    budget_anchor = inputs.budget_anchor
    services = inputs.services
    turn = inputs.turn
    labels = inputs.labels
    provider_binding = inputs.provider_binding
    settings = inputs.settings
    plugin_snapshot = inputs.plugin_snapshot
    profile_registry = inputs.profile_registry
    required_work = inputs.required_work

    recovery_intent = result.intent if isinstance(result, GraphRecoveryResponse) else result.graph_recovery_intent
    if recovery_intent is not None:
        recovery = await service.prepare_graph_recovery(
            session.id,
            context=recovery_context,
            intent=recovery_intent,
            session_operation_context=lease.context,
            candidate_state=result.state if isinstance(result, ComposerResult) and recovery_intent.action == "park" else None,
        )
        parent_id = uuid4()
        # A refused action cannot publish prospective success wording.
        # The received control arguments remain in private diagnostic
        # evidence, bound to the physical provider call and operation.
        content = recovery_intent.message if recovery.failure_reason is None else compose_graph_recovery_refusal_message()
        recovery_assistant_write = ComposerOperationAssistantWrite(
            message_id=parent_id,
            content=content,
            raw_content=None if recovery.failure_reason is None else "",
            composition_state_id=compose_base_state_id,
        )
        recovery_tools = result.tool_invocations if isinstance(result, GraphRecoveryResponse) or not result.persisted_tool_call_turn else ()
        audit_cohort, _bindings = _turn_audit_cohort_drafts(
            recovery_tools,
            result.llm_calls,
            tool_composition_state_id=compose_base_state_id,
            llm_composition_state_id=compose_base_state_id,
            parent_assistant_id=parent_id,
            session_operation_context=lease.context,
        )
        if isinstance(result, ComposerResult):
            audit_cohort = result.recovery_audit_drafts + audit_cohort
        record = await _join_shielded_task_after_cancellation(
            asyncio.create_task(
                service.complete_composer_async_operation(
                    running,
                    assistant=recovery_assistant_write,
                    assistant_record=None,
                    audit_cohort=audit_cohort,
                    audit_composition_state_id=None,
                    required_work=observation.required_work,
                    recovery=recovery,
                    build_response=_graph_recovery_response,
                ),
                name="composer-graph-recovery-terminal",
            )
        )
        observation.audit_cohort_durable = True
        await _publish_progress(
            progress_sink,
            event=ComposerProgressEvent(
                phase="complete",
                headline="The composer response is ready.",
                evidence=("The graph recovery outcome and response have been saved.",),
                likely_next="Read the recovery outcome and continue the conversation.",
                reason="composer_complete",
            ),
        )
        return record
    if isinstance(result, GraphRecoveryResponse) or state is None:
        raise AuditIntegrityError("Recovery-only provider returned without a recovery intent")
    # messages.py:361-570 / compose.py:200-385, ending in the R2 composite.
    _post_compose_updates: ComposerPostComposeUpdates = {
        "repair_turns_used": result.repair_turns_used,
        "ingress": cast("CompositionIngressRecord", deep_thaw(chat_ingress)),
        "chat_ingress_inputs": cast("list[ChatIngressInput]", deep_thaw(chat_ingress_inputs)),
    }
    if prior_completion_gates_facts is not None:
        _post_compose_updates["completion_gates"] = completion_gates_meta_from_facts(prior_completion_gates_facts)
    _post_compose_meta = merge_composer_meta_updates(
        state_record.composer_meta if state_record is not None else None,
        _post_compose_updates,
    )
    state_response: CompositionStateResponse | None = None
    live_validation: ValidationSummary | None = None
    post_compose_state_id: UUID | None = compose_base_state_id
    assistant_record: ChatMessageRecord | None = None
    route_settlement: PipelineRouteSettlement | None = None
    graph_review_draft: AuditMessageDraft | None = None
    if result.pipeline_commit_intent is not None:
        # E14: auto-commit settlement gets what is left of the job budget.
        commit_timeout_seconds = budget_anchor.remaining_seconds(monotonic_now=time.monotonic())
        if commit_timeout_seconds <= 0:
            raise HTTPException(status_code=504, detail=_SETTLEMENT_TIMEOUT_DETAIL)
        settlement_outcome = await settle_auto_commit_intent(
            services=services,
            user_id=turn.actor_user_id,
            service=service,
            session_id=session.id,
            intent=result.pipeline_commit_intent,
            composer_meta=_post_compose_meta,
            telemetry_source=labels.telemetry_source,
            session_operation_context=lease.context,
            commit_timeout_seconds=commit_timeout_seconds,
            running=running,
            required_work=observation.required_work,
            required_binding=provider_binding,
        )
        if type(settlement_outcome) is PipelineRouteSettlement:
            route_settlement = settlement_outcome
        else:
            # Auto-commit authority was durably revoked (elspeth-01d4c6e683).
            result = replace(result, message=PIPELINE_STAGED_REVIEW_MESSAGE, pipeline_commit_intent=None)
    # Below the revoked branch on purpose (elspeth-d581b3da7f).
    turn_end = composer_turn_end_assistant_row(result)
    if route_settlement is not None:
        live_validation = route_settlement.validation
        state_response = _state_response(
            route_settlement.settlement.state,
            live_validation=route_settlement.validation,
        )
        post_compose_state_id = route_settlement.settlement.state.id
        assistant_record = route_settlement.settlement.transition_message
    elif result.state.version != state.version:
        await _publish_progress(
            progress_sink,
            event=ComposerProgressEvent(
                phase="validating",
                headline="The composer is validating the pipeline and its review status.",
                evidence=("The pipeline state and review outcome are being checked before persistence.",),
                likely_next="ELSPETH will save the validated pipeline snapshot.",
            ),
        )
        try:
            state_data, validation = await _state_data_from_composer_state(
                result.state,
                settings=settings,
                secret_service=services.scoped_secret_resolver,
                user_id=turn.actor_user_id,
                session_id=session.id,
                plugin_snapshot=plugin_snapshot,
                profile_registry=profile_registry,
                catalog=services.catalog_service,
                runtime_preflight=result.runtime_preflight,
                preflight_exception_policy="raise",
                initial_version=state.version,
                telemetry_source=labels.telemetry_source,
                composer_meta=_post_compose_meta,
                advisor_gate_decision=result.advisor_gate_decision,
            )
        except ComposerRuntimePreflightError as rpf_exc:
            # Path 2 (post-compose runtime preflight).
            rpf_exc = ComposerRuntimePreflightError(
                original_exc=rpf_exc.original_exc,
                partial_state=rpf_exc.partial_state,
                tool_invocations=result.tool_invocations,
                llm_calls=result.llm_calls,
            )
            # B2-closure: recorded before any write. These are
            # result.llm_calls and the frame's persist prefers the
            # result, so the two are never written together.
            observation.pending_exception_llm_calls = _llm_calls_from_exception(rpf_exc)
            await _publish_progress(
                progress_sink,
                event=ComposerProgressEvent(
                    phase="failed",
                    headline="The composer could not safely validate the pipeline update.",
                    evidence=("Runtime preflight failed during state persistence.",),
                    likely_next="Review the visible error message, then retry after the issue is resolved.",
                    reason="runtime_preflight_failed",
                ),
            )
            response_body = await _required_audit(
                observation,
                _handle_runtime_preflight_failure(
                    rpf_exc,
                    service,
                    session.id,
                    turn.actor_user_id,
                    labels.handler_log_prefix,
                    compose_base_state_id,
                    settings=settings,
                    secret_service=services.scoped_secret_resolver,
                    plugin_snapshot=plugin_snapshot,
                    profile_registry=profile_registry,
                    catalog=services.catalog_service,
                    session_operation_context=lease.context,
                    ingress=cast("CompositionIngressRecord", deep_thaw(chat_ingress)),
                    chat_ingress_inputs=cast("list[ChatIngressInput]", deep_thaw(chat_ingress_inputs)),
                    required_audit=True,
                    required_work=required_work,
                ),
            )
            # VERIFY B2 gap: the handler just persisted result.llm_calls as
            # its last write; without this the frame writes them twice.
            observation.audit_cohort_durable = True
            raise HTTPException(status_code=500, detail=response_body) from rpf_exc.original_exc
        await _publish_progress(
            progress_sink,
            event=ComposerProgressEvent(
                phase="saving",
                headline="ELSPETH is saving the pipeline and review status.",
                evidence=("A new composition state version is being stored for this session.",),
                likely_next="The assistant response will appear after the save completes.",
            ),
        )
        checkpoint_sql, checkpoint_projection = required_work.reserve_pair(
            RequiredWorkSource.COMPOSE_CHECKPOINT_SQL,
            RequiredWorkSource.COMPOSE_CHECKPOINT_PROJECTION,
            transition_ordinal=0,
            semantic_ordinal=0,
        )
        try:
            new_state_record = await service.save_composition_state(
                session.id,
                state_data,
                provenance="post_compose",
                session_operation_context=lease.context,
                required_work=checkpoint_sql,
            )
        except BaseException:
            checkpoint_projection.complete_owned()
            raise
        try:
            state_response = _state_response(new_state_record, live_validation=validation)
            live_validation = validation
            post_compose_state_id = new_state_record.id
        except BaseException as exc:
            checkpoint_projection.complete_owned(exc)
            raise
        checkpoint_projection.complete_owned()
    else:
        gate_changed = completion_gate_decision_changes(prior_completion_gates_facts, result.advisor_gate_decision, result.state)
        if composition_content_hash(result.state) != composition_content_hash(state):
            raise AuditIntegrityError("Unversioned authored graph change cannot be a review-only projection")
        identity = result.graph_review_identity
        if gate_changed and identity is None:
            raise AuditIntegrityError("Changed graph review decision lost its final owned review identity")
        if identity is not None:
            fact = AdvisorGraphReviewFact(
                session_id=session.id,
                state_id=compose_base_state_id,
                content_hash=composition_content_hash(result.state),
                operation_id=lease.context.fence.operation_id,
                terminal_operation_id=running.claim.operation_id,
                operation_epoch=lease.context.fence.operation_epoch,
                identity=identity,
                action="passed"
                if type(result.advisor_gate_decision) is AdvisorGatePassed
                else "blocked"
                if type(result.advisor_gate_decision) is AdvisorGateBlocked
                else "unchanged",
                gates=resolve_completion_gate_facts(prior_completion_gates_facts, result.advisor_gate_decision, result.state),
            )
            envelope = fact.to_envelope()
            graph_review_draft = AuditMessageDraft(
                role="audit",
                content=canonical_json(envelope),
                tool_calls=(envelope,),
                composition_state_id=str(fact.state_id) if fact.state_id is not None else None,
            )
            if state_record is not None:
                stored = state_record.review_projection.stored if state_record.review_projection is not None else state_record
                state_response = _state_response(project_committed_graph_review(stored, fact))
    # R2: the assistant row (unless the settlement wrote it), the turn audit
    # cohort, the pending proposals and the terminal CAS in ONE transaction.
    assistant_write: ComposerOperationAssistantWrite | None
    parent_assistant_id: UUID
    if assistant_record is None:
        parent_assistant_id = uuid4()
        assistant_write = ComposerOperationAssistantWrite(
            message_id=parent_assistant_id,
            content=turn_end.content,
            raw_content=turn_end.raw_content,
            composition_state_id=post_compose_state_id,
        )
    else:
        assistant_write = None
        parent_assistant_id = assistant_record.id
    audit_cohort, _pipeline_bindings = _turn_audit_cohort_drafts(
        result.tool_invocations if not result.persisted_tool_call_turn else (),
        result.llm_calls,
        tool_composition_state_id=post_compose_state_id,
        llm_composition_state_id=compose_base_state_id,
        parent_assistant_id=parent_assistant_id,
        session_operation_context=lease.context,
    )
    if graph_review_draft is not None:
        audit_cohort = (*audit_cohort, graph_review_draft)
    # Joined through cancellation: a lease-loss cancel of this child must not
    # leave the composite's worker thread committing while the frame decides
    # the terminal and persists the cancelled-turn audit. Its own outcome is
    # authoritative: it commits (completed wins), or raises
    # ComposerOperationCancelledDuringTurn / a fence loss having written nothing.
    record = await _join_shielded_task_after_cancellation(
        asyncio.create_task(
            service.complete_composer_async_operation(
                running,
                assistant=assistant_write,
                assistant_record=assistant_record,
                audit_cohort=audit_cohort,
                audit_composition_state_id=None,
                required_work=observation.required_work,
                build_response=partial(_message_with_state_response, state_response=state_response, live_validation=live_validation),
            ),
            name="composer-operation-terminal-composite",
        )
    )
    observation.audit_cohort_durable = True
    await _publish_progress(
        progress_sink,
        event=ComposerProgressEvent(
            phase="complete",
            headline="The composer has updated the pipeline."
            if result.state.version != state.version
            else "The composer response is ready.",
            evidence=("The assistant response has been saved for this session.",),
            likely_next="Review the response and current pipeline.",
            reason="composer_complete",
        ),
    )
    return record
