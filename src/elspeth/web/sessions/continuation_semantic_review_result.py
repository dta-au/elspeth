"""Private, closed result DATA for one actually executed semantic END review.

The codec preserves the shared END gate's complete result. SQL custody and
publication are separate: a retained result cannot by itself publish a graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, update
from sqlalchemy.engine import Connection

from elspeth.contracts.composer_audit import ComposerToolInvocation
from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer._compose_loop_carriers import _AdvisorReviewState, _EndCompletionDisposition
from elspeth.web.composer.advisor_audit import (
    ADVISOR_CHECKPOINT_PASS_AUDIT_KIND,
    AdvisorCheckpointPassRecord,
    AdvisorGraphReviewIdentity,
    AdvisorTerminalPublication,
)
from elspeth.web.composer.advisor_decision import AdvisorGateDecision
from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
from elspeth.web.composer.continuation import ComposeFrame, EndFrame
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_snapshot,
    decode_frontier_chunk,
    encode_continuation_snapshot,
    encode_frontier_chunk,
    owned_json,
)
from elspeth.web.composer.continuation_end_disposition import classify_end_review_result, require_end_publication_pass
from elspeth.web.composer.continuation_end_values import decode_end_candidate, encode_end_candidate
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_progress import ActionExecutionReceipt, record_action_receipt
from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime
from elspeth.web.composer.graph_recovery import GraphRecoveryIntent
from elspeth.web.composer.protocol import ComposerResult, PipelineCommitIntent
from elspeth.web.composer.state import CompositionState
from elspeth.web.credential_guard import require_no_credential_material
from elspeth.web.execution.schemas import ValidationResult
from elspeth.web.sessions._persist_payload import AuditMessageDraft
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    StoredContinuationSnapshot,
    _effect_snapshot,
    append_complete_snapshot,
    load_snapshot,
    put_chunk,
    read_chunk,
    retain_action_execution_receipt_on_connection,
)
from elspeth.web.sessions.continuation_end import EndReviewExecution
from elspeth.web.sessions.continuation_semantic_actions import (
    installed_action_json,
    read_original_dispatched_semantic_review_on_connection,
    require_dispatched_semantic_review_on_connection,
)
from elspeth.web.sessions.continuation_semantic_advisor import _historical_snapshot, _journal_data
from elspeth.web.sessions.models import chat_messages_table as chat_messages
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.protocol import ChatMessageRole


class _AuditDraftWire(BaseModel):
    """Closed JSON projection of one immutable audit draft."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    role: ChatMessageRole
    content: str
    tool_calls_json: str | None
    tool_call_id: str | None
    parent_assistant_id: str | None
    composition_state_id: str | None
    inherit_composition_state: bool


