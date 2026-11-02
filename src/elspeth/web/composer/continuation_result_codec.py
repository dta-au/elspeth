"""Neutral complete Composer result DATA bound to its original recorder.

The recorder is retained as its own complete immutable private chunk. Result
audit lists select exact ordered members of that chunk, including an empty
tool list when the original tool rows were already persisted. No result field
or private provider request is reconstructed from a caller's assertion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.advisor_audit import AdvisorGraphReviewIdentity, AdvisorTerminalPublication
from elspeth.web.composer.advisor_decision import AdvisorGateDecision
from elspeth.web.composer.continuation import PrivateChunkRef
from elspeth.web.composer.continuation_audit_buffer import RecorderSnapshot, decode_recorder_snapshot
from elspeth.web.composer.continuation_codec import PrivateChunk, owned_json
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.graph_recovery import GraphRecoveryIntent
from elspeth.web.composer.protocol import ComposerResult, PipelineCommitIntent
from elspeth.web.composer.state import CompositionState
from elspeth.web.execution.schemas import ValidationResult
from elspeth.web.sessions._persist_payload import AuditMessageDraft
from elspeth.web.sessions.protocol import ChatMessageRole


class _AuditDraftWire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    role: ChatMessageRole
    content: str
    tool_calls_json: str | None
    tool_call_id: str | None
    parent_assistant_id: str | None
    composition_state_id: str | None
    inherit_composition_state: bool


class _ResultWire(BaseModel):
    """Closed complete result, replacing only repeated audit lists with refs."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal["composer.continuation-result.v1"] = "composer.continuation-result.v1"
    message: str
    state_json: str
    runtime_preflight: ValidationResult | None
    advisor_gate_decision: AdvisorGateDecision | None
    graph_review_identity: AdvisorGraphReviewIdentity | None
    raw_assistant_content: str | None
    pipeline_commit_intent: PipelineCommitIntent | None
    graph_recovery_intent: GraphRecoveryIntent | None
    recovery_audit_drafts: tuple[_AuditDraftWire, ...]
    recorder: PrivateChunkRef
    llm_call_indices: tuple[int, ...]
    tool_invocation_indices: tuple[int, ...]
    persisted_assistant_message_id: str | None
    persisted_assistant_content: str | None
    persisted_tool_call_turn: bool
    persisted_assistant_matches_terminal_model_turn: bool
    advisor_terminal_publication: AdvisorTerminalPublication | None
    final_persisted_state_id: UUID | None
    repair_turns_used: int
    publication_binding: str | None = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class RetainedComposerResult:
    result: ComposerResult
    publication_binding: str | None


def _audit_draft_wire(draft: AuditMessageDraft) -> _AuditDraftWire:
    if type(draft) is not AuditMessageDraft:
        raise AuditIntegrityError("continuation result lost its owned audit draft")
    return _AuditDraftWire(
        role=draft.role,
        content=draft.content,
        tool_calls_json=None if draft.tool_calls is None else canonical_json(owned_json(draft.tool_calls)),
        tool_call_id=draft.tool_call_id,
        parent_assistant_id=draft.parent_assistant_id,
        composition_state_id=draft.composition_state_id,
        inherit_composition_state=draft.inherit_composition_state,
    )


def _restore_audit_draft(wire: _AuditDraftWire) -> AuditMessageDraft:
    calls = None
    if wire.tool_calls_json is not None:
        parsed, parse_error = parse_json_strict(wire.tool_calls_json)
        if parse_error is not None or type(parsed) is not list or any(type(call) is not dict for call in parsed):
            raise AuditIntegrityError("continuation result lost its complete audit tool calls")
        if canonical_json(parsed) != wire.tool_calls_json:
            raise AuditIntegrityError("continuation result changed its canonical audit tool calls")
        calls = tuple(parsed)
    return AuditMessageDraft(
        role=wire.role,
        content=wire.content,
        tool_calls=calls,
        tool_call_id=wire.tool_call_id,
        parent_assistant_id=wire.parent_assistant_id,
        composition_state_id=wire.composition_state_id,
        inherit_composition_state=wire.inherit_composition_state,
    )


def _ordered_indices[T](values: tuple[T, ...], source: tuple[T, ...]) -> tuple[int, ...]:
    if type(values) is not tuple or type(source) is not tuple:
        raise AuditIntegrityError("continuation result requires owned ordered audit tuples")
    selected: list[int] = []
    cursor = 0
    for value in values:
        while cursor < len(source) and source[cursor] != value:
            cursor += 1
        if cursor == len(source):
            raise AuditIntegrityError("continuation result audit item lacks its original recorder source")
        selected.append(cursor)
        cursor += 1
    return tuple(selected)


def _selected_items[T](indices: tuple[int, ...], source: tuple[T, ...]) -> tuple[T, ...]:
    if type(indices) is not tuple or type(source) is not tuple:
        raise AuditIntegrityError("continuation result lost its ordered recorder selection")
    previous = -1
    selected: list[T] = []
    for index in indices:
        if type(index) is not int or index <= previous or index < 0 or index >= len(source):
            raise AuditIntegrityError("continuation result changed its ordered recorder selection")
        selected.append(source[index])
        previous = index
    return tuple(selected)


