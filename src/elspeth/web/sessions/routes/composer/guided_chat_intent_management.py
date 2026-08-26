"""Schema-8 guided Chat deferred-intent application and rewind authority."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol, cast
from uuid import UUID

from elspeth.contracts.composer_llm_audit import ComposerChatTurnStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.payload_store import PayloadStore
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.schemas import PluginKind
from elspeth.web.composer.guided.deferred_intents import (
    DeferredIntentAccepted,
    DeferredIntentAction,
    DeferredIntentCancelAction,
    DeferredIntentClarification,
    DeferredIntentEditAction,
    DeferredIntentManagementAction,
    DeferredIntentRejected,
    DeferredIntentUnsupported,
    create_deferred_clarification_intent,
    create_deferred_stage_intent,
    validate_deferred_intent_action,
    validate_deferred_intent_structure,
)
from elspeth.web.composer.guided.intent_management import (
    DeferredIntentManagementAmbiguous,
    DeferredIntentManagementApplied,
    DeferredIntentManagementBindingMismatch,
    DeferredIntentManagementUnknown,
    resolve_deferred_intent_management,
    schema8_deferred_management_rewind_step,
)
from elspeth.web.composer.guided.planning import guided_private_reviewed_facts
from elspeth.web.composer.guided.protocol import GuidedStep, Turn, TurnType
from elspeth.web.composer.guided.stage_subjects import StageName
from elspeth.web.composer.guided.state_machine import DeferredStageIntent, GuidedProposalRef, GuidedSession, TurnRecord
from elspeth.web.composer.state import CompositionState
from elspeth.web.plugin_policy.models import PluginId
from elspeth.web.sessions._guided_step_chat import StepChatResult
from elspeth.web.sessions.guided_payloads import prepare_guided_json_payload
from elspeth.web.sessions.protocol import (
    GuidedOriginatingUserMessageDraft,
    GuidedPendingProposalInvalidation,
    PreparedGuidedJsonPayload,
)


class Schema8GuidedRouteAuthority(Protocol):
    """Narrow deterministic turn-building surface used by intent rewind."""

    def _build_get_guided_turn(
        self,
        state: CompositionState,
        guided: GuidedSession,
        *,
        catalog: PolicyCatalogView,
    ) -> Turn | None: ...

    def _finalize_guided_turn(self, turn: Mapping[str, Any], *, shield_available: bool) -> Turn: ...

    def _prepare_server_turn_occurrence(
        self,
        guided: GuidedSession,
        *,
        current_step: GuidedStep,
        turn: Turn,
        payload_store: PayloadStore,
    ) -> tuple[GuidedSession, TurnRecord, TurnType, PreparedGuidedJsonPayload]: ...


@dataclass(frozen=True, slots=True)
class ManagementRewind:
    state: CompositionState
    response_payload: PreparedGuidedJsonPayload
    next_turn: Turn
    next_payload: PreparedGuidedJsonPayload
    invalidated_proposal: GuidedPendingProposalInvalidation | None


@dataclass(frozen=True, slots=True)
class ManagementRewindAuthority:
    """Frozen schema-8 checkpoint authority used to prepare one rewind."""

    guided_route: Schema8GuidedRouteAuthority
    current_state: CompositionState
    current_guided: GuidedSession
    prospective: GuidedSession
    catalog: PolicyCatalogView
    shield_available: bool
    payload_store: PayloadStore


@dataclass(frozen=True, slots=True, kw_only=True)
class DeferredRequestUnchanged:
    guided: GuidedSession
    chat: StepChatResult


@dataclass(frozen=True, slots=True, kw_only=True)
class DeferredRequestRetained:
    guided: GuidedSession
    chat: StepChatResult
    # One id per intent appended by this request, in append order
    # (elspeth-3a21f09f09: a message naming N future stages appends up to N).
    retained_intent_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if (
            type(self.retained_intent_ids) is not tuple
            or not self.retained_intent_ids
            or any(type(intent_id) is not UUID for intent_id in self.retained_intent_ids)
        ):
            raise TypeError("DeferredRequestRetained.retained_intent_ids must be a non-empty tuple of exact UUIDs")
        if len(set(self.retained_intent_ids)) != len(self.retained_intent_ids):
            raise AuditIntegrityError("retained deferred request repeats a stable intent id")
        present = {intent.intent_id for intent in self.guided.deferred_intents}
        if any(str(intent_id) not in present for intent_id in self.retained_intent_ids):
            raise AuditIntegrityError("retained deferred request lost an exact stable intent")


@dataclass(frozen=True, slots=True, kw_only=True)
class DeferredRequestCancelled:
    guided: GuidedSession
    chat: StepChatResult
    action: DeferredIntentCancelAction
    effective_intent: DeferredStageIntent
    deferred_intents: tuple[DeferredStageIntent, ...]
    invalidated_active_proposal: GuidedProposalRef | None

    def __post_init__(self) -> None:
        if type(self.action) is not DeferredIntentCancelAction:
            raise TypeError("DeferredRequestCancelled.action must be exact")
        if type(self.effective_intent) is not DeferredStageIntent or type(self.deferred_intents) is not tuple:
            raise TypeError("DeferredRequestCancelled intent fields must be exact")
        if self.effective_intent.intent_id != self.action.intent_id:
            raise AuditIntegrityError("cancelled request action and effective intent identities differ")
        if any(intent.intent_id == self.action.intent_id for intent in self.deferred_intents):
            raise AuditIntegrityError("cancelled request retained the cancelled stable intent")
        _validate_managed_request_checkpoint(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class DeferredRequestEdited:
    guided: GuidedSession
    chat: StepChatResult
    action: DeferredIntentEditAction
    effective_intent: DeferredStageIntent
    deferred_intents: tuple[DeferredStageIntent, ...]
    invalidated_active_proposal: GuidedProposalRef | None

    def __post_init__(self) -> None:
        if type(self.action) is not DeferredIntentEditAction:
            raise TypeError("DeferredRequestEdited.action must be exact")
        if type(self.effective_intent) is not DeferredStageIntent or type(self.deferred_intents) is not tuple:
            raise TypeError("DeferredRequestEdited intent fields must be exact")
        matches = tuple(intent for intent in self.deferred_intents if intent.intent_id == self.action.intent_id)
        if self.effective_intent.intent_id != self.action.intent_id or matches != (self.effective_intent,):
            raise AuditIntegrityError("edited request lost its one exact effective stable intent")
        _validate_managed_request_checkpoint(self)


type DeferredRequestApplication = DeferredRequestUnchanged | DeferredRequestRetained | DeferredRequestCancelled | DeferredRequestEdited
type DeferredRequestManaged = DeferredRequestCancelled | DeferredRequestEdited


def _validate_managed_request_checkpoint(request: DeferredRequestManaged) -> None:
    if request.invalidated_active_proposal is None:
        if request.guided.deferred_intents != request.deferred_intents:
            raise AuditIntegrityError("managed request checkpoint does not contain its exact deferred-intent mutation")
        return
    if request.guided.active_proposal != request.invalidated_active_proposal:
        raise AuditIntegrityError("managed request invalidation does not bind the active proposal")


@dataclass(frozen=True, slots=True)
class DeferredRequestAuthority:
    """Stable authority needed to retain or manage one deferred request.

    ``new_intent_ids`` is minted one-per-action by the route (at least one, so
    the clarification degrade path always has an id); actions whose
    disposition appends nothing simply leave their id unused.
    """

    guided: GuidedSession
    catalog: PolicyCatalogView
    originating_message: GuidedOriginatingUserMessageDraft
    new_intent_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if (
            type(self.new_intent_ids) is not tuple
            or not self.new_intent_ids
            or any(type(intent_id) is not UUID for intent_id in self.new_intent_ids)
            or len(set(self.new_intent_ids)) != len(self.new_intent_ids)
        ):
            raise TypeError("DeferredRequestAuthority.new_intent_ids must be a non-empty tuple of distinct exact UUIDs")


def _guided_stage_name(step: GuidedStep) -> StageName:
    if step is GuidedStep.STEP_1_SOURCE:
        return "source"
    if step is GuidedStep.STEP_2_SINK:
        return "output"
    if step is GuidedStep.STEP_3_TRANSFORMS:
        return "topology"
    if step is GuidedStep.STEP_4_WIRE:
        return "wire_review"
    raise AuditIntegrityError("Guided Chat step is outside the closed stage vocabulary")


def _contradiction_chat(rejection: DeferredIntentRejected, *, latency_ms: int, retained: bool) -> StepChatResult:
    """Render one distinct, actionable contradiction rejection (ADR-033).

    The message names the exact conflicting retained intent and its
    edit/cancel recourse (model: the planning blocker message), never the
    collapsed catch-all.  ``retained`` selects the wording for the R2-F15
    clarification-retention path versus a rejected edit of an existing
    intent, which leaves the original saved instruction in place.
    """

    contradiction = rejection.contradiction
    if contradiction is not None and contradiction.conflicting_intent_id is not None:
        conflict = (
            f"It contradicts saved instruction {contradiction.conflicting_intent_id} "
            f"({contradiction.conflicting_intent_summary}) under the closed rule {contradiction.rule!r}."
        )
        recourse = f"Edit or cancel exact intent {contradiction.conflicting_intent_id}, or restate this instruction so the two agree."
    else:
        rule = "constraint_contradiction" if contradiction is None else contradiction.rule
        conflict = f"Its structural constraints contradict each other under the closed rule {rule!r}."
        recourse = "Restate it as one consistent structural requirement."
    retention = (
        "I kept your instruction as a pending clarification instead of applying it. "
        if retained
        else "I did not change your saved instructions. "
    )
    return StepChatResult(
        assistant_message=f"{retention}{conflict} {recourse}",
        status=ComposerChatTurnStatus.SYNTHETIC_UNAVAILABLE,
        latency_ms=latency_ms,
        error_class="DeferredIntentContradiction",
    )


def _deferred_disposition_chat(
    disposition: DeferredIntentAccepted | DeferredIntentClarification | DeferredIntentUnsupported | DeferredIntentRejected,
    *,
    catalog: PolicyCatalogView,
    latency_ms: int,
) -> StepChatResult:
    if type(disposition) is DeferredIntentAccepted:
        message = f"I saved that instruction for the {disposition.action.target_stage.replace('_', ' ')} stage."
        status = ComposerChatTurnStatus.SUCCESS
        error_class = None
    elif type(disposition) is DeferredIntentClarification:
        kinds = ", ".join(disposition.plugin_kinds)
        message = f"I found {disposition.plugin_name!r} in more than one plugin category ({kinds}). Which category did you mean?"
        status = ComposerChatTurnStatus.SYNTHETIC_UNAVAILABLE
        error_class = "DeferredIntentClarification"
    elif type(disposition) is DeferredIntentUnsupported:
        if disposition.reason.value == "plugin_not_enabled":
            message = f"The {disposition.plugin_kind} plugin {disposition.plugin_name!r} is not enabled by the current policy."
        elif disposition.reason.value == "plugin_not_installed":
            message = f"The {disposition.plugin_kind} plugin {disposition.plugin_name!r} is not installed."
        else:
            message = f"The {disposition.plugin_kind} plugin {disposition.plugin_name!r} is currently unavailable."
        alternatives = _policy_visible_alternatives(
            catalog,
            plugin_kind=disposition.plugin_kind,
            excluded_name=disposition.plugin_name,
        )
        if alternatives:
            message = f"{message} Policy-visible {disposition.plugin_kind} alternatives: {', '.join(alternatives)}."
        else:
            message = f"{message} No policy-visible {disposition.plugin_kind} alternatives are available."
        status = ComposerChatTurnStatus.SYNTHETIC_UNAVAILABLE
        error_class = "DeferredIntentUnsupported"
    else:
        rejected = cast(DeferredIntentRejected, disposition)
        if rejected.reason == "constraint_contradiction":
            return _contradiction_chat(rejected, latency_ms=latency_ms, retained=False)
        message = "I couldn't safely retain that as a future-stage instruction. Please clarify the target stage and structural requirement."
        status = ComposerChatTurnStatus.SYNTHETIC_UNAVAILABLE
        error_class = "DeferredIntentRejected"
    return StepChatResult(
        assistant_message=message,
        status=status,
        latency_ms=latency_ms,
        error_class=error_class,
    )


_MAX_POLICY_VISIBLE_ALTERNATIVES = 5
_STRUCTURAL_NODE_TYPES = ("gate", "coalesce", "row_union", "queue")


def _message_names_identifier(message: str, identifier: str) -> bool:
    """Match one canonical catalog identifier without alias normalization."""

    return re.search(rf"(?<![a-z0-9_]){re.escape(identifier)}(?![a-z0-9_])", message.casefold()) is not None


def _has_unmentioned_unavailable_action_identity(
    action: DeferredIntentAction,
    *,
    catalog: PolicyCatalogView,
    originating_message_content: str,
) -> bool:
    """Recognize only the model-authored unavailable catalog identity seam."""

    if action.catalog_kind is None or action.catalog_name is None:
        return False
    if _message_names_identifier(originating_message_content, action.catalog_name):
        return False
    try:
        plugin_id = PluginId.parse(f"{action.catalog_kind}:{action.catalog_name}")
    except ValueError:
        return False
    return catalog.unavailable_reason(plugin_id) is not None


def _policy_visible_alternatives(
    catalog: PolicyCatalogView,
    *,
    plugin_kind: PluginKind,
    excluded_name: str,
) -> tuple[str, ...]:
    if plugin_kind == "source":
        plugins = catalog.list_sources()
    elif plugin_kind == "transform":
        plugins = catalog.list_transforms()
    else:
        plugins = catalog.list_sinks()
    names = sorted(plugin.name for plugin in plugins if plugin.name != excluded_name)
    return tuple(names[:_MAX_POLICY_VISIBLE_ALTERNATIVES])


def _retained_unverified_chat(
    disposition: DeferredIntentClarification | DeferredIntentRejected,
    *,
    latency_ms: int,
) -> StepChatResult:
    """Render one retained-but-unverified disposition (R2-F15).

    The instruction is kept as constraint-free clarification debt, so the copy
    must say it was kept and name the specific missing detail — never the
    collapsed "couldn't retain" catch-all that implies the instruction is gone.
    """

    if type(disposition) is DeferredIntentClarification:
        kinds = ", ".join(disposition.plugin_kinds)
        detail = f"I found {disposition.plugin_name!r} in more than one plugin category ({kinds}). Which category did you mean?"
        error_class = "DeferredIntentClarification"
    else:
        reason = cast(DeferredIntentRejected, disposition).reason
        if reason == "wrong_responsible_stage":
            detail = (
                "Its target stage does not match the stage its structural content belongs to. "
                "Tell me the stage that should own it and I'll firm it up."
            )
        elif reason in {"catalog_kind_mismatch", "malformed_catalog_identity"}:
            detail = (
                "The plugin it names does not match the catalog under that category. "
                "Name the exact plugin and its category and I'll firm it up."
            )
        else:
            detail = (
                "I couldn't verify its structural details against your message. "
                "Restate the concrete structural requirement and I'll firm it up."
            )
        error_class = "DeferredIntentRejected"
    return StepChatResult(
        assistant_message=f"I kept your instruction as a pending clarification instead of applying it. {detail}",
        status=ComposerChatTurnStatus.SYNTHETIC_UNAVAILABLE,
        latency_ms=latency_ms,
        error_class=error_class,
    )


def _model_catalog_identity_chat(*, user_message: str, latency_ms: int) -> StepChatResult:
    """Explain an unavailable model-authored catalog identity the user never named.

    The message is COMPOSED, not selected: a shared frame open, one teaching
    clause per node kind the message names, and a shared frame close (filigree
    elspeth-270e81443d, review comment 7977 §7.1). A message naming a collector
    AND a structural node gets BOTH clauses, and the frame is emitted once
    either way. Three reasons the collector teaching composes rather than
    competes:

    * Nothing true is lost. A collector arm that WON would have taken the gate
      clause away from "add a collector and a gate", trading one teaching line
      for another; appending buys the collector case at no price.
    * It deletes the ordering question. `_STRUCTURAL_NODE_TYPES` is scanned with
      `next()`, which returns the first member in TUPLE order that the message
      names — for "a queue and a gate" that is `gate`, because `gate` precedes
      `queue` in the tuple, NOT because of where the words appear in the
      message. That limitation survives — only ONE structural clause is ever
      emitted, so "a queue and a gate" teaches `gate` only — but it no longer
      decides whether the collector clause fires at all.
    * Every clause is a GENERAL TRUTH, true regardless of what happened in the
      turn, so no clause CAN be false and none needs a provenance hedge to make
      it safe. An unwanted match therefore costs at most an unhelpful sentence,
      never a wrong one. Two classes reach that state, and the second is the
      larger: NEGATION ("no collector needed" still gets its correct gate
      clause, plus a true but unsolicited collector definition) and HOMONYMS
      ("the garbage collector is slow" and "add a data collector for the survey"
      both emit the full EXPAND-scope paragraph, verified). Detecting negation
      is banned — `_message_names_identifier` is a word-boundary regex with no
      notion of it, and a negation parse would fail in the opposite direction on
      "no gate, add a collector". Homonym disambiguation is worse still: it
      needs to know what the user meant. Tolerating both is the price of never
      printing a falsehood, and it is the right trade only because every clause
      is unconditionally true.

    Note what the frame deliberately does NOT say. The only caller sits inside
    the `_has_unmentioned_unavailable_action_identity` guard, so an unavailable
    plugin the user never named is always what happened — that is what the frame
    open states, and its "for it" refers to the retained instruction, which IS
    the action the guard just checked. But nothing reaching this function says
    WHICH node that plugin was for, or what kind of plugin it is.
    `DeferredIntentAction` carries only target_stage / catalog_kind /
    catalog_name / redacted_summary / constraints; there is no node_type, and
    this function receives none of it. So "the plugin I proposed FOR THE
    COLLECTOR" is unverifiable BY CONSTRUCTION — for "add a collector and a
    scoring transform" the unavailable plugin may belong to the transform, and
    `catalog_kind` may be "sink" rather than "transform". No clause may
    attribute the plugin to a node, and none does.

    Clause order is structural, then collector, then aggregation. Not the order
    comment 7977 §7.1 lists, deliberately: it puts "not a transform plugin"
    directly beside "IS backed by a batch-transform plugin", and that contrast
    is the whole reason the collector clause exists (comment 7911). The two
    plugin-bearing kinds then sit together.

    AGGREGATION IS THE THIRD PLUGIN-HOSTING KIND, and it has a clause because
    AGENTS.md WS6 requires every `node_type` dispatch site to carry a collector
    arm or a deliberate documented exclusion — and under composition a second
    true clause costs nothing, so an exclusion would have been the more
    expensive of the two. Its field list is NOT the collector's and was
    verified separately against `state.py`'s aggregation arm, because the
    obvious guess is wrong in both directions: an aggregation's mandatory
    fields are `plugin` and `on_error` (`aggregation_missing_plugin`,
    `aggregation_missing_on_error`); `trigger` is OPTIONAL — runtime treats a
    missing or empty trigger as end-of-source-only — and `output_mode` is
    optional too, merely constrained to `OutputMode` when present. There is no
    `trigger_kinds` field at all. So an aggregation clause modelled on the
    collector's "it needs A, B and C" shape would print a falsehood; the
    asymmetry is real and the clause states it.

    `transform` is the fourth plugin-hosting kind and is deliberately EXCLUDED:
    it is the DEFAULT reading of an unavailable catalog identity, so a clause
    saying "a transform is backed by a transform plugin" teaches nothing the
    frame has not already implied.

    Collector is also deliberately absent from `_STRUCTURAL_NODE_TYPES` itself.
    That tuple is a copy-trigger keyword list whose sole membership rule is that
    "a {x} is a built-in topology node, not a transform plugin" is TRUE of x —
    exactly the plugin-free kinds. A collector is plugin-BEARING (barrier-scopes
    spec §3 types it as a transform plugin, and a collector with no `plugin` is
    rejected outright), so adding it there would print a falsehood.
    """
    clauses: list[str] = []
    structural_node = next(
        (node_type for node_type in _STRUCTURAL_NODE_TYPES if _message_names_identifier(user_message, node_type)),
        None,
    )
    if structural_node is not None:
        clauses.append(f"A {structural_node} is a built-in topology node, not a transform plugin. ")
    if _message_names_identifier(user_message, "collector"):
        clauses.append(
            "A collector is a built-in topology node that IS backed by a batch-transform plugin, and it closes "
            "an EXPAND scope — it needs scope_name, scope_opener and scope_policy. Name the batch behaviour you "
            "want and the scope it should close. "
        )
    if _message_names_identifier(user_message, "aggregation"):
        clauses.append(
            "An aggregation is a built-in topology node that IS backed by a batch-transform plugin, and it "
            "needs on_error; its trigger is optional and defaults to firing once at end of source. Name the "
            "batch behaviour you want. "
        )
    message = (
        "I kept that future-stage instruction, but its structure was not verified. "
        "The plugin I proposed for it is not available here, and you did not ask for it by name, "
        "so I did not treat it as a deployment problem. "
        f"{''.join(clauses)}"
        "Clarify the concrete topology structure and I'll firm it up."
    )
    return StepChatResult(
        assistant_message=message,
        status=ComposerChatTurnStatus.SYNTHETIC_UNAVAILABLE,
        latency_ms=latency_ms,
        error_class="DeferredIntentModelCatalogIdentity",
    )


def _apply_deferred_management(
    action: DeferredIntentManagementAction,
    *,
    guided: GuidedSession,
    catalog: PolicyCatalogView,
    originating_message: GuidedOriginatingUserMessageDraft,
    chat: StepChatResult,
) -> DeferredRequestApplication:
    management = resolve_deferred_intent_management(
        action,
        guided=guided,
        catalog=catalog,
        originating_message_id=str(originating_message.message_id),
        originating_message_content=originating_message.content,
    )
    if type(management) in {
        DeferredIntentManagementUnknown,
        DeferredIntentManagementBindingMismatch,
        DeferredIntentManagementAmbiguous,
    }:
        if type(management) is DeferredIntentManagementUnknown:
            assistant_message = "I couldn't find one current saved instruction with that stable identity, so I didn't change anything."
            error_class = "DeferredIntentUnknown"
        elif type(management) is DeferredIntentManagementBindingMismatch:
            assistant_message = "That saved-instruction selection did not match its server binding, so I didn't change anything."
            error_class = "DeferredIntentBindingMismatch"
        else:
            assistant_message = (
                "Use an exact command before I change a saved instruction: "
                "'Cancel exact intent <UUID>.' or 'Edit exact intent <UUID>: <new instruction>'."
            )
            error_class = "DeferredIntentAmbiguous"
        unavailable = StepChatResult(
            assistant_message=assistant_message,
            status=ComposerChatTurnStatus.SYNTHETIC_UNAVAILABLE,
            latency_ms=chat.latency_ms,
            error_class=error_class,
        )
        return DeferredRequestUnchanged(guided=guided, chat=unavailable)
    if type(management) is DeferredIntentManagementApplied:
        invalidated = guided.active_proposal
        prospective = guided if invalidated is not None else replace(guided, deferred_intents=management.deferred_intents)
        applied_chat = StepChatResult(
            assistant_message=management.assistant_message,
            status=ComposerChatTurnStatus.SUCCESS,
            latency_ms=chat.latency_ms,
            error_class=None,
        )
        if type(action) is DeferredIntentCancelAction:
            return DeferredRequestCancelled(
                guided=prospective,
                chat=applied_chat,
                action=action,
                effective_intent=management.effective_intent,
                deferred_intents=management.deferred_intents,
                invalidated_active_proposal=invalidated,
            )
        if type(action) is not DeferredIntentEditAction:  # pragma: no cover - closed action union
            raise TypeError("deferred management action is outside the closed union")
        return DeferredRequestEdited(
            guided=prospective,
            chat=applied_chat,
            action=action,
            effective_intent=management.effective_intent,
            deferred_intents=management.deferred_intents,
            invalidated_active_proposal=invalidated,
        )
    rejected_chat = _deferred_disposition_chat(
        cast(
            DeferredIntentAccepted | DeferredIntentClarification | DeferredIntentUnsupported | DeferredIntentRejected,
            management,
        ),
        catalog=catalog,
        latency_ms=chat.latency_ms,
    )
    return DeferredRequestUnchanged(guided=guided, chat=rejected_chat)


def _append_clarification_intent(
    guided: GuidedSession,
    *,
    intent_id: UUID,
    originating_message: GuidedOriginatingUserMessageDraft,
) -> GuidedSession:
    retained = create_deferred_clarification_intent(
        receiving_stage=_guided_stage_name(guided.step),
        intent_id=str(intent_id),
        originating_message_id=str(originating_message.message_id),
        originating_message_content=originating_message.content,
    )
    return replace(guided, deferred_intents=(*guided.deferred_intents, retained))


def _apply_one_deferred_action(
    action: DeferredIntentAction,
    *,
    guided: GuidedSession,
    catalog: PolicyCatalogView,
    originating_message: GuidedOriginatingUserMessageDraft,
    intent_id: UUID,
    latency_ms: int,
) -> tuple[GuidedSession, StepChatResult, UUID | None]:
    """Apply ONE action's disposition against the evolving guided state.

    Returns the (possibly appended-to) guided state, the disposition chat, and
    the appended intent id (``None`` when the disposition appends nothing).
    Each action keeps the exact per-action disposition machinery the singular
    path had; the fold in :func:`apply_deferred_request` composes them
    (elspeth-3a21f09f09).
    """
    structural_rejection = validate_deferred_intent_structure(
        action,
        receiving_stage=_guided_stage_name(guided.step),
    )
    if structural_rejection is not None:
        if structural_rejection.reason == "wrong_responsible_stage":
            # The action claims a later target, so it IS a future-stage
            # instruction whose encoding the server cannot verify — the
            # R2-F15 retention net keeps it as clarification debt. The
            # structural diagnostic still wins over the catalog-identity
            # copy (precedence).
            return (
                _append_clarification_intent(guided, intent_id=intent_id, originating_message=originating_message),
                _retained_unverified_chat(structural_rejection, latency_ms=latency_ms),
                intent_id,
            )
        # target_not_later: by its own claim this is not a future-stage
        # instruction — the current-stage flow owns the content, so
        # retaining it would mint spurious wire-blocking debt.
        return (
            guided,
            _deferred_disposition_chat(structural_rejection, catalog=catalog, latency_ms=latency_ms),
            None,
        )
    if _has_unmentioned_unavailable_action_identity(
        action,
        catalog=catalog,
        originating_message_content=originating_message.content,
    ):
        return (
            _append_clarification_intent(guided, intent_id=intent_id, originating_message=originating_message),
            _model_catalog_identity_chat(user_message=originating_message.content, latency_ms=latency_ms),
            intent_id,
        )
    disposition = validate_deferred_intent_action(
        action,
        receiving_stage=_guided_stage_name(guided.step),
        catalog=catalog,
        guided=guided,
        originating_message_content=originating_message.content,
    )
    if type(disposition) is DeferredIntentRejected and disposition.reason == "constraint_contradiction":
        # ADR-033 rejection path: a contradiction rejection routes through
        # the R2-F15 retention net — the instruction is kept as
        # clarification debt, never silently dropped — and the chat names
        # the exact conflicting retained intent with edit/cancel recourse.
        return (
            _append_clarification_intent(guided, intent_id=intent_id, originating_message=originating_message),
            _contradiction_chat(disposition, latency_ms=latency_ms, retained=True),
            intent_id,
        )
    if type(disposition) in {DeferredIntentClarification, DeferredIntentRejected}:
        # Every remaining unverified disposition retains the instruction as
        # clarification debt (R2-F15): the constraint-free intent carries a
        # content hash and closed summary only, so no unproven fact, option
        # literal, or mis-kinded identity persists.
        return (
            _append_clarification_intent(guided, intent_id=intent_id, originating_message=originating_message),
            _retained_unverified_chat(
                cast(DeferredIntentClarification | DeferredIntentRejected, disposition),
                latency_ms=latency_ms,
            ),
            intent_id,
        )
    resolved_chat = _deferred_disposition_chat(disposition, catalog=catalog, latency_ms=latency_ms)
    if type(disposition) is not DeferredIntentAccepted:
        # DeferredIntentUnsupported: an unavailable plugin remains a
        # distinct catalog/availability error, never clarification debt
        # (the user manual's explicit carve-out).
        return guided, resolved_chat, None
    retained = create_deferred_stage_intent(
        disposition.action,
        receiving_stage=_guided_stage_name(guided.step),
        intent_id=str(intent_id),
        originating_message_id=str(originating_message.message_id),
        originating_message_content=originating_message.content,
        guided=guided,
    )
    return (
        replace(guided, deferred_intents=(*guided.deferred_intents, retained)),
        resolved_chat,
        intent_id,
    )


def _compose_disposition_chats(chats: tuple[StepChatResult, ...]) -> StepChatResult:
    """Compose N per-action disposition chats into the turn's one chat.

    Messages join in action order; the FIRST non-success disposition supplies
    the composed status and error_class so the transcript and audit keep the
    not-applied signal (the F1 honesty contract) even when a sibling action
    succeeded in the same Send.
    """
    if len(chats) == 1:
        return chats[0]
    failed = next((candidate for candidate in chats if candidate.status is not ComposerChatTurnStatus.SUCCESS), None)
    return StepChatResult(
        assistant_message=" ".join(candidate.assistant_message for candidate in chats),
        status=chats[0].status if failed is None else failed.status,
        latency_ms=chats[0].latency_ms,
        error_class=None if failed is None else failed.error_class,
    )


def apply_deferred_request(
    deferred_actions: tuple[DeferredIntentAction, ...],
    management_action: DeferredIntentManagementAction | None,
    *,
    authority: DeferredRequestAuthority,
    chat: StepChatResult,
) -> DeferredRequestApplication:
    if deferred_actions:
        if len(authority.new_intent_ids) < len(deferred_actions):
            raise AuditIntegrityError("deferred request authority minted fewer intent ids than actions")
        guided = authority.guided
        disposition_chats: list[StepChatResult] = []
        retained_intent_ids: list[UUID] = []
        for action, intent_id in zip(deferred_actions, authority.new_intent_ids, strict=False):
            guided, action_chat, appended_id = _apply_one_deferred_action(
                action,
                guided=guided,
                catalog=authority.catalog,
                originating_message=authority.originating_message,
                intent_id=intent_id,
                latency_ms=chat.latency_ms,
            )
            disposition_chats.append(action_chat)
            if appended_id is not None:
                retained_intent_ids.append(appended_id)
        composed_chat = _compose_disposition_chats(tuple(disposition_chats))
        if retained_intent_ids:
            return DeferredRequestRetained(
                guided=guided,
                chat=composed_chat,
                retained_intent_ids=tuple(retained_intent_ids),
            )
        return DeferredRequestUnchanged(guided=guided, chat=composed_chat)
    if management_action is not None:
        return _apply_deferred_management(
            management_action,
            guided=authority.guided,
            catalog=authority.catalog,
            originating_message=authority.originating_message,
            chat=chat,
        )
    return DeferredRequestUnchanged(guided=authority.guided, chat=chat)


def apply_deferred_clarification(
    *,
    authority: DeferredRequestAuthority,
    chat: StepChatResult,
) -> DeferredRequestRetained:
    """Append the constraint-free clarification intent for one failed Send.

    Last-resort retention (R2-F15): the Send carried future-stage
    instructions whose encoding could not be verified — the model failed to
    express them as well-formed actions even after its bounded repair turn, or
    the action it produced was rejected by settlement validation. The whole
    message is kept as ONE clarification intent bound to the private
    originating message; the settlement command carries
    ``retained_deferred_intent_ids`` exactly like an ordinary retain, so
    ``_verify_guided_deferred_intent_append`` verifies the append and message
    binding unchanged.
    """
    intent_id = authority.new_intent_ids[0]
    prospective = _append_clarification_intent(
        authority.guided,
        intent_id=intent_id,
        originating_message=authority.originating_message,
    )
    return DeferredRequestRetained(
        guided=prospective,
        chat=chat,
        retained_intent_ids=(intent_id,),
    )


def deferred_request_retained_intent_ids(application: DeferredRequestApplication) -> tuple[UUID, ...]:
    if type(application) is DeferredRequestRetained:
        return application.retained_intent_ids
    return ()


def deferred_request_management(application: DeferredRequestApplication) -> DeferredRequestManaged | None:
    if type(application) is DeferredRequestCancelled:
        return application
    if type(application) is DeferredRequestEdited:
        return application
    return None


def _prepare_schema8_management_rewind(
    *,
    authority: ManagementRewindAuthority,
    managed_intent: DeferredStageIntent,
    managed_deferred_intents: tuple[DeferredStageIntent, ...],
    action: DeferredIntentManagementAction,
    invalidated_active_proposal: GuidedProposalRef | None,
) -> ManagementRewind:
    """Build the exact schema-8 rewind candidate without invoking the planner."""

    rewind_step = GuidedStep.STEP_2_SINK
    if not authority.prospective.history or authority.prospective.history[-1].response_hash is not None:
        raise AuditIntegrityError("schema-8 deferred management has no exact current turn to invalidate")
    response_payload = prepare_guided_json_payload(
        authority.payload_store,
        purpose="turn_response",
        payload={
            "action": "manage_deferred_intent",
            "management": "cancel" if type(action) is DeferredIntentCancelAction else "edit",
            "intent_id": managed_intent.intent_id,
            "target_stage": managed_intent.target_stage,
        },
    )
    answered = replace(
        authority.prospective.history[-1],
        response_hash=response_payload.payload_id,
        summary="Saved instruction changed; downstream guided review invalidated.",
    )
    invalidated_proposal = None
    if invalidated_active_proposal is not None:
        # A real supersession: the deferred-intent rewind displaces the
        # pending proposal with a re-planned successor.
        invalidated_proposal = GuidedPendingProposalInvalidation(
            proposal_id=invalidated_active_proposal.proposal_id,
            draft_hash=invalidated_active_proposal.draft_hash,
            reviewed_facts=guided_private_reviewed_facts(authority.current_guided),
            reason="superseded",
        )
    rewound_guided = replace(
        authority.prospective,
        step=rewind_step,
        history=(*authority.prospective.history[:-1], answered),
        deferred_intents=managed_deferred_intents,
        active_proposal=None,
        active_edit_target=None,
    )
    rewound_state = replace(authority.current_state, guided_session=rewound_guided)
    review_turn = authority.guided_route._build_get_guided_turn(rewound_state, rewound_guided, catalog=authority.catalog)
    if review_turn is None:
        raise AuditIntegrityError("schema-8 deferred management rewind did not produce output review")
    review_turn = authority.guided_route._finalize_guided_turn(
        review_turn,
        shield_available=authority.shield_available,
    )
    rewound_guided, _record, _turn_type, next_payload = authority.guided_route._prepare_server_turn_occurrence(
        rewound_guided,
        current_step=rewind_step,
        turn=review_turn,
        payload_store=authority.payload_store,
    )
    return ManagementRewind(
        state=replace(authority.current_state, guided_session=rewound_guided),
        response_payload=response_payload,
        next_turn=review_turn,
        next_payload=next_payload,
        invalidated_proposal=invalidated_proposal,
    )


def maybe_prepare_schema8_management_rewind(
    *,
    authority: ManagementRewindAuthority,
    management: DeferredRequestManaged | None,
) -> ManagementRewind | None:
    if management is None:
        return None
    rewind_step = schema8_deferred_management_rewind_step(
        current_step=authority.prospective.step,
        target_stage=management.effective_intent.target_stage,
    )
    if rewind_step is None and management.invalidated_active_proposal is None:
        return None
    return _prepare_schema8_management_rewind(
        authority=authority,
        managed_intent=management.effective_intent,
        managed_deferred_intents=management.deferred_intents,
        action=management.action,
        invalidated_active_proposal=management.invalidated_active_proposal,
    )


__all__ = [
    "DeferredRequestApplication",
    "DeferredRequestAuthority",
    "DeferredRequestCancelled",
    "DeferredRequestEdited",
    "DeferredRequestRetained",
    "DeferredRequestUnchanged",
    "ManagementRewindAuthority",
    "apply_deferred_clarification",
    "apply_deferred_request",
    "deferred_request_management",
    "deferred_request_retained_intent_ids",
    "maybe_prepare_schema8_management_rewind",
]
