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
        (action,),
        None,
        authority=DeferredRequestAuthority(
            guided=GuidedSession.initial(),
            catalog=catalog,
            originating_message=GuidedOriginatingUserMessageDraft(
                message_id=_MESSAGE_ID,
                content=_ROUTING_MESSAGE,
            ),
            new_intent_ids=(_INTENT_ID,),
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
    """A non-later target is not a future-stage instruction: the current-stage
    flow owns it, so the action is rejected without minting clarification debt."""

    result = _apply(
        _action("numeric_route", target_stage="source"),
        catalog=_catalog(available=frozenset({PluginId("transform", "passthrough")})),
    )

    assert type(result) is DeferredRequestUnchanged
    assert result.chat.error_class == "DeferredIntentRejected"
    assert result.guided.deferred_intents == ()


def test_responsible_stage_rejection_keeps_its_diagnostic_and_retains_clarification_debt() -> None:
    """The structural diagnostic still wins over the catalog-identity copy
    (precedence), but the instruction is retained as clarification debt
    (R2-F15) instead of vanishing."""

    result = _apply(
        _action("numeric_route", target_stage="output"),
        catalog=_catalog(available=frozenset({PluginId("transform", "passthrough")})),
    )

    assert type(result) is DeferredRequestRetained
    assert result.retained_intent_ids == (_INTENT_ID,)
    assert result.chat.error_class == "DeferredIntentRejected"
    assert "pending clarification" in result.chat.assistant_message
    assert "target stage" in result.chat.assistant_message
    assert "structure was not verified" not in result.chat.assistant_message
    (intent,) = result.guided.deferred_intents
    assert intent.constraints == ()
    assert intent.catalog_kind is None
    assert intent.catalog_name is None


def test_unproven_stated_routing_retains_instruction_as_clarification_debt() -> None:
    """A stated-fact-unproven rejection keeps the instruction as constraint-free
    clarification debt; the unproven routing facts never persist."""

    result = _apply(
        _action("passthrough"),
        catalog=_catalog(available=frozenset({PluginId("transform", "passthrough")})),
    )

    assert type(result) is DeferredRequestRetained
    assert result.retained_intent_ids == (_INTENT_ID,)
    assert result.chat.error_class == "DeferredIntentRejected"
    assert "pending clarification" in result.chat.assistant_message
    (intent,) = result.guided.deferred_intents
    assert intent.constraints == ()
    assert intent.catalog_kind is None
    assert intent.catalog_name is None


def test_catalog_kind_mismatch_retains_instruction_as_clarification_debt() -> None:
    """A user-named plugin claimed under the wrong catalog kind is retained as
    clarification debt; the mis-kinded identity never persists."""

    action = DeferredIntentAction(
        target_stage="topology",
        catalog_kind="transform",
        catalog_name="csv",
        redacted_summary="Retain a mis-kinded catalog identity.",
        constraints=(
            ComponentCountConstraint(
                kind="component_count",
                component_kind="node",
                plugin_kind="transform",
                plugin_name="csv",
                operator="at_least",
                count=1,
            ),
        ),
    )
    result = apply_deferred_request(
        (action,),
        None,
        authority=DeferredRequestAuthority(
            guided=GuidedSession.initial(),
            catalog=_catalog(available=frozenset({PluginId("source", "csv"), PluginId("sink", "csv")})),
            originating_message=GuidedOriginatingUserMessageDraft(
                message_id=_MESSAGE_ID,
                content="Later add the csv step to the processing.",
            ),
            new_intent_ids=(_INTENT_ID,),
        ),
        chat=StepChatResult(
            assistant_message="model-authored response",
            status=ComposerChatTurnStatus.SUCCESS,
            latency_ms=7,
            error_class=None,
        ),
    )

    assert type(result) is DeferredRequestRetained
    assert result.chat.error_class == "DeferredIntentRejected"
    assert "pending clarification" in result.chat.assistant_message
    (intent,) = result.guided.deferred_intents
    assert intent.constraints == ()
    assert intent.catalog_kind is None
    assert intent.catalog_name is None


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
        (_bounded_action("passthrough", operator="at_most", count=1),),
        None,
        authority=DeferredRequestAuthority(
            guided=guided,
            catalog=_catalog(available=frozenset({PluginId("transform", "passthrough")})),
            originating_message=GuidedOriginatingUserMessageDraft(
                message_id=_MESSAGE_ID,
                content="Later keep at most 1 passthrough node.",
            ),
            new_intent_ids=(_INTENT_ID,),
        ),
        chat=StepChatResult(
            assistant_message="model-authored response",
            status=ComposerChatTurnStatus.SUCCESS,
            latency_ms=7,
            error_class=None,
        ),
    )

    assert type(result) is DeferredRequestRetained
    assert result.retained_intent_ids == (_INTENT_ID,)
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
        (),
        DeferredIntentEditAction(
            intent_id=_EDITED_INTENT_ID,
            selection_token=deferred_intent_management_option(edited).selection_token,
            replacement=_bounded_action("passthrough", operator="at_most", count=1),
        ),
        authority=DeferredRequestAuthority(
            guided=guided,
            catalog=_catalog(available=frozenset({PluginId("transform", "passthrough"), PluginId("transform", "numeric_route")})),
            originating_message=GuidedOriginatingUserMessageDraft(message_id=_MESSAGE_ID, content=edit_command),
            new_intent_ids=(_INTENT_ID,),
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


def test_two_actions_in_one_send_fold_and_compose_against_the_evolving_state() -> None:
    """elspeth-3a21f09f09: N actions in one Send each keep their own disposition.

    An accepted action appends with its verified constraints; a sibling whose
    catalog identity cannot be verified appends as clarification debt — BOTH
    intents land in one application, in call order, and the composed chat
    keeps the not-applied signal from the unverified half."""

    from uuid import uuid4

    second_intent_id = uuid4()
    result = apply_deferred_request(
        (_action("passthrough"), _action("numeric_route")),
        None,
        authority=DeferredRequestAuthority(
            guided=GuidedSession.initial(),
            catalog=_catalog(available=frozenset({PluginId("transform", "passthrough")})),
            originating_message=GuidedOriginatingUserMessageDraft(
                message_id=_MESSAGE_ID,
                content="Later add the passthrough transform, and later route rows through the special step.",
            ),
            new_intent_ids=(_INTENT_ID, second_intent_id),
        ),
        chat=StepChatResult(
            assistant_message="model-authored response",
            status=ComposerChatTurnStatus.SUCCESS,
            latency_ms=7,
            error_class=None,
        ),
    )

    assert type(result) is DeferredRequestRetained
    assert result.retained_intent_ids == (_INTENT_ID, second_intent_id)
    first, second = result.guided.deferred_intents
    assert first.intent_id == str(_INTENT_ID)
    assert first.catalog_name == "passthrough"
    assert second.intent_id == str(second_intent_id)
    # The unverified half is constraint-free clarification debt, never the
    # model's unproven catalog identity.
    assert second.catalog_name is None
    # Composed chat: both dispositions visible, the not-applied signal wins.
    assert "I saved that instruction for the topology stage." in result.chat.assistant_message
    assert result.chat.status is ComposerChatTurnStatus.SYNTHETIC_UNAVAILABLE
    assert result.chat.error_class == "DeferredIntentModelCatalogIdentity"


def test_all_actions_unretainable_in_one_send_leave_state_unchanged() -> None:
    """Two same-stage actions in one Send append nothing and change nothing."""

    result = apply_deferred_request(
        (_action("passthrough", target_stage="source"), _action("csv", target_stage="source")),
        None,
        authority=DeferredRequestAuthority(
            guided=GuidedSession.initial(),
            catalog=_catalog(available=frozenset({PluginId("transform", "passthrough"), PluginId("transform", "csv")})),
            originating_message=GuidedOriginatingUserMessageDraft(
                message_id=_MESSAGE_ID,
                content="Use the passthrough and csv steps here.",
            ),
            new_intent_ids=(_INTENT_ID, UUID("55555555-5555-4555-8555-555555555555")),
        ),
        chat=StepChatResult(
            assistant_message="model-authored response",
            status=ComposerChatTurnStatus.SUCCESS,
            latency_ms=7,
            error_class=None,
        ),
    )

    assert type(result) is DeferredRequestUnchanged
    assert result.guided.deferred_intents == ()
