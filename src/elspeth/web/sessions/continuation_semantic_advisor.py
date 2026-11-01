"""Uncalled family-1 SQL lifecycle and original physical-source reconstruction.

No provider I/O occurs under this owner. A live phase, capacity reservation,
typed authority facades and supported successor cuts remain separate joins.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy import Connection, Row, insert, select, update

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.contracts.readonly_json import JsonValue
from elspeth.web.composer.advisor_output import (
    ContinuationAdvisorDecision,
    PrescribedContinuationAction,
    parse_continuation_advisor_decision,
)
from elspeth.web.composer.audit import BufferingRecorder, llm_call_audit_envelope
from elspeth.web.composer.continuation import (
    AdvisorFrame,
    ComposeFrame,
    ContinuationActionSource,
    ContinuationFrameStack,
    ContinuationValue,
    PrivateChunkRef,
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
    decode_retained_physical_audit_call,
    encode_continuation_snapshot,
    encode_continuation_text_response,
    encode_private_chunk,
    verify_private_chunk,
)
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_evidence import decode_turn_evidence, encode_turn_evidence
from elspeth.web.composer.continuation_evidence_application_data import append_primary_call_to_evidence
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_progress import retain_advisor_action
from elspeth.web.composer.continuation_provider_outcomes import (
    decode_physical_audit_call,
    encode_physical_audit_call,
    require_successful_response_audit,
)
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.continuation_semantic_advisor import PreparedSemanticAdvisorRequest, _SemanticAdvisorOptions
from elspeth.web.composer.continuation_semantic_advisor_outcome import SemanticAdvisorInvocationRaised, SemanticAdvisorInvocationReturned
from elspeth.web.composer.continuation_semantic_invocation import SemanticAdvisorInvocationData, make_semantic_advisor_invocation
from elspeth.web.composer.continuation_semantic_no_sdk import SemanticAdvisorNoSdkIssuer, SemanticAdvisorNoSdkRefusalPermit
from elspeth.web.composer.continuation_semantic_source import RawSemanticAdvisorSource
from elspeth.web.composer.prompt_guard import PromptGuardRefused
from elspeth.web.composer.provider_audit_source import ProviderAuditSource, provider_attempt_audit_drafts_for_source
from elspeth.web.composer.provider_context import capture_provider_context
from elspeth.web.coordination.quota_authority import (
    _undispatched_cancellation_event_id,
    _usage_entry_id,
    token_usage_entry_from_llm_call_envelope,
)
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
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
    private_chunk_key,
    put_chunk,
    read_chunk,
    require_snapshot_cas,
)
from elspeth.web.sessions.continuation_runtime import read_runtime_source_on_connection
from elspeth.web.sessions.continuation_selection import load_private_chunks
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_continuation_provider_ordinals_table as ordinals
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from elspeth.web.sessions.time_normalization import restore_utc


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


class _Outcome(ContinuationValue):
    schema_version: Literal["composer.semantic-provider-outcome.v1"] = "composer.semantic-provider-outcome.v1"
    effect_key: str
    input_sha256: str
    receipt_key: str
    creator_sequence: int
    creator_generation: int
    source_checkpoint_id: str
    source_cursor: int
    outcome: Literal["success", "paid_invalid", "credential_withheld"]
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
    outcome: Literal["success", "paid_invalid", "credential_withheld"]
    original_error: BaseException | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class SemanticAdvisorDecisionData:
    decision: ContinuationAdvisorDecision
    source: ContinuationActionSource
    action: PrescribedContinuationAction | None


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


def _parent(root: ContinuationFrameStack) -> ComposeFrame:
    frame = root.frames[0]
    if type(frame) is not ComposeFrame or sum(type(item) is ComposeFrame for item in root.frames) != 1:
        raise AuditIntegrityError("Semantic SQL requires its one original Compose parent")
    return frame


def _advisor(root: ContinuationFrameStack) -> AdvisorFrame:
    frame = root.frames[-1]
    if len(root.frames) != 2 or type(frame) is not AdvisorFrame:
        raise AuditIntegrityError("Semantic SQL requires its dedicated selected Advisor frame")
    return frame


def _call_id(call: ComposerLLMCall) -> str:
    value = call.call_id
    if type(value) is not str or not value:
        raise AuditIntegrityError("Semantic physical audit lost its actual original call identity")
    return value


def _effect(conn: Connection, running: ComposerOperationRunning, root: ContinuationFrameStack, key: str) -> Row[Any]:
    _parent(root)
    if len(root.frames) != 2 or type(root.frames[-1]) is not AdvisorFrame:
        raise AuditIntegrityError("Semantic SQL requires its dedicated selected Advisor frame")
    frame = root.frames[-1]
    if frame.provider_effect_key != key or frame.provider_input_sha256 is None:
        raise AuditIntegrityError("Semantic SQL substituted its selected provider locator")
    row = conn.execute(
        select(effects).where(
            effects.c.session_id == str(running.claim.session_id),
            effects.c.operation_id == running.claim.operation_id,
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
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    value = _decode(
        _Input, _blob(conn, sid, operation, f"semantic-provider-input:{effect.input_sha256}", "composer.semantic-provider-input.v1")
    )
    body = _decode(_RawBody, value.raw_source_body_json)
    original = decode_continuation_snapshot(body.selected_snapshot_json)
    frame = _parent(original)
    if len(body.source_chunks) != 6 or len(original.frames) != 1:
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
    parent = _parent(root)
    if (
        len(root.frames) != 1
        or parent.phase != "action_ready"
        or parent.provider_effect_key is not None
        or parent.pending_response is not None
        or root.active_action_json is not None
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
            root.model_copy(update={"frames": (parent, advisor)}),
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
    _descriptor(conn, binding.running, effect)
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


def _original_input(
    conn: Connection, running: ComposerOperationRunning, root: ContinuationFrameStack, effect: Row[Any]
) -> tuple[SemanticAdvisorInvocationData, _PhysicalInput]:
    _stored, data, physical_input = _descriptor(conn, running, effect)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
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
    row = conn.execute(
        select(attempts).where(
            attempts.c.session_id == str(running.claim.session_id),
            attempts.c.operation_id == running.claim.operation_id,
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
    creator = _creator(conn, running, effect)
    actual = conn.execute(select(physical).where(physical.c.attempt_id == call.call_id)).one_or_none()
    principal = conn.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == str(running.claim.session_id))).scalar_one()
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
            str(running.claim.session_id),
            "composer",
            None,
            creator.session_operation_id,
            creator.session_operation_epoch,
            creator.session_operation_lease_token,
            principal,
        )
        or actual.settled_at is None
        or restore_utc(actual.started_at) != call.started_at
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
    sid, operation = str(running.claim.session_id), running.claim.operation_id
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
    root = _current(conn, running, permit.dispatched)
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
            root.model_copy(update={"frames": (root.frames[0], advisor)}),
            data.effect_key,
            "refused",
            data.receipt_key,
            ContinuationEvent("effect_refused", _no_sdk_event(data, key)),
        )
        return SemanticAdvisorNoSdkRefusalReceipt(selected, data.effect_key, data.receipt_key, key)


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
            encode_continuation_snapshot(source.model_copy(update={"frames": (source.frames[0], advisor)})),
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


def _extended_evidence(
    conn: Connection, running: ComposerOperationRunning, data: SemanticAdvisorInvocationData, call: ComposerLLMCall, recorder: PrivateChunk
) -> PrivateChunk:
    original = decode_continuation_snapshot(data.source.selected_snapshot_json)
    admitted = decode_turn_evidence(
        data.source.evidence,
        env_ref_names=data.source.env_ref_names,
        source_chunks=load_private_chunks(
            conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id, stack=original
        ),
    )
    snapshot = decode_recorder_snapshot(recorder)
    if not snapshot.llm_calls or snapshot.llm_calls[-1] != call:
        raise AuditIntegrityError("Semantic evidence extension changed its one original physical call")
    # This installed pure graph operation extends one actual Composer call
    # atom; the semantic SQL owner above supplies its independent paid proof.
    extended = append_primary_call_to_evidence(
        admitted, call=snapshot.llm_calls[-1], recorder=snapshot, env_ref_names=data.source.env_ref_names
    )
    return encode_turn_evidence(extended, env_ref_names=data.source.env_ref_names)


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
    root = _current(conn, running, selected)
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
    kind: Literal["success", "paid_invalid", "credential_withheld"]
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
        kind = "success"
    elif call.status is ComposerLLMCallStatus.MALFORMED_RESPONSE and call.error_message == "credential_material_rejected":
        if any(
            value is not None for value in (observed.message, observed.provider_metadata, observed.decision, observed.tool_calls_present)
        ):
            raise AuditIntegrityError("Semantic withheld outcome exposed unadmitted provider fields")
        kind = "credential_withheld"
    elif call.status is ComposerLLMCallStatus.MALFORMED_RESPONSE:
        if observed.decision is not None:
            raise AuditIntegrityError("Semantic paid invalid result cannot carry an admitted decision")
        if observed.message is not None and type(observed.message.content) is str and observed.provider_metadata is not None:
            response = encode_continuation_text_response(
                content=observed.message.content, provider_metadata=observed.provider_metadata, audit_call=call
            )
        kind = "paid_invalid"
    else:
        raise AuditIntegrityError("Entered semantic timeout/cancellation/unknown result cannot be closed or retried")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    original_parent = _parent(decode_continuation_snapshot(data.source.selected_snapshot_json))
    previous = decode_recorder_snapshot(_ref(conn, sid, operation, original_parent.audit_buffer))
    captured = recorder.snapshot_for_continuation(drained=previous.drained)
    if replace(previous, llm_calls=(*previous.llm_calls, call)) != captured or captured.llm_calls[-1] is not call:
        raise AuditIntegrityError("Semantic outcome changed its original complete recorder prefix/channels")
    record = encode_recorder_snapshot(captured)
    evidence = _extended_evidence(conn, running, data, call, record)
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
    disposition: Literal["committed", "refused"] = "committed" if kind == "success" else "refused"
    with conn.begin_nested():
        for chunk in (record, evidence, *((response,) if response is not None else ())):
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
        advisor = root.frames[-1].model_copy(
            update={"phase": "response_retained", "response": None if response is None else response.reference}
        )
        result = _append(
            conn,
            running,
            selected,
            root.model_copy(update={"frames": (parent, advisor)}),
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
    effect = _effect(conn, running, root, effect_key)
    data, physical_input = _original_input(conn, running, root, effect)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
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
        or effect.disposition != ("committed" if manifest.outcome == "success" else "refused")
    ):
        raise AuditIntegrityError("Semantic cold result changed its original complete physical/effect identity")
    _physical_source(conn, running, effect, call, physical_input)
    record = _ref(conn, sid, operation, manifest.recorder)
    if record.reference.codec != "composer.audit-buffer.v2":
        raise AuditIntegrityError("Semantic cold result changed its recorder source role")
    captured = decode_recorder_snapshot(record)
    original_parent = _parent(decode_continuation_snapshot(data.source.selected_snapshot_json))
    previous = decode_recorder_snapshot(_ref(conn, sid, operation, original_parent.audit_buffer))
    if not captured.llm_calls or captured.llm_calls[-1] != call or replace(previous, llm_calls=(*previous.llm_calls, call)) != captured:
        raise AuditIntegrityError("Semantic cold result changed its complete original recorder prefix")
    call = captured.llm_calls[-1]
    evidence = _extended_evidence(conn, running, data, call, record)
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
    if manifest.outcome == "success":
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
    elif manifest.outcome == "credential_withheld" and (response is not None or call.error_message != "credential_material_rejected"):
        raise AuditIntegrityError("Semantic credential-withheld result exposed provider fields")
    elif response is not None and decode_retained_physical_audit_call(response) != call:
        raise AuditIntegrityError("Semantic invalid response changed its actual paid audit")
    frame = _advisor(root)
    expected_cursor = manifest.source_cursor + (2 if frame.phase == "decision_ready" else 1)
    if frame.phase not in ("response_retained", "decision_ready") or selected.cursor != expected_cursor:
        raise AuditIntegrityError("Semantic cold result moved outside its supported exact selected cut")
    source_root = decode_continuation_snapshot(_blob(conn, sid, operation, f"snapshot:{manifest.source_checkpoint_id}", "snapshot.v2"))
    if (
        source_root.cursor != manifest.source_cursor
        or _advisor(source_root).provider_effect_key != effect_key
        or _advisor(source_root).phase != "awaiting_response"
    ):
        raise AuditIntegrityError("Semantic result lost its original begun source checkpoint")
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
            encode_continuation_snapshot(source_root.model_copy(update={"frames": (expected_parent, paid_advisor)})),
            effect_key=effect_key,
            disposition="committed" if manifest.outcome == "success" else "refused",
            receipt_id=manifest.receipt_key,
        )
    )
    expected_paid = expected_paid.model_copy(update={"cursor": manifest.source_cursor + 1})
    if paid_root != expected_paid or (frame.phase == "response_retained" and root != paid_root):
        raise AuditIntegrityError("Semantic cold output changed its original complete retained root")
    _journal(
        conn, running, cursor=manifest.source_cursor + 1, tag="response_retained", payload=_outcome_event(manifest, effect.result_chunk_key)
    )
    return SemanticAdvisorProviderResult(selected, effect_key, call, response, record, decision, manifest.outcome), data, paid_root


def read_semantic_advisor_outcome_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: StoredContinuationSnapshot, effect_key: str
) -> SemanticAdvisorProviderResult:
    result, _data, _paid_root = _paid_result(conn, running, selected, effect_key)
    return result


def install_semantic_advisor_decision_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: StoredContinuationSnapshot, effect_key: str
) -> StoredContinuationSnapshot:
    result, data, _paid_root = _paid_result(conn, running, selected, effect_key)
    root = _current(conn, running, selected)
    if _advisor(root).phase != "response_retained" or result.outcome != "success" or result.decision is None or result.response is None:
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
    changed = changed.model_copy(update={"frames": (changed.frames[0], changed.frames[-1].model_copy(update={"phase": "decision_ready"}))})
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
    result, data, paid_root = _paid_result(conn, running, selected, effect_key)
    root = _current(conn, running, selected)
    if _advisor(root).phase != "decision_ready" or result.outcome != "success" or result.decision is None or result.response is None:
        raise AuditIntegrityError("Semantic cold action lacks an installed successful original decision")
    source = ContinuationActionSource(
        effect_key=effect_key, input_sha256=_effect(conn, running, root, effect_key).input_sha256, response=result.response.reference
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
            "frames": (expected.frames[0], _advisor(expected).model_copy(update={"phase": "decision_ready"})),
        }
    )
    if root != expected or _ref(conn, str(running.claim.session_id), running.claim.operation_id, frontier.reference) != frontier:
        raise AuditIntegrityError("Semantic cold action changed its original decision/frontier/source binding")
    _journal(
        conn,
        running,
        cursor=selected.cursor,
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
