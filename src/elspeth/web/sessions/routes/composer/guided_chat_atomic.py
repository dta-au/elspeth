"""Atomic schema-8 implementation for the guided Chat mutation surface."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import HTTPException, Request

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.web.composer.guided._display import plugin_display_label
from elspeth.web.composer.guided.audit import emit_intent_cancelled
from elspeth.web.composer.guided.chat_solver import (
    DeferredIntentManagementChatRequest,
    GuidedAdvisoryGraphAuthority,
    Step1SourceChatResolution,
    resolved_sink_config_error,
)
from elspeth.web.composer.guided.emitters import _inspection_matches_source_plugin
from elspeth.web.composer.guided.errors import InvariantError
from elspeth.web.composer.guided.protocol import ControlSignal, GuidedStep, Turn, TurnType, validate_current_turn
from elspeth.web.composer.guided.resolved import SinkResolved
from elspeth.web.composer.guided.stage_transitions import (
    AnsweredTurn,
    PluginSelectionResponse,
    SchemaFormResponse,
    transition_source_plugin_reselection,
    transition_source_plugin_selection,
    transition_source_schema_form,
)
from elspeth.web.composer.guided.state_machine import GuidedProposalRef
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.source_inspection import SourceInspectionFacts, inspect_blob_content
from elspeth.web.sessions._guided_step_chat import (
    GuidedStepChatEmptyResult,
    GuidedStepChatOnlyResult,
    GuidedStepDeferredClarificationResult,
    GuidedStepDeferredIntentResult,
    GuidedStepDeferredIntentWithheldResolutionResult,
    GuidedStepDeferredManagementResult,
    Step1SourcePluginReselectedResult,
    Step1SourceResolvedResult,
    Step2SinkResolvedResult,
    StepChatResult,
    resolve_deferred_intent_management_chat_with_auto_drop,
    resolve_step_1_source_chat_with_auto_drop,
    resolve_step_2_sink_chat_with_auto_drop,
    solve_step_chat_with_auto_drop,
)
from elspeth.web.sessions.guided_payloads import prepare_guided_json_payload
from elspeth.web.sessions.guided_replay import (
    guided_turn_token,
    load_guided_json_payload,
    parse_guided_response_descriptor,
    project_guided_response,
)
from elspeth.web.sessions.protocol import (
    GuidedAuditEvidence,
    GuidedCompositionStateResult,
    GuidedOperationFailureCode,
    GuidedOperationFailureCommand,
    GuidedOperationFenceLostError,
    GuidedOperationSettlementConflictError,
    GuidedOriginatingUserMessageDraft,
    GuidedPendingProposalInvalidation,
    GuidedReplayTurn,
    GuidedResponseDescriptor,
    GuidedStateOperationCommand,
    PreparedGuidedJsonPayload,
    SessionRecord,
    SessionServiceProtocol,
    guided_json_payload_id,
)
from elspeth.web.sessions.schemas import GuidedChatRequest, GuidedChatResponse, GuidedRespondRequest

from .._helpers import (
    BlobQuotaExceededError,
    BufferingRecorder,
    ChatRole,
    ChatTurn,
    ComposerChatInitiator,
    ComposerChatTurn,
    ComposerChatTurnStatus,
    ComposerProgressEvent,
    CompositionState,
    CompositionStateData,
    CompositionStateRecord,
    UserIdentity,
    _cancel_on_client_disconnect,
    _composer_progress_sink,
    _failure_log_request_id,
    _get_composer_progress_registry,
    _get_session_compose_lock_registry,
    _inspect_latest_ready_session_blob,
    _is_client_disconnect_cancel,
    _publish_progress,
    _replace,
    _request_plugin_policy_context,
    _safe_frame_strings,
    _state_from_record,
    client_cancelled_progress_event,
    deep_thaw,
    emit_step_advanced,
    emit_turn_answered,
    emit_turn_emitted,
    slog,
)
from ..guided_operations import (
    GuidedOperationExpired,
    GuidedOperationLease,
    raise_guided_operation_failure,
    reserve_or_replay_guided_operation,
)
from .guided_chat_intent_management import (
    DeferredRequestApplication,
    DeferredRequestAuthority,
    DeferredRequestCancelled,
    ManagementRewindAuthority,
    apply_deferred_clarification,
    apply_deferred_request,
    deferred_request_management,
    deferred_request_retained_intent_id,
    maybe_prepare_schema8_management_rewind,
)

type GuidedChatProviderOutcome = (
    GuidedStepChatOnlyResult
    | GuidedStepDeferredIntentResult
    | GuidedStepDeferredIntentWithheldResolutionResult
    | GuidedStepDeferredClarificationResult
    | GuidedStepDeferredManagementResult
    | Step1SourcePluginReselectedResult
    | Step1SourceResolvedResult
    | Step2SinkResolvedResult
)


ProviderRunner = Callable[..., Awaitable[GuidedChatProviderOutcome]]

# Human labels for an inspected upload's content kind (presentation only — the
# closed ``SourceInspectionFacts.source_kind`` vocabulary stays authoritative).
_SOURCE_KIND_LABELS = {
    "csv": "CSV",
    "json": "JSON",
    "jsonl": "JSON Lines",
    "text": "plain text",
    "unknown": "unknown",
}


@dataclass(frozen=True, slots=True)
class _ChatPreflight:
    """Frozen authority observed before reservation/provider work."""

    state_record: CompositionStateRecord | None
    state: CompositionState
    guided: Any
    current_turn: Turn
    current_payload: PreparedGuidedJsonPayload


@dataclass(frozen=True, slots=True)
class _IntermediateOccurrence:
    """One server turn both emitted AND answered inside a single settlement."""

    step: GuidedStep
    turn: Turn
    payload: PreparedGuidedJsonPayload
    response: PreparedGuidedJsonPayload


@dataclass(frozen=True, slots=True)
class _UploadedSourceBind:
    """The prepared cohort for a deterministic uploaded-blob source bind."""

    state: CompositionState
    response_payload: PreparedGuidedJsonPayload
    next_turn: Turn
    next_payload: PreparedGuidedJsonPayload
    intermediate: tuple[_IntermediateOccurrence, ...]


def _with_pair_disposition(chat: StepChatResult, disposition: str | None) -> StepChatResult:
    """Append a pair's retain disposition to a resolution-half failure copy.

    When a resolve+retain pair's RESOLUTION half fails after the intent was
    applied (storage failure, prefill re-check, transition rejection), the
    failure copy must not hide the durable retention — otherwise the turn
    claims "I didn't change your pipeline" while the settlement appended an
    intent, and a resending user piles up duplicates (R2-F15 review finding 1).
    The failure status and error_class stay scoped to the resolution half.
    """
    if disposition is None:
        return chat
    return _replace(chat, assistant_message=f"{chat.assistant_message} {disposition}")


def _unsupported_stage(step: GuidedStep) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "guided_chat_stage_unsupported",
            "detail": f"Schema-8 CHAT is not available for {step.value}.",
        },
    )


def _current_source(guided: Any) -> Any | None:
    target = guided.active_edit_target
    if target is not None and target.kind == "source":
        return guided.reviewed_sources[target.stable_id]
    return next((guided.reviewed_sources[item] for item in guided.source_order if item in guided.reviewed_sources), None)


def _current_sink(guided: Any) -> SinkResolved | None:
    outputs = tuple(guided.reviewed_outputs[item] for item in guided.output_order if item in guided.reviewed_outputs)
    return SinkResolved(outputs=outputs) if outputs else None


def _current_sink_output_indices(guided: Any) -> tuple[int, ...] | None:
    output_indices = tuple(index for index, item in enumerate(guided.output_order, start=1) if item in guided.reviewed_outputs)
    return output_indices or None


def _current_sink_revision_target(guided: Any) -> tuple[SinkResolved | None, int | None]:
    """Select the active output while preserving its user-visible review index."""
    target = guided.active_edit_target
    if target is None or target.kind != "output":
        return None, None
    if target.stable_id not in guided.reviewed_outputs:
        raise InvariantError("active output edit target is not present in reviewed outputs")
    try:
        target_index = tuple(guided.output_order).index(target.stable_id) + 1
    except ValueError as exc:
        raise InvariantError("active output edit target is not present in output order") from exc
    return SinkResolved(outputs=(guided.reviewed_outputs[target.stable_id],)), target_index


def _active_component_revision_kind(guided: Any, step: GuidedStep) -> Literal["source", "output"] | None:
    """Return the applied component whose server-owned form is being edited."""
    target = guided.active_edit_target
    if target is None:
        return None
    if step is GuidedStep.STEP_1_SOURCE and target.kind == "source":
        return "source"
    if step is GuidedStep.STEP_2_SINK and target.kind == "output":
        return "output"
    return None


def _form_directed_revision_message(message: str, revision_kind: Literal["source", "output"]) -> str:
    suffix = (
        f"No changes were applied through chat. To revise this applied {revision_kind}, update its exact settings "
        "in the current wizard form and submit the form through the wizard controls."
    )
    return message if message.endswith(suffix) else f"{message}\n\n{suffix}"


def _form_directed_withheld_resolution_message(revision_kind: Literal["source", "output"]) -> str:
    """Render one coherent disposition for a retained stale mutation pair."""
    return (
        f"I couldn't apply that {revision_kind} revision through chat, so your pipeline {revision_kind} is unchanged. "
        f"To revise this applied {revision_kind}, update its exact settings in the current wizard form and submit "
        "the form through the wizard controls."
    )


def _guided_chat_endpoint_kwargs(settings: Any) -> tuple[str | None, str | None]:
    """Resolve the PRIMARY-role endpoint affordance for guided-chat solvers.

    Guided solvers use the PRIMARY composer role only (never the advisor's —
    see AGENTS.md two-model independence rule and Phase 3 Task 2). Returns
    ``(None, None)`` when unset so every solver call below stays
    byte-identical to pre-affordance behaviour.
    """
    api_key = settings.composer_endpoint_api_key
    return settings.composer_endpoint_base_url, (api_key.get_secret_value() if api_key is not None else None)


def _guided_advisory_graph_authority(
    *,
    step: GuidedStep,
    guided: Any,
    current_turn: Turn,
    current_payload: PreparedGuidedJsonPayload,
) -> GuidedAdvisoryGraphAuthority:
    """Bind provider context to the exact current proposal/wire CAS record."""
    expected_turn_type = {
        GuidedStep.STEP_3_TRANSFORMS: TurnType.PROPOSE_PIPELINE,
        GuidedStep.STEP_4_WIRE: TurnType.CONFIRM_WIRING,
    }.get(step)
    if expected_turn_type is None:
        raise AuditIntegrityError("guided advisory graph authority escaped Steps 3 and 4")
    if type(current_payload) is not PreparedGuidedJsonPayload or current_payload.purpose != "turn":
        raise AuditIntegrityError("guided advisory graph authority requires an exact prepared turn payload")
    if not isinstance(current_turn, Mapping):
        raise AuditIntegrityError("guided advisory graph authority current turn is malformed")
    try:
        turn_type = validate_current_turn(step, current_turn)
        turn_payload = current_turn["payload"]
    except (KeyError, TypeError, ValueError) as exc:
        raise AuditIntegrityError("guided advisory graph authority current turn is malformed") from exc
    if turn_type is not expected_turn_type:
        raise AuditIntegrityError("guided advisory graph authority turn type does not match the guided step")
    if not isinstance(turn_payload, Mapping) or guided_json_payload_id("turn", turn_payload) != current_payload.payload_id:
        raise AuditIntegrityError("guided advisory graph authority turn payload differs from durable custody")
    if current_payload.payload_id != guided_json_payload_id("turn", current_payload.payload):
        raise AuditIntegrityError("guided advisory graph authority payload hash differs from durable custody")
    active_proposal = guided.active_proposal
    if type(active_proposal) is not GuidedProposalRef:
        raise AuditIntegrityError("guided advisory graph authority has no exact active proposal binding")
    proposal_id = str(active_proposal.proposal_id)
    if current_payload.payload.get("proposal_id") != proposal_id:
        raise AuditIntegrityError("guided advisory graph authority proposal id differs from active custody")
    if current_payload.payload.get("draft_hash") != active_proposal.draft_hash:
        raise AuditIntegrityError("guided advisory graph authority draft hash differs from active custody")
    return GuidedAdvisoryGraphAuthority(
        turn_type=turn_type,
        payload_id=current_payload.payload_id,
        proposal_id=proposal_id,
        draft_hash=active_proposal.draft_hash,
        covered_deferred_intent_ids=active_proposal.covered_deferred_intent_ids,
        payload=current_payload.payload,
    )


async def run_guided_chat_provider_attempt(
    *,
    session_id: UUID,
    user: UserIdentity,
    step: GuidedStep,
    guided: Any,
    state: CompositionState,
    message: str,
    settings: Any,
    catalog: Any,
    plugin_snapshot: Any,
    secret_service: Any,
    recorder: BufferingRecorder,
    progress: Any,
    current_turn: Turn | None = None,
    current_payload: PreparedGuidedJsonPayload | None = None,
) -> GuidedChatProviderOutcome:
    """Run the only provider-bearing phase, with no compose lock held."""

    from elspeth.web.composer.guided.chat_solver import build_step_chat_context_block

    endpoint_base_url, endpoint_api_key = _guided_chat_endpoint_kwargs(settings)
    source = _current_source(guided)
    sink = _current_sink(guided)
    sink_output_indices = _current_sink_output_indices(guided)
    revision_form = _active_component_revision_kind(guided, step)
    graph_authority: GuidedAdvisoryGraphAuthority | None = None
    if step in {GuidedStep.STEP_3_TRANSFORMS, GuidedStep.STEP_4_WIRE}:
        if current_turn is None or current_payload is None:
            raise AuditIntegrityError("guided advisory Step 3/4 provider call has no frozen turn authority")
        graph_authority = _guided_advisory_graph_authority(
            step=step,
            guided=guided,
            current_turn=current_turn,
            current_payload=current_payload,
        )
    context_block = build_step_chat_context_block(
        step=step,
        current_source=source,
        current_sink=sink,
        current_sink_output_indices=sink_output_indices,
        state=state,
        deferred_intents=guided.deferred_intents,
        authoritative_revision_form=revision_form,
        graph_authority=graph_authority,
    )
    if step is GuidedStep.STEP_1_SOURCE:
        plugin_hint = None
        allow_plugin_reselection = False
        target = guided.active_edit_target
        if target is not None and target.kind == "source":
            plugin_hint = guided.reviewed_sources[target.stable_id].plugin
        if plugin_hint is None:
            pending = next(
                (guided.pending_source_intents[item] for item in guided.source_order if item in guided.pending_source_intents),
                None,
            )
            plugin_hint = pending.plugin if pending is not None else None
            allow_plugin_reselection = pending is not None and pending.phase == "plugin_options"
        source_outcome = await resolve_step_1_source_chat_with_auto_drop(
            site="post_guided_chat",
            session_id=str(session_id),
            user_id=user.user_id,
            model=settings.composer_model,
            user_message=message,
            plugin_hint=plugin_hint,
            current_source=source,
            available_source_plugins=tuple(plugin.name for plugin in catalog.list_sources()),
            temperature=settings.composer_temperature,
            seed=settings.composer_seed,
            recorder=recorder,
            timeout_seconds=settings.composer_timeout_seconds,
            context_block=context_block,
            allow_plugin_reselection=allow_plugin_reselection,
            api_base=endpoint_base_url,
            api_key=endpoint_api_key,
            reasoning_effort=settings.composer_discovery_reasoning_effort,
        )
        if not isinstance(source_outcome, GuidedStepChatEmptyResult):
            if revision_form == "source":
                assistant_message = (
                    _form_directed_withheld_resolution_message(revision_form)
                    if type(source_outcome) is GuidedStepDeferredIntentWithheldResolutionResult
                    else _form_directed_revision_message(source_outcome.chat.assistant_message, revision_form)
                )
                source_outcome = _replace(
                    source_outcome,
                    chat=_replace(
                        source_outcome.chat,
                        assistant_message=assistant_message,
                    ),
                )
            return source_outcome

    elif step is GuidedStep.STEP_2_SINK:
        revision_sink, revision_target_index = _current_sink_revision_target(guided)
        sink_outcome = await resolve_step_2_sink_chat_with_auto_drop(
            site="post_guided_chat",
            session_id=str(session_id),
            user_id=user.user_id,
            model=settings.composer_model,
            user_message=message,
            current_sink=revision_sink,
            temperature=settings.composer_temperature,
            seed=settings.composer_seed,
            recorder=recorder,
            state=state,
            catalog=catalog,
            plugin_snapshot=plugin_snapshot,
            secret_service=secret_service,
            max_discovery_iters=settings.composer_max_discovery_turns,
            max_tool_calls_per_turn=settings.composer_max_tool_calls_per_turn,
            timeout_seconds=settings.composer_timeout_seconds,
            context_block=context_block,
            progress=progress,
            revision_target_index=revision_target_index,
            api_base=endpoint_base_url,
            api_key=endpoint_api_key,
            reasoning_effort=settings.composer_discovery_reasoning_effort,
        )
        if not isinstance(sink_outcome, GuidedStepChatEmptyResult):
            if revision_form == "output":
                assistant_message = (
                    _form_directed_withheld_resolution_message(revision_form)
                    if type(sink_outcome) is GuidedStepDeferredIntentWithheldResolutionResult
                    else _form_directed_revision_message(sink_outcome.chat.assistant_message, revision_form)
                )
                sink_outcome = _replace(
                    sink_outcome,
                    chat=_replace(
                        sink_outcome.chat,
                        assistant_message=assistant_message,
                    ),
                )
            return sink_outcome
    elif step in {GuidedStep.STEP_3_TRANSFORMS, GuidedStep.STEP_4_WIRE}:
        management = await resolve_deferred_intent_management_chat_with_auto_drop(
            site="post_guided_chat",
            session_id=str(session_id),
            user_id=user.user_id,
            request=DeferredIntentManagementChatRequest(
                model=settings.composer_model,
                step=step,
                user_message=message,
                temperature=settings.composer_temperature,
                seed=settings.composer_seed,
                timeout_seconds=settings.composer_timeout_seconds,
                context_block=context_block,
                api_base=endpoint_base_url,
                api_key=endpoint_api_key,
                reasoning_effort=settings.composer_discovery_reasoning_effort,
            ),
            recorder=recorder,
        )
        return management
    else:  # pragma: no cover - the closed GuidedStep enum owns this exhaustiveness
        raise AuditIntegrityError("Guided Chat provider received an unknown schema-8 stage")

    advisory = await solve_step_chat_with_auto_drop(
        site="post_guided_chat",
        session_id=str(session_id),
        user_id=user.user_id,
        model=settings.composer_model,
        step=step,
        user_message=message,
        temperature=settings.composer_temperature,
        seed=settings.composer_seed,
        recorder=recorder,
        timeout_seconds=settings.composer_timeout_seconds,
        context_block=context_block,
        api_base=endpoint_base_url,
        api_key=endpoint_api_key,
        reasoning_effort=settings.composer_discovery_reasoning_effort,
    )
    if revision_form is not None:
        advisory = _replace(
            advisory,
            assistant_message=_form_directed_revision_message(advisory.assistant_message, revision_form),
        )
    return GuidedStepChatOnlyResult(chat=advisory)


def _transition_request(
    *,
    body: GuidedChatRequest,
    guided: Any,
    current_turn: Turn,
    source_resolution: Any | None,
    sink_resolution: SinkResolved | None,
) -> GuidedRespondRequest | None:
    if _active_component_revision_kind(guided, guided.step) is not None:
        return None
    turn_type = TurnType(current_turn["type"])
    common = {"operation_id": body.operation_id, "turn_token": body.turn_token}
    if source_resolution is not None and guided.step is GuidedStep.STEP_1_SOURCE:
        if turn_type is TurnType.SINGLE_SELECT:
            return GuidedRespondRequest.model_validate({**common, "chosen": [source_resolution.plugin]}, strict=True)
        if turn_type is TurnType.SCHEMA_FORM:
            # Applying generated bytes requires the blob row, originating
            # message, state and operation result to share one transaction.
            # Until that custody participant exists, schema-form Chat is
            # deliberately advisory and the wizard remains authoritative.
            return None
        if turn_type is TurnType.INSPECT_AND_CONFIRM:
            return GuidedRespondRequest.model_validate(
                {**common, "edited_values": {"columns": list(source_resolution.observed_columns)}},
                strict=True,
            )
        return None

    if sink_resolution is not None and guided.step is GuidedStep.STEP_2_SINK:
        (output,) = sink_resolution.outputs
        if turn_type is TurnType.SINGLE_SELECT:
            return GuidedRespondRequest.model_validate({**common, "chosen": [output.plugin]}, strict=True)
        if turn_type is TurnType.SCHEMA_FORM:
            options = dict(deep_thaw(output.options))
            options["on_write_failure"] = output.on_write_failure
            return GuidedRespondRequest.model_validate(
                {**common, "edited_values": {"plugin": output.plugin, "options": options}},
                strict=True,
            )
        if turn_type is TurnType.MULTI_SELECT_WITH_CUSTOM:
            candidates = {
                column
                for stable_id in guided.source_order
                if stable_id in guided.reviewed_sources
                for column in guided.reviewed_sources[stable_id].observed_columns
            }
            chosen = [field for field in output.required_fields if field in candidates]
            custom = [field for field in output.required_fields if field not in candidates]
            payload: dict[str, object] = {**common, "chosen": chosen, "custom_inputs": custom}
            if not chosen and not custom:
                payload["control_signal"] = ControlSignal.PASSTHROUGH.value
            return GuidedRespondRequest.model_validate(payload, strict=True)
    return None


def _prepare_step_1_source_plugin_reselection(
    *,
    guided_route: Any,
    current_state: CompositionState,
    prospective: Any,
    plugin: str,
    inspection_facts: SourceInspectionFacts | None,
    catalog: Any,
    shield_available: bool,
    payload_store: Any,
) -> tuple[CompositionState, PreparedGuidedJsonPayload, Turn, PreparedGuidedJsonPayload]:
    """Answer the old form and emit a new one for an explicit plugin change."""
    if prospective.step is not GuidedStep.STEP_1_SOURCE:
        raise AuditIntegrityError("source plugin reselection escaped Step 1")
    target_id, _held_plugin = guided_route._schema8_form_target(prospective, source=True)
    updated = transition_source_plugin_reselection(
        prospective,
        target_id=target_id,
        turn=AnsweredTurn(history_index=len(prospective.history) - 1),
        plugin=plugin,
        permitted_plugins=tuple(item.name for item in catalog.list_sources()),
        inspection_facts=inspection_facts,
    )
    response_payload = {"action": "reselect_source_plugin", "plugin": plugin}
    response_id = guided_json_payload_id("turn_response", response_payload)
    answered = _replace(
        updated.history[-1],
        response_hash=response_id,
        summary="Pending source plugin reselected through guided chat.",
    )
    updated = _replace(updated, history=(*updated.history[:-1], answered))
    updated_state = _replace(current_state, guided_session=updated)
    next_turn = guided_route._build_get_guided_turn(updated_state, updated, catalog=catalog)
    if next_turn is None or TurnType(next_turn["type"]) is not TurnType.SCHEMA_FORM:
        raise AuditIntegrityError("source plugin reselection did not rebuild a schema form")
    next_turn = guided_route._finalize_guided_turn(next_turn, shield_available=shield_available)
    updated, _record, _turn_type, prepared_next = guided_route._prepare_server_turn_occurrence(
        updated,
        current_step=GuidedStep.STEP_1_SOURCE,
        turn=next_turn,
        payload_store=payload_store,
    )
    return (
        _replace(current_state, guided_session=updated),
        PreparedGuidedJsonPayload(payload_id=response_id, purpose="turn_response", payload=response_payload),
        next_turn,
        prepared_next,
    )


def _step_1_inspected_blob_id(facts: SourceInspectionFacts) -> str | None:
    """Return the blob id an inspection names, or ``None`` for inline facts."""
    blob_id = facts.redacted_identity.get("blob_id")
    return blob_id if type(blob_id) is str and blob_id != "" else None


def _step_1_uploaded_bind_is_consumable(
    *,
    guided_route: Any,
    guided: Any,
    current_turn: Turn,
    inspection_facts: SourceInspectionFacts,
) -> bool:
    """Return whether the live Step-1 turn can consume an uploaded-blob bind.

    A plugin SELECTION turn always can: its own transition captures the
    inspection facts, so the projected form and the review card that follows
    both derive from this upload.

    A schema FORM can only consume a bind naming the blob its intent already
    captured. Answering the form is what produces the confirmation gate — an
    intent holding no inspection facts would resolve the source straight into
    ``reviewed_sources`` (a silent commit this route must never make), and an
    intent holding a DIFFERENT blob would fail its own custody match. An
    active reviewed-source edit keeps its separate edit-inspection custody.
    Every other shape falls back to the ordinary chat route.
    """
    turn_type = TurnType(current_turn["type"])
    if turn_type is TurnType.SINGLE_SELECT:
        return True
    if turn_type is not TurnType.SCHEMA_FORM:
        return False
    target = guided.active_edit_target
    if target is not None and target.kind == "source":
        return False
    target_id, _plugin = guided_route._schema8_form_target(guided, source=True)
    intent = guided.pending_source_intents.get(target_id)
    if intent is None or intent.inspection_facts is None:
        return False
    bound_blob_id = _step_1_inspected_blob_id(intent.inspection_facts)
    return bound_blob_id is not None and bound_blob_id == _step_1_inspected_blob_id(inspection_facts)


def _step_1_uploaded_bind_form_options(form_turn: Turn) -> dict[str, object]:
    """Return the schema form's own server-projected prefill as its answer.

    The prefill IS the inspected upload's resolution (``blob:<id>`` path,
    inspected schema, discard-on-validation-failure), so answering the form
    with it keeps the submitted options byte-identical to the authority the
    same turn advertised — the custody and knob checks in
    ``transition_source_schema_form`` then validate exactly what a user
    pressing Continue on that form would have submitted.
    """
    prefilled = form_turn["payload"].get("prefilled")
    if not isinstance(prefilled, Mapping):
        raise AuditIntegrityError("source schema form has no server-held prefill to bind")
    options = deep_thaw(prefilled)
    if type(options) is not dict:  # pragma: no cover - deep_thaw of a Mapping is a dict
        raise AuditIntegrityError("source schema form prefill did not thaw to an exact dict")
    return options


def _prepare_step_1_uploaded_source_bind(
    *,
    guided_route: Any,
    current_state: CompositionState,
    prospective: Any,
    current_turn: Turn,
    source: Any,
    inspection_facts: SourceInspectionFacts,
    catalog: Any,
    shield_available: bool,
    payload_store: Any,
    new_stable_id: UUID,
) -> _UploadedSourceBind:
    """Bind an uploaded blob deterministically and stop at the review card.

    The upload helper's bind request names no blob id and no plugin, so a
    provider cannot resolve it without inventing the file's content. Answer
    the live Step-1 turn from server-held inspection facts instead: a plugin
    SELECTION turn is answered with the blob-derived plugin, its projected
    schema form is answered with that form's own prefill, and the settlement
    stops on the ``inspect_and_confirm`` review card.

    Nothing is committed. ``reviewed_sources`` — and therefore
    ``composition_state.sources`` — stays empty until the user confirms the
    observed columns through the ordinary wizard control.
    """
    if prospective.step is not GuidedStep.STEP_1_SOURCE:
        raise AuditIntegrityError("uploaded source bind escaped Step 1")
    blob_id = _step_1_inspected_blob_id(inspection_facts)
    if blob_id is None:
        raise AuditIntegrityError("uploaded source bind has no inspected blob custody")
    updated = prospective
    form_turn = current_turn
    form_payload: PreparedGuidedJsonPayload | None = None
    selection_response: PreparedGuidedJsonPayload | None = None
    if TurnType(current_turn["type"]) is TurnType.SINGLE_SELECT:
        selection_targets = [
            stable_id for stable_id, intent in updated.pending_source_intents.items() if intent.phase == "plugin_selection"
        ]
        updated = transition_source_plugin_selection(
            updated,
            turn=AnsweredTurn(history_index=len(updated.history) - 1),
            response=PluginSelectionResponse(chosen=(source.plugin,)),
            permitted_plugins=guided_route._schema8_permitted_plugins(current_turn),
            inspection_facts=inspection_facts,
            new_stable_id=new_stable_id if not selection_targets else None,
            target_id=selection_targets[0] if len(selection_targets) == 1 else None,
        )
        selection_payload: dict[str, object] = {"chosen": [source.plugin], "source_blob_id": blob_id}
        updated, selection_response = _answered_bind_turn(
            updated,
            payload=selection_payload,
            summary="Uploaded input bound the source plugin through guided chat.",
        )
        projected_form = guided_route._build_get_guided_turn(
            _replace(current_state, guided_session=updated),
            updated,
            catalog=catalog,
        )
        if projected_form is None or TurnType(projected_form["type"]) is not TurnType.SCHEMA_FORM:
            raise AuditIntegrityError("uploaded source bind did not project a source schema form")
        form_turn = guided_route._finalize_guided_turn(projected_form, shield_available=shield_available)
        updated, _record, _turn_type, form_payload = guided_route._prepare_server_turn_occurrence(
            updated,
            current_step=GuidedStep.STEP_1_SOURCE,
            turn=form_turn,
            payload_store=payload_store,
        )
    target_id, held_plugin = guided_route._schema8_form_target(updated, source=True)
    if held_plugin != source.plugin:
        raise AuditIntegrityError("uploaded source bind lost its server-held source plugin")
    form_options = _step_1_uploaded_bind_form_options(form_turn)
    updated = transition_source_schema_form(
        updated,
        target_id=target_id,
        turn=AnsweredTurn(history_index=len(updated.history) - 1),
        response=SchemaFormResponse(plugin=held_plugin, options=form_options),
        authority=guided_route._schema8_schema_authority(
            turn=form_turn,
            plugin=held_plugin,
            options=form_options,
            source=True,
            catalog=catalog,
        ),
    )
    updated, form_response = _answered_bind_turn(
        updated,
        payload={"edited_values": {"plugin": held_plugin, "options": form_options}},
        summary="Uploaded input bound the source form through guided chat.",
    )
    review_turn = guided_route._build_get_guided_turn(
        _replace(current_state, guided_session=updated),
        updated,
        catalog=catalog,
    )
    if review_turn is None or TurnType(review_turn["type"]) is not TurnType.INSPECT_AND_CONFIRM:
        raise AuditIntegrityError("uploaded source bind did not project an inspection review")
    review_turn = guided_route._finalize_guided_turn(review_turn, shield_available=shield_available)
    updated, _review_record, _review_type, review_payload = guided_route._prepare_server_turn_occurrence(
        updated,
        current_step=GuidedStep.STEP_1_SOURCE,
        turn=review_turn,
        payload_store=payload_store,
    )
    # The settlement's answered CURRENT turn is whichever turn the request
    # arrived on. When the bind started from a plugin selection, the schema
    # form this route emitted AND answered on the user's behalf rides as an
    # intermediate occurrence so its payloads and audit pair stay bound to the
    # same atomic cohort.
    intermediate: tuple[_IntermediateOccurrence, ...] = ()
    if selection_response is not None:
        if form_payload is None:  # pragma: no cover - set together with selection_response
            raise AuditIntegrityError("uploaded source bind emitted no intermediate form payload")
        intermediate = (
            _IntermediateOccurrence(
                step=GuidedStep.STEP_1_SOURCE,
                turn=form_turn,
                payload=form_payload,
                response=form_response,
            ),
        )
    return _UploadedSourceBind(
        state=_replace(current_state, guided_session=updated),
        response_payload=selection_response if selection_response is not None else form_response,
        next_turn=review_turn,
        next_payload=review_payload,
        intermediate=intermediate,
    )


def _answered_bind_turn(
    guided: Any,
    *,
    payload: Mapping[str, object],
    summary: str,
) -> tuple[Any, PreparedGuidedJsonPayload]:
    """Answer the guided session's live occurrence with an exact CAS payload."""
    response_id = guided_json_payload_id("turn_response", payload)
    answered = _replace(guided.history[-1], response_hash=response_id, summary=summary)
    return (
        _replace(guided, history=(*guided.history[:-1], answered)),
        PreparedGuidedJsonPayload(payload_id=response_id, purpose="turn_response", payload=payload),
    )