def _audit_draft_wire(draft: AuditMessageDraft) -> _AuditDraftWire:
    if type(draft) is not AuditMessageDraft:
        raise AuditIntegrityError("semantic Review result lost an owned audit draft")
    calls = None if draft.tool_calls is None else canonical_json(owned_json(draft.tool_calls))
    return _AuditDraftWire(
        role=draft.role,
        content=draft.content,
        tool_calls_json=calls,
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
            raise AuditIntegrityError("semantic Review audit draft lost its complete tool call list")
        if canonical_json(parsed) != wire.tool_calls_json:
            raise AuditIntegrityError("semantic Review audit draft changed its canonical tool call list")
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


class _ReviewResultWire(BaseModel):
    """Every owned ComposerResult field except its custom immutable state."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    message: str
    state_json: str
    runtime_preflight: ValidationResult | None
    advisor_gate_decision: AdvisorGateDecision | None
    graph_review_identity: AdvisorGraphReviewIdentity | None
    raw_assistant_content: str | None
    pipeline_commit_intent: PipelineCommitIntent | None
    graph_recovery_intent: GraphRecoveryIntent | None
    recovery_audit_drafts: tuple[_AuditDraftWire, ...]
    tool_invocations: tuple[ComposerToolInvocation, ...]
    llm_calls: tuple[ComposerLLMCall, ...]
    persisted_assistant_message_id: str | None
    persisted_assistant_content: str | None
    persisted_tool_call_turn: bool
    persisted_assistant_matches_terminal_model_turn: bool
    advisor_terminal_publication: AdvisorTerminalPublication | None
    final_persisted_state_id: UUID | None
    repair_turns_used: int
    publication_binding: str | None = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class RetainedSemanticReviewResult:
    result: ComposerResult
    publication_binding: str | None


class _ReviewResultCommit(BaseModel):
    """Immutable binding of result bytes to the exact dispatched END owner."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal["composer.semantic-review-result.v1"] = "composer.semantic-review-result.v1"
    session_id: UUID
    operation_id: str
    semantic_effect_key: str
    owner_effect_key: str
    dispatch_checkpoint_id: str
    dispatch_cursor: int = Field(ge=0)
    action_key: str
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_json: str
    outcome: Literal["clean", "blocked"]
    pass_audit_message_id: str | None
    pass_index: int | None


@dataclass(frozen=True, slots=True)
class SemanticReviewCommitted:
    """Cold-proved result of one real END review, without publication grant."""

    completed: StoredContinuationSnapshot
    result: ComposerResult
    publication_binding: str | None
    original_advisor_call_id: str
    owner_effect_key: str
    action_key: str
    semantic_effect_key: str
    outcome: Literal["clean", "blocked"]


def _commit_chunk_key(canonical: str) -> str:
    return "semantic-review-result:" + sha256(canonical.encode("utf-8")).hexdigest()


def _review_result_outcome(
    value: RetainedSemanticReviewResult, *, actual_disposition: Literal["clean", "blocked"]
) -> Literal["clean", "blocked"]:
    identity, binding = value.result.graph_review_identity, value.publication_binding
    if identity is not None and type(identity) is not AdvisorGraphReviewIdentity:
        raise AuditIntegrityError("semantic Review result changed its actual shared END identity")
    if binding is not None and (type(binding) is not str or len(binding) != 64 or any(char not in "0123456789abcdef" for char in binding)):
        raise AuditIntegrityError("semantic Review result changed its actual shared END binding")
    if identity is not None and binding is not None and identity.binding != binding:
        raise AuditIntegrityError("semantic Review result changed its actual shared END identity")
    if actual_disposition == "clean":
        if classify_end_review_result(value.result, publication_binding=value.publication_binding) != "clean":
            raise AuditIntegrityError("semantic Review result lost its complete clean END proof")
        return "clean"
    return "blocked"


def _require_original_clean_end_pass_audit(
    conn: Connection,
    *,
    session_id: str,
    publication_binding: str,
    pass_index: int,
    message_id: str | None = None,
) -> str:
    """Read the actual persisted model pass used by the shared END gate."""
    query = select(chat_messages).where(
        chat_messages.c.session_id == session_id,
        chat_messages.c.role == "audit",
        chat_messages.c.writer_principal == "compose_loop",
        chat_messages.c.tool_calls[0]["_kind"].as_string() == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND,
        chat_messages.c.tool_calls[0]["pass"]["phase"].as_string() == "end",
        chat_messages.c.tool_calls[0]["pass"]["source"].as_string() == "model",
        chat_messages.c.tool_calls[0]["pass"]["publication_binding"].as_string() == publication_binding,
        chat_messages.c.tool_calls[0]["pass"]["pass_index"].as_integer() == pass_index,
    )
    query = (
        query.where(chat_messages.c.id == message_id)
        if message_id is not None
        else query.order_by(chat_messages.c.sequence_no.desc()).limit(1)
    )
    row = conn.execute(query).one_or_none()
    if row is None or type(row.id) is not str or type(row.tool_calls) is not list or len(row.tool_calls) != 1:
        raise AuditIntegrityError("semantic Review result lost its actual model END pass audit row")
    envelope = row.tool_calls[0]
    if type(envelope) is not dict or envelope.get("_kind") != ADVISOR_CHECKPOINT_PASS_AUDIT_KIND:
        raise AuditIntegrityError("semantic Review result changed its one original model END pass envelope")
    try:
        accepted = AdvisorCheckpointPassRecord.from_dict(envelope["pass"])
    except (KeyError, TypeError, ValueError) as error:
        raise AuditIntegrityError("semantic Review result changed its original model END pass record") from error
    require_end_publication_pass(accepted, publication_binding=publication_binding)
    if accepted.pass_index != pass_index:
        raise AuditIntegrityError("semantic Review result lacks an original accepted model END pass")
    return row.id


def encode_semantic_review_result(value: RetainedSemanticReviewResult) -> str:
    """Encode only an exact shared-gate result with a verified cold roundtrip."""
    if type(value) is not RetainedSemanticReviewResult or type(value.result) is not ComposerResult:
        raise TypeError("semantic review requires the actual owned shared END result")
    result = value.result
    wire = _ReviewResultWire(
        message=result.message,
        state_json=canonical_json(result.state.to_dict()),
        runtime_preflight=result.runtime_preflight,
        advisor_gate_decision=result.advisor_gate_decision,
        graph_review_identity=result.graph_review_identity,
        raw_assistant_content=result.raw_assistant_content,
        pipeline_commit_intent=result.pipeline_commit_intent,
        graph_recovery_intent=result.graph_recovery_intent,
        recovery_audit_drafts=tuple(_audit_draft_wire(draft) for draft in result.recovery_audit_drafts),
        tool_invocations=result.tool_invocations,
        llm_calls=result.llm_calls,
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
    if decode_semantic_review_result(encoded) != value:
        raise AuditIntegrityError("semantic review result changed in its complete cold encoding")
    return encoded


def decode_semantic_review_result(encoded: str) -> RetainedSemanticReviewResult:
    if type(encoded) is not str:
        raise TypeError("semantic review cold result requires canonical text")
    try:
        wire = _ReviewResultWire.model_validate_json(encoded, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("semantic review result lost its closed original fields") from error
    if canonical_json(wire.model_dump(mode="json")) != encoded:
        raise AuditIntegrityError("semantic review result changed its canonical original bytes")
    raw_state, parse_error = parse_json_strict(wire.state_json)
    if parse_error is not None or type(raw_state) is not dict or canonical_json(raw_state) != wire.state_json:
        raise AuditIntegrityError("semantic review result changed its complete state source")
    state: CompositionState = CompositionState.from_dict(raw_state)
    if canonical_json(state.to_dict()) != wire.state_json:
        raise AuditIntegrityError("semantic review result changed its immutable state on restore")
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
        tool_invocations=wire.tool_invocations,
        llm_calls=wire.llm_calls,
        persisted_assistant_message_id=wire.persisted_assistant_message_id,
        persisted_assistant_content=wire.persisted_assistant_content,
        persisted_tool_call_turn=wire.persisted_tool_call_turn,
        persisted_assistant_matches_terminal_model_turn=wire.persisted_assistant_matches_terminal_model_turn,
        advisor_terminal_publication=wire.advisor_terminal_publication,
        final_persisted_state_id=wire.final_persisted_state_id,
        repair_turns_used=wire.repair_turns_used,
    )
    return RetainedSemanticReviewResult(result=result, publication_binding=wire.publication_binding)


def retain_semantic_review_result_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
    semantic_effect_key: str,
    execution: EndReviewExecution,
    captured: CapturedContinuationRuntime,
) -> StoredContinuationSnapshot:
    """Commit the real shared END gate result, effect and action receipt once."""
    if (
        type(execution) is not EndReviewExecution
        or type(captured) is not CapturedContinuationRuntime
        or type(running) is not ComposerOperationRunning
    ):
        raise TypeError("semantic Review result requires its actual running and END gate owners")
    if type(execution.disposition) is not _EndCompletionDisposition:
        raise AuditIntegrityError("semantic Review result lost its actual shared END disposition")
    outcome = _review_result_outcome(
        RetainedSemanticReviewResult(execution.public_result, execution.publication_binding),
        actual_disposition="clean" if execution.disposition is _EndCompletionDisposition.CLEAN else "blocked",
    )
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    with conn.begin_nested():
        current = load_snapshot(conn, session_id=sid, operation_id=operation)
        if (
            current != selected
            or execution.current.checkpoint_id != selected.checkpoint_id
            or encode_continuation_snapshot(execution.current.stack) != selected.payload_json
            or execution.runtime.source.selected != execution.current
            or captured.selected is not execution.current
            or captured.stack.model_copy(update={"frames": execution.current.stack.frames}) != execution.current.stack
        ):
            raise AuditIntegrityError("semantic Review result changed its actual dispatched END cut")
        admission = require_dispatched_semantic_review_on_connection(conn, running, selected=selected, effect_key=semantic_effect_key)
        candidate, owner_key = admission.review_candidate, admission.owner_effect_key
        if candidate is None or owner_key is None or admission.action_key is None:
            raise AuditIntegrityError("semantic Review result lost its original paid candidate/action/effect")
        audit_id: str | None = None
        pass_index: int | None = None
        if outcome == "clean":
            review_state = execution.runtime.live.advisor_review_state
            if type(review_state) is not _AdvisorReviewState or review_state.completed_passes < 1:
                raise AuditIntegrityError("semantic Review result lost its actual completed END pass index")
            binding = execution.publication_binding
            if type(binding) is not str:
                raise AuditIntegrityError("semantic Review result lost its actual model publication binding")
            pass_index = review_state.completed_passes
            audit_id = _require_original_clean_end_pass_audit(
                conn,
                session_id=sid,
                publication_binding=binding,
                pass_index=pass_index,
            )
        captured_compose, captured_end = captured.stack.frames[0], captured.stack.frames[-1]
        selected_end = execution.current.stack.frames[-1]
        if (
            len(captured.stack.frames) != 2
            or type(captured_compose) is not ComposeFrame
            or type(captured_end) is not EndFrame
            or captured_end != selected_end
            or type(selected_end) is not EndFrame
            or captured_compose.pending_response != candidate.response
        ):
            raise AuditIntegrityError("semantic Review result changed its retained END/Compose source")
        captured_state = next(
            (chunk for chunk in (*captured.chunks, *execution.current.chunks) if chunk.reference == captured_compose.state), None
        )
        if captured_state is None or captured_state.canonical != canonical_json(execution.public_result.state.to_dict()):
            raise AuditIntegrityError("semantic Review result changed the shared gate's actual complete state")
        for chunk in captured.chunks:
            put_chunk(
                conn,
                session_id=sid,
                operation_id=operation,
                key=f"value:{chunk.reference.codec}:{chunk.reference.digest}",
                payload=chunk.canonical.encode("utf-8"),
                codec=chunk.reference.codec,
            )
        rebound = candidate.model_copy(
            update={"state": captured_compose.state, "evidence": captured_compose.evidence, "iteration": captured_compose.iteration}
        )
        rebound_chunk = encode_end_candidate(rebound)
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=f"value:{rebound_chunk.reference.codec}:{rebound_chunk.reference.digest}",
            payload=rebound_chunk.canonical.encode("utf-8"),
            codec=rebound_chunk.reference.codec,
        )
        encoded_result = encode_semantic_review_result(RetainedSemanticReviewResult(execution.public_result, execution.publication_binding))
        commit = _ReviewResultCommit(
            session_id=running.claim.session_id,
            operation_id=operation,
            semantic_effect_key=semantic_effect_key,
            owner_effect_key=owner_key,
            dispatch_checkpoint_id=selected.checkpoint_id,
            dispatch_cursor=selected.cursor,
            action_key=admission.action_key,
            candidate_digest=candidate.response.digest,
            result_json=encoded_result,
            outcome=outcome,
            pass_audit_message_id=audit_id,
            pass_index=pass_index,
        )
        canonical = canonical_json(commit.model_dump(mode="json"))
        if len(canonical.encode("utf-8")) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
            raise AuditIntegrityError("semantic Review result exceeds its complete private document bound")
        require_no_credential_material(
            canonical,
            surface="composer_continuation_semantic_review_result",
            env_ref_names=execution.runtime.env_ref_names,
            max_string_bytes=JSON_MAX_TOTAL_UTF8_BYTES,
        )
        chunk_key = _commit_chunk_key(canonical)
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=chunk_key,
            payload=canonical.encode("utf-8"),
            codec=commit.schema_version,
        )
        changed = conn.execute(
            update(effects)
            .where(
                effects.c.session_id == sid,
                effects.c.operation_id == operation,
                effects.c.effect_key == owner_key,
                effects.c.owner == "composer_end",
                effects.c.disposition == "dispatched",
                effects.c.action_json == installed_action_json(admission),
            )
            .values(disposition="committed", receipt_key=owner_key, result_chunk_key=chunk_key)
        )
        if changed.rowcount != 1:
            raise AuditIntegrityError("semantic Review result lost its one bound dispatched END effect")
        advanced = _effect_snapshot(selected.payload_json, effect_key=owner_key, disposition="committed", receipt_id=owner_key)
        advanced = retain_action_execution_receipt_on_connection(
            conn,
            session_id=sid,
            operation_id=operation,
            effect_key=owner_key,
            disposition="committed",
            snapshot_json=advanced,
        )
        root = decode_continuation_snapshot(advanced)
        end = root.frames[-1]
        if type(end) is not EndFrame or end.phase != "review_needed":
            raise AuditIntegrityError("semantic Review result lost its actual END publication candidate")
        published = end.model_copy(
            update={
                "phase": "publication_ready" if outcome == "clean" else "repair_needed",
                "state": captured_compose.state,
                "candidate": rebound_chunk.reference,
                "publication_binding": execution.publication_binding if outcome == "clean" else None,
            }
        )
        advanced = encode_continuation_snapshot(root.model_copy(update={"frames": (captured_compose, published)}))
        stored = append_complete_snapshot(
            conn,
            session_id=sid,
            operation_id=operation,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.cursor,
            snapshot_json=advanced,
            event_batch=(
                ContinuationEvent(
                    "checkpoint",
                    canonical_json(
                        {
                            "owner": "composer_semantic_review",
                            "transition": "review_result_retained",
                            "semantic_effect_key": semantic_effect_key,
                            "owner_effect_key": owner_key,
                            "result_chunk_key": chunk_key,
                            "dispatch_checkpoint_id": selected.checkpoint_id,
                            "dispatch_cursor": selected.cursor,
                            "action_key": admission.action_key,
                            "outcome": outcome,
                        }
                    ),
                ),
            ),
            handoff=False,
        )
        from elspeth.web.composer.continuation_evidence_restore import read_selected_turn_evidence_graph_on_connection
        from elspeth.web.composer.continuation_runtime import _selected_chunks
        from elspeth.web.composer.continuation_selection import SelectedContinuationState

        complete_root = decode_continuation_snapshot(stored.payload_json)
        complete_frontier = complete_root.semantic_frontier
        if complete_frontier is None:
            raise AuditIntegrityError("semantic Review result lost its committed semantic frontier")
        complete_frontier_chunk = PrivateChunk(
            complete_frontier,
            read_chunk(
                conn,
                session_id=sid,
                operation_id=operation,
                key=f"value:{complete_frontier.codec}:{complete_frontier.digest}",
            ).decode("utf-8", errors="strict"),
        )
        complete_selected = SelectedContinuationState(
            stored.checkpoint_id,
            complete_root,
            _selected_chunks(
                complete_root,
                (*execution.current.chunks, *captured.chunks),
                (rebound_chunk, complete_frontier_chunk),
            ),
        )
        read_selected_turn_evidence_graph_on_connection(
            conn,
            running,
            selected=complete_selected,
            names=execution.runtime.materialization.inventory.names,
        )
        return stored


