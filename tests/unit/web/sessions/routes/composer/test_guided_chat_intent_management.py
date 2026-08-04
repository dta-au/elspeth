"""Application precedence for guided deferred-intent retention."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from elspeth.contracts.composer_llm_audit import ComposerChatTurnStatus
from elspeth.core.canonical import stable_hash
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.guided.deferred_intents import DeferredIntentAction, DeferredIntentEditAction
from elspeth.web.composer.guided.intent_management import deferred_intent_management_option
from elspeth.web.composer.guided.stage_subjects import ComponentCountConstraint, StageName
from elspeth.web.composer.guided.state_machine import DeferredStageIntent, GuidedSession
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId
from elspeth.web.sessions._guided_step_chat import StepChatResult
from elspeth.web.sessions.protocol import GuidedOriginatingUserMessageDraft
from elspeth.web.sessions.routes.composer.guided_chat_intent_management import (
    DeferredRequestApplication,
    DeferredRequestAuthority,
    DeferredRequestRetained,
    DeferredRequestUnchanged,
    apply_deferred_request,
)

_MESSAGE_ID = UUID("11111111-1111-4111-8111-111111111111")
_INTENT_ID = UUID("22222222-2222-4222-8222-222222222222")
_RETAINED_INTENT_ID = "33333333-3333-4333-8333-333333333333"
_EDITED_INTENT_ID = "44444444-4444-4444-8444-444444444444"
_ROUTING_MESSAGE = "Later add a gate that routes rows with amount greater than 500 to high_value, and everything else to standard."


def _catalog(*, available: frozenset[PluginId]) -> PolicyCatalogView:
    snapshot = PluginAvailabilitySnapshot.create(
        policy_hash="a" * 64,
        principal_scope="local:test",
        available=available,
        unavailable=(),
        selected=(),
        usable_profile_aliases=(),
        selected_profile_aliases=(),
        binding_generation_fingerprint="b" * 64,
    )
    return PolicyCatalogView(create_catalog_service(), snapshot, profiles=None)  # type: ignore[arg-type]


def _action(catalog_name: str, *, target_stage: StageName = "topology") -> DeferredIntentAction:
    return DeferredIntentAction(
        target_stage=target_stage,
        catalog_kind="transform",
        catalog_name=catalog_name,
        redacted_summary="Retain a weaker model-authored topology requirement.",
        constraints=(
            ComponentCountConstraint(
                kind="component_count",
                component_kind="node",
                plugin_kind="transform",
                plugin_name=catalog_name,
                operator="at_least",
                count=1,
            ),
        ),
    )


def _apply(action: DeferredIntentAction, *, catalog: PolicyCatalogView) -> DeferredRequestApplication:
    return apply_deferred_request(
        action,
        None,
        authority=DeferredRequestAuthority(
            guided=GuidedSession.initial(),
            catalog=catalog,
            originating_message=GuidedOriginatingUserMessageDraft(
                message_id=_MESSAGE_ID,
                content=_ROUTING_MESSAGE,
            ),
            new_intent_id=_INTENT_ID,
        ),
        chat=StepChatResult(
            assistant_message="model-authored response",
            status=ComposerChatTurnStatus.SUCCESS,
            latency_ms=7,
            error_class=None,
        ),
    )


def test_unmentioned_unavailable_model_catalog_identity_precedes_weaker_routing_rejection() -> None:
    result = _apply(
        _action("numeric_route"),
        catalog=_catalog(available=frozenset({PluginId("transform", "passthrough")})),
    )

    assert type(result) is DeferredRequestRetained
    assert result.chat.error_class == "DeferredIntentModelCatalogIdentity"
    assert "gate" in result.chat.assistant_message
    assert "structure was not verified" in result.chat.assistant_message
    (intent,) = result.guided.deferred_intents
    assert intent.constraints == ()
    assert intent.catalog_kind is None
    assert intent.catalog_name is None


def test_unmentioned_unavailable_identity_cannot_bypass_same_stage_rejection() -> None:
    result = _apply(
        _action("numeric_route", target_stage="source"),
        catalog=_catalog(available=frozenset({PluginId("transform", "passthrough")})),
    )

    assert type(result) is DeferredRequestUnchanged
    assert result.chat.error_class == "DeferredIntentRejected"
    assert result.guided.deferred_intents == ()


def test_unmentioned_unavailable_identity_cannot_bypass_responsible_stage_rejection() -> None:
    result = _apply(
        _action("numeric_route", target_stage="output"),
        catalog=_catalog(available=frozenset({PluginId("transform", "passthrough")})),
    )

    assert type(result) is DeferredRequestUnchanged
    assert result.chat.error_class == "DeferredIntentRejected"
    assert result.guided.deferred_intents == ()


def test_unmentioned_available_catalog_identity_still_reaches_routing_validation() -> None:
    result = _apply(
        _action("passthrough"),
        catalog=_catalog(available=frozenset({PluginId("transform", "passthrough")})),
    )

    assert type(result) is DeferredRequestUnchanged
    assert result.chat.error_class == "DeferredIntentRejected"
    assert result.guided.deferred_intents == ()


def _count_intent(
    *,
    intent_id: str,
    plugin_name: str,
    operator: str,
    count: int,
    summary: str,
) -> DeferredStageIntent:
    return DeferredStageIntent.create(
        intent_id=intent_id,
        receiving_stage="source",
        target_stage="topology",
        catalog_kind="transform",
        catalog_name=plugin_name,
        redacted_summary=summary,
        originating_message_id=str(_MESSAGE_ID),
        message_content_hash=stable_hash("private originating message"),
        constraints=(
            ComponentCountConstraint(
                kind="component_count",
                component_kind="node",
                plugin_kind="transform",
                plugin_name=plugin_name,
                operator=operator,  # type: ignore[arg-type]
                count=count,
            ),
        ),
    )


def _bounded_action(plugin_name: str, *, operator: str, count: int) -> DeferredIntentAction:
    return DeferredIntentAction(
        target_stage="topology",
        catalog_kind="transform",
        catalog_name=plugin_name,
        redacted_summary="Bound the named transform count.",
        constraints=(
            ComponentCountConstraint(
                kind="component_count",
                component_kind="node",
                plugin_kind="transform",
                plugin_name=plugin_name,
                operator=operator,  # type: ignore[arg-type]
                count=count,
            ),
        ),
    )


def test_contradiction_rejection_retains_instruction_as_clarification_debt() -> None:
    """ADR-033 rejection path: contradicted instructions surface as
    clarification debt through ``apply_deferred_clarification`` (R2-F15),
    never vanish, and the chat names the exact conflicting retained intent."""

    retained = _count_intent(
        intent_id=_RETAINED_INTENT_ID,
        plugin_name="passthrough",
        operator="at_least",
        count=2,
        summary="Future topology instruction; 1 structural constraint(s).",
    )
    guided = replace(GuidedSession.initial(), deferred_intents=(retained,))
    result = apply_deferred_request(
        _bounded_action("passthrough", operator="at_most", count=1),
        None,
        authority=DeferredRequestAuthority(
            guided=guided,
            catalog=_catalog(available=frozenset({PluginId("transform", "passthrough")})),
            originating_message=GuidedOriginatingUserMessageDraft(
                message_id=_MESSAGE_ID,
                content="Later keep at most 1 passthrough node.",
            ),
            new_intent_id=_INTENT_ID,
        ),
        chat=StepChatResult(
            assistant_message="model-authored response",
            status=ComposerChatTurnStatus.SUCCESS,
            latency_ms=7,
            error_class=None,
        ),
    )

    assert type(result) is DeferredRequestRetained
    assert result.retained_intent_id == _INTENT_ID
    assert result.chat.error_class == "DeferredIntentContradiction"
    assert _RETAINED_INTENT_ID in result.chat.assistant_message
    assert "pending clarification" in result.chat.assistant_message
    assert retained.redacted_summary in result.chat.assistant_message
    first, clarification = result.guided.deferred_intents
    assert first == retained
    assert clarification.intent_id == str(_INTENT_ID)
    assert clarification.constraints == ()


def test_management_edit_contradiction_names_conflict_and_leaves_saved_instructions_unchanged() -> None:
    conflicting = _count_intent(
        intent_id=_RETAINED_INTENT_ID,
        plugin_name="passthrough",
        operator="at_least",
        count=2,
        summary="Future topology instruction; 1 structural constraint(s).",
    )
    edited = _count_intent(
        intent_id=_EDITED_INTENT_ID,
        plugin_name="numeric_route",
        operator="at_least",
        count=1,
        summary="Future topology instruction; one numeric_route node.",
    )
    guided = replace(GuidedSession.initial(), deferred_intents=(conflicting, edited))
    edit_command = f"Edit exact intent {_EDITED_INTENT_ID}: keep at most one passthrough node."
    result = apply_deferred_request(
        None,
        DeferredIntentEditAction(
            intent_id=_EDITED_INTENT_ID,
            selection_token=deferred_intent_management_option(edited).selection_token,
            replacement=_bounded_action("passthrough", operator="at_most", count=1),
        ),
        authority=DeferredRequestAuthority(
            guided=guided,
            catalog=_catalog(available=frozenset({PluginId("transform", "passthrough"), PluginId("transform", "numeric_route")})),
            originating_message=GuidedOriginatingUserMessageDraft(message_id=_MESSAGE_ID, content=edit_command),
            new_intent_id=_INTENT_ID,
        ),
        chat=StepChatResult(
            assistant_message="model-authored response",
            status=ComposerChatTurnStatus.SUCCESS,
            latency_ms=7,
            error_class=None,
        ),
    )

    assert type(result) is DeferredRequestUnchanged
    assert result.chat.error_class == "DeferredIntentContradiction"
    assert _RETAINED_INTENT_ID in result.chat.assistant_message
    assert "did not change" in result.chat.assistant_message
    assert result.guided.deferred_intents == (conflicting, edited)