def _source(recorder_source: PrivateChunk) -> RecorderSnapshot:
    if type(recorder_source) is not PrivateChunk:
        raise TypeError("continuation result requires its actual private recorder chunk")
    return decode_recorder_snapshot(recorder_source)


def encode_retained_composer_result(value: RetainedComposerResult, *, recorder_source: PrivateChunk) -> str:
    """Encode a complete result with exact postexecution recorder selections."""
    if type(value) is not RetainedComposerResult or type(value.result) is not ComposerResult:
        raise TypeError("continuation result requires the actual owned Composer result")
    snapshot = _source(recorder_source)
    result = value.result
    wire = _ResultWire(
        schema_version="composer.continuation-result.v1",
        message=result.message,
        state_json=canonical_json(result.state.to_dict()),
        runtime_preflight=result.runtime_preflight,
        advisor_gate_decision=result.advisor_gate_decision,
        graph_review_identity=result.graph_review_identity,
        raw_assistant_content=result.raw_assistant_content,
        pipeline_commit_intent=result.pipeline_commit_intent,
        graph_recovery_intent=result.graph_recovery_intent,
        recovery_audit_drafts=tuple(_audit_draft_wire(draft) for draft in result.recovery_audit_drafts),
        recorder=recorder_source.reference,
        llm_call_indices=_ordered_indices(result.llm_calls, snapshot.llm_calls),
        tool_invocation_indices=_ordered_indices(result.tool_invocations, snapshot.invocations),
        persisted_assistant_message_id=result.persisted_assistant_message_id,
        persisted_assistant_content=result.persisted_assistant_content,
        persisted_tool_call_turn=result.persisted_tool_call_turn,
        persisted_assistant_matches_terminal_model_turn=result.persisted_assistant_matches_terminal_model_turn,
        advisor_terminal_publication=result.advisor_terminal_publication,
        final_persisted_state_id=result.final_persisted_state_id,
        repair_turns_used=result.repair_turns_used,
        publication_binding=value.publication_binding,
    )
    encoded = canonical_json(wire.model_dump(mode="json"))
    if len(encoded.encode("utf-8")) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("continuation result exceeds its complete private document bound")
    if decode_retained_composer_result(encoded, recorder_source=recorder_source) != value:
        raise AuditIntegrityError("continuation result changed in its complete cold encoding")
    return encoded


def decode_retained_composer_result(encoded: str, *, recorder_source: PrivateChunk) -> RetainedComposerResult:
    """Rehydrate every result field from closed bytes and one original source."""
    if type(encoded) is not str or len(encoded.encode("utf-8")) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("continuation result has no bounded canonical document")
    snapshot = _source(recorder_source)
    try:
        wire = _ResultWire.model_validate_json(encoded, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("continuation result lost its closed original fields") from error
    if wire.schema_version != "composer.continuation-result.v1" or canonical_json(wire.model_dump(mode="json")) != encoded:
        raise AuditIntegrityError("continuation result changed its exact canonical bytes")
    if wire.recorder != recorder_source.reference:
        raise AuditIntegrityError("continuation result changed its original recorder reference")
    raw_state, parse_error = parse_json_strict(wire.state_json)
    if parse_error is not None or type(raw_state) is not dict or canonical_json(raw_state) != wire.state_json:
        raise AuditIntegrityError("continuation result changed its complete state source")
    state = CompositionState.from_dict(raw_state)
    if canonical_json(state.to_dict()) != wire.state_json:
        raise AuditIntegrityError("continuation result changed its immutable state on restore")
    result = ComposerResult(
        message=wire.message,
        state=state,
        runtime_preflight=wire.runtime_preflight,
        advisor_gate_decision=wire.advisor_gate_decision,
        graph_review_identity=wire.graph_review_identity,
        raw_assistant_content=wire.raw_assistant_content,
        pipeline_commit_intent=wire.pipeline_commit_intent,
        graph_recovery_intent=wire.graph_recovery_intent,
        recovery_audit_drafts=tuple(_restore_audit_draft(draft) for draft in wire.recovery_audit_drafts),
        tool_invocations=_selected_items(wire.tool_invocation_indices, snapshot.invocations),
        llm_calls=_selected_items(wire.llm_call_indices, snapshot.llm_calls),
        persisted_assistant_message_id=wire.persisted_assistant_message_id,
        persisted_assistant_content=wire.persisted_assistant_content,
        persisted_tool_call_turn=wire.persisted_tool_call_turn,
        persisted_assistant_matches_terminal_model_turn=wire.persisted_assistant_matches_terminal_model_turn,
        advisor_terminal_publication=wire.advisor_terminal_publication,
        final_persisted_state_id=wire.final_persisted_state_id,
        repair_turns_used=wire.repair_turns_used,
    )
    return RetainedComposerResult(result=result, publication_binding=wire.publication_binding)
