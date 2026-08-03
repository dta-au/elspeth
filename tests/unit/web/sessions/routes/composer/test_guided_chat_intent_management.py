"""Application precedence for guided deferred-intent retention."""

from __future__ import annotations

from uuid import UUID

from elspeth.contracts.composer_llm_audit import ComposerChatTurnStatus
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.guided.deferred_intents import DeferredIntentAction
from elspeth.web.composer.guided.stage_subjects import ComponentCountConstraint, StageName
from elspeth.web.composer.guided.state_machine import GuidedSession
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
