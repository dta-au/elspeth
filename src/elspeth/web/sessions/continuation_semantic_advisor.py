"""Uncalled family-1 SQL lifecycle and original physical-source reconstruction.

No provider I/O occurs under this owner. A live phase, capacity reservation,
typed authority facades and supported successor cuts remain separate joins.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import Connection, Row, insert, select, update

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.contracts.readonly_json import JsonValue
from elspeth.web.composer.advisor_output import (
    ContinuationAdvisorDecision,
    ContinuationEvidenceRef,
    PrescribedContinuationAction,
    PrimaryContinuationAction,
    ReviewContinuationAction,
    parse_continuation_advisor_decision,
)
from elspeth.web.composer.audit import BufferingRecorder, llm_call_audit_envelope
from elspeth.web.composer.continuation import (
    AdvisorFrame,
    ComposeFrame,
    ContinuationActionSource,
    ContinuationFrameStack,
    ContinuationValue,
    EndFrame,
    PrivateChunkRef,
    ToolBatchFrame,
)
from elspeth.web.composer.continuation_advisor import (
    ContinuationAdvisorInvocation,
    encode_continuation_advisor_invocation,
)
from elspeth.web.composer.continuation_audit_buffer import decode_recorder_snapshot, encode_recorder_snapshot
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_response,
    decode_continuation_snapshot,
    decode_frontier_chunk,
    decode_retained_physical_audit_call,
    encode_continuation_snapshot,
    encode_continuation_text_response,
    encode_frontier_chunk,
    encode_private_chunk,
    verify_private_chunk,
)
from elspeth.web.composer.continuation_end_values import EndContinuationCandidate, decode_end_candidate, encode_end_candidate
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_evidence import decode_turn_evidence, encode_turn_evidence, state_from_evidence_json
from elspeth.web.composer.continuation_evidence_application_data import append_primary_call_to_evidence
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy
from elspeth.web.composer.continuation_progress import continuation_action_key, decode_prescribed_continuation_action, retain_advisor_action
from elspeth.web.composer.continuation_provider_outcomes import (
    decode_physical_audit_call,
    encode_physical_audit_call,
    require_successful_response_audit,
)
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.continuation_semantic_advisor import (
    PreparedSemanticAdvisorRequest,
    SemanticAdvisorIterationReturnInput,
    SemanticAdvisorReadInput,
    SemanticAdvisorRetainedInputs,
    SemanticAdvisorReviewInput,
    _SemanticAdvisorOptions,
    semantic_retained_json,
)
from elspeth.web.composer.continuation_semantic_advisor_outcome import SemanticAdvisorInvocationRaised, SemanticAdvisorInvocationReturned
from elspeth.web.composer.continuation_semantic_invocation import SemanticAdvisorInvocationData, make_semantic_advisor_invocation
from elspeth.web.composer.continuation_semantic_no_sdk import SemanticAdvisorNoSdkIssuer, SemanticAdvisorNoSdkRefusalPermit
from elspeth.web.composer.continuation_semantic_source import RawSemanticAdvisorSource
from elspeth.web.composer.prompt_guard import PromptGuardRefused
from elspeth.web.composer.provider_audit_source import ProviderAuditSource, provider_attempt_audit_drafts_for_source
from elspeth.web.composer.provider_context import capture_provider_context
from elspeth.web.composer.turn_effects import authored_state_hash
from elspeth.web.continuation_retention_memo import retention_stage_memo_scope
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import (
    ProviderAttempt,
    _undispatched_cancellation_event_id,
    _usage_entry_id,
    begin_provider_attempt_on_connection,
    token_usage_entry_from_llm_call_envelope,
)
from elspeth.web.sessions.composer_operations import ComposerOperationRecord, ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    ContinuationCASConflict,
    ContinuationEvent,
    ContinuationProviderBinding,
    PreparedContinuationProvider,
    StoredContinuationSnapshot,
    _effect_snapshot,
    _owed_continuation_p4_active,
    _require_current_continuation_running,
    append_complete_snapshot,
    bound_action_json,
    load_snapshot,
    prepare_snapshot_append,
    private_chunk_key,
    put_chunk,
    read_chunk,
    require_snapshot_cas,
)
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity
from elspeth.web.sessions.continuation_paid_overflow_quota import preflight_v4_requested_model
from elspeth.web.sessions.continuation_primary import read_original_committed_primary_response_data_on_connection
from elspeth.web.sessions.continuation_retained_capacity import (
    ProspectiveContinuationChunk,
    ProviderTerminalHoldSource,
    bind_provider_terminal_hold_attempt,
    reserve_provider_terminal_hold,
)
from elspeth.web.sessions.continuation_runtime import ContinuationRuntimeSource, read_runtime_source_on_connection
from elspeth.web.sessions.continuation_selection import load_private_chunks
from elspeth.web.sessions.continuation_semantic_paid_overflow_no_sdk import (
    SemanticNoSdkTailPlan,
    prepare_semantic_no_sdk_tail_plan,
)
from elspeth.web.sessions.continuation_semantic_paid_overflow_tail import (
    SemanticPaidOverflowTailPlan,
    prepare_semantic_paid_overflow_tail_plan,
)
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_continuation_provider_ordinals_table as ordinals
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as provider_holds
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from elspeth.web.sessions.time_normalization import restore_utc

if TYPE_CHECKING:
    from elspeth.web.composer.graph_recovery import GraphRecoveryContext
    from elspeth.web.sessions.continuation_iteration_return import IterationReturnCommitted
    from elspeth.web.sessions.continuation_semantic_actions import SemanticActionAdmission
    from elspeth.web.sessions.continuation_semantic_review_result import SemanticReviewCommitted
    from elspeth.web.sessions.service import SessionServiceImpl


class _ChunkBody(ContinuationValue):
    reference: PrivateChunkRef
    canonical: str


class _RawBody(ContinuationValue):
    checkpoint_id: str
    cursor: int
    selected_snapshot_json: str
    logical_json: str
    bootstrap_json: str
    actor_user_id: str
    original_request_json: str
    original_user_message: str
    candidate: str
    admission_policy_json: str
    admission_policy_hash: str
    plugin_policy_hash: str
    catalog_snapshot_hash: str
    plugin_snapshot_json: str
    tool_manifest_json: str
    env_ref_names: tuple[str, ...]
    source_chunks: tuple[_ChunkBody, ...]
    unselected_graph_proposal: None


class _PhysicalInput(ContinuationValue):
    source: ContinuationAdvisorInvocation
    frontier: PrivateChunkRef
    messages: tuple[dict[str, JsonValue], ...]
    options: _SemanticAdvisorOptions
    genuine_user: str | None


class _Input(ContinuationValue):
    schema_version: Literal["composer.semantic-provider-input.v1"] = "composer.semantic-provider-input.v1"
    raw_source_body_json: str
    invocation_json: str
    request_input_json: str
    policy_preimage_json: str
    physical_request_json: str


class _NoSdk(ContinuationValue):
    schema_version: Literal["composer.semantic-no-sdk-refusal.v1"] = "composer.semantic-no-sdk-refusal.v1"
    session_id: str
    operation_id: str
    effect_key: str
    input_sha256: str
    creator_sequence: int
    creator_generation: int
    source_checkpoint_id: str
    source_cursor: int
    receipt_key: str
    reason: Literal["guard_refused", "cost_refused", "chargeable_refused", "pre_sdk_deadline", "cancelled_before_sdk"]
    original_failure_class: str
    undispatched_attempt_id: str | None
    undispatched_model: str | None
    guard_audit_id: str | None
    guard_disposition: str | None


class SemanticAdvisorProviderOutcome(StrEnum):
    """Retained semantic provider dispositions, distinct from row outcomes."""

    SUCCESS = "success"
    PAID_INVALID = "paid_invalid"
    CREDENTIAL_WITHHELD = "credential_withheld"


class _Outcome(ContinuationValue):
    schema_version: Literal["composer.semantic-provider-outcome.v1"] = "composer.semantic-provider-outcome.v1"
    effect_key: str
    input_sha256: str
    receipt_key: str
    creator_sequence: int
    creator_generation: int
    source_checkpoint_id: str
    source_cursor: int
    outcome: SemanticAdvisorProviderOutcome
    audit_call_json: str
    response: PrivateChunkRef | None
    recorder: PrivateChunkRef
    decision_json: str | None
    original_failure_class: str | None


@dataclass(frozen=True, slots=True)
class SemanticAdvisorNoSdkRefusalReceipt:
    snapshot: StoredContinuationSnapshot
    effect_key: str
    receipt_key: str
    result_chunk_key: str


@dataclass(frozen=True, slots=True)
class SemanticAdvisorProviderResult:
    snapshot: StoredContinuationSnapshot
    effect_key: str
    audit_call: ComposerLLMCall
    response: PrivateChunk | None
    recorder: PrivateChunk
    decision: ContinuationAdvisorDecision | None
    outcome: SemanticAdvisorProviderOutcome
    original_error: BaseException | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class V4SemanticProviderAdmission:
    attempt: ProviderAttempt
    selected: StoredContinuationSnapshot
    hold: ProviderTerminalHoldSource
    tail: SemanticPaidOverflowTailPlan
    no_sdk_tail: SemanticNoSdkTailPlan


@dataclass(frozen=True, slots=True)
class SemanticAdvisorDecisionData:
    decision: ContinuationAdvisorDecision
    source: ContinuationActionSource
    action: PrescribedContinuationAction | None


@dataclass(frozen=True, slots=True)
class SemanticAdvisorPaidDecisionData:
    """Original paid DATA observed under current admission, never a writer grant."""

    selected: StoredContinuationSnapshot
    paid_root: ContinuationFrameStack
    invocation: SemanticAdvisorInvocationData
    decision: ContinuationAdvisorDecision
    decision_json: str
    decision_sha256: str
    source: ContinuationActionSource
    audit_call: ComposerLLMCall
    session_id: UUID
    operation_id: str
    claim_attempt: int
    operation_epoch: int
    creator_sequence: int
    creator_generation: int
    creator_operation_id: str
    creator_operation_epoch: int
    creator_lease_token: str = field(repr=False)


def _canonical(value: ContinuationValue) -> str:
    return canonical_json(value.model_dump(mode="json"))


def _decode[T: ContinuationValue](owner: type[T], canonical: str) -> T:
    try:
        value = owner.model_validate_json(canonical, strict=True)
    except (ValueError, TypeError, ValidationError) as error:
        raise AuditIntegrityError("Semantic SQL source violates its closed versioned codec") from error
    if _canonical(value) != canonical:
        raise AuditIntegrityError("Semantic SQL source changed its complete canonical body")
    return value


def _blob(conn: Connection, sid: str, operation: str, key: str, codec: str) -> str:
    actual = conn.execute(
        select(chunks.c.codec).where(chunks.c.session_id == sid, chunks.c.operation_id == operation, chunks.c.chunk_key == key)
    ).scalar_one_or_none()
    if actual != codec:
        raise AuditIntegrityError("Semantic source changed its exact original chunk role")
    try:
        return read_chunk(conn, session_id=sid, operation_id=operation, key=key).decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise AuditIntegrityError("Semantic source lost original strict UTF8 bytes") from error


def _ref(conn: Connection, sid: str, operation: str, reference: PrivateChunkRef) -> PrivateChunk:
    key = f"value:{reference.codec}:{reference.digest}"
    value = PrivateChunk(reference, _blob(conn, sid, operation, key, reference.codec))
    verify_private_chunk(reference, value.canonical)
    return value


def _current(conn: Connection, running: ComposerOperationRunning, selected: StoredContinuationSnapshot) -> ContinuationFrameStack:
    if type(selected) is not StoredContinuationSnapshot:
        raise AuditIntegrityError("Semantic SQL requires its exact selected root DATA")
    _require_current_continuation_running(conn, running)
    if _owed_continuation_p4_active(conn, running.session_operation_context):
        raise AuditIntegrityError("Semantic SQL cannot borrow owed P4 authority")
    actual = require_snapshot_cas(
        conn,
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.cursor,
    )
    if actual.payload_json != selected.payload_json:
        raise ContinuationCASConflict("Semantic SQL substituted its complete selected root")
    root = decode_continuation_snapshot(actual.payload_json)
    if (root.logical.session_id, root.logical.operation_id) != (str(running.claim.session_id), running.claim.operation_id):
        raise AuditIntegrityError("Semantic SQL changed its logical owner")
    return root


def _current_original_paid_finish(
    conn: Connection,
    prepared: PreparedContinuationProvider,
    selected: StoredContinuationSnapshot,
    outcome: SemanticAdvisorInvocationReturned | SemanticAdvisorInvocationRaised,
    issuer: SemanticAdvisorNoSdkIssuer,
) -> ContinuationFrameStack:
    """Only the original completed physical owner may finish after Stop/expiry.

    This is not an action admission or an owed-P4 capability. Original minted
    custody and the sealed actual outcome precede the narrower fenced read;
    all physical accounting, recorder and complete source checks still run
    before retention writes.
    """
    if type(issuer) is not SemanticAdvisorNoSdkIssuer or issuer.running is not prepared.binding.running:
        raise AuditIntegrityError("Semantic paid finish requires its original nominal issuer/running owner")
    issuer.require_original_paid_outcome(prepared, selected, outcome)
    observed = outcome.observation
    if not observed.sdk_entered or not observed.recorder_completed or observed.audit_call is None or observed.attempt is None:
        raise AuditIntegrityError("Semantic paid finish cannot borrow authority from a status-only observation")
    running = prepared.binding.running
    _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
    if _owed_continuation_p4_active(conn, running.session_operation_context):
        raise AuditIntegrityError("Semantic paid finish cannot borrow owed P4 authority")
    if type(selected) is not StoredContinuationSnapshot:
        raise AuditIntegrityError("Semantic paid finish requires its exact original selected DATA")
    actual = require_snapshot_cas(
        conn,
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.cursor,
    )
    if actual != selected:
        raise ContinuationCASConflict("Semantic paid finish substituted its complete original dispatched root")
    root = decode_continuation_snapshot(actual.payload_json)
    if (root.logical.session_id, root.logical.operation_id) != (str(running.claim.session_id), running.claim.operation_id):
        raise AuditIntegrityError("Semantic paid finish changed its exact logical owner")
    return root


def _current_original_no_sdk_finish(conn: Connection, permit: SemanticAdvisorNoSdkRefusalPermit) -> ContinuationFrameStack:
    """Finish only the original proved unentered/cancelled physical owner."""
    permit.issuer.require_refusal_permit(permit)
    running = permit.prepared.binding.running
    if permit.issuer.running is not running:
        raise AuditIntegrityError("Semantic no-SDK finish changed its original running owner")
    _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
    if _owed_continuation_p4_active(conn, running.session_operation_context):
        raise AuditIntegrityError("Semantic no-SDK finish cannot borrow owed P4 authority")
    actual = require_snapshot_cas(
        conn,
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        expected_checkpoint_id=permit.dispatched.checkpoint_id,
        expected_cursor=permit.dispatched.cursor,
    )
    if actual != permit.dispatched:
        raise ContinuationCASConflict("Semantic no-SDK finish substituted its complete original dispatched root")
    root = decode_continuation_snapshot(actual.payload_json)
    if (root.logical.session_id, root.logical.operation_id) != (str(running.claim.session_id), running.claim.operation_id):
        raise AuditIntegrityError("Semantic no-SDK finish changed its exact logical owner")
    return root


def _parent(root: ContinuationFrameStack) -> ComposeFrame:
    frame = root.frames[0]
    if type(frame) is not ComposeFrame or sum(type(item) is ComposeFrame for item in root.frames) != 1:
        raise AuditIntegrityError("Semantic SQL requires its one original Compose parent")
    return frame


def _advisor(root: ContinuationFrameStack) -> AdvisorFrame:
    frame = root.frames[-1]
    if (
        type(frame) is not AdvisorFrame
        or len(root.frames) not in (2, 3)
        or (len(root.frames) == 3 and type(root.frames[1]) not in (EndFrame, ToolBatchFrame))
    ):
        raise AuditIntegrityError("Semantic SQL requires its dedicated selected Advisor frame")
    _parent(root)
    return frame


@dataclass(frozen=True, slots=True)
class _SemanticEndSourceObservation:
    candidate: EndContinuationCandidate | None
    review: SemanticReviewCommitted | None = None
    iteration_return: IterationReturnCommitted | None = None


def _input_end_source_observation(
    conn: Connection, sid: str, operation: str, root: ContinuationFrameStack, *, checkpoint_id: str, cursor: int
) -> _SemanticEndSourceObservation:
    """Original complete input shape, including a genuine retained END reply.

    The historical PRIMARY reader proves its actual SDK response, creator,
    descriptor, accounting and source checkpoint; an End-shaped DTO cannot
    substitute its candidate or invent publication permission.
    """
    parent = _parent(root)
    if (
        root.pending_owner_results
        or root.pending_settlement_ids
        or root.active_action_json is not None
        or root.active_action_source is not None
        or any(item.disposition not in ("committed", "refused") for item in root.effect_frontier)
    ):
        raise AuditIntegrityError("Semantic input requires its complete quiescent unprescribed source")
    if len(root.frames) == 1:
        if (
            parent.phase != "action_ready"
            or parent.provider_effect_key is not None
            or parent.provider_input_sha256 is not None
            or parent.pending_response is not None
        ):
            raise AuditIntegrityError("Semantic Compose input lost its original quiescent action-ready source")
        return _SemanticEndSourceObservation(None)
    if len(root.frames) == 2 and type(root.frames[1]) is ToolBatchFrame:
        original_return = _returned_iteration_source_data(
            conn, sid, operation, StoredContinuationSnapshot(encode_continuation_snapshot(root), cursor, checkpoint_id)
        )
        if original_return.current_stage == "publication_ready":
            raise AuditIntegrityError("Semantic input cannot issue another paid assessment after its original consumed whole Stop")
        return _SemanticEndSourceObservation(None, iteration_return=original_return)
    if len(root.frames) != 2 or type(root.frames[1]) is not EndFrame:
        raise AuditIntegrityError("Semantic input cannot replace an unowned intermediate frame")
    end = root.frames[1]
    if (
        end.state != parent.state
        or parent.phase != "response_retained"
        or parent.pending_response is None
        or parent.provider_effect_key is None
        or parent.provider_input_sha256 is None
    ):
        raise AuditIntegrityError("Semantic END input lost its original unreviewed paid reply")
    candidate = decode_end_candidate(_ref(conn, sid, operation, end.candidate))
    if candidate.semantic_source is not None:
        from elspeth.web.sessions.continuation_semantic_actions import read_original_semantic_review_for_advisor_on_connection

        review = read_original_semantic_review_for_advisor_on_connection(
            conn, session_id=UUID(sid), operation_id=operation, original_candidate=candidate
        )
        expected_phase = "publication_ready" if review.outcome == "clean" else "repair_needed"
        expected_binding = review.publication_binding if review.outcome == "clean" else None
        if end.phase != expected_phase or end.publication_binding != expected_binding:
            raise AuditIntegrityError("Semantic END input substituted its actual clean/blocked Review disposition")
        return _SemanticEndSourceObservation(candidate, review)
    if end.phase != "review_needed" or end.publication_binding is not None:
        raise AuditIntegrityError("Semantic END input cannot invent an original shared Review result")
    original = read_original_committed_primary_response_data_on_connection(
        conn,
        session_id=UUID(sid),
        operation_id=operation,
        source_checkpoint_id=checkpoint_id,
        source_cursor=cursor,
        response=parent.pending_response,
    )
    response = decode_continuation_response(original)
    if response.tool_batch.calls:
        raise AuditIntegrityError("Semantic END reply cannot invent a candidate from a tool response")
    expected = EndContinuationCandidate(
        kind="reply",
        response=parent.pending_response,
        primary_effect_key=parent.provider_effect_key,
        primary_input_sha256=parent.provider_input_sha256,
        candidate_text=response.message.content or "",
        state=parent.state,
        evidence=parent.evidence,
        iteration=parent.iteration,
    )
    if candidate != expected:
        raise AuditIntegrityError("Semantic END input substituted its exact original paid candidate/source")
    return _SemanticEndSourceObservation(candidate)


def _input_end_source_data(
    conn: Connection, sid: str, operation: str, root: ContinuationFrameStack, *, checkpoint_id: str, cursor: int
) -> EndContinuationCandidate | None:
    return _input_end_source_observation(conn, sid, operation, root, checkpoint_id=checkpoint_id, cursor=cursor).candidate


def _call_id(call: ComposerLLMCall) -> str:
    value = call.call_id
    if type(value) is not str or not value:
        raise AuditIntegrityError("Semantic physical audit lost its actual original call identity")
    return value


def _effect(conn: Connection, running: ComposerOperationRunning, root: ContinuationFrameStack, key: str) -> Row[Any]:
    return _effect_data(conn, str(running.claim.session_id), running.claim.operation_id, root, key)


def _effect_data(conn: Connection, sid: str, operation: str, root: ContinuationFrameStack, key: str) -> Row[Any]:
    _parent(root)
    frame = _advisor(root)
    if frame.provider_effect_key != key or frame.provider_input_sha256 is None:
        raise AuditIntegrityError("Semantic SQL substituted its selected provider locator")
    row = conn.execute(
        select(effects).where(
            effects.c.session_id == sid,
            effects.c.operation_id == operation,
            effects.c.effect_key == key,
        )
    ).one_or_none()
    pieces = key.split(":")
    if (
        row is None
        or len(pieces) != 3
        or pieces[:2] != ["provider", "1"]
        or not pieces[2].isdigit()
        or str(int(pieces[2])) != pieces[2]
        or row.owner != "provider"
        or row.action_json is not None
        or row.input_sha256 != frame.provider_input_sha256
    ):
        raise AuditIntegrityError("Semantic SQL changed its family-1 effect/input identity")
    entries = tuple(item for item in root.effect_frontier if item.effect_id == key)
    if len(entries) != 1 or (entries[0].disposition, entries[0].receipt_id) != (row.disposition, row.receipt_key):
        raise AuditIntegrityError("Semantic selected root differs from its actual effect disposition")
    return row


def _descriptor(
    conn: Connection, running: ComposerOperationRunning, effect: Row[Any]
) -> tuple[_Input, SemanticAdvisorInvocationData, _PhysicalInput]:
    return _descriptor_data(conn, str(running.claim.session_id), running.claim.operation_id, effect)


def _descriptor_data(
    conn: Connection, sid: str, operation: str, effect: Row[Any]
) -> tuple[_Input, SemanticAdvisorInvocationData, _PhysicalInput]:
    value = _decode(
        _Input, _blob(conn, sid, operation, f"semantic-provider-input:{effect.input_sha256}", "composer.semantic-provider-input.v1")
    )
    body = _decode(_RawBody, value.raw_source_body_json)
    original = decode_continuation_snapshot(body.selected_snapshot_json)
    frame = _parent(original)
    if len(body.source_chunks) != 6:
        raise AuditIntegrityError("Semantic input lost its complete original quiescent source")
    if _blob(conn, sid, operation, f"snapshot:{body.checkpoint_id}", "snapshot.v2") != body.selected_snapshot_json:
        raise AuditIntegrityError("Semantic input substituted its original complete SQL checkpoint")
    retained = tuple(PrivateChunk(item.reference, item.canonical) for item in body.source_chunks)
    for chunk in retained:
        if _ref(conn, sid, operation, chunk.reference) != chunk:
            raise AuditIntegrityError("Semantic input changed its actual original source bytes")
    bootstrap = _ref(conn, sid, operation, frame.bootstrap)
    if bootstrap.canonical != body.bootstrap_json:
        raise AuditIntegrityError("Semantic input changed its original authenticated bootstrap")
    state, evidence, transcript, iteration, tools, frontier = retained
    raw = RawSemanticAdvisorSource(
        checkpoint_id=body.checkpoint_id,
        cursor=body.cursor,
        selected_snapshot_json=body.selected_snapshot_json,
        logical_json=body.logical_json,
        bootstrap=bootstrap,
        actor_user_id=body.actor_user_id,
        original_request_json=body.original_request_json,
        original_user_message=body.original_user_message,
        state=state,
        evidence=evidence,
        transcript=transcript,
        iteration=iteration,
        tools=tools,
        predecessor_frontier=frontier,
        proposed_frontier=None,
        candidate=body.candidate,
        admission_policy_json=body.admission_policy_json,
        admission_policy_hash=body.admission_policy_hash,
        plugin_policy_hash=body.plugin_policy_hash,
        catalog_snapshot_hash=body.catalog_snapshot_hash,
        plugin_snapshot_json=body.plugin_snapshot_json,
        tool_manifest_json=body.tool_manifest_json,
        env_ref_names=frozenset(body.env_ref_names),
        source_body_json=value.raw_source_body_json,
    )
    data = make_semantic_advisor_invocation(raw)
    physical_input = _decode(_PhysicalInput, value.physical_request_json)
    retained_inputs = _retained_inputs_data(conn, sid, operation, original, checkpoint_id=body.checkpoint_id, cursor=body.cursor)
    _require_retained_input_document(
        conn, sid, operation, original, physical_input, retained_inputs, env_ref_names=frozenset(body.env_ref_names)
    )
    if (
        value.invocation_json != canonical_json(data.invocation.model_dump(mode="json"))
        or value.request_input_json != data.request_input_json
        or value.policy_preimage_json != data.policy_preimage_json
        or physical_input.source != data.invocation
        or physical_input.frontier != raw.predecessor_frontier.reference
        or physical_input.genuine_user != raw.original_user_message
        or sha256(value.physical_request_json.encode("utf-8")).hexdigest() != effect.input_sha256
    ):
        raise AuditIntegrityError("Semantic physical descriptor changed its complete original preimage")
    return value, data, physical_input


def _append(
    conn: Connection,
    running: ComposerOperationRunning,
    selected: StoredContinuationSnapshot,
    root: ContinuationFrameStack,
    key: str,
    disposition: Literal["prepared", "dispatched", "committed", "refused", "unknown"],
    receipt: str | None,
    event: ContinuationEvent,
) -> StoredContinuationSnapshot:
    return append_complete_snapshot(
        conn,
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.cursor,
        snapshot_json=_effect_snapshot(encode_continuation_snapshot(root), effect_key=key, disposition=disposition, receipt_id=receipt),
        event_batch=(event,),
        handoff=False,
    )


def prepare_semantic_advisor_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    invocation: SemanticAdvisorInvocationData,
    request: PreparedSemanticAdvisorRequest,
    inventory: CurrentContinuationEnvInventory,
    no_sdk_issuer: SemanticAdvisorNoSdkIssuer,
) -> PreparedContinuationProvider:
    from elspeth.web.sessions.service import SessionServiceImpl

    if (
        type(selected) is not SelectedContinuationState
        or type(invocation) is not SemanticAdvisorInvocationData
        or type(request) is not PreparedSemanticAdvisorRequest
        or type(no_sdk_issuer) is not SemanticAdvisorNoSdkIssuer
    ):
        raise AuditIntegrityError("Semantic preparation requires its actual original owned values")
    stored = StoredContinuationSnapshot(encode_continuation_snapshot(selected.stack), selected.stack.cursor, selected.checkpoint_id)
    root = _current(conn, running, stored)
    if (
        root.active_action_json is not None
        or root.pending_owner_results
        or root.pending_settlement_ids
        or any(item.disposition in ("prepared", "dispatched", "unknown") for item in root.effect_frontier)
    ):
        raise ContinuationCASConflict("Semantic preparation would replace unresolved work")
    invocation.verify()
    request.verify()
    if (
        request.source is not invocation.invocation
        or request.frontier != invocation.frontier
        or invocation.source.selected_snapshot_json != stored.payload_json
        or invocation.source.checkpoint_id != selected.checkpoint_id
        or inventory.running is not running
        or no_sdk_issuer.running is not running
        or no_sdk_issuer.input_sha256 != request.request_sha256
        or no_sdk_issuer.original_checkpoint_id != selected.checkpoint_id
        or no_sdk_issuer.original_cursor != selected.stack.cursor
    ):
        raise AuditIntegrityError("Semantic preparation changed its complete source or original owners")
    no_sdk_issuer.require_original_clock()
    no_sdk_issuer.clock.require_preparation_time()
    resolved = read_runtime_source_on_connection(conn, running, selected=selected, inventory=inventory)
    if invocation.source.env_ref_names != resolved.evidence_env_ref_names:
        raise AuditIntegrityError("Semantic preparation changed its actual admitted source credential scope")
    if (resolved.bootstrap.actor_user_id, resolved.original.message, resolved.policy.policy_hash) != (
        invocation.source.actor_user_id,
        invocation.source.original_user_message,
        root.policy_hash,
    ):
        raise AuditIntegrityError("Semantic preparation changed its current authenticated source")
    service = request.owner._require_sessions_service()
    if (
        type(service) is not SessionServiceImpl
        or service._engine is not conn.engine
        or request.provider_owner.service is not service
        or request.provider_owner.required_work.coordinator is not no_sdk_issuer.work.coordinator
        or no_sdk_issuer.clock.policy != resolved.policy.actions.advisor
    ):
        raise AuditIntegrityError("Semantic preparation changed its actual service or required-work owners")
    physical_json = canonical_json(
        {
            "source": request.source.model_dump(mode="json"),
            "frontier": invocation.source.predecessor_frontier.reference.model_dump(mode="json"),
            "messages": request.prepared.evidence.messages,
            "options": {key: value for key, value in deep_thaw(request.options).items() if key != "api_key"},
            "genuine_user": request.prepared.genuine_user,
        }
    )
    if sha256(physical_json.encode("utf-8")).hexdigest() != request.request_sha256:
        raise AuditIntegrityError("Semantic preparation lost its original physical input SHA")
    retained_inputs = _retained_inputs_data(
        conn, str(running.claim.session_id), running.claim.operation_id, root, checkpoint_id=stored.checkpoint_id, cursor=stored.cursor
    )
    _require_retained_input_document(
        conn,
        str(running.claim.session_id),
        running.claim.operation_id,
        root,
        _decode(_PhysicalInput, physical_json),
        retained_inputs,
        env_ref_names=invocation.source.env_ref_names,
    )
    descriptor = _Input(
        raw_source_body_json=invocation.source.source_body_json,
        invocation_json=canonical_json(invocation.invocation.model_dump(mode="json")),
        request_input_json=invocation.request_input_json,
        policy_preimage_json=invocation.policy_preimage_json,
        physical_request_json=physical_json,
    )
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    with conn.begin_nested():
        ordinal = conn.execute(
            select(ordinals.c.next_ordinal).where(
                ordinals.c.session_id == sid, ordinals.c.operation_id == operation, ordinals.c.family == 1
            )
        ).scalar_one_or_none()
        number = 0 if ordinal is None else ordinal
        key = f"provider:1:{number}"
        conn.execute(
            insert(effects).values(
                session_id=sid,
                operation_id=operation,
                effect_key=key,
                input_sha256=request.request_sha256,
                owner="provider",
                disposition="prepared",
                creator_sequence=running.claim.segment_sequence,
                creator_generation=running.claim.ownership_generation,
                action_json=None,
            )
        )
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=f"semantic-provider-input:{request.request_sha256}",
            payload=_canonical(descriptor).encode("utf-8"),
            codec="composer.semantic-provider-input.v1",
        )
        review = encode_continuation_advisor_invocation(invocation.invocation)
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=private_chunk_key(review),
            payload=review.canonical.encode("utf-8"),
            codec=review.reference.codec,
        )
        if ordinal is None:
            conn.execute(insert(ordinals).values(session_id=sid, operation_id=operation, family=1, next_ordinal=1))
        else:
            conn.execute(
                update(ordinals)
                .where(ordinals.c.session_id == sid, ordinals.c.operation_id == operation, ordinals.c.family == 1)
                .values(next_ordinal=number + 1)
            )
        advisor = AdvisorFrame(
            phase="awaiting_response",
            review=review.reference,
            response=None,
            coverage_binding=invocation.invocation.coverage_binding,
            provider_effect_key=key,
            provider_input_sha256=request.request_sha256,
        )
        policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
        conn.execute(
            update(segments)
            .where(
                segments.c.session_id == sid,
                segments.c.operation_id == operation,
                segments.c.sequence == running.claim.segment_sequence,
                segments.c.generation == running.claim.ownership_generation,
            )
            .values(next_action_json=policy.actions.advisor.model_dump_json(), policy_sha256=root.policy_hash)
        )
        snapshot = _append(
            conn,
            running,
            stored,
            root.model_copy(update={"frames": (*root.frames, advisor)}),
            key,
            "prepared",
            None,
            ContinuationEvent("input_prepared", canonical_json({"effect_key": key, "input_sha256": request.request_sha256})),
        )
        result = PreparedContinuationProvider(ContinuationProviderBinding(running, key, 1, number, request.request_sha256), snapshot)
        no_sdk_issuer.clock.require_preparation_time()
        no_sdk_issuer.bind_request(request)
        no_sdk_issuer.note_prepared_candidate(result)
        return result


def begin_semantic_advisor_on_connection(
    conn: Connection, prepared: PreparedContinuationProvider, *, no_sdk_issuer: SemanticAdvisorNoSdkIssuer
) -> StoredContinuationSnapshot:
    if (
        type(prepared) is not PreparedContinuationProvider
        or type(no_sdk_issuer) is not SemanticAdvisorNoSdkIssuer
        or no_sdk_issuer.prepared_candidate is not prepared
    ):
        raise AuditIntegrityError("Semantic begin changed its actual prepared result/issuer")
    binding = prepared.binding
    root = _current(conn, binding.running, prepared.snapshot)
    effect = _effect(conn, binding.running, root, binding.effect_key)
    no_sdk_issuer.require_original_clock()
    if no_sdk_issuer.clock.policy != decode_continuation_policy(root.admission_policy_json, root.policy_hash).actions.advisor:
        raise AuditIntegrityError("Semantic begin changed its original admitted action allowance")
    no_sdk_issuer.clock.require_preparation_time()
    if (
        binding.family != 1
        or binding.input_sha256 != effect.input_sha256
        or effect.disposition != "prepared"
        or any(
            value is not None
            for value in (
                effect.result_chunk_key,
                effect.receipt_key,
                effect.provider_attempt_id,
                effect.physical_sequence,
                effect.physical_generation,
                effect.requested_model,
                effect.response_finished_at,
            )
        )
        or (effect.creator_sequence, effect.creator_generation)
        != (binding.running.claim.segment_sequence, binding.running.claim.ownership_generation)
    ):
        raise ContinuationCASConflict("Semantic begin cannot retry unresolved or foreign preparation")
    _inflight_source_data(conn, str(binding.running.claim.session_id), binding.running.claim.operation_id, prepared.snapshot, root)
    with conn.begin_nested():
        conn.execute(
            update(effects)
            .where(
                effects.c.session_id == str(binding.running.claim.session_id),
                effects.c.operation_id == binding.running.claim.operation_id,
                effects.c.effect_key == binding.effect_key,
            )
            .values(disposition="dispatched")
        )
        result = _append(
            conn,
            binding.running,
            prepared.snapshot,
            root,
            binding.effect_key,
            "dispatched",
            None,
            ContinuationEvent("may_dispatch", canonical_json({"effect_key": binding.effect_key})),
        )
        no_sdk_issuer.clock.require_preparation_time()
        no_sdk_issuer.note_dispatched_candidate(result)
        return result


def admit_v4_semantic_provider_on_connection(
    conn: Connection,
    service: SessionServiceImpl,
    prepared: PreparedContinuationProvider,
    *,
    model: str,
    no_sdk_issuer: SemanticAdvisorNoSdkIssuer,
) -> V4SemanticProviderAdmission:
    """Mint the original physical attempt, H and dispatch in one V4 SQL cut."""
    from elspeth.web.sessions.service import SessionServiceImpl

    if type(service) is not SessionServiceImpl or type(prepared) is not PreparedContinuationProvider:
        raise TypeError("V4 semantic admission requires original sessions service and prepared source")
    if type(no_sdk_issuer) is not SemanticAdvisorNoSdkIssuer or no_sdk_issuer.prepared_candidate is not prepared:
        raise AuditIntegrityError("V4 semantic admission changed its original no-SDK issuer")
    no_sdk_issuer.require_original_clock()
    no_sdk_issuer.clock.require_preparation_time()
    binding, selected = prepared.binding, prepared.snapshot
    running = binding.running
    root = _current(conn, running, selected)
    effect = _effect(conn, running, root, binding.effect_key)
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    if (
        type(policy) is not ContinuationAdmissionPolicyV4
        or binding.family != 1
        or binding.effect_key != f"provider:1:{binding.ordinal}"
        or binding.input_sha256 != effect.input_sha256
        or effect.disposition != "prepared"
        or effect.provider_attempt_id is not None
        or effect.result_chunk_key is not None
        or effect.receipt_key is not None
        or (effect.creator_sequence, effect.creator_generation) != (running.claim.segment_sequence, running.claim.ownership_generation)
    ):
        raise ContinuationCASConflict("V4 semantic admission lost its one prepared original effect")
    _inflight_source_data(conn, str(running.claim.session_id), running.claim.operation_id, selected, root)
    _stored_descriptor, _invocation, physical_input = _descriptor(conn, running, effect)
    requested_model = preflight_v4_requested_model(model)
    if physical_input.options["model"] != requested_model.value:
        raise AuditIntegrityError("V4 semantic model differs from its prepared original physical request")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    with conn.begin_nested():
        attempt = begin_provider_attempt_on_connection(
            conn,
            session_operation_context=running.session_operation_context,
            source="composer",
            policy=service._chargeable_admission_policy,
            run_id=None,
        )
        dispatched_json = _effect_snapshot(selected.payload_json, effect_key=binding.effect_key, disposition="dispatched", receipt_id=None)
        transition = ContinuationEvent("may_dispatch", canonical_json({"effect_key": binding.effect_key}))
        published_at = database_now(conn)
        append = prepare_snapshot_append(selected, snapshot_json=dispatched_json, events=(transition,), published_at=published_at)
        admitted = StoredContinuationSnapshot(append.snapshot_json, selected.cursor + 1, append.checkpoint_id)
        fence = running.session_operation_context.fence
        identity = PaidOverflowIdentity(
            session_id=sid,
            operation_id=operation,
            effect_key=binding.effect_key,
            attempt_id=attempt.attempt_id,
            session_operation_id=fence.operation_id,
            session_operation_epoch=fence.operation_epoch,
            ownership_generation=running.claim.ownership_generation,
            lease_sha256=sha256(fence.lease_token.encode("utf-8")).hexdigest(),
            policy_sha256=policy.policy_hash,
            source_checkpoint_id=selected.checkpoint_id,
            source_cursor=selected.cursor,
            source_snapshot_sha256=sha256(selected.payload_json.encode("utf-8")).hexdigest(),
        )
        tail = prepare_semantic_paid_overflow_tail_plan(
            identity=identity, admitted=admitted, creator_sequence=running.claim.segment_sequence
        )
        no_sdk_tail = prepare_semantic_no_sdk_tail_plan(
            admitted=admitted,
            attempt_id=attempt.attempt_id,
            requested_model=requested_model.value,
            input_sha256=effect.input_sha256,
            effect_key=effect.effect_key,
            creator_sequence=running.claim.segment_sequence,
            creator_generation=running.claim.ownership_generation,
        )
        reserved_bytes = max(tail.reserved_bytes, no_sdk_tail.reserved_bytes)
        contract_sha256 = sha256(
            canonical_json(
                {
                    "version": "composer.semantic-v4-combined-tail.v1",
                    "paid": tail.fallback_contract_sha256,
                    "no_sdk": no_sdk_tail.contract_sha256,
                    "reserved_bytes": reserved_bytes,
                }
            ).encode("utf-8")
        ).hexdigest()
        hold = ProviderTerminalHoldSource(
            session_id=sid,
            operation_id=operation,
            effect_key=binding.effect_key,
            policy_sha256=policy.policy_hash,
            allowance_bytes=policy.max_retained_payload_bytes,
            source_checkpoint_id=selected.checkpoint_id,
            source_cursor=selected.cursor,
            source_snapshot_sha256=identity.source_snapshot_sha256,
            source_input_sha256=effect.input_sha256,
            creator_sequence=running.claim.segment_sequence,
            creator_generation=running.claim.ownership_generation,
            claim_attempt=running.claim.attempt,
            session_operation_id=fence.operation_id,
            session_operation_epoch=fence.operation_epoch,
            lease_sha256=identity.lease_sha256,
            reserved_bytes=reserved_bytes,
            fallback_contract_sha256=contract_sha256,
        )
        prospective = (
            ProspectiveContinuationChunk(f"event:{admitted.cursor}", transition.payload_json.encode("utf-8"), "event.v2"),
            ProspectiveContinuationChunk(f"snapshot:{admitted.checkpoint_id}", append.snapshot_json.encode("utf-8"), "snapshot.v2"),
        )
        reserve_provider_terminal_hold(conn, source=hold, prospective=prospective, created_at=published_at)
        changed = conn.execute(
            update(effects)
            .where(
                effects.c.session_id == sid,
                effects.c.operation_id == operation,
                effects.c.effect_key == binding.effect_key,
                effects.c.disposition == "prepared",
                effects.c.provider_attempt_id.is_(None),
            )
            .values(
                disposition="dispatched",
                provider_attempt_id=attempt.attempt_id,
                physical_sequence=running.claim.segment_sequence,
                physical_generation=running.claim.ownership_generation,
                requested_model=requested_model.value,
            )
        )
        if changed.rowcount != 1:
            raise AuditIntegrityError("V4 semantic dispatch lost its original prepared effect CAS")
        bind_provider_terminal_hold_attempt(conn, source=hold, provider_attempt_id=attempt.attempt_id)
        stored = append_complete_snapshot(
            conn,
            session_id=sid,
            operation_id=operation,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.cursor,
            snapshot_json=append.snapshot_json,
            event_batch=append.events,
            handoff=False,
            prepared_append=append,
        )
        if stored != admitted:
            raise AuditIntegrityError("V4 semantic dispatch changed its reserved complete snapshot")
        no_sdk_issuer.clock.require_preparation_time()
        no_sdk_issuer.note_dispatched_candidate(stored)
        return V4SemanticProviderAdmission(attempt, stored, hold, tail, no_sdk_tail)


def _original_input(
    conn: Connection, running: ComposerOperationRunning, root: ContinuationFrameStack, effect: Row[Any]
) -> tuple[SemanticAdvisorInvocationData, _PhysicalInput]:
    return _original_input_data(conn, str(running.claim.session_id), running.claim.operation_id, root, effect)


def _original_input_data(
    conn: Connection, sid: str, operation: str, root: ContinuationFrameStack, effect: Row[Any]
) -> tuple[SemanticAdvisorInvocationData, _PhysicalInput]:
    _stored, data, physical_input = _descriptor_data(conn, sid, operation, effect)
    frame = root.frames[-1]
    if type(frame) is not AdvisorFrame:
        raise AuditIntegrityError("Semantic outcome lost its actual selected Advisor frame")
    original = decode_continuation_snapshot(data.source.selected_snapshot_json)
    if (
        root.logical != original.logical
        or root.mutation_ceiling != original.mutation_ceiling
        or root.policy_hash != original.policy_hash
        or root.admission_policy_json != original.admission_policy_json
        or root.provider_cost_policy != original.provider_cost_policy
        or frame.coverage_binding != data.invocation.coverage_binding
        or _ref(conn, sid, operation, frame.review) != encode_continuation_advisor_invocation(data.invocation)
    ):
        raise AuditIntegrityError("Semantic outcome changed its original policy, invocation or logical source")
    return data, physical_input


def _creator(conn: Connection, running: ComposerOperationRunning, effect: Row[Any]) -> Row[Any]:
    return _creator_data(conn, str(running.claim.session_id), running.claim.operation_id, effect)


def _creator_data(conn: Connection, sid: str, operation: str, effect: Row[Any]) -> Row[Any]:
    row = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation,
            attempts.c.sequence == effect.creator_sequence,
            attempts.c.generation == effect.creator_generation,
        )
    ).one_or_none()
    if row is None:
        raise AuditIntegrityError("Semantic result lost its immutable physical creator")
    return row


def _physical_source(
    conn: Connection, running: ComposerOperationRunning, effect: Row[Any], call: ComposerLLMCall, physical_input: _PhysicalInput
) -> None:
    _physical_source_data(conn, str(running.claim.session_id), running.claim.operation_id, effect, call, physical_input)


def _physical_source_data(
    conn: Connection, sid: str, operation: str, effect: Row[Any], call: ComposerLLMCall, physical_input: _PhysicalInput
) -> None:
    if type(call) is not ComposerLLMCall or call.call_id is None:
        raise AuditIntegrityError("Semantic retention requires its actual original paid audit")
    # Restore the original JSON array after immutable descriptor decoding;
    # the physical context owner deliberately refuses Python tuple carriers.
    wire = list(physical_input.messages)
    options = physical_input.options
    if (
        call.model_requested != options["model"]
        or call.max_completion_tokens_requested != options["max_tokens"]
        or call.temperature != options.get("temperature")
        or call.seed != options.get("seed")
        or call.messages_hash != stable_hash(wire)
        or call.tools_spec_hash is not None
        or call.private_request != capture_provider_context(messages=wire, tools=None)
    ):
        raise AuditIntegrityError("Semantic physical audit changed its exact original physical request")
    creator = _creator_data(conn, sid, operation, effect)
    actual = conn.execute(select(physical).where(physical.c.attempt_id == call.call_id)).one_or_none()
    v4_hold = conn.execute(
        select(provider_holds.c.provider_attempt_id).where(
            provider_holds.c.session_id == sid,
            provider_holds.c.operation_id == operation,
            provider_holds.c.effect_key == effect.effect_key,
        )
    ).scalar_one_or_none()
    principal = conn.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == sid)).scalar_one()
    if (
        actual is None
        or (
            actual.session_id,
            actual.source,
            actual.run_id,
            actual.operation_id,
            actual.operation_epoch,
            actual.lease_token,
            actual.identity_id,
        )
        != (
            sid,
            "composer",
            None,
            creator.session_operation_id,
            creator.session_operation_epoch,
            creator.session_operation_lease_token,
            principal,
        )
        or actual.settled_at is None
        or (
            (restore_utc(actual.started_at) != call.started_at and v4_hold is None)
            or (v4_hold is not None and (v4_hold != call.call_id or effect.provider_attempt_id != call.call_id))
        )
    ):
        raise AuditIntegrityError("Semantic result changed its original creator or actual settled attempt")
    entry = token_usage_entry_from_llm_call_envelope(llm_call_audit_envelope(call))
    if entry is None:
        raise AuditIntegrityError("Semantic paid result lacks its original accounting projection")
    entry_id = _usage_entry_id(session_id=actual.session_id, source="composer", run_id=None, call_id=call.call_id)
    ledger = conn.execute(select(usage).where(usage.c.entry_id == entry_id)).one_or_none()
    if (
        ledger is None
        or actual.ledger_entry_id != entry_id
        or (
            ledger.identity_id,
            ledger.session_id,
            ledger.source,
            ledger.run_id,
            ledger.model,
            ledger.prompt_tokens,
            ledger.completion_tokens,
            ledger.cached_prompt_tokens,
            ledger.reasoning_tokens,
            restore_utc(ledger.recorded_at),
        )
        != (
            actual.identity_id,
            actual.session_id,
            "composer",
            None,
            entry.model,
            entry.prompt_tokens,
            entry.completion_tokens,
            entry.cached_prompt_tokens,
            entry.reasoning_tokens,
            entry.recorded_at,
        )
    ):
        raise AuditIntegrityError("Semantic paid result changed its original complete ledger")
    source = ProviderAuditSource(actual.session_id, actual.operation_id, actual.operation_epoch)
    expected = tuple(
        canonical_json(item)
        for draft in provider_attempt_audit_drafts_for_source(call, source)
        if draft.tool_calls is not None
        for item in draft.tool_calls
    )
    rows = conn.execute(
        select(messages.c.tool_calls).where(
            messages.c.session_id == actual.session_id, messages.c.role == "audit", messages.c.writer_principal == "compose_loop"
        )
    ).all()
    retained = tuple(canonical_json(item) for row in rows if row.tool_calls is not None for item in row.tool_calls)
    if any(retained.count(item) != 1 for item in expected):
        raise AuditIntegrityError("Semantic paid result lost its complete original public/private audit cohort")


def _journal(conn: Connection, running: ComposerOperationRunning, *, cursor: int, tag: str, payload: str) -> None:
    _journal_data(conn, str(running.claim.session_id), running.claim.operation_id, cursor=cursor, tag=tag, payload=payload)


def _journal_data(conn: Connection, sid: str, operation: str, *, cursor: int, tag: str, payload: str) -> None:
    row = conn.execute(
        select(events).where(events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == cursor)
    ).one_or_none()
    if (
        row is None
        or row.tag != tag
        or read_chunk(conn, session_id=sid, operation_id=operation, key=row.payload_chunk_key) != payload.encode("utf-8")
    ):
        raise AuditIntegrityError("Semantic outcome lost its original exact journal cohort")


def _no_sdk_event(data: _NoSdk, key: str) -> str:
    return canonical_json(
        {
            "schema_version": data.schema_version,
            "effect_key": data.effect_key,
            "input_sha256": data.input_sha256,
            "receipt_key": data.receipt_key,
            "result_chunk_key": key,
        }
    )


def _undispatched_source(conn: Connection, running: ComposerOperationRunning, effect: Row[Any], data: _NoSdk) -> None:
    if (data.undispatched_attempt_id is None) != (data.undispatched_model is None):
        raise AuditIntegrityError("Semantic undispatched receipt lost its paired attempt/model")
    if data.undispatched_attempt_id is None:
        return
    creator = _creator(conn, running, effect)
    actual = conn.execute(select(physical).where(physical.c.attempt_id == data.undispatched_attempt_id)).one_or_none()
    entry_id = _usage_entry_id(session_id=data.session_id, source="composer", run_id=None, call_id=data.undispatched_attempt_id)
    ledger = conn.execute(select(usage).where(usage.c.entry_id == entry_id)).one_or_none()
    audit = conn.execute(
        select(messages).where(messages.c.id == _undispatched_cancellation_event_id(data.undispatched_attempt_id))
    ).one_or_none()
    principal = conn.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == data.session_id)).scalar_one()
    if (
        actual is None
        or ledger is None
        or audit is None
        or (
            actual.session_id,
            actual.source,
            actual.run_id,
            actual.operation_id,
            actual.operation_epoch,
            actual.lease_token,
            actual.identity_id,
            actual.ledger_entry_id,
        )
        != (
            data.session_id,
            "composer",
            None,
            creator.session_operation_id,
            creator.session_operation_epoch,
            creator.session_operation_lease_token,
            principal,
            entry_id,
        )
        or actual.settled_at is None
        or (
            ledger.identity_id,
            ledger.session_id,
            ledger.source,
            ledger.run_id,
            ledger.model,
            ledger.prompt_tokens,
            ledger.completion_tokens,
            ledger.cached_prompt_tokens,
            ledger.reasoning_tokens,
        )
        != (principal, data.session_id, "composer", None, data.undispatched_model, 0, 0, 0, 0)
        or (
            audit.session_id,
            audit.role,
            audit.writer_principal,
            audit.raw_content,
            audit.tool_calls,
            audit.composition_state_id,
            audit.tool_call_id,
            audit.parent_assistant_id,
        )
        != (data.session_id, "audit", "compose_loop", None, None, None, None, None)
        or restore_utc(audit.created_at) != restore_utc(ledger.recorded_at)
        or restore_utc(audit.created_at) != restore_utc(actual.settled_at)
    ):
        raise AuditIntegrityError("Semantic no-SDK receipt lacks actual successful zero-use settlement")
    expected = canonical_json(
        {
            "kind": "provider_attempt.cancelled_before_dispatch",
            "attempt_id": data.undispatched_attempt_id,
            "source": "composer",
            "run_id": None,
            "requested_model": data.undispatched_model,
            "operation_id": creator.session_operation_id,
            "operation_epoch": creator.session_operation_epoch,
            "lease_token_sha256": sha256(actual.lease_token.encode("utf-8")).hexdigest(),
        }
    )
    if audit.content != expected:
        raise AuditIntegrityError("Semantic undispatched settlement changed its actual audit")


def _guard_refusal_source(conn: Connection, running: ComposerOperationRunning, effect: Row[Any], data: _NoSdk) -> None:
    if data.reason != "guard_refused":
        if data.guard_audit_id is not None or data.guard_disposition is not None:
            raise AuditIntegrityError("Semantic non-guard refusal cannot claim a guard audit")
        return
    if data.guard_audit_id is None or data.guard_disposition is None or data.guard_disposition == "allow":
        raise AuditIntegrityError("Semantic guard refusal lost its original actual audit identity")
    creator = _creator(conn, running, effect)
    audits = conn.execute(
        select(messages.c.tool_calls).where(
            messages.c.session_id == data.session_id, messages.c.role == "audit", messages.c.writer_principal == "compose_loop"
        )
    ).all()
    matches = tuple(
        item["check"]
        for row in audits
        if row.tool_calls is not None
        for item in row.tool_calls
        if item.get("_kind") == "prompt_guard_audit" and item["check"]["audit_id"] == data.guard_audit_id
    )
    if len(matches) != 1 or (
        matches[0]["role"],
        matches[0]["disposition"],
        matches[0]["operation_id"],
        matches[0]["operation_epoch"],
    ) != ("advisor", data.guard_disposition, creator.session_operation_id, creator.session_operation_epoch):
        raise AuditIntegrityError("Semantic no-SDK refusal changed its actual original guarded disposition")


def refuse_semantic_advisor_no_sdk_on_connection(
    conn: Connection, permit: SemanticAdvisorNoSdkRefusalPermit
) -> SemanticAdvisorNoSdkRefusalReceipt:
    if type(permit) is not SemanticAdvisorNoSdkRefusalPermit:
        raise TypeError("Semantic no-SDK closure requires its one original nominal permit")
    permit.issuer.require_refusal_permit(permit)
    binding = permit.prepared.binding
    running = binding.running
    root = _current_original_no_sdk_finish(conn, permit)
    effect = _effect(conn, running, root, binding.effect_key)
    _original_input(conn, running, root, effect)
    if (
        effect.disposition != "dispatched"
        or any(
            value is not None
            for value in (
                effect.provider_attempt_id,
                effect.physical_sequence,
                effect.physical_generation,
                effect.requested_model,
                effect.response_finished_at,
                effect.receipt_key,
                effect.result_chunk_key,
            )
        )
        or (effect.creator_sequence, effect.creator_generation) != (running.claim.segment_sequence, running.claim.ownership_generation)
    ):
        raise AuditIntegrityError("Semantic no-SDK closure cannot relabel entered or foreign work")
    data = _NoSdk(
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        effect_key=binding.effect_key,
        input_sha256=binding.input_sha256,
        creator_sequence=effect.creator_sequence,
        creator_generation=effect.creator_generation,
        source_checkpoint_id=permit.dispatched.checkpoint_id,
        source_cursor=permit.dispatched.cursor,
        receipt_key=permit.receipt_key,
        reason=permit.reason.value,
        original_failure_class=type(permit.original_failure).__name__,
        undispatched_attempt_id=permit.undispatched_attempt_id,
        undispatched_model=permit.undispatched_model,
        guard_audit_id=permit.original_failure.audit_id if type(permit.original_failure) is PromptGuardRefused else None,
        guard_disposition=permit.original_failure.disposition if type(permit.original_failure) is PromptGuardRefused else None,
    )
    _undispatched_source(conn, running, effect, data)
    _guard_refusal_source(conn, running, effect, data)
    chunk = encode_private_chunk(codec="composer.semantic-no-sdk-refusal.v1", canonical=_canonical(data))
    key = private_chunk_key(chunk)
    with conn.begin_nested():
        put_chunk(
            conn,
            session_id=data.session_id,
            operation_id=data.operation_id,
            key=key,
            payload=chunk.canonical.encode("utf-8"),
            codec=chunk.reference.codec,
        )
        conn.execute(
            update(effects)
            .where(
                effects.c.session_id == data.session_id,
                effects.c.operation_id == data.operation_id,
                effects.c.effect_key == data.effect_key,
            )
            .values(disposition="refused", receipt_key=data.receipt_key, result_chunk_key=key)
        )
        advisor = root.frames[-1].model_copy(update={"phase": "response_retained", "response": chunk.reference})
        selected = _append(
            conn,
            running,
            permit.dispatched,
            root.model_copy(update={"frames": (*root.frames[:-1], advisor)}),
            data.effect_key,
            "refused",
            data.receipt_key,
            ContinuationEvent("effect_refused", _no_sdk_event(data, key)),
        )
        return SemanticAdvisorNoSdkRefusalReceipt(selected, data.effect_key, data.receipt_key, key)


def close_semantic_advisor_no_sdk_on_connection(conn: Connection, permit: SemanticAdvisorNoSdkRefusalPermit) -> ComposerOperationRecord:
    """Retain the original no-SDK receipt and failed parent atomically."""
    from elspeth.web.coordination.composer_operation_authority import (
        _read,
        _select_failure_on_connection,
        settle_composer_operation_on_connection,
    )
    from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error

    if type(permit) is not SemanticAdvisorNoSdkRefusalPermit:
        raise AuditIntegrityError("Semantic terminal no-SDK close requires its original permit")
    running = permit.prepared.binding.running
    with conn.begin_nested():
        receipt = refuse_semantic_advisor_no_sdk_on_connection(conn, permit)
        parent = _read(conn, running.claim.session_id, running.claim.operation_id)
        if parent is None or parent.status != "running":
            raise AuditIntegrityError("Semantic terminal no-SDK close lost its actual current parent")
        failure = _select_failure_on_connection(
            parent, project_composer_operation_error(permit.original_failure, request_id=parent.request_id), authoritative_failure=False
        )
        terminal = settle_composer_operation_on_connection(conn, running, outcome=failure, authoritative_failure=True)
        if terminal.status != "failed" or terminal.journal_cursor != receipt.snapshot.cursor + 1:
            raise AuditIntegrityError("Semantic no-SDK close lost its atomic receipt/failed terminal")
        return terminal


def read_semantic_advisor_no_sdk_refusal_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: StoredContinuationSnapshot, effect_key: str
) -> SemanticAdvisorNoSdkRefusalReceipt:
    root = _current(conn, running, selected)
    effect = _effect(conn, running, root, effect_key)
    _original_input(conn, running, root, effect)
    if effect.disposition != "refused" or _advisor(root).phase != "response_retained" or effect.result_chunk_key is None:
        raise AuditIntegrityError("Semantic cold no-SDK read requires actual original refusal")
    canonical = _blob(
        conn, str(running.claim.session_id), running.claim.operation_id, effect.result_chunk_key, "composer.semantic-no-sdk-refusal.v1"
    )
    data = _decode(_NoSdk, canonical)
    chunk = encode_private_chunk(codec="composer.semantic-no-sdk-refusal.v1", canonical=canonical)
    if (
        private_chunk_key(chunk) != effect.result_chunk_key
        or _advisor(root).response != chunk.reference
        or (
            data.session_id,
            data.operation_id,
            data.effect_key,
            data.input_sha256,
            data.receipt_key,
            data.creator_sequence,
            data.creator_generation,
        )
        != (
            str(running.claim.session_id),
            running.claim.operation_id,
            effect_key,
            effect.input_sha256,
            effect.receipt_key,
            effect.creator_sequence,
            effect.creator_generation,
        )
        or selected.cursor != data.source_cursor + 1
        or any(
            value is not None
            for value in (
                effect.provider_attempt_id,
                effect.physical_sequence,
                effect.physical_generation,
                effect.requested_model,
                effect.response_finished_at,
            )
        )
    ):
        raise AuditIntegrityError("Semantic cold refusal changed its original complete effect/frame identity")
    _creator(conn, running, effect)
    original = _blob(conn, data.session_id, data.operation_id, f"snapshot:{data.source_checkpoint_id}", "snapshot.v2")
    source = decode_continuation_snapshot(original)
    if (
        source.cursor != data.source_cursor
        or _advisor(source).provider_effect_key != effect_key
        or _advisor(source).provider_input_sha256 != effect.input_sha256
        or _advisor(source).response is not None
        or _advisor(source).phase != "awaiting_response"
    ):
        raise AuditIntegrityError("Semantic refusal lost its original dispatched root")
    source_entries = tuple(item for item in source.effect_frontier if item.effect_id == effect_key)
    if len(source_entries) != 1 or (source_entries[0].disposition, source_entries[0].receipt_id) != ("dispatched", None):
        raise AuditIntegrityError("Semantic refusal lost its original dispatched frontier")
    advisor = _advisor(source).model_copy(update={"phase": "response_retained", "response": chunk.reference})
    expected = decode_continuation_snapshot(
        _effect_snapshot(
            encode_continuation_snapshot(source.model_copy(update={"frames": (*source.frames[:-1], advisor)})),
            effect_key=effect_key,
            disposition="refused",
            receipt_id=data.receipt_key,
        )
    )
    if root != expected.model_copy(update={"cursor": selected.cursor}):
        raise AuditIntegrityError("Semantic no-SDK receipt changed its complete original source transition")
    _journal(conn, running, cursor=data.source_cursor + 1, tag="effect_refused", payload=_no_sdk_event(data, effect.result_chunk_key))
    _undispatched_source(conn, running, effect, data)
    _guard_refusal_source(conn, running, effect, data)
    return SemanticAdvisorNoSdkRefusalReceipt(selected, effect_key, effect.receipt_key, effect.result_chunk_key)


@dataclass(frozen=True, slots=True)
class _ExtendedSemanticEvidence:
    evidence: PrivateChunk
    applied_batch: PrivateChunk | None


def _extended_evidence(
    conn: Connection, running: ComposerOperationRunning, data: SemanticAdvisorInvocationData, call: ComposerLLMCall, recorder: PrivateChunk
) -> _ExtendedSemanticEvidence:
    return _extended_evidence_data(conn, str(running.claim.session_id), running.claim.operation_id, data, call, recorder)


def _extended_evidence_data(
    conn: Connection, sid: str, operation: str, data: SemanticAdvisorInvocationData, call: ComposerLLMCall, recorder: PrivateChunk
) -> _ExtendedSemanticEvidence:
    from elspeth.web.composer.continuation_evidence_application_data import require_selected_applied_batch_chunk
    from elspeth.web.composer.continuation_iteration_transitions import applied_batch_cut

    original = decode_continuation_snapshot(data.source.selected_snapshot_json)
    admitted = decode_turn_evidence(
        data.source.evidence,
        env_ref_names=data.source.env_ref_names,
        source_chunks=load_private_chunks(conn, session_id=sid, operation_id=operation, stack=original),
    )
    snapshot = decode_recorder_snapshot(recorder)
    if not snapshot.llm_calls or snapshot.llm_calls[-1] != call:
        raise AuditIntegrityError("Semantic evidence extension changed its one original physical call")
    # This installed pure graph operation extends one actual Composer call
    # atom; the semantic SQL owner above supplies its independent paid proof.
    extended = append_primary_call_to_evidence(
        admitted, call=snapshot.llm_calls[-1], recorder=snapshot, env_ref_names=data.source.env_ref_names
    )
    applied: PrivateChunk | None = None
    if len(original.frames) == 2 and type(original.frames[1]) is ToolBatchFrame:
        applied_batch_cut(original.frames)
        entry = extended.snapshot.application_entry
        if entry is None:
            raise AuditIntegrityError("Semantic paid applied source lost its actual extended application graph")
        applied = encode_private_chunk(
            codec="composer.applied-p4-batch.v2", canonical=entry.graph_json, env_ref_names=data.source.env_ref_names
        )
        require_selected_applied_batch_chunk(applied, evidence=extended, env_ref_names=data.source.env_ref_names)
    return _ExtendedSemanticEvidence(encode_turn_evidence(extended, env_ref_names=data.source.env_ref_names), applied)


def _paid_evidence_root(
    root: ContinuationFrameStack, parent: ComposeFrame, advisor: AdvisorFrame, extended: _ExtendedSemanticEvidence
) -> ContinuationFrameStack:
    middle = root.frames[1:-1]
    if extended.applied_batch is not None:
        if len(middle) != 1 or type(middle[0]) is not ToolBatchFrame:
            raise AuditIntegrityError("Semantic paid application projection changed its one original ToolBatch")
        middle = (middle[0].model_copy(update={"accumulator": extended.applied_batch.reference}),)
    elif any(type(frame) is ToolBatchFrame for frame in middle):
        raise AuditIntegrityError("Semantic paid ToolBatch lost its exact extended application companion")
    return root.model_copy(update={"frames": (parent, *middle, advisor)})


def _outcome_event(value: _Outcome, key: str) -> str:
    return canonical_json(
        {
            "schema_version": value.schema_version,
            "effect_key": value.effect_key,
            "input_sha256": value.input_sha256,
            "receipt_key": value.receipt_key,
            "result_chunk_key": key,
            "outcome": value.outcome,
        }
    )


def retain_semantic_advisor_outcome_on_connection(
    conn: Connection,
    prepared: PreparedContinuationProvider,
    *,
    selected: StoredContinuationSnapshot,
    outcome: SemanticAdvisorInvocationReturned | SemanticAdvisorInvocationRaised,
    recorder: BufferingRecorder,
    issuer: SemanticAdvisorNoSdkIssuer,
) -> SemanticAdvisorProviderResult:
    if (
        type(prepared) is not PreparedContinuationProvider
        or type(outcome) not in (SemanticAdvisorInvocationReturned, SemanticAdvisorInvocationRaised)
        or type(recorder) is not BufferingRecorder
    ):
        raise AuditIntegrityError("Semantic retention requires its original owned physical outcome and recorder")
    running, binding = prepared.binding.running, prepared.binding
    root = _current_original_paid_finish(conn, prepared, selected, outcome, issuer)
    effect = _effect(conn, running, root, binding.effect_key)
    data, physical_input = _original_input(conn, running, root, effect)
    observed, call = outcome.observation, outcome.observation.audit_call
    if (
        effect.disposition != "dispatched"
        or _advisor(root).phase != "awaiting_response"
        or effect.result_chunk_key is not None
        or effect.provider_attempt_id is not None
        or effect.receipt_key is not None
        or (effect.creator_sequence, effect.creator_generation) != (running.claim.segment_sequence, running.claim.ownership_generation)
        or observed.source != data.invocation
        or observed.request_sha256 != binding.input_sha256
        or not observed.sdk_entered
        or not observed.recorder_completed
        or observed.attempt is None
        or call is None
        or observed.attempt.attempt_id != call.call_id
        or observed.attempt.started_at != call.started_at
    ):
        raise AuditIntegrityError("Semantic retention cannot infer an actual paid outcome from DATA or absence")
    if type(issuer) is not SemanticAdvisorNoSdkIssuer:
        raise AuditIntegrityError("Semantic paid retention requires its original nominal issuer")
    issuer.require_original_paid_outcome(prepared, selected, outcome)
    _physical_source(conn, running, effect, call, physical_input)
    kind: SemanticAdvisorProviderOutcome
    response: PrivateChunk | None = None
    decision: ContinuationAdvisorDecision | None = None
    if call.status is ComposerLLMCallStatus.SUCCESS:
        if (
            observed.message is None
            or type(observed.message.content) is not str
            or observed.provider_metadata is None
            or observed.decision is None
        ):
            raise AuditIntegrityError("Semantic successful outcome lost its original admitted response")
        require_successful_response_audit(
            call,
            observed.provider_metadata,
            attempt_id=observed.attempt.attempt_id,
            started_at=call.started_at,
            finished_at=call.finished_at,
        )
        decision = parse_continuation_advisor_decision(observed.message.content, actual_advisor_call_id=_call_id(call))
        if decision != observed.decision:
            raise AuditIntegrityError("Semantic decision differs from its one original physical response")
        response = encode_continuation_text_response(
            content=observed.message.content, provider_metadata=observed.provider_metadata, audit_call=call
        )
        if type(outcome) is SemanticAdvisorInvocationReturned and (
            outcome.result.response != response or outcome.result.audit_call != call or outcome.result.decision != decision
        ):
            raise AuditIntegrityError("Semantic returned sum changed its original physical result")
        kind = SemanticAdvisorProviderOutcome.SUCCESS
    elif call.status is ComposerLLMCallStatus.MALFORMED_RESPONSE and call.error_message == "credential_material_rejected":
        if any(
            value is not None for value in (observed.message, observed.provider_metadata, observed.decision, observed.tool_calls_present)
        ):
            raise AuditIntegrityError("Semantic withheld outcome exposed unadmitted provider fields")
        kind = SemanticAdvisorProviderOutcome.CREDENTIAL_WITHHELD
    elif call.status is ComposerLLMCallStatus.MALFORMED_RESPONSE:
        if observed.decision is not None:
            raise AuditIntegrityError("Semantic paid invalid result cannot carry an admitted decision")
        if observed.message is not None and type(observed.message.content) is str and observed.provider_metadata is not None:
            response = encode_continuation_text_response(
                content=observed.message.content, provider_metadata=observed.provider_metadata, audit_call=call
            )
        kind = SemanticAdvisorProviderOutcome.PAID_INVALID
    else:
        raise AuditIntegrityError("Entered semantic timeout/cancellation/unknown result cannot be closed or retried")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    original_parent = _parent(decode_continuation_snapshot(data.source.selected_snapshot_json))
    previous = decode_recorder_snapshot(_ref(conn, sid, operation, original_parent.audit_buffer))
    captured = recorder.snapshot_for_continuation(drained=previous.drained)
    if replace(previous, llm_calls=(*previous.llm_calls, call)) != captured or captured.llm_calls[-1] is not call:
        raise AuditIntegrityError("Semantic outcome changed its original complete recorder prefix/channels")
    record = encode_recorder_snapshot(captured)
    extended = _extended_evidence(conn, running, data, call, record)
    evidence = extended.evidence
    from uuid import uuid4

    receipt_key = str(uuid4())
    manifest = _Outcome(
        effect_key=binding.effect_key,
        input_sha256=binding.input_sha256,
        receipt_key=receipt_key,
        creator_sequence=effect.creator_sequence,
        creator_generation=effect.creator_generation,
        source_checkpoint_id=selected.checkpoint_id,
        source_cursor=selected.cursor,
        outcome=kind,
        audit_call_json=encode_physical_audit_call(call),
        response=None if response is None else response.reference,
        recorder=record.reference,
        decision_json=None if decision is None else canonical_json(decision.model_dump(mode="json")),
        original_failure_class=None if observed.original_failure is None else type(observed.original_failure).__name__,
    )
    manifest_json = _canonical(manifest)
    result_key = f"semantic-provider-outcome:{sha256(manifest_json.encode('utf-8')).hexdigest()}"
    disposition: Literal["committed", "refused"] = "committed" if kind is SemanticAdvisorProviderOutcome.SUCCESS else "refused"
    with conn.begin_nested():
        for chunk in (
            record,
            evidence,
            *((extended.applied_batch,) if extended.applied_batch is not None else ()),
            *((response,) if response is not None else ()),
        ):
            put_chunk(
                conn,
                session_id=sid,
                operation_id=operation,
                key=private_chunk_key(chunk),
                payload=chunk.canonical.encode("utf-8"),
                codec=chunk.reference.codec,
            )
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=result_key,
            payload=manifest_json.encode("utf-8"),
            codec="composer.semantic-provider-outcome.v1",
        )
        conn.execute(
            update(effects)
            .where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == binding.effect_key)
            .values(
                disposition=disposition,
                receipt_key=receipt_key,
                result_chunk_key=result_key,
                provider_attempt_id=call.call_id,
                physical_sequence=effect.creator_sequence,
                physical_generation=effect.creator_generation,
                requested_model=call.model_requested,
                response_finished_at=call.finished_at,
            )
        )
        parent = _parent(root).model_copy(
            update={"audit_buffer": record.reference, "evidence": evidence.reference, "advisor_calls": original_parent.advisor_calls + 1}
        )
        advisor = _advisor(root).model_copy(
            update={"phase": "response_retained", "response": None if response is None else response.reference}
        )
        result = _append(
            conn,
            running,
            selected,
            _paid_evidence_root(root, parent, advisor, extended),
            binding.effect_key,
            disposition,
            receipt_key,
            ContinuationEvent("response_retained", _outcome_event(manifest, result_key)),
        )
        return SemanticAdvisorProviderResult(
            result,
            binding.effect_key,
            call,
            response,
            record,
            decision,
            kind,
            outcome.error if type(outcome) is SemanticAdvisorInvocationRaised else None,
        )


def _paid_result(
    conn: Connection, running: ComposerOperationRunning, selected: StoredContinuationSnapshot, effect_key: str
) -> tuple[SemanticAdvisorProviderResult, SemanticAdvisorInvocationData, ContinuationFrameStack]:
    root = _current(conn, running, selected)
    return _paid_result_data(conn, str(running.claim.session_id), running.claim.operation_id, selected, root, effect_key)


def _paid_result_data(
    conn: Connection,
    sid: str,
    operation: str,
    selected: StoredContinuationSnapshot,
    root: ContinuationFrameStack,
    effect_key: str,
) -> tuple[SemanticAdvisorProviderResult, SemanticAdvisorInvocationData, ContinuationFrameStack]:
    """Reconstruct immutable physical DATA after a separate admission proof.

    Primitive identities identify stored facts. They grant no current claim,
    live session context, quota custody or writer permission.
    """
    effect = _effect_data(conn, sid, operation, root, effect_key)
    data, physical_input = _original_input_data(conn, sid, operation, root, effect)
    if effect.result_chunk_key is None or effect.receipt_key is None:
        raise AuditIntegrityError("Semantic cold result requires actual retained paid output")
    manifest_json = _blob(conn, sid, operation, effect.result_chunk_key, "composer.semantic-provider-outcome.v1")
    manifest = _decode(_Outcome, manifest_json)
    call = decode_physical_audit_call(manifest.audit_call_json)
    expected_key = f"semantic-provider-outcome:{sha256(manifest_json.encode('utf-8')).hexdigest()}"
    if (
        effect.result_chunk_key != expected_key
        or (
            manifest.effect_key,
            manifest.input_sha256,
            manifest.receipt_key,
            manifest.creator_sequence,
            manifest.creator_generation,
        )
        != (effect_key, effect.input_sha256, effect.receipt_key, effect.creator_sequence, effect.creator_generation)
        or (
            effect.provider_attempt_id,
            effect.physical_sequence,
            effect.physical_generation,
            effect.requested_model,
        )
        != (call.call_id, effect.creator_sequence, effect.creator_generation, call.model_requested)
        or effect.response_finished_at is None
        or restore_utc(effect.response_finished_at) != call.finished_at
        or effect.disposition != ("committed" if manifest.outcome is SemanticAdvisorProviderOutcome.SUCCESS else "refused")
    ):
        raise AuditIntegrityError("Semantic cold result changed its original complete physical/effect identity")
    _physical_source_data(conn, sid, operation, effect, call, physical_input)
    record = _ref(conn, sid, operation, manifest.recorder)
    if record.reference.codec != "composer.audit-buffer.v2":
        raise AuditIntegrityError("Semantic cold result changed its recorder source role")
    captured = decode_recorder_snapshot(record)
    original_parent = _parent(decode_continuation_snapshot(data.source.selected_snapshot_json))
    previous = decode_recorder_snapshot(_ref(conn, sid, operation, original_parent.audit_buffer))
    if not captured.llm_calls or captured.llm_calls[-1] != call or replace(previous, llm_calls=(*previous.llm_calls, call)) != captured:
        raise AuditIntegrityError("Semantic cold result changed its complete original recorder prefix")
    call = captured.llm_calls[-1]
    extended = _extended_evidence_data(conn, sid, operation, data, call, record)
    evidence = extended.evidence
    if extended.applied_batch is not None and _ref(conn, sid, operation, extended.applied_batch.reference) != extended.applied_batch:
        raise AuditIntegrityError("Semantic cold paid application changed its exact extended graph companion")
    expected_parent = original_parent.model_copy(
        update={"audit_buffer": record.reference, "evidence": evidence.reference, "advisor_calls": original_parent.advisor_calls + 1}
    )
    if (
        root.frames[0] != expected_parent
        or _ref(conn, sid, operation, expected_parent.evidence) != evidence
        or _advisor(root).response != manifest.response
    ):
        raise AuditIntegrityError("Semantic cold result changed its complete selected parent/response source")
    response = None if manifest.response is None else _ref(conn, sid, operation, manifest.response)
    decision: ContinuationAdvisorDecision | None = None
    if manifest.outcome is SemanticAdvisorProviderOutcome.SUCCESS:
        if response is None or response.reference.codec != "composer.response.v1" or call.status is not ComposerLLMCallStatus.SUCCESS:
            raise AuditIntegrityError("Semantic decision requires its genuine original successful physical response")
        decoded = decode_continuation_response(response)
        require_successful_response_audit(
            call, decoded.provider_metadata, attempt_id=_call_id(call), started_at=call.started_at, finished_at=call.finished_at
        )
        if type(decoded.message.content) is not str:
            raise AuditIntegrityError("Semantic decision lost its complete original response text")
        decision = parse_continuation_advisor_decision(decoded.message.content, actual_advisor_call_id=_call_id(call))
        if (
            decision is None
            or manifest.decision_json != canonical_json(decision.model_dump(mode="json"))
            or decode_retained_physical_audit_call(response) != call
        ):
            raise AuditIntegrityError("Semantic cold decision changed its one original response/call ID")
    elif manifest.decision_json is not None or call.status is not ComposerLLMCallStatus.MALFORMED_RESPONSE:
        raise AuditIntegrityError("Semantic refused paid output cannot carry an installed decision")
    elif manifest.outcome is SemanticAdvisorProviderOutcome.CREDENTIAL_WITHHELD and (
        response is not None or call.error_message != "credential_material_rejected"
    ):
        raise AuditIntegrityError("Semantic credential-withheld result exposed provider fields")
    elif response is not None and decode_retained_physical_audit_call(response) != call:
        raise AuditIntegrityError("Semantic invalid response changed its actual paid audit")
    frame = _advisor(root)
    from elspeth.web.sessions.continuation_semantic_recovery import SemanticRecoverySource, semantic_recovery_source

    if decode_continuation_snapshot(selected.payload_json) != root:
        raise AuditIntegrityError("Semantic cold result changed its complete selected root")
    selected_source = semantic_recovery_source(conn, sid, operation, selected)
    original_selected = selected_source.snapshot
    recoveries: tuple[SemanticRecoverySource, ...]
    if frame.phase not in ("response_retained", "decision_ready"):
        raise AuditIntegrityError("Semantic cold result moved outside its supported exact selected cut")
    if frame.phase == "decision_ready":
        installed_event = conn.execute(
            select(events.c.tag).where(
                events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == original_selected.cursor
            )
        ).scalar_one_or_none()
        if installed_event != "checkpoint":
            raise AuditIntegrityError("Semantic cold decision lost its original installation event")
        predecessor = _historical_snapshot(conn, sid, operation, cursor=original_selected.cursor - 1)
        paid_source = semantic_recovery_source(conn, sid, operation, predecessor)
        recoveries = (paid_source, selected_source)
    else:
        paid_source = selected_source
        recoveries = (selected_source,)
    if paid_source.snapshot.cursor != manifest.source_cursor + 1:
        raise AuditIntegrityError("Semantic cold result moved outside its supported exact selected cut")
    generation = effect.creator_generation
    for recovery in recoveries:
        if recovery.previous_generation is not None:
            if recovery.sequence != effect.creator_sequence or recovery.previous_generation != generation or recovery.generation is None:
                raise AuditIntegrityError("Semantic cold result changed its original paid recovery lineage")
            generation = recovery.generation
    begun = _historical_snapshot(conn, sid, operation, checkpoint_id=manifest.source_checkpoint_id)
    source_root = decode_continuation_snapshot(begun.payload_json)
    original_root = decode_continuation_snapshot(data.source.selected_snapshot_json)
    original_advisor = _advisor(root).model_copy(update={"phase": "awaiting_response", "response": None})
    expected_begun = decode_continuation_snapshot(
        _effect_snapshot(
            encode_continuation_snapshot(original_root.model_copy(update={"frames": (*original_root.frames, original_advisor)})),
            effect_key=effect_key,
            disposition="dispatched",
            receipt_id=None,
        )
    ).model_copy(update={"cursor": data.source.cursor + 2})
    if (
        source_root.cursor != manifest.source_cursor
        or begun.cursor != manifest.source_cursor
        or source_root != expected_begun
        or _advisor(source_root).provider_effect_key != effect_key
        or _advisor(source_root).phase != "awaiting_response"
    ):
        raise AuditIntegrityError("Semantic result lost its original begun source checkpoint")
    _journal_data(
        conn,
        sid,
        operation,
        cursor=data.source.cursor + 1,
        tag="input_prepared",
        payload=canonical_json({"effect_key": effect_key, "input_sha256": effect.input_sha256}),
    )
    _journal_data(
        conn, sid, operation, cursor=data.source.cursor + 2, tag="may_dispatch", payload=canonical_json({"effect_key": effect_key})
    )
    retained_row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation, checkpoints.c.cursor == manifest.source_cursor + 1
        )
    ).one_or_none()
    if retained_row is None:
        raise AuditIntegrityError("Semantic result lost its complete original retained checkpoint")
    paid_root = decode_continuation_snapshot(_blob(conn, sid, operation, retained_row.snapshot_chunk_key, "snapshot.v2"))
    paid_advisor = _advisor(source_root).model_copy(update={"phase": "response_retained", "response": manifest.response})
    expected_paid = decode_continuation_snapshot(
        _effect_snapshot(
            encode_continuation_snapshot(_paid_evidence_root(source_root, expected_parent, paid_advisor, extended)),
            effect_key=effect_key,
            disposition="committed" if manifest.outcome is SemanticAdvisorProviderOutcome.SUCCESS else "refused",
            receipt_id=manifest.receipt_key,
        )
    )
    expected_paid = expected_paid.model_copy(update={"cursor": manifest.source_cursor + 1})
    if paid_root != expected_paid or decode_continuation_snapshot(paid_source.snapshot.payload_json) != paid_root:
        raise AuditIntegrityError("Semantic cold output changed its original complete retained root")
    _journal_data(
        conn,
        sid,
        operation,
        cursor=manifest.source_cursor + 1,
        tag="response_retained",
        payload=_outcome_event(manifest, effect.result_chunk_key),
    )
    return SemanticAdvisorProviderResult(selected, effect_key, call, response, record, decision, manifest.outcome), data, paid_root


def read_semantic_advisor_outcome_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: StoredContinuationSnapshot, effect_key: str
) -> SemanticAdvisorProviderResult:
    result, _data, _paid_root = _paid_result(conn, running, selected, effect_key)
    return result


def read_current_paid_semantic_decision_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: StoredContinuationSnapshot, effect_key: str
) -> SemanticAdvisorPaidDecisionData:
    """Prove an uninstalled original decision before a separate action owner.

    Read/review/coverage owners must derive their actual admission inputs and
    re-enter their own current writer. This API grants no read permission,
    complete-coverage claim, physical quota custody or publication authority.
    """
    root = _current(conn, running, selected)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    result, data, paid_root = _paid_result_data(conn, sid, operation, selected, root, effect_key)
    if (
        _advisor(root).phase != "response_retained"
        or root != paid_root.model_copy(update={"cursor": selected.cursor})
        or result.outcome is not SemanticAdvisorProviderOutcome.SUCCESS
        or result.decision is None
        or result.response is None
    ):
        raise AuditIntegrityError("Semantic action admission requires its uninstalled original successful paid decision")
    effect = _effect_data(conn, sid, operation, root, effect_key)
    creator = _creator_data(conn, sid, operation, effect)
    canonical = canonical_json(result.decision.model_dump(mode="json"))
    return SemanticAdvisorPaidDecisionData(
        selected=selected,
        paid_root=paid_root,
        invocation=data,
        decision=result.decision,
        decision_json=canonical,
        decision_sha256=sha256(canonical.encode("utf-8")).hexdigest(),
        source=ContinuationActionSource(effect_key=effect_key, input_sha256=effect.input_sha256, response=result.response.reference),
        audit_call=result.audit_call,
        session_id=running.claim.session_id,
        operation_id=operation,
        claim_attempt=running.claim.attempt,
        operation_epoch=running.session_operation_context.fence.operation_epoch,
        creator_sequence=effect.creator_sequence,
        creator_generation=effect.creator_generation,
        creator_operation_id=creator.session_operation_id,
        creator_operation_epoch=creator.session_operation_epoch,
        creator_lease_token=creator.session_operation_lease_token,
    )


def install_semantic_advisor_decision_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: StoredContinuationSnapshot, effect_key: str
) -> StoredContinuationSnapshot:
    result, data, _paid_root = _paid_result(conn, running, selected, effect_key)
    root = _current(conn, running, selected)
    if (
        _advisor(root).phase != "response_retained"
        or result.outcome is not SemanticAdvisorProviderOutcome.SUCCESS
        or result.decision is None
        or result.response is None
    ):
        raise AuditIntegrityError("Semantic installation requires its one uninstalled original successful decision")
    if result.decision.prescribed_action is not None and result.decision.prescribed_action.kind != "primary_turn":
        raise AuditIntegrityError("Semantic read/review actions require their separate actual execution-owner joins")
    premise = data.invocation.premise
    if premise is None:
        raise AuditIntegrityError("Semantic installation lost its original exact premise")
    source = ContinuationActionSource(
        effect_key=effect_key, input_sha256=_effect(conn, running, root, effect_key).input_sha256, response=result.response.reference
    )
    changed, frontier = retain_advisor_action(
        root,
        result.decision,
        actual_advisor_call_id=_call_id(result.audit_call),
        frontier=data.frontier,
        actual_source=source,
        retained_evidence={item.receipt_id: item for item in data.frontier.evidence},
        input_hash=premise.input_hash,
        policy_hash=premise.policy_hash,
        tool_manifest_hash=premise.tool_manifest_hash,
        permitted_read_tools=frozenset(),
        complete_required_coverage=False,
    )
    changed = changed.model_copy(
        update={"frames": (*changed.frames[:-1], changed.frames[-1].model_copy(update={"phase": "decision_ready"}))}
    )
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    with conn.begin_nested():
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=private_chunk_key(frontier),
            payload=frontier.canonical.encode("utf-8"),
            codec=frontier.reference.codec,
        )
        return append_complete_snapshot(
            conn,
            session_id=sid,
            operation_id=operation,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.cursor,
            snapshot_json=encode_continuation_snapshot(changed),
            event_batch=(
                ContinuationEvent(
                    "checkpoint",
                    canonical_json(
                        {
                            "kind": "semantic_decision",
                            "effect_key": effect_key,
                            "input_sha256": source.input_sha256,
                            "decision_id": result.decision.decision_id,
                            "response": source.response.model_dump(mode="json"),
                            "action_json": changed.active_action_json,
                        }
                    ),
                ),
            ),
            handoff=False,
        )


def read_semantic_advisor_decision_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: StoredContinuationSnapshot, effect_key: str
) -> SemanticAdvisorDecisionData:
    root = _current(conn, running, selected)
    return _decision_data(conn, str(running.claim.session_id), running.claim.operation_id, selected, root, effect_key)


def _decision_data(
    conn: Connection,
    sid: str,
    operation: str,
    selected: StoredContinuationSnapshot,
    root: ContinuationFrameStack,
    effect_key: str,
) -> SemanticAdvisorDecisionData:
    from elspeth.web.sessions.continuation_semantic_recovery import semantic_recovery_source

    result, data, paid_root = _paid_result_data(conn, sid, operation, selected, root, effect_key)
    original_selected = semantic_recovery_source(conn, sid, operation, selected).snapshot
    if (
        _advisor(root).phase != "decision_ready"
        or result.outcome is not SemanticAdvisorProviderOutcome.SUCCESS
        or result.decision is None
        or result.response is None
    ):
        raise AuditIntegrityError("Semantic cold action lacks an installed successful original decision")
    source = ContinuationActionSource(
        effect_key=effect_key,
        input_sha256=_effect_data(conn, sid, operation, root, effect_key).input_sha256,
        response=result.response.reference,
    )
    premise = data.invocation.premise
    if premise is None:
        raise AuditIntegrityError("Semantic cold action lost its actual original premise")
    expected, frontier = retain_advisor_action(
        paid_root,
        result.decision,
        actual_advisor_call_id=_call_id(result.audit_call),
        frontier=data.frontier,
        actual_source=source,
        retained_evidence={item.receipt_id: item for item in data.frontier.evidence},
        input_hash=premise.input_hash,
        policy_hash=premise.policy_hash,
        tool_manifest_hash=premise.tool_manifest_hash,
        permitted_read_tools=frozenset(),
        complete_required_coverage=False,
    )
    expected = expected.model_copy(
        update={
            "cursor": selected.cursor,
            "frames": (*expected.frames[:-1], _advisor(expected).model_copy(update={"phase": "decision_ready"})),
        }
    )
    if root != expected or _ref(conn, sid, operation, frontier.reference) != frontier:
        raise AuditIntegrityError("Semantic cold action changed its original decision/frontier/source binding")
    _journal_data(
        conn,
        sid,
        operation,
        cursor=original_selected.cursor,
        tag="checkpoint",
        payload=canonical_json(
            {
                "kind": "semantic_decision",
                "effect_key": effect_key,
                "input_sha256": source.input_sha256,
                "decision_id": result.decision.decision_id,
                "response": source.response.model_dump(mode="json"),
                "action_json": root.active_action_json,
            }
        ),
    )
    return SemanticAdvisorDecisionData(result.decision, source, result.decision.prescribed_action)


class _PrimarySourceBridge(ContinuationValue):
    schema_version: Literal["composer.semantic-primary-source.v1"] = "composer.semantic-primary-source.v1"
    session_id: str
    operation_id: str
    primary_effect_key: str
    action_json: str
    action_key: str
    source: ContinuationActionSource
    decision_checkpoint_id: str
    decision_cursor: int
    consume_cursor: int


class _ReviewSourceBridge(ContinuationValue):
    schema_version: Literal["composer.semantic-review-source.v1"] = "composer.semantic-review-source.v1"
    session_id: str
    operation_id: str
    action_json: str
    action_key: str
    source: ContinuationActionSource
    decision_checkpoint_id: str
    decision_cursor: int
    consume_cursor: int
    original_candidate: PrivateChunkRef
    candidate: PrivateChunkRef
    candidate_ref: str
    evidence_binding: str


class _ReadSourceBridge(ContinuationValue):
    schema_version: Literal["composer.semantic-read-source.v1"] = "composer.semantic-read-source.v1"
    session_id: str
    operation_id: str
    semantic_effect_key: str
    owner_effect_key: str
    commit_checkpoint_id: str
    commit_cursor: int
    consume_cursor: int
    source: ContinuationActionSource
    reference: ContinuationEvidenceRef
    semantic_result_json: str
    original_result_sha256: str


class _WholeStopSourceBridge(ContinuationValue):
    schema_version: Literal["composer.semantic-whole-stop-source.v1"] = "composer.semantic-whole-stop-source.v1"
    session_id: str
    operation_id: str
    decision_checkpoint_id: str
    decision_cursor: int
    consume_cursor: int
    source: ContinuationActionSource
    decision_json: str
    original_candidate: PrivateChunkRef
    candidate: PrivateChunkRef
    review_effect_key: str
    review_result_chunk_key: str
    review_checkpoint_id: str
    review_cursor: int
    publication_binding: str


@dataclass(frozen=True, slots=True)
class SemanticIterationPublicationProof:
    """One original clean classified result and its later actual paid Stop."""

    admission: SemanticActionAdmission
    returned: IterationReturnCommitted
    end_pass_message_id: str
    end_pass_index: int
    publication_binding: str


type _OwnedIterationSourceKind = Literal["source_proof_refusal", "reply_protocol_echo", "reply_deadline_exhausted", "graph_recovery"]


@dataclass(frozen=True, slots=True)
class SemanticOwnedIterationPublicationProof:
    """Original owned terminal DATA and paid Stop, never clean model approval."""

    admission: SemanticActionAdmission
    returned: IterationReturnCommitted
    outcome_kind: Literal["owned_refusal", "owned_recovery"]
    source_kind: _OwnedIterationSourceKind
    recovery_context: GraphRecoveryContext | None


class _OwnedIterationStopBridge(ContinuationValue):
    schema_version: Literal["composer.semantic-owned-iteration-stop-source.v1"] = "composer.semantic-owned-iteration-stop-source.v1"
    session_id: str
    operation_id: str
    decision_checkpoint_id: str
    decision_cursor: int
    consume_cursor: int
    source: ContinuationActionSource
    decision_json: str
    iteration_effect_key: str
    iteration_checkpoint_id: str
    iteration_cursor: int
    result_sha256: str
    recorder: PrivateChunkRef
    outcome_kind: Literal["owned_refusal", "owned_recovery"]
    source_kind: _OwnedIterationSourceKind


class _OwnedIterationStopEvent(ContinuationValue):
    kind: Literal["semantic_iteration_owned_stop_ready"] = "semantic_iteration_owned_stop_ready"
    bridge_chunk_key: str
    bridge: _OwnedIterationStopBridge


class _IterationWholeStopBridge(ContinuationValue):
    schema_version: Literal["composer.semantic-iteration-whole-stop-source.v1"] = "composer.semantic-iteration-whole-stop-source.v1"
    session_id: str
    operation_id: str
    decision_checkpoint_id: str
    decision_cursor: int
    consume_cursor: int
    source: ContinuationActionSource
    decision_json: str
    iteration_effect_key: str
    iteration_checkpoint_id: str
    iteration_cursor: int
    result_sha256: str
    recorder: PrivateChunkRef
    primary_effect_key: str
    primary_input_sha256: str
    primary_response: PrivateChunkRef
    end_effect_key: str
    pending_stage_key: str
    end_pass_message_id: str
    end_pass_index: int
    publication_binding: str


class _IterationWholeStopEvent(ContinuationValue):
    kind: Literal["semantic_iteration_whole_stop_ready"] = "semantic_iteration_whole_stop_ready"
    bridge_chunk_key: str
    bridge: _IterationWholeStopBridge


def _clean_iteration_return_for_stop(
    conn: Connection, sid: str, operation: str, admission: SemanticActionAdmission
) -> IterationReturnCommitted:
    from elspeth.web.sessions.continuation_classifier_primary import read_original_classifier_primary_on_connection
    from elspeth.web.sessions.continuation_end_pass_source import read_original_accepted_end_pass_on_connection

    source = admission.paid.invocation.source
    selected = _historical_snapshot(conn, sid, operation, checkpoint_id=source.checkpoint_id)
    if (selected.cursor, selected.payload_json) != (source.cursor, source.selected_snapshot_json):
        raise AuditIntegrityError("Semantic iteration Stop changed its exact original assessed return")
    returned = _returned_iteration_source_data(conn, sid, operation, selected)
    if (
        returned.terminal_kind != "model_end"
        or returned.end_disposition != "clean"
        or returned.primary_effect_key is None
        or returned.primary_input_sha256 is None
        or returned.primary_response is None
        or returned.end_effect_key is None
        or returned.pending_stage_key is None
        or returned.end_pass_message_id is None
        or returned.end_pass_index is None
        or returned.publication_binding is None
    ):
        raise AuditIntegrityError("Semantic iteration Stop requires its original authenticated clean model END")
    primary = read_original_classifier_primary_on_connection(
        conn, session_id=UUID(sid), operation_id=operation, effect_key=returned.primary_effect_key
    )
    if (primary.input_sha256, primary.response.reference) != (returned.primary_input_sha256, returned.primary_response):
        raise AuditIntegrityError("Semantic iteration Stop substituted its real classifier PRIMARY source")
    read_original_accepted_end_pass_on_connection(
        conn,
        session_id=UUID(sid),
        message_id=returned.end_pass_message_id,
        pass_index=returned.end_pass_index,
        publication_binding=returned.publication_binding,
    )
    return returned


def _iteration_stop_bridge(
    sid: str,
    operation: str,
    decision: StoredContinuationSnapshot,
    admission: SemanticActionAdmission,
    returned: IterationReturnCommitted,
) -> _IterationWholeStopBridge:
    if (
        returned.primary_effect_key is None
        or returned.primary_input_sha256 is None
        or returned.primary_response is None
        or returned.end_effect_key is None
        or returned.pending_stage_key is None
        or returned.end_pass_message_id is None
        or returned.end_pass_index is None
        or returned.publication_binding is None
    ):
        raise AuditIntegrityError("Semantic iteration Stop lost its complete original model END locators")
    return _IterationWholeStopBridge(
        session_id=sid,
        operation_id=operation,
        decision_checkpoint_id=decision.checkpoint_id,
        decision_cursor=decision.cursor,
        consume_cursor=decision.cursor + 1,
        source=admission.paid.source,
        decision_json=admission.paid.decision_json,
        iteration_effect_key=returned.owner_effect_key,
        iteration_checkpoint_id=returned.completed.checkpoint_id,
        iteration_cursor=returned.completed.cursor,
        result_sha256=sha256(returned.result_json.encode("utf-8")).hexdigest(),
        recorder=returned.recorder_source.reference,
        primary_effect_key=returned.primary_effect_key,
        primary_input_sha256=returned.primary_input_sha256,
        primary_response=returned.primary_response,
        end_effect_key=returned.end_effect_key,
        pending_stage_key=returned.pending_stage_key,
        end_pass_message_id=returned.end_pass_message_id,
        end_pass_index=returned.end_pass_index,
        publication_binding=returned.publication_binding,
    )


def _semantic_iteration_publication_data(
    conn: Connection, sid: str, operation: str, selected: StoredContinuationSnapshot
) -> SemanticIterationPublicationProof | None:
    from elspeth.web.sessions.continuation_semantic_recovery import semantic_recovery_source

    # Current ownership/CAS belongs to the public caller. Reconstruct the
    # original consumed checkpoint only through proved recovery transitions.
    selected = semantic_recovery_source(conn, sid, operation, selected).snapshot
    from elspeth.web.composer.continuation_iteration_transitions import applied_batch_cut
    from elspeth.web.sessions.continuation_semantic_actions import _reprove_installed_snapshot

    original = _historical_snapshot(conn, sid, operation, checkpoint_id=selected.checkpoint_id)
    if original != selected:
        raise AuditIntegrityError("Semantic iteration publication substituted its exact complete checkpoint")
    row = conn.execute(
        select(events.c.tag, events.c.payload_chunk_key).where(
            events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == selected.cursor
        )
    ).one_or_none()
    if row is None or row.tag != "checkpoint":
        return None
    text = read_chunk(conn, session_id=sid, operation_id=operation, key=row.payload_chunk_key).decode("utf-8", errors="strict")
    body, error = parse_json_strict(text)
    if error is not None or type(body) is not dict or canonical_json(body) != text:
        raise AuditIntegrityError("Semantic iteration publication lost its complete canonical checkpoint event")
    if body.get("kind") != "semantic_iteration_whole_stop_ready":
        return None
    event = _decode(_IterationWholeStopEvent, text)
    bridge = event.bridge
    if (
        (bridge.session_id, bridge.operation_id, bridge.consume_cursor) != (sid, operation, selected.cursor)
        or bridge.decision_cursor + 1 != selected.cursor
        or event.bridge_chunk_key != f"semantic-iteration-stop-source:{bridge.source.effect_key}"
        or _blob(conn, sid, operation, event.bridge_chunk_key, bridge.schema_version) != _canonical(bridge)
    ):
        raise AuditIntegrityError("Semantic iteration publication changed its exact original Stop bridge")
    decision = _historical_snapshot(conn, sid, operation, checkpoint_id=bridge.decision_checkpoint_id)
    if decision.cursor != bridge.decision_cursor:
        raise AuditIntegrityError("Semantic iteration publication changed its paid Stop checkpoint")
    admission = _reprove_installed_snapshot(conn, UUID(sid), operation, decision, bridge.source.effect_key)
    paid_root = decode_continuation_snapshot(decision.payload_json)
    if (
        len(paid_root.frames) != 3
        or _advisor(paid_root).phase != "decision_ready"
        or type(paid_root.frames[1]) is not ToolBatchFrame
        or admission.action is not None
        or admission.action_key is not None
        or not admission.evidence.coverage.complete
        or paid_root.active_action_json is not None
        or paid_root.active_action_source is not None
        or paid_root.pending_owner_results
        or paid_root.pending_settlement_ids
        or any(item.disposition not in ("committed", "refused") for item in paid_root.effect_frontier)
    ):
        raise AuditIntegrityError("Semantic iteration publication lost its actual closed whole-request Stop")
    expected_root = paid_root.model_copy(update={"frames": paid_root.frames[:-1], "cursor": selected.cursor})
    applied_batch_cut(expected_root.frames)
    if decode_continuation_snapshot(selected.payload_json) != expected_root:
        raise AuditIntegrityError("Semantic iteration publication substituted its full consumed applied root")
    returned = _clean_iteration_return_for_stop(conn, sid, operation, admission)
    if bridge != _iteration_stop_bridge(sid, operation, decision, admission, returned):
        raise AuditIntegrityError("Semantic iteration publication substituted its original result, END or physical source")
    return SemanticIterationPublicationProof(
        admission, returned, bridge.end_pass_message_id, bridge.end_pass_index, bridge.publication_binding
    )


def require_semantic_iteration_publication_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: StoredContinuationSnapshot
) -> SemanticIterationPublicationProof | None:
    """Current final SQL admission plus one exact original applied-result Stop."""
    if type(selected) is not StoredContinuationSnapshot:
        raise AuditIntegrityError("Semantic iteration publication requires its actual nominal selected root")
    _require_current_continuation_running(conn, running, allow_cancel=False, allow_expired_segment=True)
    if _owed_continuation_p4_active(conn, running.session_operation_context):
        raise AuditIntegrityError("Semantic iteration publication cannot borrow an active P4 finish")
    current = require_snapshot_cas(
        conn,
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.cursor,
    )
    if current != selected:
        raise ContinuationCASConflict("Semantic iteration publication changed its complete current selected root")
    proof = _semantic_iteration_publication_data(conn, str(running.claim.session_id), running.claim.operation_id, selected)
    root = decode_continuation_snapshot(selected.payload_json)
    if proof is None and len(root.frames) == 2 and type(root.frames[1]) is ToolBatchFrame:
        raise AuditIntegrityError("Semantic iteration publication requires its original consumed whole-request Stop")
    return proof


def consume_semantic_advisor_iteration_stop_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: StoredContinuationSnapshot, effect_key: str
) -> StoredContinuationSnapshot:
    """Pop only the actual paid Stop over its original clean classified result."""
    from elspeth.web.composer.continuation_iteration_transitions import applied_batch_cut
    from elspeth.web.sessions.continuation_semantic_actions import require_semantic_whole_stop_on_connection

    root = _current(conn, running, selected)
    admission = require_semantic_whole_stop_on_connection(conn, running, selected=selected, effect_key=effect_key)
    if len(root.frames) != 3 or _advisor(root).phase != "decision_ready":
        raise AuditIntegrityError("Semantic iteration Stop lost its original paid applied stack")
    applied_batch_cut(root.frames[:-1])
    if (
        root.active_action_json is not None
        or root.active_action_source is not None
        or root.pending_owner_results
        or root.pending_settlement_ids
        or any(item.disposition not in ("committed", "refused") for item in root.effect_frontier)
    ):
        raise AuditIntegrityError("Semantic iteration Stop cannot leave unresolved source work")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    returned = _clean_iteration_return_for_stop(conn, sid, operation, admission)
    bridge = _iteration_stop_bridge(sid, operation, selected, admission, returned)
    key = f"semantic-iteration-stop-source:{effect_key}"
    event = _IterationWholeStopEvent(bridge_chunk_key=key, bridge=bridge)
    ready = root.model_copy(update={"frames": root.frames[:-1]})
    with conn.begin_nested():
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=key,
            payload=_canonical(bridge).encode("utf-8"),
            codec=bridge.schema_version,
        )
        consumed = append_complete_snapshot(
            conn,
            session_id=sid,
            operation_id=operation,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.cursor,
            snapshot_json=encode_continuation_snapshot(ready),
            event_batch=(ContinuationEvent("checkpoint", _canonical(event)),),
            handoff=False,
        )
        if require_semantic_iteration_publication_on_connection(conn, running, selected=consumed) is None:
            raise AuditIntegrityError("Semantic iteration Stop lost its exact consumed final source proof")
        return consumed


def _owned_iteration_return_for_stop(
    conn: Connection, sid: str, operation: str, admission: SemanticActionAdmission
) -> tuple[IterationReturnCommitted, Literal["owned_refusal", "owned_recovery"], _OwnedIterationSourceKind, GraphRecoveryContext | None]:
    from elspeth.web.composer.graph_recovery import GraphRecoveryContext, GraphRecoveryIntent
    from elspeth.web.sessions.continuation_iteration_return import (
        OwnedIterationReturnSource,
        read_original_owned_iteration_return_on_connection,
    )

    source = admission.paid.invocation.source
    selected = _historical_snapshot(conn, sid, operation, checkpoint_id=source.checkpoint_id)
    if (selected.cursor, selected.payload_json) != (source.cursor, source.selected_snapshot_json):
        raise AuditIntegrityError("Owned iteration Stop changed its exact original assessed terminal")
    returned = _returned_iteration_source_data(conn, sid, operation, selected)
    owned = read_original_owned_iteration_return_on_connection(
        conn, session_id=UUID(sid), operation_id=operation, effect_key=returned.owner_effect_key
    )
    if (
        type(owned) is not OwnedIterationReturnSource
        or owned.returned != returned
        or returned.end_disposition == "clean"
        or returned.result.pipeline_commit_intent is not None
    ):
        raise AuditIntegrityError("Owned iteration Stop substituted its original outcome or invented pipeline approval")
    if owned.kind == "graph_recovery":
        if (
            returned.terminal_kind != "owned_recovery"
            or type(returned.result.graph_recovery_intent) is not GraphRecoveryIntent
            or type(owned.recovery_context) is not GraphRecoveryContext
        ):
            raise AuditIntegrityError("Owned recovery Stop lost its exact original provider-authored recovery")
        return returned, "owned_recovery", owned.kind, owned.recovery_context
    if (
        owned.kind not in ("source_proof_refusal", "reply_protocol_echo", "reply_deadline_exhausted")
        or returned.terminal_kind != "owned_refusal"
        or returned.result.graph_recovery_intent is not None
    ):
        raise AuditIntegrityError("Owned refusal Stop cannot replace a recovery or model END outcome")
    return returned, "owned_refusal", owned.kind, None


def _owned_iteration_stop_bridge(
    sid: str,
    operation: str,
    decision: StoredContinuationSnapshot,
    admission: SemanticActionAdmission,
    returned: IterationReturnCommitted,
    outcome_kind: Literal["owned_refusal", "owned_recovery"],
    source_kind: _OwnedIterationSourceKind,
) -> _OwnedIterationStopBridge:
    return _OwnedIterationStopBridge(
        session_id=sid,
        operation_id=operation,
        decision_checkpoint_id=decision.checkpoint_id,
        decision_cursor=decision.cursor,
        consume_cursor=decision.cursor + 1,
        source=admission.paid.source,
        decision_json=admission.paid.decision_json,
        iteration_effect_key=returned.owner_effect_key,
        iteration_checkpoint_id=returned.completed.checkpoint_id,
        iteration_cursor=returned.completed.cursor,
        result_sha256=sha256(returned.result_json.encode("utf-8")).hexdigest(),
        recorder=returned.recorder_source.reference,
        outcome_kind=outcome_kind,
        source_kind=source_kind,
    )


def _semantic_owned_iteration_publication_data(
    conn: Connection, sid: str, operation: str, selected: StoredContinuationSnapshot
) -> SemanticOwnedIterationPublicationProof | None:
    from elspeth.web.sessions.continuation_semantic_recovery import semantic_recovery_source

    selected = semantic_recovery_source(conn, sid, operation, selected).snapshot
    from elspeth.web.composer.continuation_iteration_transitions import applied_batch_cut
    from elspeth.web.sessions.continuation_semantic_actions import _reprove_installed_snapshot

    actual = _historical_snapshot(conn, sid, operation, checkpoint_id=selected.checkpoint_id)
    if actual != selected:
        raise AuditIntegrityError("Owned iteration publication substituted its exact complete checkpoint")
    row = conn.execute(
        select(events.c.tag, events.c.payload_chunk_key).where(
            events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == selected.cursor
        )
    ).one_or_none()
    if row is None or row.tag != "checkpoint":
        return None
    text = read_chunk(conn, session_id=sid, operation_id=operation, key=row.payload_chunk_key).decode("utf-8", errors="strict")
    body, error = parse_json_strict(text)
    if error is not None or type(body) is not dict or canonical_json(body) != text:
        raise AuditIntegrityError("Owned iteration publication lost its complete canonical event")
    if body.get("kind") != "semantic_iteration_owned_stop_ready":
        return None
    event = _decode(_OwnedIterationStopEvent, text)
    bridge = event.bridge
    if (
        (bridge.session_id, bridge.operation_id, bridge.consume_cursor) != (sid, operation, selected.cursor)
        or bridge.decision_cursor + 1 != selected.cursor
        or event.bridge_chunk_key != f"semantic-owned-iteration-stop-source:{bridge.source.effect_key}"
        or _blob(conn, sid, operation, event.bridge_chunk_key, bridge.schema_version) != _canonical(bridge)
    ):
        raise AuditIntegrityError("Owned iteration publication changed its original Stop bridge")
    decision = _historical_snapshot(conn, sid, operation, checkpoint_id=bridge.decision_checkpoint_id)
    if decision.cursor != bridge.decision_cursor:
        raise AuditIntegrityError("Owned iteration publication changed its original paid Stop checkpoint")
    admission = _reprove_installed_snapshot(conn, UUID(sid), operation, decision, bridge.source.effect_key)
    paid_root = decode_continuation_snapshot(decision.payload_json)
    if (
        len(paid_root.frames) != 3
        or _advisor(paid_root).phase != "decision_ready"
        or type(paid_root.frames[1]) is not ToolBatchFrame
        or admission.action is not None
        or admission.action_key is not None
        or not admission.evidence.coverage.complete
        or paid_root.active_action_json is not None
        or paid_root.active_action_source is not None
        or paid_root.pending_owner_results
        or paid_root.pending_settlement_ids
        or any(item.disposition not in ("committed", "refused") for item in paid_root.effect_frontier)
    ):
        raise AuditIntegrityError("Owned iteration publication lost its actual closed whole-request Stop")
    expected = paid_root.model_copy(update={"frames": paid_root.frames[:-1], "cursor": selected.cursor})
    applied_batch_cut(expected.frames)
    if decode_continuation_snapshot(selected.payload_json) != expected:
        raise AuditIntegrityError("Owned iteration publication replaced its complete consumed applied root")
    returned, outcome_kind, source_kind, recovery_context = _owned_iteration_return_for_stop(conn, sid, operation, admission)
    if bridge != _owned_iteration_stop_bridge(sid, operation, decision, admission, returned, outcome_kind, source_kind):
        raise AuditIntegrityError("Owned iteration publication substituted its original terminal kind, source or complete result")
    return SemanticOwnedIterationPublicationProof(admission, returned, outcome_kind, source_kind, recovery_context)


def require_semantic_owned_iteration_publication_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: StoredContinuationSnapshot
) -> SemanticOwnedIterationPublicationProof | None:
    """Reprove original owned terminal and paid Stop under current final SQL."""
    if type(selected) is not StoredContinuationSnapshot:
        raise AuditIntegrityError("Owned iteration publication requires its actual nominal selected root")
    _require_current_continuation_running(conn, running, allow_cancel=False, allow_expired_segment=True)
    if _owed_continuation_p4_active(conn, running.session_operation_context):
        raise AuditIntegrityError("Owned iteration publication cannot borrow an active P4 finish")
    current = require_snapshot_cas(
        conn,
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.cursor,
    )
    if current != selected:
        raise ContinuationCASConflict("Owned iteration publication changed its complete current root")
    return _semantic_owned_iteration_publication_data(conn, str(running.claim.session_id), running.claim.operation_id, selected)


def consume_semantic_advisor_owned_iteration_stop_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: StoredContinuationSnapshot, effect_key: str
) -> StoredContinuationSnapshot:
    """Consume actual paid Stop over an independently proved owned terminal."""
    from elspeth.web.composer.continuation_iteration_transitions import applied_batch_cut
    from elspeth.web.sessions.continuation_semantic_actions import require_semantic_whole_stop_on_connection

    root = _current(conn, running, selected)
    admission = require_semantic_whole_stop_on_connection(conn, running, selected=selected, effect_key=effect_key)
    if len(root.frames) != 3 or _advisor(root).phase != "decision_ready":
        raise AuditIntegrityError("Owned iteration Stop lost its exact original paid applied stack")
    applied_batch_cut(root.frames[:-1])
    if (
        root.active_action_json is not None
        or root.active_action_source is not None
        or root.pending_owner_results
        or root.pending_settlement_ids
        or any(item.disposition not in ("committed", "refused") for item in root.effect_frontier)
    ):
        raise AuditIntegrityError("Owned iteration Stop cannot leave unresolved source work")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    returned, outcome_kind, source_kind, _recovery_context = _owned_iteration_return_for_stop(conn, sid, operation, admission)
    bridge = _owned_iteration_stop_bridge(sid, operation, selected, admission, returned, outcome_kind, source_kind)
    key = f"semantic-owned-iteration-stop-source:{effect_key}"
    event = _OwnedIterationStopEvent(bridge_chunk_key=key, bridge=bridge)
    with conn.begin_nested():
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=key,
            payload=_canonical(bridge).encode("utf-8"),
            codec=bridge.schema_version,
        )
        consumed = append_complete_snapshot(
            conn,
            session_id=sid,
            operation_id=operation,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.cursor,
            snapshot_json=encode_continuation_snapshot(root.model_copy(update={"frames": root.frames[:-1]})),
            event_batch=(ContinuationEvent("checkpoint", _canonical(event)),),
            handoff=False,
        )
        if require_semantic_owned_iteration_publication_on_connection(conn, running, selected=consumed) is None:
            raise AuditIntegrityError("Owned iteration Stop lost its exact current cold source proof")
        return consumed


def consume_semantic_advisor_terminal_iteration_stop_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: StoredContinuationSnapshot, effect_key: str
) -> StoredContinuationSnapshot:
    """Select an original source arm; each consumer proves its own authority."""
    from elspeth.web.sessions.continuation_semantic_actions import require_semantic_whole_stop_on_connection

    _current(conn, running, selected)
    admission = require_semantic_whole_stop_on_connection(conn, running, selected=selected, effect_key=effect_key)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    source = admission.paid.invocation.source
    assessed = _historical_snapshot(conn, sid, operation, checkpoint_id=source.checkpoint_id)
    if (assessed.cursor, assessed.payload_json) != (source.cursor, source.selected_snapshot_json):
        raise AuditIntegrityError("Iteration Stop selection changed its original assessed source")
    returned = _returned_iteration_source_data(conn, sid, operation, assessed)
    if returned.terminal_kind == "model_end":
        return consume_semantic_advisor_iteration_stop_on_connection(conn, running, selected=selected, effect_key=effect_key)
    if returned.terminal_kind in ("owned_refusal", "owned_recovery"):
        return consume_semantic_advisor_owned_iteration_stop_on_connection(conn, running, selected=selected, effect_key=effect_key)
    raise AuditIntegrityError("Iteration Stop has no implemented original terminal source arm")


def consume_semantic_advisor_whole_stop_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: StoredContinuationSnapshot, effect_key: str
) -> StoredContinuationSnapshot:
    """Consume an actual whole-request Stop without replacing its reviewed result."""
    from elspeth.web.sessions.continuation_semantic_actions import (
        read_original_semantic_review_for_stop_on_connection,
        require_semantic_publication_on_connection,
        require_semantic_whole_stop_on_connection,
    )

    root = _current(conn, running, selected)
    admission = require_semantic_whole_stop_on_connection(conn, running, selected=selected, effect_key=effect_key)
    if len(root.frames) != 3 or _advisor(root).phase != "decision_ready" or type(root.frames[1]) is not EndFrame:
        raise AuditIntegrityError("Semantic whole Stop lost its exact original reviewed END stack")
    parent, end = _parent(root), root.frames[1]
    original = admission.evidence.coverage.end_candidate
    if original is None or admission.evidence.coverage.candidate_source != end.candidate or end.phase != "publication_ready":
        raise AuditIntegrityError("Semantic whole Stop cannot invent an original clean shared result")
    review = read_original_semantic_review_for_stop_on_connection(
        conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, original_candidate=original
    )
    if review.outcome != "clean" or review.publication_binding is None or review.publication_binding != end.publication_binding:
        raise AuditIntegrityError("Semantic whole Stop cannot grant blocked or substituted publication")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    result_key = conn.execute(
        select(effects.c.result_chunk_key).where(
            effects.c.session_id == sid,
            effects.c.operation_id == operation,
            effects.c.effect_key == review.owner_effect_key,
            effects.c.owner == "composer_end",
            effects.c.disposition == "committed",
        )
    ).scalar_one_or_none()
    if type(result_key) is not str:
        raise AuditIntegrityError("Semantic whole Stop lost its original complete Review result chunk")
    candidate = encode_end_candidate(
        original.model_copy(
            update={
                "state": parent.state,
                "evidence": parent.evidence,
                "iteration": parent.iteration,
                "semantic_source": admission.paid.source,
                "semantic_decision_json": admission.paid.decision_json,
            }
        )
    )
    bridge = _WholeStopSourceBridge(
        session_id=sid,
        operation_id=operation,
        decision_checkpoint_id=selected.checkpoint_id,
        decision_cursor=selected.cursor,
        consume_cursor=selected.cursor + 1,
        source=admission.paid.source,
        decision_json=admission.paid.decision_json,
        original_candidate=end.candidate,
        candidate=candidate.reference,
        review_effect_key=review.owner_effect_key,
        review_result_chunk_key=result_key,
        review_checkpoint_id=review.completed.checkpoint_id,
        review_cursor=review.completed.cursor,
        publication_binding=review.publication_binding,
    )
    bridge_key = f"semantic-whole-stop-source:{effect_key}"
    event = canonical_json(
        {
            "kind": "semantic_whole_stop_ready",
            "decision_checkpoint_id": bridge.decision_checkpoint_id,
            "decision_cursor": bridge.decision_cursor,
            "source": bridge.source.model_dump(mode="json"),
            "decision_json": bridge.decision_json,
            "original_candidate": bridge.original_candidate.model_dump(mode="json"),
            "candidate": bridge.candidate.model_dump(mode="json"),
            "review_effect_key": bridge.review_effect_key,
            "review_result_chunk_key": bridge.review_result_chunk_key,
            "review_checkpoint_id": bridge.review_checkpoint_id,
            "review_cursor": bridge.review_cursor,
            "publication_binding": bridge.publication_binding,
            "bridge_chunk_key": bridge_key,
        }
    )
    ready = root.model_copy(update={"frames": (parent, end.model_copy(update={"state": parent.state, "candidate": candidate.reference}))})
    with conn.begin_nested():
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=private_chunk_key(candidate),
            payload=candidate.canonical.encode("utf-8"),
            codec=candidate.reference.codec,
        )
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=bridge_key,
            payload=_canonical(bridge).encode("utf-8"),
            codec=bridge.schema_version,
        )
        consumed = append_complete_snapshot(
            conn,
            session_id=sid,
            operation_id=operation,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.cursor,
            snapshot_json=encode_continuation_snapshot(ready),
            event_batch=(ContinuationEvent("checkpoint", event),),
            handoff=False,
        )
        if require_semantic_publication_on_connection(conn, running, selected=consumed) is None:
            raise AuditIntegrityError("Semantic whole Stop lost its exact consumed final SQL proof")
        return consumed


def _read_consumed_root(
    conn: Connection, sid: str, operation: str, committed: StoredContinuationSnapshot, reference: ContinuationEvidenceRef
) -> tuple[ContinuationFrameStack, PrivateChunk, PrivateChunk | None]:
    root = decode_continuation_snapshot(committed.payload_json)
    if (
        _advisor(root).phase != "decision_ready"
        or root.active_action_json is not None
        or root.active_action_source is not None
        or root.semantic_frontier is None
        or root.pending_owner_results
        or root.pending_settlement_ids
        or any(item.disposition not in ("committed", "refused") for item in root.effect_frontier)
    ):
        raise AuditIntegrityError("Semantic read consume lost its exact completed quiescent original result")
    frontier = decode_frontier_chunk(_ref(conn, sid, operation, root.semantic_frontier))
    if any(item.receipt_id == reference.receipt_id for item in frontier.evidence):
        raise AuditIntegrityError("Semantic read consume cannot repeat or substitute an existing evidence receipt")
    changed = encode_frontier_chunk(replace(frontier, evidence=(*frontier.evidence, reference)))
    frames = root.frames[:-1]
    candidate_chunk = None
    if len(frames) == 2 and type(frames[1]) is EndFrame:
        parent, end = _parent(root), frames[1]
        candidate = decode_end_candidate(_ref(conn, sid, operation, end.candidate))
        # Initial unreviewed candidate refs may follow actual read capture.
        # A prior completed Review retains its immutable original result locator.
        if candidate.semantic_source is None:
            candidate_chunk = encode_end_candidate(
                candidate.model_copy(update={"state": parent.state, "evidence": parent.evidence, "iteration": parent.iteration})
            )
            frames = (parent, end.model_copy(update={"state": parent.state, "candidate": candidate_chunk.reference}))
    return root.model_copy(update={"frames": frames, "semantic_frontier": changed.reference}), changed, candidate_chunk


def _read_bridge_event(bridge: _ReadSourceBridge) -> str:
    return canonical_json(
        {
            "kind": "semantic_read_applied",
            "semantic_effect_key": bridge.semantic_effect_key,
            "owner_effect_key": bridge.owner_effect_key,
            "commit_checkpoint_id": bridge.commit_checkpoint_id,
            "commit_cursor": bridge.commit_cursor,
            "source": bridge.source.model_dump(mode="json"),
            "reference": bridge.reference.model_dump(mode="json"),
            "original_result_sha256": bridge.original_result_sha256,
            "bridge_chunk_key": f"semantic-read-source:{bridge.owner_effect_key}",
        }
    )


def consume_committed_semantic_read_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: StoredContinuationSnapshot, effect_key: str
) -> StoredContinuationSnapshot:
    """Expose actual non-provider read values; a receipt alone is not novelty."""
    from elspeth.web.composer.continuation_semantic_advisor_evidence import derive_semantic_read_evidence
    from elspeth.web.sessions.continuation_semantic_actions import read_original_committed_semantic_read_on_connection

    _current(conn, running, selected)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    original = read_original_committed_semantic_read_on_connection(
        conn,
        session_id=running.claim.session_id,
        operation_id=operation,
        commit_checkpoint_id=selected.checkpoint_id,
        semantic_effect_key=effect_key,
    )
    if original.committed.snapshot != selected:
        raise AuditIntegrityError("Semantic read consume substituted its exact original committed source")
    evidence = derive_semantic_read_evidence(original.admission, original.committed)
    ready, frontier, candidate = _read_consumed_root(conn, sid, operation, selected, evidence.reference)
    bridge = _ReadSourceBridge(
        session_id=sid,
        operation_id=operation,
        semantic_effect_key=effect_key,
        owner_effect_key=original.committed.owner_effect_key,
        commit_checkpoint_id=selected.checkpoint_id,
        commit_cursor=selected.cursor,
        consume_cursor=selected.cursor + 1,
        source=original.admission.paid.source,
        reference=evidence.reference,
        semantic_result_json=evidence.semantic_result_json,
        original_result_sha256=evidence.original_result_sha256,
    )
    with conn.begin_nested():
        for chunk in (frontier, *((candidate,) if candidate is not None else ())):
            put_chunk(
                conn,
                session_id=sid,
                operation_id=operation,
                key=private_chunk_key(chunk),
                payload=chunk.canonical.encode("utf-8"),
                codec=chunk.reference.codec,
            )
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=f"semantic-read-source:{bridge.owner_effect_key}",
            payload=_canonical(bridge).encode("utf-8"),
            codec=bridge.schema_version,
        )
        return append_complete_snapshot(
            conn,
            session_id=sid,
            operation_id=operation,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.cursor,
            snapshot_json=encode_continuation_snapshot(ready),
            event_batch=(ContinuationEvent("checkpoint", _read_bridge_event(bridge)),),
            handoff=False,
        )


def _read_input_data(conn: Connection, sid: str, operation: str, reference: ContinuationEvidenceRef) -> SemanticAdvisorReadInput:
    from elspeth.web.composer.continuation_semantic_advisor_evidence import derive_semantic_read_evidence
    from elspeth.web.sessions.continuation_semantic_actions import read_original_committed_semantic_read_on_connection

    bridge = _decode(
        _ReadSourceBridge, _blob(conn, sid, operation, f"semantic-read-source:{reference.receipt_id}", "composer.semantic-read-source.v1")
    )
    if (bridge.session_id, bridge.operation_id, bridge.owner_effect_key, bridge.reference) != (
        sid,
        operation,
        reference.receipt_id,
        reference,
    ):
        raise AuditIntegrityError("Semantic read input substituted its original scoped source bridge")
    original = read_original_committed_semantic_read_on_connection(
        conn,
        session_id=UUID(sid),
        operation_id=operation,
        commit_checkpoint_id=bridge.commit_checkpoint_id,
        semantic_effect_key=bridge.semantic_effect_key,
    )
    evidence = derive_semantic_read_evidence(original.admission, original.committed)
    if (
        bridge.commit_cursor != original.committed.snapshot.cursor
        or bridge.consume_cursor != bridge.commit_cursor + 1
        or bridge.source != original.admission.paid.source
        or bridge.owner_effect_key != original.committed.owner_effect_key
        or bridge.reference != evidence.reference
        or bridge.semantic_result_json != evidence.semantic_result_json
        or bridge.original_result_sha256 != evidence.original_result_sha256
    ):
        raise AuditIntegrityError("Semantic read input changed its exact original result or stable semantic values")
    expected, frontier, candidate = _read_consumed_root(conn, sid, operation, original.committed.snapshot, evidence.reference)
    consumed = _historical_snapshot(conn, sid, operation, cursor=bridge.consume_cursor)
    if (
        consumed.payload_json != encode_continuation_snapshot(expected.model_copy(update={"cursor": consumed.cursor}))
        or _ref(conn, sid, operation, frontier.reference) != frontier
        or (candidate is not None and _ref(conn, sid, operation, candidate.reference) != candidate)
    ):
        raise AuditIntegrityError("Semantic read input lost its complete original consumed root")
    _journal_data(conn, sid, operation, cursor=consumed.cursor, tag="checkpoint", payload=_read_bridge_event(bridge))
    return SemanticAdvisorReadInput(
        reference,
        evidence.semantic_result_json,
        evidence.original_result_sha256,
        bridge.semantic_effect_key,
        bridge.owner_effect_key,
        bridge.commit_checkpoint_id,
        bridge.commit_cursor,
    )


def _retained_inputs_data(
    conn: Connection, sid: str, operation: str, root: ContinuationFrameStack, *, checkpoint_id: str, cursor: int
) -> SemanticAdvisorRetainedInputs:
    original = _input_end_source_observation(conn, sid, operation, root, checkpoint_id=checkpoint_id, cursor=cursor)
    if root.semantic_frontier is None:
        raise AuditIntegrityError("Semantic retained inputs lost their actual source frontier")
    frontier = decode_frontier_chunk(_ref(conn, sid, operation, root.semantic_frontier))
    reads = tuple(_read_input_data(conn, sid, operation, reference) for reference in frontier.evidence if reference.source_kind == "read")
    review_input = None
    if original.review is not None:
        from elspeth.web.sessions.continuation_semantic_review_result import RetainedSemanticReviewResult, encode_semantic_review_result

        # This exact nominal DATA was just re-proved from this connection,
        # root and checkpoint. Other calls still perform their own current
        # admissions and original immutable-source reconstruction.
        review = original.review
        review_input = SemanticAdvisorReviewInput(
            encode_semantic_review_result(RetainedSemanticReviewResult(review.result, review.publication_binding)),
            review.owner_effect_key,
            review.completed.checkpoint_id,
            review.completed.cursor,
            review.outcome,
        )
    return_input = None
    if original.iteration_return is not None:
        returned = original.iteration_return
        return_input = SemanticAdvisorIterationReturnInput(
            returned.result_json,
            returned.owner_effect_key,
            returned.completed.checkpoint_id,
            returned.completed.cursor,
            returned.recorder_source.reference,
        )
    return SemanticAdvisorRetainedInputs(sid, operation, checkpoint_id, cursor, reads, review_input, return_input)


def read_semantic_advisor_retained_inputs_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: StoredContinuationSnapshot
) -> SemanticAdvisorRetainedInputs:
    root = _current(conn, running, selected)
    return _retained_inputs_data(
        conn, str(running.claim.session_id), running.claim.operation_id, root, checkpoint_id=selected.checkpoint_id, cursor=selected.cursor
    )


def _require_retained_input_document(
    conn: Connection,
    sid: str,
    operation: str,
    root: ContinuationFrameStack,
    physical: _PhysicalInput,
    retained: SemanticAdvisorRetainedInputs,
    *,
    env_ref_names: frozenset[str],
) -> None:
    if len(physical.messages) != 3 or physical.messages[-1].get("role") != "user":
        raise AuditIntegrityError("Semantic physical input lost its original complete source document")
    text = physical.messages[-1].get("content")
    if type(text) is not str:
        raise AuditIntegrityError("Semantic physical source document lost its original text")
    document, error = parse_json_strict(text)
    if error is not None or type(document) is not dict or canonical_json(document) != text:
        raise AuditIntegrityError("Semantic physical input substituted its canonical invocation/source document")
    parent = _parent(root)
    expected_evidence = decode_turn_evidence(
        _ref(conn, sid, operation, parent.evidence),
        env_ref_names=env_ref_names,
        source_chunks=load_private_chunks(conn, session_id=sid, operation_id=operation, stack=root),
    ).snapshot.model_dump(mode="json", by_alias=True, exclude={"transcript"})
    expected_document: dict[str, JsonValue] = {
        "source": physical.source.model_dump(mode="json"),
        "frontier": _ref(conn, sid, operation, root.semantic_frontier).canonical if root.semantic_frontier is not None else None,
        "evidence": expected_evidence,
        "iteration": semantic_retained_json(_ref(conn, sid, operation, parent.iteration).canonical),
    }
    end = root.frames[-1]
    if type(end) is EndFrame:
        expected_document["end_candidate"] = decode_end_candidate(_ref(conn, sid, operation, end.candidate)).model_dump(mode="json")
    if retained.reads:
        expected_document["read_results"] = [semantic_retained_json(item.semantic_result_json) for item in retained.reads]
    if retained.review is not None:
        expected_document["review_result"] = semantic_retained_json(retained.review.result_json)
    if retained.iteration_return is not None:
        expected_document["iteration_return"] = semantic_retained_json(retained.iteration_return.result_json)
    if canonical_json(expected_document) != text:
        raise AuditIntegrityError("Semantic physical input omitted, added or substituted its complete original source/result DATA")


def _historical_snapshot(
    conn: Connection, sid: str, operation: str, *, checkpoint_id: str | None = None, cursor: int | None = None
) -> StoredContinuationSnapshot:
    """Read one immutable checkpoint; this supplies no live owner authority."""
    if (checkpoint_id is None) == (cursor is None):
        raise ValueError("historical semantic source requires exactly one checkpoint locator")
    query = select(checkpoints).where(checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation)
    query = (
        query.where(checkpoints.c.checkpoint_id == checkpoint_id)
        if checkpoint_id is not None
        else query.where(checkpoints.c.cursor == cursor)
    )
    row = conn.execute(query).one_or_none()
    if row is None or row.snapshot_chunk_key != f"snapshot:{row.checkpoint_id}":
        raise AuditIntegrityError("Semantic source lost its exact original checkpoint row")
    payload = _blob(conn, sid, operation, row.snapshot_chunk_key, "snapshot.v2")
    root = decode_continuation_snapshot(payload)
    if root.cursor != row.cursor or (root.logical.session_id, root.logical.operation_id) != (sid, operation):
        raise AuditIntegrityError("Semantic source changed its complete historical checkpoint identity")
    return StoredContinuationSnapshot(payload_json=payload, cursor=row.cursor, checkpoint_id=row.checkpoint_id)


def _returned_iteration_source_data(
    conn: Connection, sid: str, operation: str, selected: StoredContinuationSnapshot
) -> IterationReturnCommitted:
    """Reprove a complete return and only its implemented quiescent descendants.

    The original return remains an actual applied-P4 root. A later metadata
    shape or a merely similar tool frame cannot manufacture its result source.
    """
    from elspeth.web.composer.continuation_iteration_transitions import advance_effect_key, applied_batch_cut
    from elspeth.web.sessions.continuation_iteration_return import read_original_iteration_return_on_connection
    from elspeth.web.sessions.continuation_semantic_recovery import semantic_recovery_source

    recovered_source = semantic_recovery_source(conn, sid, operation, selected).snapshot
    if recovered_source != selected and _semantic_checkpoint_kind_data(conn, sid, operation, recovered_source.cursor) in (
        "semantic_iteration_whole_stop_ready",
        "semantic_iteration_owned_stop_ready",
    ):
        selected = recovered_source

    actual = _historical_snapshot(conn, sid, operation, checkpoint_id=selected.checkpoint_id)
    if actual != selected:
        raise AuditIntegrityError("Semantic iteration return substituted its complete selected checkpoint")
    root = decode_continuation_snapshot(selected.payload_json)
    _compose, batch = applied_batch_cut(root.frames)
    original = read_original_iteration_return_on_connection(
        conn, session_id=UUID(sid), operation_id=operation, effect_key=advance_effect_key(batch.response)
    )
    if selected != original.completed and _semantic_checkpoint_kind_data(conn, sid, operation, selected.cursor) == (
        "semantic_iteration_whole_stop_ready"
    ):
        proof = _semantic_iteration_publication_data(conn, sid, operation, selected)
        if proof is None or proof.returned != original:
            raise AuditIntegrityError("Semantic iteration publication changed its original complete classified result")
        return replace(original, current_stage="publication_ready")
    if selected != original.completed and _semantic_checkpoint_kind_data(conn, sid, operation, selected.cursor) == (
        "semantic_iteration_owned_stop_ready"
    ):
        owned = _semantic_owned_iteration_publication_data(conn, sid, operation, selected)
        if owned is None or owned.returned != original:
            raise AuditIntegrityError("Owned iteration publication changed its original complete terminal source")
        return replace(original, current_stage="publication_ready")
    _prove_returned_iteration_descendant(conn, sid, operation, selected, original)
    return original


def _prove_returned_iteration_descendant(
    conn: Connection,
    sid: str,
    operation: str,
    selected: StoredContinuationSnapshot,
    original: IterationReturnCommitted,
) -> None:
    if selected == original.completed:
        return
    if selected.cursor <= original.completed.cursor:
        raise AuditIntegrityError("Semantic iteration return replaced its original completed source")
    root = decode_continuation_snapshot(selected.payload_json)
    kind = _semantic_checkpoint_kind_data(conn, sid, operation, selected.cursor)
    if kind == "semantic_current_graph":
        previous = _historical_snapshot(conn, sid, operation, cursor=selected.cursor - 1)
        _prove_returned_iteration_descendant(conn, sid, operation, previous, original)
        before = decode_continuation_snapshot(previous.payload_json)
        parent = _parent(before)
        if before.semantic_frontier is None:
            raise AuditIntegrityError("Semantic returned graph selection lost its original frontier")
        state = state_from_evidence_json(_ref(conn, sid, operation, parent.state).canonical)
        old_frontier = decode_frontier_chunk(_ref(conn, sid, operation, before.semantic_frontier))
        changed = encode_frontier_chunk(replace(old_frontier, graph_hash=authored_state_hash(state)))
        expected = before.model_copy(update={"semantic_frontier": changed.reference, "cursor": selected.cursor})
        if root != expected or _ref(conn, sid, operation, changed.reference) != changed:
            raise AuditIntegrityError("Semantic returned graph selection changed its complete original root")
        _journal_data(
            conn,
            sid,
            operation,
            cursor=selected.cursor,
            tag="checkpoint",
            payload=canonical_json(
                {
                    "kind": "semantic_current_graph",
                    "predecessor": before.semantic_frontier.model_dump(mode="json"),
                    "selected": changed.reference.model_dump(mode="json"),
                    "state": parent.state.model_dump(mode="json"),
                }
            ),
        )
        return
    if kind == "semantic_read_applied":
        event = conn.execute(
            select(events.c.payload_chunk_key).where(
                events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == selected.cursor
            )
        ).scalar_one()
        text = read_chunk(conn, session_id=sid, operation_id=operation, key=event).decode("utf-8", errors="strict")
        payload, error = parse_json_strict(text)
        if error is not None or type(payload) is not dict or "reference" not in payload:
            raise AuditIntegrityError("Semantic returned read source lost its complete event")
        try:
            reference = ContinuationEvidenceRef.model_validate_json(canonical_json(payload["reference"]), strict=True)
        except ValidationError as cause:
            raise AuditIntegrityError("Semantic returned read source replaced its original evidence reference") from cause
        value = _read_input_data(conn, sid, operation, reference)
        # The original Read reader replays its prescribing paid input (which
        # re-enters this proof at an earlier cursor), complete committed result,
        # consume root, immutable bridge and event. The exact current snapshot
        # at that cursor was independently selected above.
        if value.commit_cursor + 1 != selected.cursor or root.semantic_frontier is None:
            raise AuditIntegrityError("Semantic returned read source changed its original consume checkpoint")
        frontier = decode_frontier_chunk(_ref(conn, sid, operation, root.semantic_frontier))
        if reference not in frontier.evidence:
            raise AuditIntegrityError("Semantic returned read source omitted its original retained values")
        return
    raise AuditIntegrityError("Semantic iteration return has no source-proved descendant transition")


def require_returned_iteration_descendant_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
    effect_key: str,
) -> IterationReturnCommitted:
    """Current admission is distinct from original completed return DATA."""
    _current(conn, running, selected)
    return read_returned_iteration_descendant_data_on_connection(
        conn,
        session_id=running.claim.session_id,
        operation_id=running.claim.operation_id,
        selected=selected,
        effect_key=effect_key,
    )


def read_returned_iteration_descendant_data_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    selected: StoredContinuationSnapshot,
    effect_key: str,
) -> IterationReturnCommitted:
    """Original immutable source DATA; no current actor or SDK authority."""
    if type(session_id) is not UUID or type(operation_id) is not str or type(selected) is not StoredContinuationSnapshot:
        raise TypeError("Semantic returned source requires its nominal SQL identity and complete checkpoint")
    original = _returned_iteration_source_data(conn, str(session_id), operation_id, selected)
    if original.owner_effect_key != effect_key:
        raise AuditIntegrityError("Semantic iteration return substituted its original physical effect")
    return original


def _primary_action_with_evidence(conn: Connection, sid: str, operation: str, root: ContinuationFrameStack) -> tuple[str, str]:
    action_json = bound_action_json(root, action_owner="primary_turn", read_tool_name=None, read_tool_arguments_json=None)
    if action_json is None or root.active_action_source is None or root.semantic_frontier is None:
        raise AuditIntegrityError("Semantic PRIMARY bridge requires its exact selected action/source/frontier")
    action = decode_prescribed_continuation_action(action_json)
    if type(action) is not PrimaryContinuationAction or canonical_json(action.model_dump(mode="json")) != action_json:
        raise AuditIntegrityError("Semantic PRIMARY bridge changed its canonical model-authored action")
    frontier = decode_frontier_chunk(_ref(conn, sid, operation, root.semantic_frontier))
    return action_json, continuation_action_key(action, retained_evidence={item.receipt_id: item for item in frontier.evidence})


def _primary_ready_root(root: ContinuationFrameStack) -> ContinuationFrameStack:
    if _advisor(root).phase != "decision_ready":
        raise AuditIntegrityError("Semantic PRIMARY consume requires its original installed decision")
    parent = _parent(root)
    if len(root.frames) == 3:
        end = root.frames[1]
        if type(end) is ToolBatchFrame:
            from elspeth.web.composer.continuation_iteration_transitions import applied_batch_cut

            applied_batch_cut((parent, end))
            # Original return and complete paid input were re-proved by the
            # decision owner. Its immutable full result remains retained;
            # only this new prescribed PRIMARY becomes the next live author.
            parent = parent.model_copy(
                update={"phase": "action_ready", "provider_effect_key": None, "provider_input_sha256": None, "pending_response": None}
            )
            return root.model_copy(update={"frames": (parent,)})
        if (
            type(end) is not EndFrame
            or end.phase not in ("review_needed", "repair_needed", "publication_ready")
            or parent.phase != "response_retained"
            or parent.pending_response is None
        ):
            raise AuditIntegrityError("Semantic PRIMARY consume lost its original superseded END candidate")
        parent = parent.model_copy(
            update={"phase": "action_ready", "provider_effect_key": None, "provider_input_sha256": None, "pending_response": None}
        )
    elif parent.phase != "action_ready" or parent.provider_effect_key is not None or parent.pending_response is not None:
        raise AuditIntegrityError("Semantic PRIMARY consume cannot discard an unresolved Compose source")
    return root.model_copy(update={"frames": (parent,)})


def _primary_bridge_event(value: _PrimarySourceBridge) -> str:
    return canonical_json(
        {
            "kind": "semantic_primary_ready",
            "primary_effect_key": value.primary_effect_key,
            "action_json": value.action_json,
            "action_key": value.action_key,
            "source": value.source.model_dump(mode="json"),
            "decision_checkpoint_id": value.decision_checkpoint_id,
            "decision_cursor": value.decision_cursor,
            "bridge_chunk_key": f"semantic-primary-source:{value.primary_effect_key}",
        }
    )


def consume_semantic_advisor_primary_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: StoredContinuationSnapshot, effect_key: str
) -> StoredContinuationSnapshot:
    """Consume one proved paid family-1 decision without allocating PRIMARY.

    The immutable bridge binds the next family-0 key. Its execution owner
    independently allocates that ordinal and records actual execution receipts.
    """
    root = _current(conn, running, selected)
    decision = _decision_data(conn, str(running.claim.session_id), running.claim.operation_id, selected, root, effect_key)
    if type(decision.action) is not PrimaryContinuationAction or root.active_action_source != decision.source:
        raise AuditIntegrityError("Semantic PRIMARY consume lost its original prescribed action source")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    action_json, action_key = _primary_action_with_evidence(conn, sid, operation, root)
    ready = _primary_ready_root(root)
    ordinal = conn.execute(
        select(ordinals.c.next_ordinal).where(ordinals.c.session_id == sid, ordinals.c.operation_id == operation, ordinals.c.family == 0)
    ).scalar_one_or_none()
    primary_key = f"provider:0:{0 if ordinal is None else ordinal}"
    if (
        conn.execute(
            select(effects.c.effect_key).where(
                effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == primary_key
            )
        ).one_or_none()
        is not None
    ):
        raise AuditIntegrityError("Semantic PRIMARY bridge cannot substitute an already allocated effect")
    bridge = _PrimarySourceBridge(
        session_id=sid,
        operation_id=operation,
        primary_effect_key=primary_key,
        action_json=action_json,
        action_key=action_key,
        source=decision.source,
        decision_checkpoint_id=selected.checkpoint_id,
        decision_cursor=selected.cursor,
        consume_cursor=selected.cursor + 1,
    )
    with conn.begin_nested():
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=f"semantic-primary-source:{primary_key}",
            payload=_canonical(bridge).encode("utf-8"),
            codec=bridge.schema_version,
        )
        return append_complete_snapshot(
            conn,
            session_id=sid,
            operation_id=operation,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.cursor,
            snapshot_json=encode_continuation_snapshot(ready),
            event_batch=(ContinuationEvent("checkpoint", _primary_bridge_event(bridge)),),
            handoff=False,
        )


def consume_semantic_advisor_review_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: StoredContinuationSnapshot, effect_key: str
) -> StoredContinuationSnapshot:
    """Return the original paid Review action to its genuine retained END owner."""
    from elspeth.web.sessions.continuation_semantic_actions import (
        installed_action_json,
        require_consumed_semantic_review_on_connection,
        require_installed_semantic_action_on_connection,
    )

    root = _current(conn, running, selected)
    admission = require_installed_semantic_action_on_connection(conn, running, selected=selected, effect_key=effect_key)
    if (
        type(admission.action) is not ReviewContinuationAction
        or admission.action_key is None
        or _advisor(root).phase != "decision_ready"
        or len(root.frames) != 3
    ):
        raise AuditIntegrityError("Semantic Review consume lost its exact installed original paid action")
    parent, end = _parent(root), root.frames[1]
    original = admission.evidence.coverage.end_candidate
    candidate_ref = admission.evidence.coverage.candidate_ref
    evidence_binding = admission.evidence.coverage.evidence_binding
    if (
        type(end) is not EndFrame
        or end.phase != "review_needed"
        or original is None
        or admission.evidence.coverage.candidate_source != end.candidate
        or candidate_ref is None
        or evidence_binding is None
        or original.publication_source is not None
        or original.publication_record_json is not None
    ):
        raise AuditIntegrityError("Semantic Review consume cannot invent or replace the genuine unpaid END candidate")
    candidate = encode_end_candidate(
        original.model_copy(
            update={
                "state": parent.state,
                "evidence": parent.evidence,
                "iteration": parent.iteration,
                "semantic_source": admission.paid.source,
                "semantic_decision_json": admission.paid.decision_json,
            }
        )
    )
    bridge = _ReviewSourceBridge(
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        action_json=installed_action_json(admission),
        action_key=admission.action_key,
        source=admission.paid.source,
        decision_checkpoint_id=selected.checkpoint_id,
        decision_cursor=selected.cursor,
        consume_cursor=selected.cursor + 1,
        original_candidate=end.candidate,
        candidate=candidate.reference,
        candidate_ref=candidate_ref,
        evidence_binding=evidence_binding,
    )
    bridge_key = f"semantic-review-source:{effect_key}"
    event = canonical_json(
        {
            "kind": "semantic_review_ready",
            "action_json": bridge.action_json,
            "action_key": bridge.action_key,
            "source": bridge.source.model_dump(mode="json"),
            "decision_checkpoint_id": bridge.decision_checkpoint_id,
            "decision_cursor": bridge.decision_cursor,
            "original_candidate": bridge.original_candidate.model_dump(mode="json"),
            "candidate": bridge.candidate.model_dump(mode="json"),
            "candidate_ref": bridge.candidate_ref,
            "evidence_binding": bridge.evidence_binding,
            "bridge_chunk_key": bridge_key,
        }
    )
    ready = root.model_copy(update={"frames": (parent, end.model_copy(update={"state": parent.state, "candidate": candidate.reference}))})
    with conn.begin_nested():
        put_chunk(
            conn,
            session_id=bridge.session_id,
            operation_id=bridge.operation_id,
            key=private_chunk_key(candidate),
            payload=candidate.canonical.encode("utf-8"),
            codec=candidate.reference.codec,
        )
        put_chunk(
            conn,
            session_id=bridge.session_id,
            operation_id=bridge.operation_id,
            key=bridge_key,
            payload=_canonical(bridge).encode("utf-8"),
            codec=bridge.schema_version,
        )
        consumed = append_complete_snapshot(
            conn,
            session_id=bridge.session_id,
            operation_id=bridge.operation_id,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.cursor,
            snapshot_json=encode_continuation_snapshot(ready),
            event_batch=(ContinuationEvent("checkpoint", event),),
            handoff=False,
        )
        require_consumed_semantic_review_on_connection(conn, running, selected=consumed, effect_key=effect_key)
        return consumed


def read_original_prescribed_primary_source_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str, action_json: str, effect_key: str
) -> ContinuationActionSource:
    """Reprove original paid decision and consumption after active fields clear.

    This is immutable DATA only. The PRIMARY execution owner must separately
    prove its current effect, creator and actual evolving action receipt.
    """
    sid, operation = str(session_id), operation_id
    pieces = effect_key.split(":")
    if len(pieces) != 3 or pieces[:2] != ["provider", "0"] or not pieces[2].isdigit() or str(int(pieces[2])) != pieces[2]:
        raise AuditIntegrityError("Semantic PRIMARY source requires its exact family-0 effect key")
    bridge = _decode(
        _PrimarySourceBridge, _blob(conn, sid, operation, f"semantic-primary-source:{effect_key}", "composer.semantic-primary-source.v1")
    )
    if (bridge.session_id, bridge.operation_id, bridge.primary_effect_key, bridge.action_json) != (
        sid,
        operation,
        effect_key,
        action_json,
    ) or bridge.consume_cursor != bridge.decision_cursor + 1:
        raise AuditIntegrityError("Semantic PRIMARY source substituted its original bridge identity")
    selected = _historical_snapshot(conn, sid, operation, checkpoint_id=bridge.decision_checkpoint_id)
    root = decode_continuation_snapshot(selected.payload_json)
    if selected.cursor != bridge.decision_cursor:
        raise AuditIntegrityError("Semantic PRIMARY source changed its original decision cursor")
    decision = _decision_data(conn, sid, operation, selected, root, bridge.source.effect_key)
    actual_action_json, actual_action_key = _primary_action_with_evidence(conn, sid, operation, root)
    if (decision.source, root.active_action_source, actual_action_json, actual_action_key) != (
        bridge.source,
        bridge.source,
        bridge.action_json,
        bridge.action_key,
    ):
        raise AuditIntegrityError("Semantic PRIMARY source changed its original paid decision/action/frontier")
    consumed = _historical_snapshot(conn, sid, operation, cursor=bridge.consume_cursor)
    expected = _primary_ready_root(root).model_copy(update={"cursor": bridge.consume_cursor})
    if decode_continuation_snapshot(consumed.payload_json) != expected:
        raise AuditIntegrityError("Semantic PRIMARY source changed its complete consume checkpoint")
    _journal_data(conn, sid, operation, cursor=bridge.consume_cursor, tag="checkpoint", payload=_primary_bridge_event(bridge))
    return bridge.source


def require_current_prescribed_primary_action_on_connection(
    conn: Connection, running: ComposerOperationRunning, root: ContinuationFrameStack
) -> str | None:
    """Current execution admission, separate from the historical DATA reader."""
    if type(root) is not ContinuationFrameStack:
        raise AuditIntegrityError("Semantic PRIMARY admission requires its nominal current root")
    selected = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
    actual = _current(conn, running, selected)
    if actual != root:
        raise ContinuationCASConflict("Semantic PRIMARY admission changed its complete current root")
    if root.active_action_json is None and root.active_action_source is None:
        return None
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    action_json, _action_key = _primary_action_with_evidence(conn, sid, operation, root)
    parent = _parent(root)
    primary_key = parent.provider_effect_key
    if primary_key is None:
        ordinal = conn.execute(
            select(ordinals.c.next_ordinal).where(
                ordinals.c.session_id == sid, ordinals.c.operation_id == operation, ordinals.c.family == 0
            )
        ).scalar_one_or_none()
        primary_key = f"provider:0:{0 if ordinal is None else ordinal}"
    original = read_original_prescribed_primary_source_on_connection(
        conn, session_id=running.claim.session_id, operation_id=operation, action_json=action_json, effect_key=primary_key
    )
    if original != root.active_action_source:
        raise AuditIntegrityError("Semantic PRIMARY admission substituted its original source")
    return action_json


def _inflight_source_data(
    conn: Connection, sid: str, operation: str, selected: StoredContinuationSnapshot, root: ContinuationFrameStack
) -> Row[Any]:
    frame = _advisor(root)
    if frame.phase != "awaiting_response" or frame.provider_effect_key is None or frame.response is not None:
        raise AuditIntegrityError("Semantic in-flight source changed its original selected frame")
    effect = _effect_data(conn, sid, operation, root, frame.provider_effect_key)
    with retention_stage_memo_scope():
        data, _physical = _original_input_data(conn, sid, operation, root, effect)
    _creator_data(conn, sid, operation, effect)
    if effect.disposition not in ("prepared", "dispatched") or any(
        value is not None
        for value in (
            effect.receipt_key,
            effect.result_chunk_key,
            effect.provider_attempt_id,
            effect.physical_sequence,
            effect.physical_generation,
            effect.requested_model,
            effect.response_finished_at,
        )
    ):
        raise AuditIntegrityError("Semantic in-flight source cannot infer a complete known output")
    original = _historical_snapshot(conn, sid, operation, checkpoint_id=data.source.checkpoint_id)
    if original.payload_json != data.source.selected_snapshot_json:
        raise AuditIntegrityError("Semantic preparation changed its original complete source checkpoint")
    original_root = decode_continuation_snapshot(original.payload_json)
    expected = original_root.model_copy(update={"frames": (*original_root.frames, frame)})
    expected = decode_continuation_snapshot(
        _effect_snapshot(
            encode_continuation_snapshot(expected), effect_key=effect.effect_key, disposition=effect.disposition, receipt_id=None
        )
    ).model_copy(update={"cursor": original.cursor + (1 if effect.disposition == "prepared" else 2)})
    if root != expected or selected.cursor != expected.cursor:
        raise AuditIntegrityError("Semantic in-flight source changed its complete original preparation/begin root")
    _journal_data(
        conn,
        sid,
        operation,
        cursor=original.cursor + 1,
        tag="input_prepared",
        payload=canonical_json(
            {
                "effect_key": effect.effect_key,
                "input_sha256": effect.input_sha256,
            }
        ),
    )
    if effect.disposition == "dispatched":
        _journal_data(
            conn, sid, operation, cursor=original.cursor + 2, tag="may_dispatch", payload=canonical_json({"effect_key": effect.effect_key})
        )
    return effect


def _semantic_checkpoint_kind_data(conn: Connection, sid: str, operation: str, cursor: int) -> str:
    row = conn.execute(
        select(events.c.tag, events.c.payload_chunk_key).where(
            events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == cursor
        )
    ).one_or_none()
    if row is None or row.tag != "checkpoint":
        raise AuditIntegrityError("Semantic current action lost its exact checkpoint event")
    text = read_chunk(conn, session_id=sid, operation_id=operation, key=row.payload_chunk_key).decode("utf-8", errors="strict")
    value, error = parse_json_strict(text)
    if error is not None or type(value) is not dict or canonical_json(value) != text:
        raise AuditIntegrityError("Semantic current action lost its closed checkpoint discriminator")
    kind = value.get("kind")
    if type(kind) is not str:
        raise AuditIntegrityError("Semantic current action lost its checkpoint kind")
    return kind


def validate_semantic_advisor_source_data_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str, selected: StoredContinuationSnapshot
) -> tuple[str, ...]:
    """Full original selected source proof for the structural SQL reader.

    A known prepared/begun effect remains unresolved DATA. This never grants
    SDK replay, caller custody or an invented successful response.
    """
    sid, operation = str(session_id), operation_id
    original = _historical_snapshot(conn, sid, operation, checkpoint_id=selected.checkpoint_id)
    if original != selected:
        raise AuditIntegrityError("Semantic structural source substituted its complete checkpoint")
    root = decode_continuation_snapshot(selected.payload_json)
    frame = _advisor(root)
    if frame.phase == "awaiting_response":
        effect = _inflight_source_data(conn, sid, operation, selected, root)
        return (effect.effect_key,)
    if frame.provider_effect_key is None:
        raise AuditIntegrityError("Semantic structural source lost its actual original provider effect")
    if frame.phase == "decision_ready":
        if (
            root.active_action_json is None
            and _semantic_checkpoint_kind_data(conn, sid, operation, selected.cursor) == "semantic_read_committed"
        ):
            from elspeth.web.sessions.continuation_semantic_actions import read_original_committed_semantic_read_on_connection

            read_original_committed_semantic_read_on_connection(
                conn,
                session_id=session_id,
                operation_id=operation_id,
                commit_checkpoint_id=selected.checkpoint_id,
                semantic_effect_key=frame.provider_effect_key,
            )
            return ()
        if (
            root.active_action_json is not None
            and type(decode_prescribed_continuation_action(root.active_action_json)) is PrimaryContinuationAction
        ):
            _decision_data(conn, sid, operation, selected, root, frame.provider_effect_key)
        else:
            from elspeth.web.sessions.continuation_semantic_actions import _reprove_installed_snapshot

            _reprove_installed_snapshot(conn, session_id, operation_id, selected, frame.provider_effect_key)
    elif frame.phase == "response_retained":
        _paid_result_data(conn, sid, operation, selected, root, frame.provider_effect_key)
    else:
        raise AuditIntegrityError("Semantic structural source has no installed original producer for this cut")
    return ()


def read_semantic_advisor_runtime_source_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory
) -> ContinuationRuntimeSource:
    """Restore the actual full Advisor cut after its original paid proof.

    The shared runtime read receives the unchanged selection and inventory;
    no Compose-only surrogate or historical authorization is constructed.
    """
    if type(selected) is not SelectedContinuationState or type(inventory) is not CurrentContinuationEnvInventory:
        raise AuditIntegrityError("Semantic runtime source requires its nominal full selected source/inventory")
    stored = StoredContinuationSnapshot(encode_continuation_snapshot(selected.stack), selected.stack.cursor, selected.checkpoint_id)
    root = _current(conn, running, stored)
    frame = _advisor(root)
    if frame.phase not in ("response_retained", "decision_ready"):
        raise AuditIntegrityError("Semantic runtime cannot replay unresolved prepared/begun provider work")
    validate_semantic_advisor_source_data_on_connection(
        conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, selected=stored
    )
    return read_runtime_source_on_connection(conn, running, selected=selected, inventory=inventory)


def select_semantic_advisor_current_graph_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory
) -> StoredContinuationSnapshot:
    """Select only the actual retained authored graph's semantic projection.

    Every obligation, source evidence, pending review and action receipt keeps
    its original value. This authors no graph and declares no timer/note progress.
    """
    stored = StoredContinuationSnapshot(encode_continuation_snapshot(selected.stack), selected.stack.cursor, selected.checkpoint_id)
    root = _current(conn, running, stored)
    parent = _parent(root)
    _input_end_source_data(
        conn, str(running.claim.session_id), running.claim.operation_id, root, checkpoint_id=stored.checkpoint_id, cursor=stored.cursor
    )
    if (
        root.active_action_json is not None
        or root.active_action_source is not None
        or root.semantic_frontier is None
        or any(item.disposition in ("prepared", "dispatched", "unknown") for item in root.effect_frontier)
    ):
        raise AuditIntegrityError("Semantic graph selection requires its quiescent unprescribed original Compose source")
    read_runtime_source_on_connection(conn, running, selected=selected, inventory=inventory)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    state = state_from_evidence_json(_ref(conn, sid, operation, parent.state).canonical)
    previous = decode_frontier_chunk(_ref(conn, sid, operation, root.semantic_frontier))
    changed = encode_frontier_chunk(replace(previous, graph_hash=authored_state_hash(state)))
    if changed.reference == root.semantic_frontier:
        return stored
    with conn.begin_nested():
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=private_chunk_key(changed),
            payload=changed.canonical.encode("utf-8"),
            codec=changed.reference.codec,
        )
        return append_complete_snapshot(
            conn,
            session_id=sid,
            operation_id=operation,
            expected_checkpoint_id=stored.checkpoint_id,
            expected_cursor=stored.cursor,
            snapshot_json=encode_continuation_snapshot(root.model_copy(update={"semantic_frontier": changed.reference})),
            event_batch=(
                ContinuationEvent(
                    "checkpoint",
                    canonical_json(
                        {
                            "kind": "semantic_current_graph",
                            "predecessor": root.semantic_frontier.model_dump(mode="json"),
                            "selected": changed.reference.model_dump(mode="json"),
                            "state": parent.state.model_dump(mode="json"),
                        }
                    ),
                ),
            ),
            handoff=False,
        )