async def _step_1_inline_source_inspection_facts(
    *,
    blob_service: Any,
    session_id: UUID,
    resolution: Step1SourceChatResolution,
    source_description: str,
) -> SourceInspectionFacts | None:
    """Materialize inline resolve_source content as an upload-equivalent blob.

    The newest ready session blob stays authoritative — exactly the blob the
    wizard respond route would bind — so an uploaded file always wins over
    inline content and a retried operation reuses its own earlier blob instead
    of duplicating it. Inline bytes are stored only when the session has no
    ready blob at all. Facts that cannot prefill the chosen plugin are dropped
    so the transition falls back to the existing advisory-only flow rather
    than failing the turn.

    Custody note (deliberate, reviewed): the blob is written through
    ``create_blob`` with ``created_by="assistant"`` — upload-equivalent
    custody, the mechanism the implementation brief settled on. Full
    ``reserve_inline_custody`` provenance (LLM_GENERATED modality) is not
    reachable here: it requires a durable originating chat-message row, which
    only exists after this operation settles. The LLM authorship breadcrumb
    lives in ``source_description`` instead.
    """
    facts = await _inspect_latest_ready_session_blob(blob_service, session_id)
    if facts is None:
        content = resolution.content.encode("utf-8")
        record = await blob_service.create_blob(
            session_id,
            resolution.filename,
            content,
            resolution.mime_type,
            created_by="assistant",
            source_description=source_description,
        )
        facts = inspect_blob_content(
            content=content,
            filename=record.filename,
            mime_type=record.mime_type,
            blob_id=record.id,
            content_hash=record.content_hash,
        )
    if not _inspection_matches_source_plugin(resolution.plugin, facts):
        return None
    return facts


