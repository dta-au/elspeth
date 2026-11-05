"""Closed END gate DATA; decoding never grants provider or mutation authority."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Annotated, Literal
from weakref import ReferenceType, WeakKeyDictionary, ref

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from elspeth.contracts.composer_interpretation import InterpretationEventRecord, InterpretationKind
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer._compose_loop_carriers import _AdmittedAssistantMessage, _AdvisorReviewState
from elspeth.web.composer.continuation import ComposeFrame, ContinuationValue, PrivateChunkRef
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_response,
    encode_private_chunk,
    owned_json,
    verify_private_chunk,
)
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4
from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime, RestoredContinuationRuntime
from elspeth.web.composer.continuation_tool_batch import _decode_owned, _dump_owned
from elspeth.web.composer.no_tool_response import parse_no_tool_response
from elspeth.web.composer.turn_effects import AdvisorPublicationReview
from elspeth.web.credential_guard import require_no_credential_material
from elspeth.web.execution.completion_gates import CompletionGateFacts, completion_gate_fingerprint
from elspeth.web.execution.schemas import ValidationResult

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_checkpoint_end_locals import CheckpointEndGateLocals
    from elspeth.web.composer.continuation_checkpoint_phase import CheckpointPhaseOwner
    from elspeth.web.composer.interpretation_surfacing import PendingInterpretationReviewObservation
    from elspeth.web.sessions.continuation_checkpoint_paid import CheckpointPhaseSource

_MESSAGE = TypeAdapter(_AdmittedAssistantMessage)
_REVIEW = TypeAdapter(_AdvisorReviewState)
_PUBLICATION = TypeAdapter(AdvisorPublicationReview)
_GATES = TypeAdapter(CompletionGateFacts)
_VALIDATION = TypeAdapter(ValidationResult)
_ORPHANS = TypeAdapter(tuple[InterpretationEventRecord, ...])


class CheckpointOrdinaryEndCaller(ContinuationValue):
    kind: Literal["ordinary_no_tools"] = "ordinary_no_tools"
    proof_runtime_preflight_json: str | None
    proof_repair_action: Literal["clear", "blocked"] | None
    proof_repair_diagnostics_json: str | None
    confirmed_stop: bool
    confirmed_stop_evidence_sha256: str | None


class CheckpointSourceProofEndCaller(ContinuationValue):
    kind: Literal["source_proof_stop"] = "source_proof_stop"
    proof_kind: Literal["unavailable", "malformed_claim"]
    diagnostics_json: str | None
    source_name: str | None
    reason: str | None
    composed_stop_validation_json: str
    dialogue_kind: Literal["terminal_only_empty"] = "terminal_only_empty"


class CheckpointIterationEndCaller(ContinuationValue):
    kind: Literal["iteration_end"] = "iteration_end"
    pending_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    primary_effect_key: str = Field(min_length=1)
    pending_json: str


type CheckpointEndCaller = Annotated[
    CheckpointOrdinaryEndCaller | CheckpointSourceProofEndCaller | CheckpointIterationEndCaller,
    Field(discriminator="kind"),
]


class CheckpointEndInputSnapshot(ContinuationValue):
    schema_version: Literal["composer.checkpoint-end-input.v1"] = "composer.checkpoint-end-input.v1"
    session_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    outer_effect_key: str = Field(min_length=1)
    original_checkpoint_id: str = Field(min_length=1)
    original_cursor: int = Field(ge=0)
    captured_runtime_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_chunks: tuple[PrivateChunkRef, ...]
    state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    graph_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    assistant_message_json: str
    runtime_preflight_json: str | None
    review_state_json: str
    initial_review_state_json: str
    publication_review_json: str
    completion_gates_json: str | None
    advisor_checkpoint_passes_used: int = Field(ge=0)
    passes_delta: int = Field(ge=0)
    pass_index: int = Field(ge=1)
    max_passes: int = Field(ge=1)
    retry_ordinal: Literal[0, 1]
    allow_repair_continue: bool
    orphaned_precheck: tuple[tuple[str, str, InterpretationKind], ...]
    orphan_events_json: str
    orphan_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    caller: CheckpointEndCaller
    superseded_reply_index: int | None = Field(ge=0)
    superseded_reply_sha256: str | None
    elide_advisor_exchange: bool

    @model_validator(mode="after")
    def require_closed_pass_and_caller(self) -> CheckpointEndInputSnapshot:
        if self.pass_index != self.advisor_checkpoint_passes_used + self.passes_delta + 1 or self.pass_index > self.max_passes:
            raise ValueError("END input reset or exhausted its original pass budget")
        if (self.superseded_reply_index is None) != (self.superseded_reply_sha256 is None):
            raise ValueError("END input lost its exact superseded-message identity")
        if type(self.caller) is CheckpointSourceProofEndCaller:
            if self.allow_repair_continue:
                raise ValueError("source-proof END input cannot reopen authoring")
            unavailable = self.caller.proof_kind == "unavailable"
            if unavailable != (self.caller.diagnostics_json is not None):
                raise ValueError("source-proof END input changed its original proof DATA")
            if unavailable == (self.caller.source_name is not None or self.caller.reason is not None):
                raise ValueError("source-proof END input mixed unavailable and malformed claims")
            if not unavailable and (not self.caller.source_name or not self.caller.reason):
                raise ValueError("source-proof END input lost its complete malformed-claim DATA")
        if type(self.caller) is CheckpointOrdinaryEndCaller:
            if (self.caller.proof_repair_action is None) != (self.caller.proof_repair_diagnostics_json is None):
                raise ValueError("ordinary END input lost its proof-repair decision")
            if self.caller.confirmed_stop != (self.caller.confirmed_stop_evidence_sha256 is not None):
                raise ValueError("ordinary END input lost its confirmed-stop evidence")
        return self


def _validate_embedded(snapshot: CheckpointEndInputSnapshot, env_ref_names: frozenset[str]) -> None:
    _decode_owned(_MESSAGE, snapshot.assistant_message_json)
    _decode_owned(_REVIEW, snapshot.review_state_json)
    _decode_owned(_REVIEW, snapshot.initial_review_state_json)
    publication = _decode_owned(_PUBLICATION, snapshot.publication_review_json)
    events = _decode_owned(_ORPHANS, snapshot.orphan_events_json)
    if snapshot.orphan_source_sha256 != stable_hash(
        {
            "session_id": snapshot.session_id,
            "state_sha256": snapshot.state_sha256,
            "events": _ORPHANS.dump_python(events, mode="json"),
        }
    ):
        raise AuditIntegrityError("END input lost its complete original orphan cohort")
    if snapshot.completion_gates_json is not None:
        _decode_owned(_GATES, snapshot.completion_gates_json)
    validations = [snapshot.runtime_preflight_json]
    embedded = [publication.payload_json, publication.provider_payload_json, snapshot.orphan_events_json]
    if publication.transcript is not None:
        embedded.append(publication.transcript.provider_json)
    if type(snapshot.caller) is CheckpointOrdinaryEndCaller:
        validations.append(snapshot.caller.proof_runtime_preflight_json)
        if snapshot.caller.proof_repair_diagnostics_json is not None:
            embedded.append(snapshot.caller.proof_repair_diagnostics_json)
    elif type(snapshot.caller) is CheckpointSourceProofEndCaller:
        validations.append(snapshot.caller.composed_stop_validation_json)
        if snapshot.caller.diagnostics_json is not None:
            embedded.append(snapshot.caller.diagnostics_json)
    elif type(snapshot.caller) is CheckpointIterationEndCaller:
        from elspeth.web.composer.continuation_classifier_handoff import decode_classify_post_primary

        pending = decode_classify_post_primary(snapshot.caller.pending_json)
        if (
            stable_hash(pending.model_dump(mode="json")) != snapshot.caller.pending_source_sha256
            or pending.primary_effect_key != snapshot.caller.primary_effect_key
        ):
            raise AuditIntegrityError("END input changed its original classifier caller")
        embedded.append(snapshot.caller.pending_json)
    for encoded in validations:
        if encoded is not None:
            _decode_owned(_VALIDATION, encoded)
            embedded.append(encoded)
    for encoded in embedded:
        value, error = parse_json_strict(encoded)
        if error is not None:
            raise AuditIntegrityError("END gate input lost its complete owned embedded JSON")
        require_no_credential_material(value, surface="checkpoint_end_input", env_ref_names=env_ref_names)


def encode_checkpoint_end_input(snapshot: CheckpointEndInputSnapshot, *, env_ref_names: frozenset[str]) -> PrivateChunk:
    if type(snapshot) is not CheckpointEndInputSnapshot:
        raise TypeError("END input requires its closed nominal snapshot")
    canonical = canonical_json(snapshot.model_dump(mode="json"))
    try:
        checked = CheckpointEndInputSnapshot.model_validate_json(canonical)
    except (ValidationError, TypeError, ValueError) as error:
        raise AuditIntegrityError("END gate input violates its closed DATA contract") from error
    if canonical_json(checked.model_dump(mode="json")) != canonical:
        raise AuditIntegrityError("END gate input normalized its original DATA")
    _validate_embedded(snapshot, env_ref_names)
    return encode_private_chunk(codec="composer.checkpoint-end-input.v1", canonical=canonical, env_ref_names=env_ref_names)


def _read_checkpoint_end_input(chunk: PrivateChunk) -> CheckpointEndInputSnapshot:
    if type(chunk) is not PrivateChunk:
        raise TypeError("END input reader requires its nominal private chunk")
    verify_private_chunk(chunk.reference, chunk.canonical)
    if chunk.reference.codec != "composer.checkpoint-end-input.v1":
        raise AuditIntegrityError("END input reader received another private codec")
    try:
        snapshot = CheckpointEndInputSnapshot.model_validate_json(chunk.canonical)
    except (ValidationError, TypeError, ValueError) as error:
        raise AuditIntegrityError("END gate input violates its closed DATA contract") from error
    if canonical_json(snapshot.model_dump(mode="json")) != chunk.canonical:
        raise AuditIntegrityError("END gate input omitted or normalized original DATA")
    return snapshot


def checkpoint_end_input_references(chunk: PrivateChunk) -> tuple[PrivateChunkRef, ...]:
    """Structural closure only; current source and credential admission remain owed."""
    snapshot = _read_checkpoint_end_input(chunk)
    if type(snapshot.caller) is CheckpointIterationEndCaller:
        from elspeth.web.composer.continuation_classifier_handoff import decode_classify_post_primary

        pending = decode_classify_post_primary(snapshot.caller.pending_json)
        if pending.response not in snapshot.runtime_chunks:
            raise AuditIntegrityError("END input lost its retained classifier PRIMARY reference")
    return snapshot.runtime_chunks


def require_checkpoint_end_input_closure(chunk: PrivateChunk, runtime_chunks: tuple[PrivateChunk, ...]) -> None:
    """Prove all declared runtime bytes and their nested closure, without admission."""
    from elspeth.web.sessions.continuation_selection import nested_private_chunks

    references = checkpoint_end_input_references(chunk)
    if (
        type(runtime_chunks) is not tuple
        or any(type(item) is not PrivateChunk for item in runtime_chunks)
        or references != tuple(item.reference for item in runtime_chunks)
    ):
        raise AuditIntegrityError("END input lost its exact complete runtime chunk cohort")
    indexed = {(item.reference.codec, item.reference.digest): item.reference for item in runtime_chunks}
    if len(indexed) != len(runtime_chunks):
        raise AuditIntegrityError("END input duplicated a runtime chunk identity")
    for item in runtime_chunks:
        verify_private_chunk(item.reference, item.canonical)
        if any(indexed.get((reference.codec, reference.digest)) != reference for reference in nested_private_chunks(item)):
            raise AuditIntegrityError("END input lost a nested runtime chunk reference")


def decode_checkpoint_end_input(chunk: PrivateChunk, *, env_ref_names: frozenset[str]) -> CheckpointEndInputSnapshot:
    snapshot = _read_checkpoint_end_input(chunk)
    if encode_checkpoint_end_input(snapshot, env_ref_names=env_ref_names) != chunk:
        raise AuditIntegrityError("END gate input omitted or normalized original DATA")
    return snapshot


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class CapturedCheckpointEndInput:
    chunk: PrivateChunk
    _issued_record: _OriginalEndInput | None = field(default=None, init=False, repr=False, compare=False)


@dataclass(frozen=True, slots=True, weakref_slot=True)
class _OriginalEndInput:
    phase_source: CheckpointPhaseSource
    runtime: RestoredContinuationRuntime
    captured: CapturedContinuationRuntime
    chunk: PrivateChunk


_issued: WeakKeyDictionary[CapturedCheckpointEndInput, ReferenceType[_OriginalEndInput]] = WeakKeyDictionary()


def _original_input(result: CapturedCheckpointEndInput) -> _OriginalEndInput:
    reference = _issued.get(result) if type(result) is CapturedCheckpointEndInput else None
    source = None if reference is None else reference()
    if source is None or source is not result._issued_record:
        raise AuditIntegrityError("END input requires its original pre-call capture producer")
    return source


def capture_checkpoint_end_input(
    snapshot: CheckpointEndInputSnapshot,
    *,
    phase_source: CheckpointPhaseSource,
    runtime: RestoredContinuationRuntime,
    captured: CapturedContinuationRuntime,
    orphan_observation: PendingInterpretationReviewObservation,
    gate_locals: CheckpointEndGateLocals,
    phase_owner: CheckpointPhaseOwner,
) -> CapturedCheckpointEndInput:
    """Bind actual gate DATA to the original captured pre-call runtime.

    The original caller-local issuer proves the orphan observation and caller
    continuation. This carrier grants no SQL admission;
    the source preparer must reprove those sources under its original locks.
    """
    from elspeth.web.composer.continuation_checkpoint_end_locals import (
        IterationEndCallerLocals,
        OrdinaryEndCallerLocals,
        SourceProofEndCallerLocals,
        require_original_checkpoint_end_gate_locals,
    )
    from elspeth.web.composer.interpretation_surfacing import require_original_pending_review_observation
    from elspeth.web.sessions.continuation_checkpoint_paid import CheckpointPhaseSource

    require_original_checkpoint_end_gate_locals(gate_locals, phase_owner=phase_owner)

    if (
        type(snapshot) is not CheckpointEndInputSnapshot
        or phase_owner.runtime is not runtime
        or phase_owner.phase_source != phase_source
        or gate_locals.orphan_observation is not orphan_observation
        or type(phase_source) is not CheckpointPhaseSource
        or type(runtime) is not RestoredContinuationRuntime
        or type(captured) is not CapturedContinuationRuntime
        or type(runtime.source.policy) is not ContinuationAdmissionPolicyV4
        or phase_source.phase != "end"
        or captured.selected != runtime.source.selected
        or captured.selected.checkpoint_id != phase_source.current.checkpoint_id
        or snapshot.original_checkpoint_id != phase_source.current.checkpoint_id
        or snapshot.original_cursor != phase_source.current.cursor
        or snapshot.outer_effect_key != phase_source.outer_effect_key
        or snapshot.session_id != captured.stack.logical.session_id
        or snapshot.operation_id != captured.stack.logical.operation_id
        or snapshot.captured_runtime_sha256 != stable_hash(captured.stack.model_dump(mode="json"))
        or snapshot.runtime_chunks != tuple(chunk.reference for chunk in captured.chunks)
        or snapshot.graph_fingerprint != completion_gate_fingerprint(runtime.live.state)
    ):
        raise AuditIntegrityError("END gate input changed its original pre-call source")
    frames = tuple(frame for frame in captured.stack.frames if type(frame) is ComposeFrame)
    if len(frames) != 1 or snapshot.state_sha256 != frames[0].state.digest:
        raise AuditIntegrityError("END gate input lost its original captured graph")
    if (
        snapshot.assistant_message_json != _dump_owned(_MESSAGE, gate_locals.assistant_message)
        or snapshot.review_state_json != _dump_owned(_REVIEW, gate_locals.review_state)
        or snapshot.initial_review_state_json != _dump_owned(_REVIEW, gate_locals.initial_review_state)
        or snapshot.runtime_preflight_json
        != (None if gate_locals.runtime_preflight is None else _dump_owned(_VALIDATION, gate_locals.runtime_preflight))
        or snapshot.publication_review_json != _dump_owned(_PUBLICATION, gate_locals.publication_review)
        or snapshot.completion_gates_json
        != (None if gate_locals.completion_gates is None else _dump_owned(_GATES, gate_locals.completion_gates))
        or (
            snapshot.advisor_checkpoint_passes_used,
            snapshot.passes_delta,
            snapshot.pass_index,
            snapshot.max_passes,
            snapshot.allow_repair_continue,
        )
        != (
            gate_locals.advisor_checkpoint_passes_used,
            gate_locals.passes_delta,
            gate_locals.pass_index,
            gate_locals.max_passes,
            gate_locals.allow_repair_continue,
        )
        or snapshot.superseded_reply_index != phase_owner.superseded_reply_index
        or snapshot.elide_advisor_exchange != phase_owner.elide_advisor_exchange
    ):
        raise AuditIntegrityError("END gate input substituted its original gate locals")
    caller = gate_locals.caller
    expected_caller: CheckpointOrdinaryEndCaller | CheckpointSourceProofEndCaller | CheckpointIterationEndCaller
    if type(caller) is OrdinaryEndCallerLocals:
        expected_caller = CheckpointOrdinaryEndCaller(
            proof_runtime_preflight_json=None
            if caller.proof_runtime_preflight is None
            else _dump_owned(_VALIDATION, caller.proof_runtime_preflight),
            proof_repair_action=caller.proof_repair_action,
            proof_repair_diagnostics_json=None
            if caller.proof_repair_diagnostics is None
            else canonical_json(owned_json(deep_thaw(caller.proof_repair_diagnostics))),
            confirmed_stop=caller.confirmed_stop,
            confirmed_stop_evidence_sha256=frames[0].evidence.digest if caller.confirmed_stop else None,
        )
    elif type(caller) is SourceProofEndCallerLocals:
        expected_caller = CheckpointSourceProofEndCaller(
            proof_kind=caller.proof_kind,
            diagnostics_json=None if caller.diagnostics is None else canonical_json(owned_json(deep_thaw(caller.diagnostics))),
            source_name=caller.source_name,
            reason=caller.reason,
            composed_stop_validation_json=_dump_owned(_VALIDATION, caller.composed_stop_validation),
        )
    elif type(caller) is IterationEndCallerLocals:
        from elspeth.web.composer.continuation_classifier_handoff import encode_classify_post_primary

        expected_caller = CheckpointIterationEndCaller(
            pending_source_sha256=stable_hash(caller.pending.model_dump(mode="json")),
            primary_effect_key=caller.pending.primary_effect_key,
            pending_json=encode_classify_post_primary(caller.pending),
        )
    else:
        raise AuditIntegrityError("END gate input lacks an original caller continuation")
    if snapshot.caller != expected_caller:
        raise AuditIntegrityError("END gate input substituted its original caller continuation")
    require_original_pending_review_observation(
        orphan_observation,
        owner=runtime.materialization.service._completion._interpretation_surfacing,
        state=runtime.live.state,
        session_id=runtime.bindings.session_id,
    )
    if (
        snapshot.orphaned_precheck != orphan_observation.missing_sites
        or snapshot.orphan_source_sha256 != orphan_observation.source_sha256
        or snapshot.orphan_events_json != _dump_owned(_ORPHANS, orphan_observation.events)
    ):
        raise AuditIntegrityError("END gate input replaced its original orphan observation")
    message = _decode_owned(_MESSAGE, snapshot.assistant_message_json)
    primary_reference = caller.pending.response if type(caller) is IterationEndCallerLocals else frames[0].pending_response
    primary_chunks = tuple(chunk for chunk in captured.chunks if chunk.reference == primary_reference)
    if len(primary_chunks) != 1:
        raise AuditIntegrityError("END gate input lost its original retained PRIMARY message")
    original_message = decode_continuation_response(primary_chunks[0]).message
    expected_message = (
        original_message
        if type(snapshot.caller) is CheckpointSourceProofEndCaller
        else replace(original_message, content=parse_no_tool_response(original_message.content or "").body)
    )
    if message != expected_message:
        raise AuditIntegrityError("END gate input changed its original caller's PRIMARY body projection")
    publication = runtime.live.turn_evidence.review_candidate(
        candidate=message.content or "", request=runtime.bindings.message, current_state=runtime.live.state
    )
    if _dump_owned(_PUBLICATION, publication) != snapshot.publication_review_json:
        raise AuditIntegrityError("END gate input changed its original complete publication packet")
    if snapshot.superseded_reply_index is not None:
        index = snapshot.superseded_reply_index
        if index >= len(runtime.live.llm_messages) or stable_hash(runtime.live.llm_messages[index]) != snapshot.superseded_reply_sha256:
            raise AuditIntegrityError("END gate input changed its exact superseded reply")
    chunk = encode_checkpoint_end_input(snapshot, env_ref_names=runtime.env_ref_names)
    result = CapturedCheckpointEndInput(chunk)
    original = _OriginalEndInput(phase_source, runtime, captured, chunk)
    object.__setattr__(result, "_issued_record", original)
    _issued[result] = ref(original)
    return result


def require_original_checkpoint_end_input(
    result: CapturedCheckpointEndInput, *, phase_source: CheckpointPhaseSource, captured: CapturedContinuationRuntime
) -> PrivateChunk:
    source = _original_input(result)
    if source.phase_source != phase_source or source.captured is not captured or source.chunk is not result.chunk:
        raise AuditIntegrityError("END input requires its original pre-call capture producer")
    decode_checkpoint_end_input(result.chunk, env_ref_names=source.runtime.env_ref_names)
    return result.chunk


def require_original_checkpoint_end_snapshot(
    result: CapturedCheckpointEndInput, *, phase_source: CheckpointPhaseSource, captured: CapturedContinuationRuntime
) -> tuple[PrivateChunk, CheckpointEndInputSnapshot]:
    chunk = require_original_checkpoint_end_input(result, phase_source=phase_source, captured=captured)
    source = _original_input(result)
    return chunk, decode_checkpoint_end_input(chunk, env_ref_names=source.runtime.env_ref_names)


def require_original_checkpoint_end_env_names(
    result: CapturedCheckpointEndInput, *, phase_source: CheckpointPhaseSource, captured: CapturedContinuationRuntime
) -> frozenset[str]:
    require_original_checkpoint_end_input(result, phase_source=phase_source, captured=captured)
    return _original_input(result).runtime.env_ref_names
