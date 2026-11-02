"""Original warm END caller locals; neither a codec nor paid-call authority."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypedDict
from weakref import WeakKeyDictionary

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import freeze_fields
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.prompt_guard import GuardedProviderMessage
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
class _OriginalEndGateLocals:
    phase_owner: CheckpointPhaseOwner
    state: CompositionState
    evidence: TurnEvidence
    surfacing: InterpretationSurfacing
    session_id: str | None
    request: str
    publication_binding: str
    dialogue: Sequence[GuardedProviderMessage] | None
    dialogue_sha256: str | None


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
    publication = evidence.review_candidate(candidate=value.assistant_message.content or "", request=request, current_state=state)
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
        dialogue,
        None if dialogue is None else stable_hash(dialogue),
    )
    return value


def require_original_checkpoint_end_gate_locals(value: CheckpointEndGateLocals, *, phase_owner: CheckpointPhaseOwner) -> None:
    """Reprove original warm production; a decoded/copied value is not issued."""
    source = _issued.get(value) if type(value) is CheckpointEndGateLocals else None
    if source is None or source.phase_owner is not phase_owner:
        raise AuditIntegrityError("END gate input requires its original caller-local capture")
    if stable_hash(source.state.to_dict()) != value.orphan_observation.state_sha256:
        raise AuditIntegrityError("END gate state changed after local capture")
    if source.dialogue is not None and stable_hash(source.dialogue) != source.dialogue_sha256:
        raise AuditIntegrityError("END gate dialogue changed after local capture")
    require_original_pending_review_observation(
        value.orphan_observation, owner=source.surfacing, state=source.state, session_id=source.session_id
    )
    publication = source.evidence.review_candidate(
        candidate=value.assistant_message.content or "", request=source.request, current_state=source.state
    )
    if publication != value.publication_review or publication.binding != source.publication_binding:
        raise AuditIntegrityError("END gate publication changed after local capture")