async def post_guided_chat_schema8(
    *,
    session_id: UUID,
    body: GuidedChatRequest,
    request: Request,
    user: UserIdentity,
    owned_session: SessionRecord,
    provider_runner: ProviderRunner,
) -> GuidedChatResponse:
    """Reserve, run, and atomically settle one schema-8 guided Chat turn.

    ``owned_session`` is the ownership-verified session record from the
    route wrapper; the sink-admission path namespace derives from its id,
    never from raw route input (elspeth-ef92db3e16).
    """

    from . import guided as guided_route

    service: SessionServiceProtocol = request.app.state.session_service
    payload_store = request.app.state.payload_store
    compose_lock = await _get_session_compose_lock_registry(request).get_lock(str(session_id))

    # A chat operation can only answer an already-durable guided checkpoint.
    # Ordinary first intent belongs to /guided/start; GET is hydration-only.
    # Reject before even looking up operation custody so a cold request cannot
    # allocate retry state, call a provider, or write a chat row.
    if await service.get_current_state(session_id) is None:
        raise HTTPException(status_code=409, detail="Start the guided session before sending guided chat.")

    def response_from_record(record: CompositionStateRecord) -> GuidedChatResponse:
        descriptor = parse_guided_response_descriptor(record)
        if descriptor.kind != "guided_chat":
            raise AuditIntegrityError("Guided Chat result has the wrong replay descriptor")
        payloads: tuple[PreparedGuidedJsonPayload, ...] = ()
        if descriptor.next_turn is not None:
            payloads = (load_guided_json_payload(payload_store, payload_id=descriptor.next_turn.payload_id, purpose="turn"),)
        response = project_guided_response(record, payloads=payloads)
        if type(response) is not GuidedChatResponse:
            raise AuditIntegrityError("Guided Chat projection returned the wrong response type")
        return response

    async def replay(result: object) -> GuidedChatResponse:
        if type(result) is not GuidedCompositionStateResult:
            raise AuditIntegrityError("Guided Chat replay has a non-state result locator")
        return response_from_record(await service.get_state_in_session(result.state_id, session_id))

    pending = await reserve_or_replay_guided_operation(
        service=service,
        session_id=session_id,
        kind="guided_chat",
        request=body,
        replay=replay,
        reserve_if_absent=False,
        takeover_expired=False,
    )
    if pending is not None and not isinstance(pending, (GuidedOperationLease, GuidedOperationExpired)):
        return pending

    catalog, plugin_snapshot = _request_plugin_policy_context(request, user)
    shield_available = guided_route._resolve_shield_available(plugin_snapshot)

    async def preflight() -> _ChatPreflight:
        state_record = await service.get_current_state(session_id)
        if state_record is None:
            raise AuditIntegrityError("Guided Chat durable checkpoint disappeared after admission")
        state = _state_from_record(state_record)
        guided = state.guided_session
        if guided is None:
            raise HTTPException(status_code=400, detail="Session is not in guided mode. Use /api/sessions/{id}/messages.")
        if guided.terminal is not None:
            raise HTTPException(status_code=409, detail="Guided session is already terminal.")
        if guided.step not in {
            GuidedStep.STEP_1_SOURCE,
            GuidedStep.STEP_2_SINK,
            GuidedStep.STEP_3_TRANSFORMS,
            GuidedStep.STEP_4_WIRE,
        }:
            raise _unsupported_stage(guided.step)
        if guided.step in {GuidedStep.STEP_3_TRANSFORMS, GuidedStep.STEP_4_WIRE} and not guided.deferred_intents:
            raise _unsupported_stage(guided.step)
        prospective, current_turn, current_payload = guided_route._schema8_prospective_occurrence(
            state,
            guided,
            catalog=catalog,
            shield_available=shield_available,
            payload_store=payload_store,
        )
        if body.turn_token != guided_turn_token(prospective):
            raise HTTPException(status_code=409, detail="turn_token does not identify the current unanswered turn.")
        return _ChatPreflight(
            state_record=state_record,
            state=state,
            guided=prospective,
            current_turn=current_turn,
            current_payload=current_payload,
        )

    admission_lock = await _get_session_compose_lock_registry(request).get_lock(f"{session_id}:guided-chat-admission")
    while True:
        rejoin_after_lock = False
        frozen: _ChatPreflight | None = None
        bypass_admission = isinstance(pending, GuidedOperationExpired)
        if bypass_admission:
            frozen = await preflight()
            pending = await reserve_or_replay_guided_operation(
                service=service,
                session_id=session_id,
                kind="guided_chat",
                request=body,
                replay=replay,
            )
            if pending is None:  # pragma: no cover
                raise AuditIntegrityError("Guided Chat takeover was not reserved")
            if not isinstance(pending, GuidedOperationLease):
                return pending

        attempt_guard = contextlib.nullcontext() if bypass_admission else admission_lock
        async with attempt_guard:
            if pending is None:
                rechecked = await reserve_or_replay_guided_operation(
                    service=service,
                    session_id=session_id,
                    kind="guided_chat",
                    request=body,
                    replay=replay,
                    reserve_if_absent=False,
                    takeover_expired=False,
                )
                if isinstance(rechecked, GuidedOperationExpired):
                    pending = rechecked
                    continue
                if rechecked is not None and not isinstance(rechecked, GuidedOperationLease):
                    return rechecked
                pending = rechecked

            if not bypass_admission:
                async with compose_lock:
                    frozen = await preflight()
            if frozen is None:  # pragma: no cover
                raise AuditIntegrityError("Guided Chat has no frozen preflight authority")

            reserved = pending or await reserve_or_replay_guided_operation(
                service=service,
                session_id=session_id,
                kind="guided_chat",
                request=body,
                replay=replay,
            )
            pending = None
            if not isinstance(reserved, GuidedOperationLease):
                if reserved is None or isinstance(reserved, GuidedOperationExpired):  # pragma: no cover
                    raise AuditIntegrityError("Guided Chat reservation returned no lease")
                return reserved

            recorder = BufferingRecorder()
            attempt_message_id = uuid4()
            originating_message = GuidedOriginatingUserMessageDraft(
                message_id=attempt_message_id,
                content=body.message,
            )
            progress_started = False
            progress_registry = _get_composer_progress_registry(request)
            try:
                progress_sink = _composer_progress_sink(
                    progress_registry,
                    session_id=str(session_id),
                    request_id=body.operation_id,
                    user_id=str(user.user_id),
                )
                await _publish_progress(
                    progress_sink,
                    event=ComposerProgressEvent(
                        phase="starting",
                        headline="I'm reading your message for this guided turn.",
                        evidence=("The message and current turn token were accepted.",),
                        likely_next="ELSPETH will prepare a bounded guided response.",
                    ),
                )
                progress_started = True
                settings = request.app.state.settings
                started_at = datetime.now(UTC)
                async with _cancel_on_client_disconnect(request):
                    uploaded_candidate = None
                    # The upload sentinel binds deterministically on every live
                    # Step-1 turn that can hold a source resolution, not just a
                    # schema form: a fresh session opens on the plugin SELECT
                    # turn, which is exactly where a first upload arrives. The
                    # message is matched FIRST so an ordinary chat turn neither
                    # reads blob storage nor requires it to be configured.
                    if (
                        frozen.guided.step is GuidedStep.STEP_1_SOURCE
                        and TurnType(frozen.current_turn["type"])
                        in {
                            TurnType.SINGLE_SELECT,
                            TurnType.SCHEMA_FORM,
                        }
                        and guided_route._step_1_uploaded_input_filename(body.message) is not None
                    ):
                        uploaded_candidate = await guided_route._source_from_latest_uploaded_blob_for_step_1_chat(
                            message=body.message,
                            plugin_hint=guided_route._step_1_plugin_hint(frozen.guided),
                            selectable_plugins=(
                                guided_route._schema8_permitted_plugins(frozen.current_turn)
                                if TurnType(frozen.current_turn["type"]) is TurnType.SINGLE_SELECT
                                else ()
                            ),
                            blob_service=request.app.state.blob_service,
                            session_id=session_id,
                        )
                    uploaded_mismatch_facts = (
                        uploaded_candidate[1] if uploaded_candidate is not None and uploaded_candidate[0] is None else None
                    )
                    uploaded_bind: tuple[Any, SourceInspectionFacts] | None = None
                    if (
                        uploaded_candidate is not None
                        and uploaded_candidate[0] is not None
                        and _step_1_uploaded_bind_is_consumable(
                            guided_route=guided_route,
                            guided=frozen.guided,
                            current_turn=frozen.current_turn,
                            inspection_facts=uploaded_candidate[1],
                        )
                    ):
                        uploaded_bind = (uploaded_candidate[0], uploaded_candidate[1])

                    if uploaded_mismatch_facts is not None:
                        filename = guided_route._step_1_uploaded_input_filename(body.message)
                        if filename is None:  # pragma: no cover - upload helper contract
                            raise AuditIntegrityError("uploaded mismatch facts have no upload-helper filename")
                        source_kind_label = _SOURCE_KIND_LABELS[uploaded_mismatch_facts.source_kind]
                        plugin_hint = guided_route._step_1_plugin_hint(frozen.guided)
                        if plugin_hint is None:  # pragma: no cover - upload helper contract
                            raise AuditIntegrityError("uploaded mismatch facts have no selected Step-1 plugin")
                        selected_plugin_labels = {
                            "csv": "CSV",
                            "json": "JSON",
                            "text": "Text",
                        }
                        selected_plugin_label = (
                            selected_plugin_labels[plugin_hint]
                            if plugin_hint in selected_plugin_labels
                            else plugin_hint.replace("_", " ").title()
                        )
                        chat_result = StepChatResult(
                            assistant_message=(
                                f'I received "{filename}" and inspected it as {source_kind_label} content, '
                                f"but the current source type is {selected_plugin_label}. I did not apply it; "
                                "the file is still uploaded. Use a source configured for "
                                f"{source_kind_label} content, or upload content that matches {selected_plugin_label}."
                            ),
                            status=ComposerChatTurnStatus.SYNTHETIC_UNAVAILABLE,
                            latency_ms=max(0, int((datetime.now(UTC) - started_at).total_seconds() * 1000)),
                            error_class="UploadedSourceTypeMismatch",
                        )
                        source_resolution = None
                        source_plugin_reselection = None
                        sink_resolution = None
                        deferred_action = None
                        deferred_management_action = None
                        deferred_clarification = False
                        deferred_paired_resolution = False
                    elif uploaded_bind is not None:
                        # No provider work: the bind request names a file this
                        # session already holds, and its inspected facts are the
                        # authority. Asking a model to "resolve" it would demand
                        # content it cannot know (and the solver correctly
                        # rejects an empty-content resolution).
                        bind_filename = guided_route._step_1_uploaded_input_filename(body.message)
                        if bind_filename is None:  # pragma: no cover - upload helper contract
                            raise AuditIntegrityError("uploaded source bind has no upload-helper filename")
                        bind_source, bind_facts = uploaded_bind
                        chat_result = StepChatResult(
                            assistant_message=(
                                f'I inspected "{bind_filename}" as {_SOURCE_KIND_LABELS[bind_facts.source_kind]} content '
                                f"and prepared it as a {plugin_display_label(bind_source.plugin)} input. "
                                "Confirm the observed columns below and it becomes your pipeline source."
                            ),
                            status=ComposerChatTurnStatus.SUCCESS,
                            latency_ms=max(0, int((datetime.now(UTC) - started_at).total_seconds() * 1000)),
                            error_class=None,
                        )
                        source_resolution = None
                        source_plugin_reselection = None
                        sink_resolution = None
                        deferred_action = None
                        deferred_management_action = None
                        deferred_clarification = False
                        deferred_paired_resolution = False
                    else:
                        provider_outcome = await provider_runner(
                            session_id=session_id,
                            user=user,
                            step=frozen.guided.step,
                            guided=frozen.guided,
                            state=frozen.state,
                            message=body.message,
                            settings=settings,
                            catalog=catalog,
                            plugin_snapshot=plugin_snapshot,
                            secret_service=request.app.state.scoped_secret_resolver,
                            recorder=recorder,
                            progress=progress_sink,
                            current_turn=frozen.current_turn,
                            current_payload=frozen.current_payload,
                        )
                        chat_result = provider_outcome.chat
                        source_resolution = provider_outcome.resolution if type(provider_outcome) is Step1SourceResolvedResult else None
                        source_plugin_reselection = (
                            provider_outcome.plugin if type(provider_outcome) is Step1SourcePluginReselectedResult else None
                        )
                        sink_resolution = provider_outcome.sink if type(provider_outcome) is Step2SinkResolvedResult else None
                        if type(provider_outcome) is GuidedStepDeferredIntentResult:
                            deferred_action = provider_outcome.action
                        elif type(provider_outcome) is GuidedStepDeferredIntentWithheldResolutionResult:
                            # Retain-alone: the pair's resolution half was
                            # withheld; its chat carries the scoped not-applied
                            # failure and composes with the disposition below,
                            # exactly like the F1 contract.
                            deferred_action = provider_outcome.action
                        elif type(provider_outcome) is Step1SourceResolvedResult:
                            # A resolve+retain PAIR: the resolution applies at
                            # this stage AND the future-stage instruction is
                            # retained in the same settlement (R2-F15).
                            deferred_action = provider_outcome.deferred_action
                        elif type(provider_outcome) is Step2SinkResolvedResult:
                            deferred_action = provider_outcome.deferred_action
                        else:
                            deferred_action = None
                        deferred_paired_resolution = deferred_action is not None and (
                            type(provider_outcome) is Step1SourceResolvedResult
                            or type(provider_outcome) is Step2SinkResolvedResult
                            or type(provider_outcome) is GuidedStepDeferredIntentWithheldResolutionResult
                        )
                        deferred_management_action = (
                            provider_outcome.action if type(provider_outcome) is GuidedStepDeferredManagementResult else None
                        )
                        deferred_clarification = type(provider_outcome) is GuidedStepDeferredClarificationResult
                    revision_kind = _active_component_revision_kind(frozen.guided, frozen.guided.step)
                    if revision_kind is not None and (
                        source_resolution is not None or source_plugin_reselection is not None or sink_resolution is not None
                    ):
                        source_resolution = None
                        source_plugin_reselection = None
                        sink_resolution = None
                        chat_result = StepChatResult(
                            assistant_message=(
                                f"I didn't apply that chat revision because the current {revision_kind} wizard form "
                                "is authoritative. Update its exact settings and submit it through the wizard "
                                "controls; your existing settings remain unchanged."
                            ),
                            status=ComposerChatTurnStatus.SYNTHETIC_UNAVAILABLE,
                            latency_ms=chat_result.latency_ms,
                            error_class="ComponentRevisionNotApplied",
                        )
                    elif revision_kind is not None:
                        chat_result = _replace(
                            chat_result,
                            assistant_message=(
                                _form_directed_withheld_resolution_message(revision_kind)
                                if deferred_paired_resolution
                                else _form_directed_revision_message(
                                    chat_result.assistant_message,
                                    revision_kind,
                                )
                            ),
                        )
                    if source_resolution is not None and TurnType(frozen.current_turn["type"]) is TurnType.SCHEMA_FORM:
                        source_resolution = None
                        chat_result = StepChatResult(
                            assistant_message=(
                                "I did not apply generated source content. Review the current source form and "
                                "submit it through the wizard controls."
                            ),
                            status=ComposerChatTurnStatus.SYNTHETIC_UNAVAILABLE,
                            latency_ms=chat_result.latency_ms,
                            error_class="InlineSourceNotApplied",
                        )

                async with compose_lock:
                    fence = await service.renew_guided_operation(reserved.fence, actor="composer_route", lease_seconds=300)
                    current_record = await service.get_current_state(session_id)
                    if (current_record is None) != (frozen.state_record is None):
                        raise GuidedOperationSettlementConflictError()
                    if (
                        current_record is not None
                        and frozen.state_record is not None
                        and (current_record.id != frozen.state_record.id or current_record.version != frozen.state_record.version)
                    ):
                        raise GuidedOperationSettlementConflictError()
                    if current_record is None:
                        raise AuditIntegrityError("Guided Chat durable checkpoint disappeared before settlement")
                    current_state = _state_from_record(current_record)
                    if current_record is not None and composition_content_hash(current_state) != composition_content_hash(frozen.state):
                        raise AuditIntegrityError("Guided Chat current state content changed after provider work")
                    current_guided = current_state.guided_session
                    if current_guided is None:
                        raise AuditIntegrityError("Guided Chat head lost its guided checkpoint")
                    prospective, current_turn, planned_current = guided_route._schema8_prospective_occurrence(
                        current_state,
                        current_guided,
                        catalog=catalog,
                        shield_available=shield_available,
                        payload_store=payload_store,
                    )
                    if body.turn_token != guided_turn_token(prospective) or planned_current.payload_id != frozen.current_payload.payload_id:
                        raise AuditIntegrityError("Guided Chat turn custody changed after provider work")

                    occurrence_was_prospective = not (current_guided.history and current_guided.history[-1].response_hash is None)
                    # On a resolve+retain pair, the disposition copy from
                    # apply_deferred_request must not displace the message the
                    # resolution half produced — both applications (or the
                    # guard's explanation for a withheld resolution, e.g. the
                    # advisory-only schema form) stay visible.
                    paired_resolution_chat = chat_result if deferred_paired_resolution else None
                    deferred_authority = DeferredRequestAuthority(
                        guided=prospective,
                        catalog=catalog,
                        originating_message=originating_message,
                        new_intent_id=uuid4(),
                    )
                    deferred: DeferredRequestApplication
                    if deferred_clarification:
                        # Retain repair exhausted: keep the instruction as a
                        # constraint-free clarification intent instead of
                        # discarding it (R2-F15). The chat copy already says
                        # the instruction was kept and asks for the missing
                        # structural constraint.
                        deferred = apply_deferred_clarification(
                            authority=deferred_authority,
                            chat=chat_result,
                        )
                    else:
                        deferred = apply_deferred_request(
                            deferred_action,
                            deferred_management_action,
                            authority=deferred_authority,
                            chat=chat_result,
                        )
                    prospective = deferred.guided
                    chat_result = deferred.chat
                    deferred_disposition_message: str | None = None
                    if paired_resolution_chat is not None:
                        deferred_disposition_message = deferred.chat.assistant_message
                        composed_message = f"{paired_resolution_chat.assistant_message} {deferred.chat.assistant_message}"
                        if paired_resolution_chat.status is ComposerChatTurnStatus.SUCCESS:
                            chat_result = _replace(chat_result, assistant_message=composed_message)
                        else:
                            # A withheld resolution half (advisory-only schema
                            # form) keeps its synthetic-failure status and
                            # error_class so the transcript and audit retain
                            # the not-applied signal; the retain disposition
                            # stays visible in the message (review finding 2).
                            chat_result = StepChatResult(
                                assistant_message=composed_message,
                                status=paired_resolution_chat.status,
                                latency_ms=deferred.chat.latency_ms,
                                error_class=paired_resolution_chat.error_class,
                            )
                    retained_intent_id = deferred_request_retained_intent_id(deferred)
                    management = deferred_request_management(deferred)
                    settled_management_action = management.action if management is not None else None
                    cancelled_intent = management.effective_intent if type(management) is DeferredRequestCancelled else None
                    source_inspection_facts: SourceInspectionFacts | None = None
                    if (
                        source_resolution is not None
                        and prospective.step is GuidedStep.STEP_1_SOURCE
                        and TurnType(current_turn["type"]) is TurnType.SINGLE_SELECT
                    ):
                        try:
                            source_inspection_facts = await _step_1_inline_source_inspection_facts(
                                blob_service=request.app.state.blob_service,
                                session_id=session_id,
                                resolution=source_resolution,
                                source_description=(
                                    "Guided Step-1 chat resolve_source inline content "
                                    f"(LLM-generated; model {settings.composer_model}; "
                                    f"operation {body.operation_id})."
                                ),
                            )
                        except (BlobQuotaExceededError, UnicodeEncodeError) as materialize_exc:
                            source_resolution = None
                            chat_result = _with_pair_disposition(
                                StepChatResult(
                                    assistant_message=(
                                        (
                                            "I could not store the generated source content because this "
                                            "session's storage quota is full. Remove an uploaded file or "
                                            "provide a smaller source, then try again."
                                        )
                                        if isinstance(materialize_exc, BlobQuotaExceededError)
                                        else (
                                            "I could not store the generated source content because it "
                                            "contains characters that cannot be encoded. Describe the "
                                            "source again or upload the file directly."
                                        )
                                    ),
                                    status=ComposerChatTurnStatus.SYNTHETIC_UNAVAILABLE,
                                    latency_ms=chat_result.latency_ms,
                                    error_class="InlineSourceNotApplied",
                                ),
                                deferred_disposition_message,
                            )
                    source_reselection_facts: SourceInspectionFacts | None = None
                    if source_plugin_reselection is not None:
                        source_reselection_facts = await _inspect_latest_ready_session_blob(
                            request.app.state.blob_service,
                            session_id,
                            source_plugin=source_plugin_reselection,
                        )
                    sink_prefill_options: dict[str, Any] | None = None
                    sink_prefill_name: str | None = None
                    if (
                        sink_resolution is not None
                        and prospective.step is GuidedStep.STEP_2_SINK
                        and TurnType(current_turn["type"]) is TurnType.SINGLE_SELECT
                    ):
                        # Staged prefill becomes server-held authority that every
                        # /guided/respond echo re-validates through the plugin
                        # config model, so options that fail it must never be
                        # staged — the session would wedge with an unrepairable
                        # 400 turn-contract rejection (elspeth-a88c07cd47). The
                        # solver validates resolutions before returning them;
                        # this guard covers every other producer of a
                        # sink resolution.
                        (resolved_output,) = sink_resolution.outputs
                        prefill_config_rejection = resolved_sink_config_error(sink_resolution)
                        if prefill_config_rejection is not None:
                            slog.warning(
                                "guided.step_2_sink_prefill_config_rejected",
                                session_id=str(session_id),
                                user_id=user.user_id,
                                rejection_code=prefill_config_rejection.rejection_code,
                                exc_class=prefill_config_rejection.exception_class,
                            )
                            chat_result = _with_pair_disposition(
                                StepChatResult(
                                    assistant_message=(
                                        "I couldn't apply that output configuration because it fails the "
                                        "selected plugin's validation, so I didn't change your pipeline. "
                                        "Describe the output again and I'll rebuild it."
                                    ),
                                    status=ComposerChatTurnStatus.SYNTHETIC_UNAVAILABLE,
                                    latency_ms=chat_result.latency_ms,
                                    error_class="SinkPrefillConfigRejected",
                                ),
                                deferred_disposition_message,
                            )
                            sink_resolution = None
                        else:
                            candidate_prefill_options = dict(deep_thaw(resolved_output.options))
                            candidate_prefill_options["on_write_failure"] = resolved_output.on_write_failure
                            try:
                                # Deployment sink admission must run before the
                                # options become server-held prefill: the
                                # SCHEMA_FORM chat lane gets this check inside
                                # _schema8_answer_and_project_next, but this
                                # SINGLE_SELECT lane answers a plugin-selection
                                # turn, so without it an inadmissible option
                                # set is staged and the rejection surfaces
                                # only at the user's form POST as a 400
                                # blaming their submission.
                                guided_route._schema8_require_runnable_sink_options(
                                    candidate_prefill_options,
                                    plugin=resolved_output.plugin,
                                    data_dir=str(request.app.state.settings.data_dir),
                                    session_id=str(owned_session.id),
                                )
                            except guided_route.SinkAdmissionRejectedError as prefill_admission_exc:
                                slog.warning(
                                    "guided.step_2_sink_prefill_admission_rejected",
                                    session_id=str(session_id),
                                    user_id=user.user_id,
                                    detail=prefill_admission_exc.detail,
                                )
                                chat_result = _with_pair_disposition(
                                    StepChatResult(
                                        assistant_message=(
                                            "I couldn't apply those output settings, so I didn't change your pipeline. "
                                            f"{prefill_admission_exc.detail} "
                                            "Describe the output again and I'll rebuild it."
                                        ),
                                        status=ComposerChatTurnStatus.SYNTHETIC_UNAVAILABLE,
                                        latency_ms=chat_result.latency_ms,
                                        error_class="SinkAdmissionRejected",
                                    ),
                                    deferred_disposition_message,
                                )
                                sink_resolution = None
                            else:
                                sink_prefill_name = resolved_output.name
                                sink_prefill_options = candidate_prefill_options
                    transition_body = _transition_request(
                        body=body,
                        guided=prospective,
                        current_turn=current_turn,
                        source_resolution=source_resolution,
                        sink_resolution=sink_resolution,
                    )
                    resulting_state = _replace(current_state, guided_session=prospective)
                    planned_response: PreparedGuidedJsonPayload | None = None
                    next_turn: Turn | None = current_turn
                    prepared_next: PreparedGuidedJsonPayload | None = planned_current
                    transition_succeeded = False
                    rewound = False
                    intermediate_occurrences: tuple[_IntermediateOccurrence, ...] = ()
                    invalidated_pending_proposal: GuidedPendingProposalInvalidation | None = None
                    rewind = maybe_prepare_schema8_management_rewind(
                        authority=ManagementRewindAuthority(
                            guided_route=guided_route,
                            current_state=current_state,
                            current_guided=current_guided,
                            prospective=prospective,
                            catalog=catalog,
                            shield_available=shield_available,
                            payload_store=payload_store,
                        ),
                        management=management,
                    )
                    if rewind is not None:
                        resulting_state = rewind.state
                        planned_response = rewind.response_payload
                        next_turn = rewind.next_turn
                        prepared_next = rewind.next_payload
                        invalidated_pending_proposal = rewind.invalidated_proposal
                        transition_succeeded = True
                        rewound = True
                    elif source_plugin_reselection is not None:
                        resulting_state, planned_response, next_turn, prepared_next = _prepare_step_1_source_plugin_reselection(
                            guided_route=guided_route,
                            current_state=current_state,
                            prospective=prospective,
                            plugin=source_plugin_reselection,
                            inspection_facts=source_reselection_facts,
                            catalog=catalog,
                            shield_available=shield_available,
                            payload_store=payload_store,
                        )
                        transition_succeeded = True
                        rewound = True
                    elif uploaded_bind is not None:
                        try:
                            bind = _prepare_step_1_uploaded_source_bind(
                                guided_route=guided_route,
                                current_state=current_state,
                                prospective=prospective,
                                current_turn=current_turn,
                                source=uploaded_bind[0],
                                inspection_facts=uploaded_bind[1],
                                catalog=catalog,
                                shield_available=shield_available,
                                payload_store=payload_store,
                                new_stable_id=uuid4(),
                            )
                        except (PluginConfigError, InvariantError, TypeError, ValueError):
                            # Same degradation as a rejected chat transition: the
                            # upload stays uploaded and the authoritative turn is
                            # unchanged, so the wizard remains usable.
                            chat_result = StepChatResult(
                                assistant_message=(
                                    "I couldn't apply that uploaded file to this step, so I didn't change your "
                                    "pipeline. The file is still uploaded — continue with the wizard controls."
                                ),
                                status=ComposerChatTurnStatus.SYNTHETIC_UNAVAILABLE,
                                latency_ms=chat_result.latency_ms,
                                error_class="StepTransitionRejected",
                            )
                            next_turn = current_turn
                            prepared_next = planned_current
                        else:
                            resulting_state = bind.state
                            planned_response = bind.response_payload
                            next_turn = bind.next_turn
                            prepared_next = bind.next_payload
                            intermediate_occurrences = bind.intermediate
                            transition_succeeded = True
                    elif transition_body is not None:
                        try:
                            resulting_state, planned_response, next_turn, prepared_next = guided_route._schema8_answer_and_project_next(
                                current_state,
                                prospective,
                                current_turn,
                                transition_body,
                                catalog=catalog,
                                shield_available=shield_available,
                                new_stable_id=uuid4(),
                                data_dir=str(request.app.state.settings.data_dir),
                                session_id=str(owned_session.id),
                                source_inspection_facts=source_inspection_facts,
                                sink_prefill_options=sink_prefill_options,
                                sink_prefill_name=sink_prefill_name,
                            )
                            transition_succeeded = True
                        except guided_route.SinkAdmissionRejectedError as admission_exc:
                            # Deployment sink admission rejected the
                            # LLM-authored options (elspeth-ef92db3e16) — the
                            # same 400 the manual form POST returns. Chat has
                            # no 400 channel, so degrade to the safe
                            # not-applied response with the admission reason
                            # and keep the wizard authoritative. Only the
                            # typed admission rejection settles here: any
                            # other HTTPException (e.g. the 409 dict-detail
                            # stage guards) propagates as an operation
                            # failure.
                            admission_detail = admission_exc.detail
                            chat_result = _with_pair_disposition(
                                StepChatResult(
                                    assistant_message=(
                                        "I couldn't apply those output settings, so I didn't change your pipeline. "
                                        f"{admission_detail} "
                                        "Adjust the values in the wizard form and submit there."
                                    ),
                                    status=ComposerChatTurnStatus.SYNTHETIC_UNAVAILABLE,
                                    latency_ms=chat_result.latency_ms,
                                    error_class="SinkAdmissionRejected",
                                ),
                                deferred_disposition_message,
                            )
                            next_turn = current_turn
                            prepared_next = planned_current
                        except (PluginConfigError, InvariantError, TypeError, ValueError):
                            chat_result = _with_pair_disposition(
                                StepChatResult(
                                    assistant_message=(
                                        "I couldn't apply that configuration, so I didn't change your pipeline. "
                                        "Review the wizard fields and try again, or keep going with the wizard controls."
                                    ),
                                    status=ComposerChatTurnStatus.SYNTHETIC_UNAVAILABLE,
                                    latency_ms=chat_result.latency_ms,
                                    error_class="StepTransitionRejected",
                                ),
                                deferred_disposition_message,
                            )
                            next_turn = current_turn
                            prepared_next = planned_current

                    resulting_guided = resulting_state.guided_session
                    if resulting_guided is None:  # pragma: no cover
                        raise AuditIntegrityError("Guided Chat transition removed its checkpoint")
                    finished_at = datetime.now(UTC)
                    # Transcript custody (R2-F15): the rendered transcript always
                    # carries the author's verbatim words — including deferred
                    # retains, failed retains, and management commands. Privacy
                    # is enforced at the provider/audit boundary (later-stage
                    # prompts see only the rendered durable_summary; audit rows
                    # carry hashes), never by blanking the user's own turn.
                    user_turn = ChatTurn(
                        role=ChatRole.USER,
                        content=body.message,
                        seq=resulting_guided.chat_turn_seq,
                        step=prospective.step,
                        ts_iso=finished_at.isoformat(),
                        # The occurrence this message answered (validated
                        # against guided_turn_token above): Retry must resend
                        # THIS token, not whatever is current at click time,
                        # so a stale retry draws the ordinary 409 instead of
                        # applying old prose to newer session state
                        # (elspeth-ea80e34fdc).
                        turn_token=body.turn_token,
                    )
                    assistant_kind: Literal["assistant", "synthetic_failure"] = (
                        "assistant" if chat_result.status is ComposerChatTurnStatus.SUCCESS else "synthetic_failure"
                    )
                    assistant_turn = ChatTurn(
                        role=ChatRole.ASSISTANT,
                        content=chat_result.assistant_message,
                        seq=resulting_guided.chat_turn_seq + 1,
                        step=prospective.step,
                        ts_iso=finished_at.isoformat(),
                        assistant_message_kind=assistant_kind,
                        synthetic_failure_reason=(
                            None
                            if assistant_kind == "assistant"
                            else "not_applied"
                            if chat_result.error_class
                            in {
                                "SinkAdmissionRejected",
                                "StepTransitionRejected",
                                "ComponentRevisionNotApplied",
                                "InlineSourceNotApplied",
                                "UploadedSourceTypeMismatch",
                                "DeferredIntentActionShapeError",
                                "DeferredIntentManagementActionShapeError",
                                "DeferredIntentUnknown",
                                "DeferredIntentBindingMismatch",
                                "DeferredIntentAmbiguous",
                                "DeferredIntentClarification",
                                "DeferredIntentUnsupported",
                                "DeferredIntentRejected",
                                # ADR-033: a contradiction rejection is a
                                # deliberate not-applied verdict (the
                                # instruction is retained as clarification
                                # debt), never provider weather.
                                "DeferredIntentContradiction",
                                "DeferredIntentModelCatalogIdentity",
                                # The model's sink config failed plugin
                                # validation and was deliberately not staged —
                                # a rejected application, not provider weather
                                # (inv-f1 incidental 2).
                                "SinkPrefillConfigRejected",
                                # Retain-alone: the pair's resolution half was
                                # withheld while the retain applied — the
                                # not-applied signal is scoped to that half
                                # (round-2 review finding).
                                "PairedResolutionShapeRejected",
                                "PairedResolutionConfigRejected",
                                "PairedResolutionNotResent",
                            }
                            else "quality_guard"
                            if chat_result.error_class == "AssistantScaffoldLeakError"
                            # The provider ANSWERED; the reply violated the
                            # tool's argument contract. Calling that
                            # "unavailable" mislabels a model-output defect as
                            # provider weather and contradicts the turn's own
                            # copy ("Press Retry to have me redo this step") —
                            # inv-f1 D4.
                            else "model_defect"
                            if chat_result.error_class == "GuidedToolArgumentShapeError"
                            else "unavailable"
                        ),
                    )
                    resulting_guided = _replace(
                        resulting_guided,
                        chat_history=(*resulting_guided.chat_history, user_turn, assistant_turn),
                        chat_turn_seq=resulting_guided.chat_turn_seq + 2,
                    )
                    resulting_state = _replace(resulting_state, guided_session=resulting_guided)
                    recorder.record_chat_turn(
                        ComposerChatTurn(
                            step=prospective.step.value,
                            initiator=ComposerChatInitiator.USER,
                            chat_turn_seq=user_turn.seq,
                            user_message_hash=stable_hash(body.message),
                            assistant_message_hash=stable_hash(chat_result.assistant_message),
                            latency_ms=chat_result.latency_ms,
                            model=settings.composer_model,
                            status=chat_result.status,
                            started_at=started_at,
                            finished_at=finished_at,
                            # Same occurrence binding the session-side ChatTurn
                            # carries: the token this message answered, so the
                            # audit trail can distinguish a retry of an earlier
                            # occurrence from a fresh message (elspeth-ea80e34fdc).
                            turn_token=body.turn_token,
                            error_class=chat_result.error_class,
                        )
                    )

                    audit = BufferingRecorder()
                    if cancelled_intent is not None:
                        emit_intent_cancelled(
                            audit,
                            intent=cancelled_intent,
                            composition_version=current_state.version,
                            actor=user.user_id,
                        )
                    if occurrence_was_prospective:
                        emit_turn_emitted(
                            audit,
                            step=prospective.step,
                            turn_type=TurnType(current_turn["type"]),
                            payload_hash=planned_current.payload_id,
                            payload_payload_id=planned_current.payload_id,
                            emitter="server",
                            composition_version=current_state.version,
                            actor=user.user_id,
                        )
                    if transition_succeeded and planned_response is not None:
                        emit_turn_answered(
                            audit,
                            step=prospective.step,
                            turn_type=TurnType(current_turn["type"]),
                            response_hash=planned_response.payload_id,
                            response_payload_id=planned_response.payload_id,
                            control_signal=None,
                            composition_version=current_state.version,
                            actor=user.user_id,
                        )
                        for occurrence in intermediate_occurrences:
                            emit_turn_emitted(
                                audit,
                                step=occurrence.step,
                                turn_type=TurnType(occurrence.turn["type"]),
                                payload_hash=occurrence.payload.payload_id,
                                payload_payload_id=occurrence.payload.payload_id,
                                emitter="server",
                                composition_version=current_state.version,
                                actor=user.user_id,
                            )
                            emit_turn_answered(
                                audit,
                                step=occurrence.step,
                                turn_type=TurnType(occurrence.turn["type"]),
                                response_hash=occurrence.response.payload_id,
                                response_payload_id=occurrence.response.payload_id,
                                control_signal=None,
                                composition_version=current_state.version,
                                actor=user.user_id,
                            )
                        if resulting_guided.step is not prospective.step and not rewound:
                            emit_step_advanced(
                                audit,
                                prev=prospective.step,
                                next_=resulting_guided.step,
                                reason="user_advanced",
                                composition_version=current_state.version,
                                actor=user.user_id,
                            )
                        if prepared_next is not None and next_turn is not None:
                            emit_turn_emitted(
                                audit,
                                step=resulting_guided.step,
                                turn_type=TurnType(next_turn["type"]),
                                payload_hash=prepared_next.payload_id,
                                payload_payload_id=prepared_next.payload_id,
                                emitter="server",
                                composition_version=current_state.version,
                                actor=user.user_id,
                            )

                    prepared_payloads: list[PreparedGuidedJsonPayload] = []
                    planned_cohort: list[PreparedGuidedJsonPayload | None] = [planned_current, planned_response]
                    for occurrence in intermediate_occurrences:
                        planned_cohort.extend((occurrence.payload, occurrence.response))
                    planned_cohort.append(prepared_next)
                    for planned in planned_cohort:
                        if planned is None or planned.payload_id in {item.payload_id for item in prepared_payloads}:
                            continue
                        prepared = prepare_guided_json_payload(
                            payload_store,
                            purpose=planned.purpose,
                            payload=planned.payload,
                        )
                        if prepared.payload_id != planned.payload_id:
                            raise AuditIntegrityError("Guided Chat payload changed before settlement")
                        prepared_payloads.append(prepared)

                    existing_meta = (
                        dict(deep_thaw(current_record.composer_meta))
                        if current_record is not None and current_record.composer_meta is not None
                        else {}
                    )
                    existing_meta["guided_session"] = resulting_guided.to_dict()
                    state_dict = resulting_state.to_dict()
                    is_valid, validation_errors = guided_route._guided_persisted_validity(resulting_state, catalog=catalog)
                    state_data = CompositionStateData(
                        sources=state_dict["sources"],
                        nodes=state_dict["nodes"],
                        edges=state_dict["edges"],
                        outputs=state_dict["outputs"],
                        metadata_=state_dict["metadata"],
                        is_valid=is_valid,
                        validation_errors=validation_errors,
                        composer_meta=existing_meta,
                    )
                    replay_turn = (
                        GuidedReplayTurn(
                            turn_type=TurnType(next_turn["type"]),
                            step_index=next_turn["step_index"],
                            payload_id=prepared_next.payload_id,
                        )
                        if next_turn is not None and prepared_next is not None
                        else None
                    )
                    evidence = GuidedAuditEvidence(
                        invocations=(*audit.invocations, *recorder.invocations),
                        llm_calls=recorder.llm_calls,
                        chat_turns=recorder.chat_turns,
                    )
                    await _publish_progress(
                        progress_sink,
                        event=ComposerProgressEvent(
                            phase="saving",
                            headline="I'm saving this guided turn.",
                            evidence=(
                                ("The uploaded file was inspected and its source-type mismatch was preserved without provider work.")
                                if uploaded_mismatch_facts is not None
                                else ("The uploaded file was inspected and bound for confirmation without provider work.")
                                if uploaded_bind is not None
                                else "The provider response passed the guided transition checks.",
                            ),
                            likely_next="ELSPETH will finish the atomic state and audit settlement.",
                        ),
                    )
                    settlement = await service.settle_guided_state_operation(
                        GuidedStateOperationCommand(
                            fence=fence,
                            expected_current_state_id=current_record.id if current_record is not None else None,
                            expected_current_state_version=current_record.version if current_record is not None else None,
                            expected_current_content_hash=(composition_content_hash(current_state) if current_record is not None else None),
                            state_id=uuid4(),
                            state=state_data,
                            provenance="convergence_persist",
                            actor="composer_route",
                            response=GuidedResponseDescriptor(
                                kind="guided_chat",
                                next_turn=replay_turn,
                                assistant_turn_seq=assistant_turn.seq,
                            ),
                            payloads=tuple(prepared_payloads),
                            audit_evidence=evidence,
                            originating_message=originating_message,
                            retained_deferred_intent_id=retained_intent_id,
                            deferred_intent_action=settled_management_action,
                            invalidated_pending_proposal=invalidated_pending_proposal,
                        ),
                        payload_store=payload_store,
                    )
                    response = response_from_record(settlement.result_state)

                await _publish_progress(
                    progress_sink,
                    event=ComposerProgressEvent(
                        phase="complete",
                        headline="ELSPETH finished responding to this guided message.",
                        evidence=("The guided chat turn was settled atomically.",),
                        likely_next="Review the reply and continue the wizard.",
                        reason="composer_complete",
                    ),
                )
                return response
            except GuidedOperationFenceLostError:
                rejoin_after_lock = True
            except asyncio.CancelledError as exc:
                with contextlib.suppress(Exception):
                    await asyncio.shield(
                        service.fail_guided_operation_with_audit(
                            GuidedOperationFailureCommand(
                                fence=reserved.fence,
                                failure_code="operation_failed",
                                actor="composer_route",
                                audit_evidence=GuidedAuditEvidence(
                                    invocations=recorder.invocations,
                                    llm_calls=recorder.llm_calls,
                                    chat_turns=recorder.chat_turns,
                                ),
                            )
                        )
                    )
                if progress_started:
                    with contextlib.suppress(Exception):
                        await asyncio.shield(
                            _publish_progress(
                                progress_sink,
                                event=client_cancelled_progress_event(),
                            )
                        )
                if _is_client_disconnect_cancel(exc):
                    raise HTTPException(status_code=499, detail="Client disconnected while the guided chat turn was running.") from exc
                raise
            except Exception as exc:
                failure_code: GuidedOperationFailureCode = (
                    "stale_conflict"
                    if isinstance(exc, GuidedOperationSettlementConflictError)
                    else "integrity_error"
                    if isinstance(exc, (AuditIntegrityError, InvariantError))
                    else "operation_failed"
                )
                with contextlib.suppress(Exception):
                    slog.error(
                        "guided.operation_terminal_failure",
                        session_id=str(session_id),
                        user_id=user.user_id,
                        exc_class=type(exc).__name__,
                        site="post_guided_chat",
                        frames=_safe_frame_strings(exc),
                        # See ``guided.py``'s post_guided_start site (R2-F16b):
                        # correlates this log line to the response's
                        # X-Request-ID; lenient read so a missing middleware
                        # cannot break the error path.
                        request_id=_failure_log_request_id(request),
                    )
                try:
                    failed = await service.fail_guided_operation_with_audit(
                        GuidedOperationFailureCommand(
                            fence=reserved.fence,
                            failure_code=failure_code,
                            actor="composer_route",
                            audit_evidence=GuidedAuditEvidence(
                                invocations=recorder.invocations,
                                llm_calls=recorder.llm_calls,
                                chat_turns=recorder.chat_turns,
                            ),
                        )
                    )
                except GuidedOperationFenceLostError:
                    rejoin_after_lock = True
                else:
                    raise_guided_operation_failure(failed)
        if rejoin_after_lock:
            joined = await reserve_or_replay_guided_operation(
                service=service,
                session_id=session_id,
                kind="guided_chat",
                request=body,
                replay=replay,
                reserve_if_absent=False,
            )
            if joined is None:
                raise AuditIntegrityError("Guided Chat fence was lost without a joinable winner")
            if isinstance(joined, GuidedOperationLease):
                pending = joined
                continue
            return joined
        raise AuditIntegrityError("Guided Chat settlement loop exited without a result")


__all__ = ["post_guided_chat_schema8", "run_guided_chat_provider_attempt"]