def read_original_semantic_review_result_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    review_effect_key: str,
) -> SemanticReviewCommitted:
    """Replay original paid Review, result bytes, END effect and checkpoint."""
    sid = str(session_id)
    if type(session_id) is not UUID or type(operation_id) is not str or type(review_effect_key) is not str:
        raise TypeError("semantic Review cold proof requires exact original identity")
    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == sid,
            effects.c.operation_id == operation_id,
            effects.c.effect_key == review_effect_key,
        )
    ).one_or_none()
    if (
        effect is None
        or effect.owner != "composer_end"
        or effect.disposition != "committed"
        or effect.receipt_key != review_effect_key
        or type(effect.result_chunk_key) is not str
        or not effect.result_chunk_key.startswith("semantic-review-result:")
        or effect.provider_attempt_id is not None
    ):
        raise AuditIntegrityError("semantic Review cold result lost its original committed END effect")
    raw = read_chunk(conn, session_id=sid, operation_id=operation_id, key=effect.result_chunk_key)
    codec = conn.execute(
        select(chunks.c.codec).where(
            chunks.c.session_id == sid,
            chunks.c.operation_id == operation_id,
            chunks.c.chunk_key == effect.result_chunk_key,
        )
    ).scalar_one_or_none()
    canonical = raw.decode("utf-8", errors="strict")
    if codec != "composer.semantic-review-result.v1" or _commit_chunk_key(canonical) != effect.result_chunk_key:
        raise AuditIntegrityError("semantic Review cold result changed its exact private chunk")
    try:
        commit = _ReviewResultCommit.model_validate_json(canonical, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("semantic Review cold result lost its closed receipt") from error
    if canonical_json(commit.model_dump(mode="json")) != canonical:
        raise AuditIntegrityError("semantic Review cold result changed its canonical receipt")
    if commit.session_id != session_id or commit.operation_id != operation_id or commit.owner_effect_key != review_effect_key:
        raise AuditIntegrityError("semantic Review cold result changed its original request/effect")
    admission = read_original_dispatched_semantic_review_on_connection(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        dispatch_checkpoint_id=commit.dispatch_checkpoint_id,
        semantic_effect_key=commit.semantic_effect_key,
    )
    require_no_credential_material(
        canonical,
        surface="composer_continuation_semantic_review_result",
        env_ref_names=admission.paid.invocation.source.env_ref_names,
        max_string_bytes=JSON_MAX_TOTAL_UTF8_BYTES,
    )
    candidate = admission.review_candidate
    if (
        candidate is None
        or admission.owner_effect_key != review_effect_key
        or admission.action_key != commit.action_key
        or candidate.response.digest != commit.candidate_digest
        or effect.action_json != installed_action_json(admission)
    ):
        raise AuditIntegrityError("semantic Review cold result changed its original paid candidate/action")
    dispatch = _historical_snapshot(conn, sid, operation_id, checkpoint_id=commit.dispatch_checkpoint_id)
    if dispatch.cursor != commit.dispatch_cursor:
        raise AuditIntegrityError("semantic Review cold result changed its exact dispatched cursor")
    completed = _historical_snapshot(conn, sid, operation_id, cursor=commit.dispatch_cursor + 1)
    retained = decode_semantic_review_result(commit.result_json)
    if _review_result_outcome(retained, actual_disposition=commit.outcome) != commit.outcome:
        raise AuditIntegrityError("semantic Review cold result changed its original END disposition")
    if commit.outcome == "clean":
        if (
            type(retained.publication_binding) is not str
            or type(commit.pass_index) is not int
            or type(commit.pass_audit_message_id) is not str
            or not commit.pass_audit_message_id
        ):
            raise AuditIntegrityError("semantic Review cold result lost its original model pass locator")
        _require_original_clean_end_pass_audit(
            conn,
            session_id=sid,
            publication_binding=retained.publication_binding,
            pass_index=commit.pass_index,
            message_id=commit.pass_audit_message_id,
        )
    elif commit.pass_index is not None or commit.pass_audit_message_id is not None:
        raise AuditIntegrityError("blocked semantic Review cannot claim a clean model pass")
    event = canonical_json(
        {
            "owner": "composer_semantic_review",
            "transition": "review_result_retained",
            "semantic_effect_key": commit.semantic_effect_key,
            "owner_effect_key": review_effect_key,
            "result_chunk_key": effect.result_chunk_key,
            "dispatch_checkpoint_id": commit.dispatch_checkpoint_id,
            "dispatch_cursor": commit.dispatch_cursor,
            "action_key": commit.action_key,
            "outcome": commit.outcome,
        }
    )
    _journal_data(conn, sid, operation_id, cursor=completed.cursor, tag="checkpoint", payload=event)
    dispatch_root = decode_continuation_snapshot(dispatch.payload_json)
    reference = dispatch_root.semantic_frontier
    if reference is None or dispatch_root.active_action_json != installed_action_json(admission):
        raise AuditIntegrityError("semantic Review cold result lost its dispatched frontier/action")
    frontier_canonical = read_chunk(
        conn,
        session_id=sid,
        operation_id=operation_id,
        key=f"value:{reference.codec}:{reference.digest}",
    ).decode("utf-8", errors="strict")
    frontier = decode_frontier_chunk(PrivateChunk(reference, frontier_canonical))
    after_receipt = encode_frontier_chunk(
        record_action_receipt(
            frontier,
            ActionExecutionReceipt(commit.action_key, review_effect_key, "committed"),
            actual_owner_receipt_id=review_effect_key,
        )
    )
    retained_frontier = read_chunk(
        conn,
        session_id=sid,
        operation_id=operation_id,
        key=f"value:{after_receipt.reference.codec}:{after_receipt.reference.digest}",
    )
    if retained_frontier != after_receipt.canonical.encode("utf-8"):
        raise AuditIntegrityError("semantic Review cold result changed its exact committed action receipt")
    advanced = decode_continuation_snapshot(
        _effect_snapshot(dispatch.payload_json, effect_key=review_effect_key, disposition="committed", receipt_id=review_effect_key)
    )
    end = advanced.frames[-1]
    if type(end) is not EndFrame or end.phase != "review_needed":
        raise AuditIntegrityError("semantic Review cold result lost its original END publication owner")
    completed_root = decode_continuation_snapshot(completed.payload_json)
    completed_compose, completed_end = completed_root.frames[0], completed_root.frames[-1]
    if len(completed_root.frames) != 2 or type(completed_compose) is not ComposeFrame or type(completed_end) is not EndFrame:
        raise AuditIntegrityError("semantic Review cold result lost its complete captured END pair")
    rebound_candidate = candidate.model_copy(
        update={"state": completed_compose.state, "evidence": completed_compose.evidence, "iteration": completed_compose.iteration}
    )
    rebound_chunk = encode_end_candidate(rebound_candidate)
    candidate_canonical = read_chunk(
        conn,
        session_id=sid,
        operation_id=operation_id,
        key=f"value:{rebound_chunk.reference.codec}:{rebound_chunk.reference.digest}",
    ).decode("utf-8", errors="strict")
    if (
        completed_end.candidate != rebound_chunk.reference
        or decode_end_candidate(PrivateChunk(rebound_chunk.reference, candidate_canonical)) != rebound_candidate
        or completed_end.state != completed_compose.state
    ):
        raise AuditIntegrityError("semantic Review cold result changed its captured original candidate")
    state_canonical = read_chunk(
        conn,
        session_id=sid,
        operation_id=operation_id,
        key=f"value:{completed_compose.state.codec}:{completed_compose.state.digest}",
    ).decode("utf-8", errors="strict")
    if state_canonical != canonical_json(retained.result.state.to_dict()):
        raise AuditIntegrityError("semantic Review cold result changed its captured shared gate state")
    expected = advanced.model_copy(
        update={
            "cursor": completed.cursor,
            "semantic_frontier": after_receipt.reference,
            "active_action_json": None,
            "active_action_source": None,
            "frames": (
                completed_compose,
                end.model_copy(
                    update={
                        "phase": "publication_ready" if commit.outcome == "clean" else "repair_needed",
                        "state": completed_compose.state,
                        "candidate": rebound_chunk.reference,
                        "publication_binding": retained.publication_binding if commit.outcome == "clean" else None,
                    }
                ),
            ),
        }
    )
    if completed.payload_json != encode_continuation_snapshot(expected):
        raise AuditIntegrityError("semantic Review cold result changed its complete committed END checkpoint")
    original_advisor_call_id = admission.paid.audit_call.call_id
    if type(original_advisor_call_id) is not str or not original_advisor_call_id:
        raise AuditIntegrityError("semantic Review cold result lost its original paid call identity")
    return SemanticReviewCommitted(
        completed=completed,
        result=retained.result,
        publication_binding=retained.publication_binding if commit.outcome == "clean" else None,
        original_advisor_call_id=original_advisor_call_id,
        owner_effect_key=review_effect_key,
        action_key=commit.action_key,
        semantic_effect_key=commit.semantic_effect_key,
        outcome=commit.outcome,
    )
