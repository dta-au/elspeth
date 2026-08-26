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
_SECOND_INTENT_ID = UUID("55555555-5555-4555-8555-555555555555")
_RETAINED_INTENT_ID = "33333333-3333-4333-8333-333333333333"
_EDITED_INTENT_ID = "44444444-4444-4444-8444-444444444444"
_ROUTING_MESSAGE = "Later add a gate that routes rows with amount greater than 500 to high_value, and everything else to standard."
_COLLECTOR_MESSAGE = "Later add a collector that stitches the exploded pages back into one row per document."
_COLLECTOR_AND_GATE_MESSAGE = "Later add a collector and a gate so the exploded pages come back together before routing."
_COLLECTOR_DECLINED_MESSAGE = "Later add a gate that routes high-value rows to review; no collector needed for this one."
_COLLECTOR_AND_SCORER_MESSAGE = (
    "Later add a collector that stitches the exploded pages back together, and later score each row for sentiment."
)
_FRAME_OPEN = (
    "I kept that future-stage instruction, but its structure was not verified. "
    "The plugin I proposed for it is not available here, and you did not ask for it by name, "
    "so I did not treat it as a deployment problem. "
)
_GATE_CLAUSE = "A gate is a built-in topology node, not a transform plugin. "
_COLLECTOR_CLAUSE = (
    "A collector is a built-in topology node that IS backed by a batch-transform plugin, and it closes "
    "an EXPAND scope — it needs scope_name, scope_opener and scope_policy. Name the batch behaviour you "
    "want and the scope it should close. "
)
_FRAME_CLOSE = "Clarify the concrete topology structure and I'll firm it up."


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


def _apply(action: DeferredIntentAction, *, catalog: PolicyCatalogView, message: str = _ROUTING_MESSAGE) -> DeferredRequestApplication:
    return apply_deferred_request(
        (action,),
        None,
        authority=DeferredRequestAuthority(
            guided=GuidedSession.initial(),
            catalog=catalog,
            originating_message=GuidedOriginatingUserMessageDraft(
                message_id=_MESSAGE_ID,
                content=message,
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


def _apply_many(*actions: DeferredIntentAction, catalog: PolicyCatalogView, message: str) -> DeferredRequestApplication:
    return apply_deferred_request(
        actions,
        None,
        authority=DeferredRequestAuthority(
            guided=GuidedSession.initial(),
            catalog=catalog,
            originating_message=GuidedOriginatingUserMessageDraft(
                message_id=_MESSAGE_ID,
                content=message,
            ),
            new_intent_ids=(_INTENT_ID, _SECOND_INTENT_ID),
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


def test_unmentioned_unavailable_model_catalog_identity_teaches_collector_scopes() -> None:
    """A message naming only a collector gets the frame PLUS the collector clause.

    Collector is deliberately absent from `_STRUCTURAL_NODE_TYPES` — that
    tuple's copy asserts "not a transform plugin", which is false for a
    plugin-bearing collector — so the teaching composes into the frame instead
    (filigree elspeth-270e81443d).

    No clause may attribute the unavailable plugin to the collector: nothing
    reaching `_model_catalog_identity_chat` says which node the plugin was for
    or what kind it is, so that referent is unverifiable by construction.
    """

    result = _apply(
        _action("stitch_pages"),
        catalog=_catalog(available=frozenset({PluginId("transform", "passthrough")})),
        message=_COLLECTOR_MESSAGE,
    )

    assert type(result) is DeferredRequestRetained
    assert result.chat.error_class == "DeferredIntentModelCatalogIdentity"
    assert result.chat.assistant_message == _FRAME_OPEN + _COLLECTOR_CLAUSE + _FRAME_CLOSE
    # Nothing may bind the proposed plugin to the collector, and the collector
    # must never be called "not a transform plugin".
    assert "proposed for the collector" not in result.chat.assistant_message
    assert "batch-transform plugin I proposed" not in result.chat.assistant_message
    assert "A collector is a built-in topology node, not a transform plugin" not in result.chat.assistant_message
    (intent,) = result.guided.deferred_intents
    assert intent.constraints == ()
    assert intent.catalog_kind is None
    assert intent.catalog_name is None


def test_collector_and_structural_clauses_both_emit_inside_one_frame() -> None:
    """BOTH teaching clauses appear, and the shared frame is emitted ONCE.

    "add a collector and a gate" is the case that made this additive. A
    collector arm that REPLACED the structural arm would take the true gate
    clause away; a returning collector branch placed after the `next()` scan
    would never be reached whenever a structural node was also named. Composing
    the clauses gives the user both, so no ordering is load-bearing and nothing
    true is traded away (filigree elspeth-270e81443d, review comment 7977 §7.1).
    """

    result = _apply(
        _action("stitch_pages"),
        catalog=_catalog(available=frozenset({PluginId("transform", "passthrough")})),
        message=_COLLECTOR_AND_GATE_MESSAGE,
    )

    assert type(result) is DeferredRequestRetained
    assert result.chat.assistant_message == _FRAME_OPEN + _GATE_CLAUSE + _COLLECTOR_CLAUSE + _FRAME_CLOSE
    # Exact equality above already pins single emission; these state the
    # property a reader is looking for when a clause is added.
    assert result.chat.assistant_message.count(_FRAME_OPEN) == 1
    assert result.chat.assistant_message.count(_FRAME_CLOSE) == 1


def test_declining_a_collector_still_gets_the_true_structural_clause() -> None:
    """Negation must not turn a TRUE clause into a FALSE one.

    `_message_names_identifier` is a word-boundary regex with no notion of
    negation, and detecting negation here is banned — it would fail the other
    way on "no gate, add a collector". Composition makes that safe: "no
    collector needed" keeps its correct gate clause, and the appended collector
    clause is a general truth, so it is at worst unhelpful. A precedence arm
    would have REPLACED the gate clause with collector copy, turning a true
    message false — the regression this shape exists to prevent.
    """

    result = _apply(
        _action("numeric_route"),
        catalog=_catalog(available=frozenset({PluginId("transform", "passthrough")})),
        message=_COLLECTOR_DECLINED_MESSAGE,
    )

    assert type(result) is DeferredRequestRetained
    assert result.chat.assistant_message == _FRAME_OPEN + _GATE_CLAUSE + _COLLECTOR_CLAUSE + _FRAME_CLOSE


def test_collector_clause_stays_true_beside_a_successfully_saved_sibling_action() -> None:
    """Two actions in one turn: the saved-instruction line and the frame coexist.

    `_compose_disposition_chats` joins per-action chats in action order, so a
    retained sibling prepends "I saved that instruction for the topology stage."
    to this frame. Under the superseded copy that turn was self-contradictory —
    it said the COLLECTOR request was unverified because of a plugin that in
    fact belonged to the scoring transform. The frame now attributes the
    unavailable plugin to no node, so both halves are true at once
    (review comment 7977 §2B).
    """

    result = _apply_many(
        _action("passthrough"),
        _action("sentiment_scorer"),
        catalog=_catalog(available=frozenset({PluginId("transform", "passthrough")})),
        message=_COLLECTOR_AND_SCORER_MESSAGE,
    )

    assert type(result) is DeferredRequestRetained
    assert result.chat.assistant_message == (
        "I saved that instruction for the topology stage. " + _FRAME_OPEN + _COLLECTOR_CLAUSE + _FRAME_CLOSE
    )
    assert "collector request" not in result.chat.assistant_message


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
