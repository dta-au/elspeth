"""Original warm END caller locals; neither a codec nor paid-call authority."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypedDict
from weakref import WeakKeyDictionary

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import freeze_fields
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.contracts.prompt_guard import (
    GuardedProviderMessage,
    PromptGuardContentPart,
    PromptGuardThinkingParts,
    PromptGuardToolArgumentParts,
    detach_prompt_guard_evidence,
)
from elspeth.contracts.readonly_json import JsonValue
from elspeth.web.composer._compose_loop_carriers import _AdmittedAssistantMessage, _AdvisorReviewState
from elspeth.web.composer.continuation_classifier_handoff import ClassifyPostPrimary
from elspeth.web.composer.interpretation_surfacing import (
    InterpretationSurfacing,
    PendingInterpretationReviewObservation,
    require_original_pending_review_observation,
)
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.turn_effects import AdvisorPublicationReview, TurnEvidence
from elspeth.web.execution.completion_gates import CompletionGateFacts
from elspeth.web.execution.schemas import ValidationResult

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_checkpoint_end_format_retry import CheckpointEndRetryPublicationSource
    from elspeth.web.composer.continuation_checkpoint_phase import CheckpointPhaseOwner


class EndProofDiagnostic(TypedDict):
    """Original proof producer fields, including informational diagnostics."""

    code: str
    severity: Literal["blocking", "info"]
    message: str
    suggested_repair: str | None
    evidence_locator: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class OrdinaryEndCallerLocals:
    proof_runtime_preflight: ValidationResult | None
    proof_repair_action: Literal["clear", "blocked"] | None
    proof_repair_diagnostics: tuple[EndProofDiagnostic, ...] | None
    confirmed_stop: bool

    def __post_init__(self) -> None:
        freeze_fields(self, "proof_repair_diagnostics")
        if (self.proof_repair_action is None) != (self.proof_repair_diagnostics is None):
            raise AuditIntegrityError("ordinary END caller lost its proof repair DATA")


@dataclass(frozen=True, slots=True)
class SourceProofEndCallerLocals:
    proof_kind: Literal["unavailable", "malformed_claim"]
    diagnostics: tuple[EndProofDiagnostic, ...] | None
    source_name: str | None
    reason: str | None
    composed_stop_validation: ValidationResult

    def __post_init__(self) -> None:
        freeze_fields(self, "diagnostics")


@dataclass(frozen=True, slots=True)
class IterationEndCallerLocals:
    pending: ClassifyPostPrimary


type EndCallerLocals = OrdinaryEndCallerLocals | SourceProofEndCallerLocals | IterationEndCallerLocals


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class CheckpointEndGateLocals:
    assistant_message: _AdmittedAssistantMessage
    runtime_preflight: ValidationResult | None
    review_state: _AdvisorReviewState
    initial_review_state: _AdvisorReviewState
    publication_review: AdvisorPublicationReview
    completion_gates: CompletionGateFacts | None
    advisor_checkpoint_passes_used: int
    passes_delta: int
    pass_index: int
    max_passes: int
    allow_repair_continue: bool
    orphan_observation: PendingInterpretationReviewObservation
    caller: EndCallerLocals
    current_state_id: str | None
    initial_version: int
    repair_turns_used: int
    persisted_assistant_message_id: str | None
    persisted_assistant_content: str | None
    persisted_tool_call_turn: bool


@dataclass(frozen=True, slots=True)
class _EndDialogueProof:
    """Detached wire bytes and deeply immutable original producer provenance."""

    messages_json: str
    trusted_message_indexes: frozenset[int]
    content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...]
    tool_argument_parts: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...]
    thinking_parts: tuple[PromptGuardThinkingParts | None, ...]

    def __post_init__(self) -> None:
        freeze_fields(self, "trusted_message_indexes", "content_parts", "tool_argument_parts", "thinking_parts")

    @classmethod
    def capture(cls, messages: Sequence[GuardedProviderMessage]) -> _EndDialogueProof:
        detached = detach_prompt_guard_evidence(messages)
        return cls(
            canonical_json(detached.messages),
            detached.trusted_message_indexes,
            detached.content_parts,
            detached.tool_argument_parts,
            detached.thinking_parts,
        )


class _BorrowedEndDialogue:
    """Warm live alias with a detached proof; deliberately not an immutable value.

    Copying the live list would prevent detection of later caller mutation.
    Only the separate proof is frozen, and it never shares mutable wire data.
    """

    __slots__ = ("_messages", "_proof")

    def __init__(self, messages: Sequence[GuardedProviderMessage]) -> None:
        self._messages = messages
        self._proof = _EndDialogueProof.capture(messages)

    def require_unchanged(self) -> None:
        if _EndDialogueProof.capture(self._messages) != self._proof:
            raise AuditIntegrityError("END gate dialogue or its owned provenance changed after local capture")


@dataclass(frozen=True, slots=True)
class _OriginalEndGateLocals:
    phase_owner: CheckpointPhaseOwner
    state: CompositionState
    evidence: TurnEvidence
    surfacing: InterpretationSurfacing
    session_id: str | None
    request: str
    publication_binding: str
    dialogue: _BorrowedEndDialogue | None
    format_retry_publication_source: CheckpointEndRetryPublicationSource | None


_issued: WeakKeyDictionary[CheckpointEndGateLocals, _OriginalEndGateLocals] = WeakKeyDictionary()


def capture_checkpoint_end_gate_locals(
    value: CheckpointEndGateLocals,
    *,
    phase_owner: CheckpointPhaseOwner,
    state: CompositionState,
    evidence: TurnEvidence,
    surfacing: InterpretationSurfacing,
    session_id: str | None,
    request: str,
    dialogue: Sequence[GuardedProviderMessage] | None,
    format_retry_publication_source: CheckpointEndRetryPublicationSource | None = None,
) -> CheckpointEndGateLocals:
    """Issue only at the original gate; check actual runtime values before SDK."""
    from elspeth.web.composer.continuation_checkpoint_phase import CheckpointPhaseOwner

    if type(value) is not CheckpointEndGateLocals or type(phase_owner) is not CheckpointPhaseOwner:
        raise TypeError("END locals require their exact original phase owner")
    live, bindings = phase_owner.runtime.live, phase_owner.runtime.bindings
    if (
        phase_owner.phase_source.phase != "end"
        or state is not live.state
        or evidence is not live.turn_evidence
        or session_id != bindings.session_id
        or request != bindings.message
        or value.current_state_id != live.current_state_id
        or value.initial_version != live.initial_version
        or value.repair_turns_used != live.repair_turns_used
        or value.persisted_assistant_message_id != live.persisted_assistant_message_id
        or value.persisted_assistant_content != live.persisted_assistant_content
        or value.persisted_tool_call_turn != live.persisted_tool_call_turn
        or value.advisor_checkpoint_passes_used != live.advisor_checkpoint_passes_used
        or value.pass_index != value.advisor_checkpoint_passes_used + value.passes_delta + 1
        or not 1 <= value.pass_index <= value.max_passes
        or type(value.caller) not in (OrdinaryEndCallerLocals, SourceProofEndCallerLocals, IterationEndCallerLocals)
    ):
        raise AuditIntegrityError("END gate locals differ from their actual pre-call runtime")
    require_original_pending_review_observation(value.orphan_observation, owner=surfacing, state=state, session_id=session_id)
    if type(value.caller) is OrdinaryEndCallerLocals:
        if (
            dialogue is not live.llm_messages
            or value.caller.confirmed_stop != evidence.confirmed_abandonment(current_state=state, plugin_snapshot=bindings.plugin_snapshot)
            or value.allow_repair_continue != (value.caller.proof_runtime_preflight is None and not value.caller.confirmed_stop)
            or (value.caller.proof_runtime_preflight is not None and value.caller.proof_runtime_preflight != value.runtime_preflight)
        ):
            raise AuditIntegrityError("ordinary END input changed its original caller continuation")
    elif type(value.caller) is SourceProofEndCallerLocals:
        if dialogue != [] or value.allow_repair_continue or value.runtime_preflight != value.caller.composed_stop_validation:
            raise AuditIntegrityError("source-proof END input changed its terminal-only dialogue or verdict")
    elif dialogue is not None or value.allow_repair_continue or phase_owner.phase_source.kind != "iteration_end":
        raise AuditIntegrityError("iteration END input changed its terminal-only caller")
    publication = _current_end_publication(
        evidence=evidence,
        candidate=value.assistant_message.content or "",
        request=request,
        state=state,
        phase_owner=phase_owner,
        format_retry_publication_source=format_retry_publication_source,
    )
    if publication != value.publication_review:
        raise AuditIntegrityError("END gate locals changed the original publication review")
    _issued[value] = _OriginalEndGateLocals(
        phase_owner,
        state,
        evidence,
        surfacing,
        session_id,
        request,
        publication.binding,
        None if dialogue is None else _BorrowedEndDialogue(dialogue),
        format_retry_publication_source,
    )
    return value


def require_original_checkpoint_end_gate_locals(value: CheckpointEndGateLocals, *, phase_owner: CheckpointPhaseOwner) -> None:
    """Reprove original warm production; a decoded/copied value is not issued."""
    source = _issued.get(value) if type(value) is CheckpointEndGateLocals else None
    if source is None or source.phase_owner is not phase_owner:
        raise AuditIntegrityError("END gate input requires its original caller-local capture")
    if stable_hash(source.state.to_dict()) != value.orphan_observation.state_sha256:
        raise AuditIntegrityError("END gate state changed after local capture")
    if source.dialogue is not None:
        source.dialogue.require_unchanged()
    require_original_pending_review_observation(
        value.orphan_observation, owner=source.surfacing, state=source.state, session_id=source.session_id
    )
    publication = _current_end_publication(
        evidence=source.evidence,
        candidate=value.assistant_message.content or "",
        request=source.request,
        state=source.state,
        phase_owner=phase_owner,
        format_retry_publication_source=source.format_retry_publication_source,
    )
    if publication != value.publication_review or publication.binding != source.publication_binding:
        raise AuditIntegrityError("END gate publication changed after local capture")


def original_end_retry_publication_operation_id(value: CheckpointEndGateLocals, *, phase_owner: CheckpointPhaseOwner) -> str:
    """Return first-H scope only from an issued retry gate and its actual owner."""
    require_original_checkpoint_end_gate_locals(value, phase_owner=phase_owner)
    source = _issued[value]
    token = source.format_retry_publication_source
    if token is None:
        raise AuditIntegrityError("END retry publication lacks its issued original H scope")
    from elspeth.web.composer.continuation_checkpoint_end_format_retry import require_original_checkpoint_end_retry_publication

    return require_original_checkpoint_end_retry_publication(token, phase_owner=phase_owner)


def _current_end_publication(
    *,
    evidence: TurnEvidence,
    candidate: str,
    request: str,
    state: CompositionState,
    phase_owner: CheckpointPhaseOwner,
    format_retry_publication_source: CheckpointEndRetryPublicationSource | None,
) -> AdvisorPublicationReview:
    if format_retry_publication_source is None:
        return evidence.review_candidate(candidate=candidate, request=request, current_state=state)
    from elspeth.web.composer.continuation_checkpoint_end_format_retry import require_original_checkpoint_end_retry_publication

    original_operation_id = require_original_checkpoint_end_retry_publication(format_retry_publication_source, phase_owner=phase_owner)
    return evidence.review_candidate_for_source_operation(
        source_operation_id=original_operation_id,
        candidate=candidate,
        request=request,
        current_state=state,
    )
