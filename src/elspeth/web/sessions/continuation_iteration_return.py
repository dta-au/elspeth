"""Durable post-P4 stage DATA and the actual returned-result owner.

The SQL fold, optional EARLY Advisor and classifier are separate driver
actions. A persisted stage never authorizes replay of an interrupted paid
stage. The original advance effect remains the classifier's effect.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.continuation import ComposeFrame, ContinuationFrameStack, EffectFrontierEntry, PrivateChunkRef, ToolBatchFrame
from elspeth.web.composer.continuation_codec import encode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory, require_current_env_inventory_on_connection
from elspeth.web.composer.continuation_evidence_restore import read_selected_turn_evidence_graph_on_connection
from elspeth.web.composer.continuation_iteration_transitions import ITERATION_EFFECT_OWNER, advance_effect_key, applied_batch_cut
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_policy import ContinuationActionPolicy, decode_continuation_policy
from elspeth.web.composer.continuation_result_codec import (
    RetainedComposerResult,
    decode_retained_composer_result,
    encode_retained_composer_result,
)
from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime, _selected_chunks
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.no_tool_policy import state_is_structurally_empty
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.credential_guard import require_no_credential_material
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    StoredContinuationSnapshot,
    _require_current_continuation_running,
    append_complete_snapshot,
    load_snapshot,
    private_chunk_key,
    put_chunk,
    read_chunk,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_classifier_handoff import ClassifyPostPrimary, ClassifySourceProofStop
    from elspeth.web.composer.continuation_classifier_owned_source import ClassifierOwnedSourceObservation, ClassifyReplyRefusalReady
    from elspeth.web.composer.continuation_clock import ContinuationActionClock
    from elspeth.web.composer.continuation_codec import PrivateChunk
    from elspeth.web.composer.continuation_evidence import AdmittedTurnEvidence
    from elspeth.web.composer.continuation_iteration_return_codec import IterationReturnEnvelope
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
    from elspeth.web.composer.continuation_semantic_advisor_return import (
        ObservedClassification,
        ObservedClassifierReplyDeadline,
        ObservedIterationEarly,
        ObservedIterationEndInput,
        ObservedIterationReturn,
    )
    from elspeth.web.composer.graph_recovery import GraphRecoveryContext
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
    from elspeth.web.sessions.continuation_p4 import ReadmittedP4Result

type IterationStoredStage = Literal[
    "early_ready",
    "early_dispatched",
    "classify_ready",
    "classify_dispatched",
    "end_ready",
    "end_dispatched",
    "reply_refusal_ready",
    "reply_refusal_dispatched",
    "returned",
]
type IterationStage = IterationStoredStage | Literal["publication_ready"]


class _StageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal["composer.iteration-stage.v1"] = "composer.iteration-stage.v1"
    session_id: UUID
    operation_id: str
    source_checkpoint_id: str
    source_cursor: int = Field(ge=0)
    before_checkpoint_id: str
    before_cursor: int = Field(ge=0)
    stage: IterationStoredStage
    early_required: bool
    creator_sequence: int = Field(gt=0)
    creator_generation: int = Field(gt=0)
    creator_claim_attempt: int = Field(gt=0)
    creator_operation_id: str
    creator_operation_epoch: int = Field(gt=0)
    env_ref_names: tuple[str, ...]
    compose: ComposeFrame
    batch: ToolBatchFrame
    result_json: str | None = None


class _RecoveredStageEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[2]
    same_sequence: int = Field(gt=0)
    previous_generation: int = Field(gt=0)
    generation: int = Field(gt=0)


class _EndInputRecord(BaseModel):
    """Original live END input and its SQL sequence boundary, never a grant."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["composer.iteration-end-input.v1"] = "composer.iteration-end-input.v1"
    session_id: UUID
    operation_id: str
    ready_stage_key: str
    before_checkpoint_id: str
    pending_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    publication_binding: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    messages_before: int = Field(ge=-1)
    creator_sequence: int = Field(gt=0)
    creator_generation: int = Field(gt=0)
    creator_claim_attempt: int = Field(gt=0)
    creator_operation_id: str
    creator_operation_epoch: int = Field(gt=0)


class _OwnedObservationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["composer.iteration-owned-observation.v1"] = "composer.iteration-owned-observation.v1"
    session_id: UUID
    operation_id: str
    owner_effect_key: str
    source_record_key: str
    observation_json: str
    pending_sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")


class _OwnedCompletionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["composer.iteration-owned-completion.v1"] = "composer.iteration-owned-completion.v1"
    observation_key: str
    observation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    before_checkpoint_id: str
    end_effect_key: str | None
    pending_stage_key: str | None
    end_disposition: Literal["clean", "blocked"] | None


@dataclass(frozen=True, slots=True)
class IterationStageData:
    """Exact current persisted stage; DATA rather than SDK permission."""

    stage: IterationStage
    current: StoredContinuationSnapshot
    prepared: StoredContinuationSnapshot
    owner_effect_key: str
    early_effect_key: str
    early_required: bool
    creator_sequence: int
    creator_generation: int
    pending: ClassifyPostPrimary | ClassifySourceProofStop | ClassifyReplyRefusalReady | None = None


@dataclass(frozen=True, slots=True)
class IterationReturnCommitted:
    """Complete original returned DATA, independently bound to its SQL owner."""

    completed: StoredContinuationSnapshot
    result: ComposerResult
    publication_binding: str | None
    owner_effect_key: str
    original_source: StoredContinuationSnapshot
    recorder_source: PrivateChunk
    result_json: str
    terminal_kind: Literal["model_end", "owned_refusal", "owned_control", "owned_error", "owned_recovery"] | None = None
    end_disposition: Literal["clean", "blocked"] | None = None
    primary_effect_key: str | None = None
    primary_input_sha256: str | None = None
    primary_response: PrivateChunkRef | None = None
    end_pass_message_id: str | None = None
    end_pass_index: int | None = None
    end_effect_key: str | None = None
    pending_stage_key: str | None = None
    current_stage: Literal["returned", "publication_ready"] = "returned"


@dataclass(frozen=True, slots=True)
class AppliedIterationP4Data:
    """Same-transaction P4 receipt and exact raw-versus-folded source DATA."""

    result: ReadmittedP4Result
    stage: IterationStageData | None


@dataclass(frozen=True, slots=True)
class OwnedIterationReturnSource:
    """Kind-specific original terminal DATA, never a current publication grant."""

    kind: Literal["source_proof_refusal", "reply_protocol_echo", "reply_deadline_exhausted", "graph_recovery"]
    returned: IterationReturnCommitted
    recovery_context: GraphRecoveryContext | None = None


@dataclass(frozen=True, slots=True)
class IterationPendingEndData:
    stage: IterationStageData
    pending: ClassifyPostPrimary | ClassifySourceProofStop
    pending_stage_key: str
    end_effect_key: str
    primary_response: PrivateChunk | None
    owned_source_json: str | None = None


def _reply_refusal_key(pending_json: str) -> str:
    return "iteration-refusal:" + sha256(pending_json.encode("utf-8")).hexdigest()


def _reply_refusal_input(record: _StageRecord) -> str:
    if record.result_json is None:
        raise AuditIntegrityError("reply refusal execution lost its original ready DATA")
    return stable_hash(
        {
            "schema": "composer.iteration-reply-refusal-execution.v1",
            "pending": record.result_json,
            "creator_sequence": record.creator_sequence,
            "creator_generation": record.creator_generation,
            "creator_claim_attempt": record.creator_claim_attempt,
            "creator_operation_id": record.creator_operation_id,
            "creator_operation_epoch": record.creator_operation_epoch,
        }
    )


def _key(canonical: str) -> str:
    return "iteration-stage:" + sha256(canonical.encode("utf-8")).hexdigest()


def _decode_pending(encoded: str) -> ClassifyPostPrimary | ClassifySourceProofStop:
    from elspeth.contracts.json_parser import parse_json_strict
    from elspeth.web.composer.continuation_classifier_handoff import (
        decode_classify_post_primary,
        decode_classify_source_proof_stop,
    )

    value, error = parse_json_strict(encoded)
    if error is not None or type(value) is not dict:
        raise AuditIntegrityError("iteration pending END lost its original closed DATA")
    if value.get("schema_version") == "composer.classify-post-primary.v2":
        return decode_classify_post_primary(encoded)
    if value.get("schema_version") == "composer.classify-source-proof-stop.v2":
        return decode_classify_source_proof_stop(encoded)
    raise AuditIntegrityError("iteration pending END requires original used-count source facts")


def _encode_pending(value: ClassifyPostPrimary | ClassifySourceProofStop) -> str:
    from elspeth.web.composer.continuation_classifier_handoff import (
        ClassifyPostPrimary,
        ClassifySourceProofStop,
        encode_classify_post_primary,
        encode_classify_source_proof_stop,
    )

    if type(value) is ClassifyPostPrimary:
        return encode_classify_post_primary(value)
    if type(value) is ClassifySourceProofStop:
        return encode_classify_source_proof_stop(value)
    raise TypeError("iteration pending END requires its actual nominal source")


def _end_key(pending_json: str) -> str:
    return "iteration-end:" + sha256(pending_json.encode("utf-8")).hexdigest()


def _end_input_key(pending_json: str) -> str:
    return "iteration-end-input:" + sha256(pending_json.encode("utf-8")).hexdigest()


def _read_end_input(conn: Connection, record: _StageRecord, pending_json: str) -> _EndInputRecord:
    codec = conn.execute(
        select(chunks.c.codec).where(
            chunks.c.session_id == str(record.session_id),
            chunks.c.operation_id == record.operation_id,
            chunks.c.chunk_key == _end_input_key(pending_json),
        )
    ).scalar_one_or_none()
    canonical = read_chunk(
        conn, session_id=str(record.session_id), operation_id=record.operation_id, key=_end_input_key(pending_json)
    ).decode("utf-8", errors="strict")
    try:
        value = _EndInputRecord.model_validate_json(canonical, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("iteration END lost its original live input source") from error
    before = _historical(conn, str(record.session_id), record.operation_id, checkpoint_id=value.before_checkpoint_id)
    ready = _stage_source_record(conn, before, str(record.session_id), record.operation_id)
    if (
        canonical_json(value.model_dump(mode="json")) != canonical
        or codec != value.schema_version
        or value.session_id != record.session_id
        or value.operation_id != record.operation_id
        or ready is None
        or ready[0].stage != "end_ready"
        or ready[1] != value.ready_stage_key
        or ready[0].result_json != pending_json
        or value.pending_sha256 != sha256(pending_json.encode("utf-8")).hexdigest()
        or (record.stage == "end_dispatched" and value.before_checkpoint_id != record.before_checkpoint_id)
        or (
            value.creator_sequence,
            value.creator_generation,
            value.creator_claim_attempt,
            value.creator_operation_id,
            value.creator_operation_epoch,
        )
        != (
            record.creator_sequence,
            record.creator_generation,
            record.creator_claim_attempt,
            record.creator_operation_id,
            record.creator_operation_epoch,
        )
    ):
        raise AuditIntegrityError("iteration END substituted its actual original input source")
    return value


def _stage_event(record: _StageRecord, key: str) -> str:
    return canonical_json({"owner": ITERATION_EFFECT_OWNER, "transition": "iteration_stage", "stage": record.stage, "source": key})


def _historical(
    conn: Connection, sid: str, operation: str, *, checkpoint_id: str | None = None, cursor: int | None = None
) -> StoredContinuationSnapshot:
    from elspeth.web.sessions.continuation_semantic_advisor import _historical_snapshot

    return _historical_snapshot(conn, sid, operation, checkpoint_id=checkpoint_id, cursor=cursor)


def _stage_record(conn: Connection, current: StoredContinuationSnapshot, sid: str, operation: str) -> tuple[_StageRecord, str] | None:
    row = conn.execute(
        select(events).where(events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == current.cursor)
    ).one_or_none()
    if row is None:
        raise AuditIntegrityError("iteration stage lost its actual current event")
    text = read_chunk(conn, session_id=sid, operation_id=operation, key=row.payload_chunk_key).decode("utf-8", errors="strict")
    from elspeth.contracts.json_parser import parse_json_strict

    payload, error = parse_json_strict(text)
    if error is not None:
        raise AuditIntegrityError("iteration stage event lost its strict original JSON")
    if type(payload) is not dict or payload.get("transition") != "iteration_stage":
        return None
    if row.tag != "checkpoint" or type(payload.get("source")) is not str:
        raise AuditIntegrityError("iteration stage changed its exact checkpoint event")
    key = payload["source"]
    canonical = read_chunk(conn, session_id=sid, operation_id=operation, key=key).decode("utf-8", errors="strict")
    codec = conn.execute(
        select(chunks.c.codec).where(chunks.c.session_id == sid, chunks.c.operation_id == operation, chunks.c.chunk_key == key)
    ).scalar_one_or_none()
    try:
        record = _StageRecord.model_validate_json(canonical, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("iteration stage lost its closed source record") from error
    if codec != record.schema_version or _key(canonical) != key or canonical_json(record.model_dump(mode="json")) != canonical:
        raise AuditIntegrityError("iteration stage changed its immutable original source bytes")
    if text != _stage_event(record, key) or str(record.session_id) != sid or record.operation_id != operation:
        raise AuditIntegrityError("iteration stage changed its current event/request binding")
    return record, key


def _stage_source_record(
    conn: Connection, current: StoredContinuationSnapshot, sid: str, operation: str
) -> tuple[_StageRecord, str, StoredContinuationSnapshot] | None:
    """Follow only actual unpaid handoff DATA; preserve the source producer."""
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
    from elspeth.web.composer.continuation_handoff import ContinuationYieldEvent, roll_slice_for_successor

    selected = current
    crossed = False
    while True:
        source = _stage_record(conn, selected, sid, operation)
        if source is not None:
            if crossed and source[0].stage not in ("early_ready", "classify_ready", "end_ready", "reply_refusal_ready", "returned"):
                raise AuditIntegrityError("handoff cannot renew or replay an entered paid iteration stage")
            return source[0], source[1], selected
        event = conn.execute(
            select(events).where(events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == selected.cursor)
        ).one_or_none()
        if event is None:
            return None
        if event.tag == "response_retained":
            from elspeth.contracts.json_parser import parse_json_strict
            from elspeth.web.sessions.continuation_classifier_primary import read_original_classifier_primary_on_connection

            text = read_chunk(conn, session_id=sid, operation_id=operation, key=event.payload_chunk_key).decode("utf-8", errors="strict")
            payload, error = parse_json_strict(text)
            if error is not None or type(payload) is not dict or payload.get("owner") != "classifier_primary":
                return None
            effect_key = payload.get("effect_key")
            if type(effect_key) is not str or crossed:
                raise AuditIntegrityError("entered classifier PRIMARY cannot become an unpaid successor source")
            paid = read_original_classifier_primary_on_connection(conn, session_id=UUID(sid), operation_id=operation, effect_key=effect_key)
            if paid.snapshot != selected:
                raise AuditIntegrityError("classifier bridge changed its exact committed physical snapshot")
            original = _historical(conn, sid, operation, checkpoint_id=paid.source_checkpoint_id)
            found = _stage_record(conn, original, sid, operation)
            if found is None or found[0].stage != "classify_dispatched" or original.cursor != paid.source_cursor:
                raise AuditIntegrityError("classifier bridge lost its exact original dispatched source")
            return found[0], found[1], original
        if event.tag not in ("yielded", "recovered"):
            return None
        before = _historical(conn, sid, operation, cursor=selected.cursor - 1)
        before_root = decode_continuation_snapshot(before.payload_json)
        text = read_chunk(conn, session_id=sid, operation_id=operation, key=event.payload_chunk_key).decode("utf-8", errors="strict")
        try:
            if event.tag == "yielded":
                yielded = ContinuationYieldEvent.model_validate_json(text, strict=True)
                expected = roll_slice_for_successor(before_root, predecessor_sequence=yielded.predecessor_sequence)
                canonical = canonical_json(yielded.model_dump(mode="json"))
            else:
                recovered = _RecoveredStageEvent.model_validate_json(text, strict=True)
                if (
                    recovered.generation != recovered.previous_generation + 1
                    or applied_batch_cut(before_root.frames)[0].slice_sequence != recovered.same_sequence
                ):
                    raise AuditIntegrityError("iteration recovery changed its exact original generation/slice")
                expected = before_root
                canonical = canonical_json(recovered.model_dump(mode="json"))
        except ValidationError as error:
            raise AuditIntegrityError("iteration handoff lost its closed original event") from error
        if text != canonical or selected.payload_json != encode_continuation_snapshot(
            expected.model_copy(update={"cursor": selected.cursor})
        ):
            raise AuditIntegrityError("iteration handoff changed its complete original unpaid source")
        selected = before
        crossed = True


def _creator(conn: Connection, record: _StageRecord) -> None:
    actual = conn.execute(
        select(attempts).where(
            attempts.c.session_id == str(record.session_id),
            attempts.c.operation_id == record.operation_id,
            attempts.c.sequence == record.creator_sequence,
            attempts.c.generation == record.creator_generation,
        )
    ).one_or_none()
    if (
        actual is None
        or actual.claim_attempt != record.creator_claim_attempt
        or actual.session_operation_id != record.creator_operation_id
        or actual.session_operation_epoch != record.creator_operation_epoch
    ):
        raise AuditIntegrityError("iteration stage changed its exact original physical creator")


def _require_compose_capture(before: ComposeFrame, after: ComposeFrame) -> None:
    mutable = {
        "messages",
        "tools",
        "state",
        "evidence",
        "transcript",
        "audit_buffer",
        "caches",
        "iteration",
        "current_state_id",
        "composition_slice",
        "discovery_slice",
        "composition_total",
        "discovery_total",
        "advisor_calls",
        "repair_turns",
        "advisor_checkpoint_passes",
        "mutation_success_seen",
        "persisted_assistant_message_id",
        "persisted_assistant_content",
        "persisted_tool_call_turn",
        "advisor_repair_context_introduced",
        "pending_advisor_elision_indices",
    }
    if before.model_dump(exclude=mutable) != after.model_dump(exclude=mutable) or any(
        (
            after.composition_slice < before.composition_slice,
            after.discovery_slice < before.discovery_slice,
            after.advisor_calls < before.advisor_calls,
            after.repair_turns < before.repair_turns,
            after.advisor_checkpoint_passes < before.advisor_checkpoint_passes,
            before.mutation_success_seen and not after.mutation_success_seen,
            after.composition_total - before.composition_total != after.composition_slice - before.composition_slice,
            after.discovery_total - before.discovery_total != after.discovery_slice - before.discovery_slice,
        )
    ):
        raise AuditIntegrityError("iteration capture reset actual counters or immutable request data")


def _source_batch(conn: Connection, record: _StageRecord) -> ToolBatchFrame:
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot

    original = _historical(conn, str(record.session_id), record.operation_id, checkpoint_id=record.source_checkpoint_id)
    if original.cursor != record.source_cursor:
        raise AuditIntegrityError("iteration stage changed its actual original P4 cursor")
    return applied_batch_cut(decode_continuation_snapshot(original.payload_json).frames)[1]


def _prove_captured_data(conn: Connection, current: StoredContinuationSnapshot, record: _StageRecord) -> AdmittedTurnEvidence:
    """Reprove the original immutable closure; this grants no current lease."""
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
    from elspeth.web.composer.continuation_evidence_restore import read_selected_turn_evidence_graph_data_on_connection
    from elspeth.web.sessions.continuation_p4_application_store import require_p4_application_journal_data_on_connection
    from elspeth.web.sessions.continuation_selection import load_snapshot_private_data_on_connection

    if record.env_ref_names != tuple(sorted(set(record.env_ref_names))):
        raise AuditIntegrityError("iteration capture changed its original declared credential scope")
    sid, operation = str(record.session_id), record.operation_id
    selected = SelectedContinuationState(
        current.checkpoint_id,
        decode_continuation_snapshot(current.payload_json),
        load_snapshot_private_data_on_connection(conn, session_id=sid, operation_id=operation, selected=current),
    )
    names = frozenset(record.env_ref_names)
    admitted = read_selected_turn_evidence_graph_data_on_connection(
        conn, session_id=record.session_id, operation_id=operation, selected=selected, names=names
    )
    require_p4_application_journal_data_on_connection(
        conn, session_id=record.session_id, operation_id=operation, selected=selected, evidence=admitted, env_ref_names=names
    )
    return admitted


def _frontier(before: ContinuationFrameStack, record: _StageRecord, key: str) -> tuple[EffectFrontierEntry, ...]:
    _compose, batch = applied_batch_cut(before.frames)
    main, early = advance_effect_key(batch.response), f"advance-early:{batch.response.digest}"
    values = {item.effect_id: item for item in before.effect_frontier}
    if record.stage in ("early_ready", "classify_ready") and record.before_checkpoint_id == record.source_checkpoint_id:
        if main in values or early in values or any(item.disposition not in ("committed", "refused") for item in values.values()):
            raise AuditIntegrityError("iteration preparation cannot replace an existing or unresolved owner")
    elif record.stage == "early_dispatched":
        if main in values or early in values or any(item.disposition not in ("committed", "refused") for item in values.values()):
            raise AuditIntegrityError("EARLY begin replaced existing or unresolved work")
        values[early] = EffectFrontierEntry(effect_id=early, disposition="dispatched", receipt_id=None)
    elif record.stage == "classify_ready":
        if values.get(early) != EffectFrontierEntry(effect_id=early, disposition="dispatched", receipt_id=None):
            raise AuditIntegrityError("EARLY completion lost its actual dispatched owner")
        values[early] = EffectFrontierEntry(effect_id=early, disposition="committed", receipt_id=key)
    elif record.stage == "classify_dispatched":
        if main in values or any(item.disposition not in ("committed", "refused") for item in values.values()):
            raise AuditIntegrityError("classifier begin replaced existing or unresolved work")
        values[main] = EffectFrontierEntry(effect_id=main, disposition="dispatched", receipt_id=None)
    elif record.stage in ("end_ready", "reply_refusal_ready"):
        if values.get(main) != EffectFrontierEntry(effect_id=main, disposition="dispatched", receipt_id=None):
            raise AuditIntegrityError("pending END lost its actual original classifier execution")
        values[main] = EffectFrontierEntry(effect_id=main, disposition="committed", receipt_id=key)
    elif record.stage == "end_dispatched":
        if record.result_json is None:
            raise AuditIntegrityError("END begin lost its original ready DATA")
        end_key = _end_key(record.result_json)
        if end_key in values or any(item.disposition not in ("committed", "refused") for item in values.values()):
            raise AuditIntegrityError("END begin replaced existing or unresolved execution")
        values[end_key] = EffectFrontierEntry(effect_id=end_key, disposition="dispatched", receipt_id=None)
    elif record.stage == "reply_refusal_dispatched":
        if record.result_json is None:
            raise AuditIntegrityError("reply refusal begin lost its original ready DATA")
        refusal_key = _reply_refusal_key(record.result_json)
        if refusal_key in values or any(item.disposition not in ("committed", "refused") for item in values.values()):
            raise AuditIntegrityError("reply refusal begin replaced unresolved work")
        values[refusal_key] = EffectFrontierEntry(effect_id=refusal_key, disposition="dispatched", receipt_id=None)
    elif record.stage == "returned":
        entered_end = tuple(
            item
            for item in values.values()
            if item.effect_id.startswith(("iteration-end:", "iteration-refusal:")) and item.disposition == "dispatched"
        )
        if entered_end:
            if len(entered_end) != 1 or values.get(main) is None or values[main].disposition != "committed":
                raise AuditIntegrityError("iteration END return lost its exact original execution")
            values[entered_end[0].effect_id] = EffectFrontierEntry(
                effect_id=entered_end[0].effect_id, disposition="committed", receipt_id=key
            )
            return tuple(values.values())
        if values.get(main) != EffectFrontierEntry(effect_id=main, disposition="dispatched", receipt_id=None):
            raise AuditIntegrityError("classifier return lost its actual dispatched owner")
        values[main] = EffectFrontierEntry(effect_id=main, disposition="committed", receipt_id=key)
    else:
        raise AuditIntegrityError("iteration stage has no original source transition")
    return tuple(values.values())


def _prove_pending_source(conn: Connection, record: _StageRecord, before: StoredContinuationSnapshot) -> PrivateChunk | None:
    from elspeth.web.composer.continuation_classifier_handoff import ClassifyPostPrimary, ClassifySourceProofStop
    from elspeth.web.composer.continuation_classifier_owned_source import decode_classifier_owned_source
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
    from elspeth.web.sessions.continuation_classifier_primary import read_original_classifier_primary_on_connection

    if record.result_json is None:
        raise AuditIntegrityError("iteration pending END lost its complete original branch")
    pending = _decode_pending(record.result_json)
    original = _historical(conn, str(record.session_id), record.operation_id, checkpoint_id=pending.source_checkpoint_id)
    source = _stage_record(conn, original, str(record.session_id), record.operation_id)
    if original.cursor != pending.source_cursor or source is None or source[0].stage != "classify_dispatched":
        raise AuditIntegrityError("iteration pending END replaced its actual classifier source")
    _prove_record(conn, original, source[0], source[1])
    compose, _batch = applied_batch_cut(decode_continuation_snapshot(original.payload_json).frames)
    if (pending.composition_turns_used, pending.discovery_turns_used) != (compose.composition_slice, compose.discovery_slice):
        raise AuditIntegrityError("iteration pending END changed its original used counters")
    if type(pending) is ClassifyPostPrimary:
        paid = read_original_classifier_primary_on_connection(
            conn, session_id=record.session_id, operation_id=record.operation_id, effect_key=pending.primary_effect_key
        )
        if (
            paid.snapshot != before
            or paid.source_checkpoint_id != original.checkpoint_id
            or paid.source_cursor != original.cursor
            or paid.branch != pending.branch
            or paid.effect_key != pending.primary_effect_key
            or paid.input_sha256 != pending.primary_input_sha256
            or paid.response.reference != pending.response
            or paid.completion.tool_batch.calls
        ):
            raise AuditIntegrityError("iteration pending END changed its original physical no-tool reply")
        return paid.response
    if type(pending) is not ClassifySourceProofStop or before != original:
        raise AuditIntegrityError("source-proof stop cannot invent a classifier PRIMARY or another source cut")
    observed = _read_owned_observation(conn, record, pending_json=record.result_json)
    observation = decode_classifier_owned_source(observed.observation_json)
    if observation.kind != "source_proof_refusal" or (
        observation.source_checkpoint_id,
        observation.source_cursor,
        observation.proof_json,
    ) != (pending.source_checkpoint_id, pending.source_cursor, pending.proof_json):
        raise AuditIntegrityError("source-proof stop changed its actual original source observation")
    return None


def _prove_record(conn: Connection, current: StoredContinuationSnapshot, record: _StageRecord, key: str) -> None:
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot

    sid, operation = str(record.session_id), record.operation_id
    _creator(conn, record)
    before = _historical(conn, sid, operation, checkpoint_id=record.before_checkpoint_id)
    before_root = decode_continuation_snapshot(before.payload_json)
    before_compose, batch = applied_batch_cut(before_root.frames)
    source_batch = _source_batch(conn, record)
    _require_compose_capture(before_compose, record.compose)
    if (
        record.batch.model_copy(update={"accumulator": batch.accumulator}) != batch
        or record.batch.accumulator.codec != batch.accumulator.codec
    ):
        raise AuditIntegrityError("iteration capture changed its immutable original applied batch")
    if before.cursor != record.before_cursor or current.cursor != before.cursor + 1:
        raise AuditIntegrityError("iteration stage changed its exact predecessor cursor")
    previous = _stage_source_record(conn, before, sid, operation)
    if record.before_checkpoint_id == record.source_checkpoint_id:
        if (
            previous is not None
            or record.source_cursor != before.cursor
            or record.stage != ("early_ready" if record.early_required else "classify_ready")
        ):
            raise AuditIntegrityError("iteration preparation changed its original applied source")
    else:
        if previous is None:
            raise AuditIntegrityError("iteration paid stage has no prepared original source")
        parent, parent_key, parent_selected = previous
        _prove_record(conn, parent_selected, parent, parent_key)
        expected = {
            "early_dispatched": "early_ready",
            "classify_ready": "early_dispatched",
            "classify_dispatched": "classify_ready",
            "end_ready": "classify_dispatched",
            "end_dispatched": "end_ready",
            "reply_refusal_ready": "classify_dispatched",
            "reply_refusal_dispatched": "reply_refusal_ready",
        }
        if (
            parent.stage not in ("classify_dispatched", "end_dispatched", "reply_refusal_dispatched")
            if record.stage == "returned"
            else expected.get(record.stage) != parent.stage
        ) or record.model_copy(
            update={
                "before_checkpoint_id": parent.before_checkpoint_id,
                "before_cursor": parent.before_cursor,
                "stage": parent.stage,
                "compose": parent.compose,
                "batch": parent.batch,
                "result_json": parent.result_json,
                "creator_sequence": parent.creator_sequence,
                "creator_generation": parent.creator_generation,
                "creator_claim_attempt": parent.creator_claim_attempt,
                "creator_operation_id": parent.creator_operation_id,
                "creator_operation_epoch": parent.creator_operation_epoch,
            }
        ) != parent:
            raise AuditIntegrityError("iteration stage changed its original paid-stage lineage")
        if record.stage not in ("early_dispatched", "classify_dispatched", "end_dispatched", "reply_refusal_dispatched") and (
            record.creator_sequence,
            record.creator_generation,
            record.creator_claim_attempt,
            record.creator_operation_id,
            record.creator_operation_epoch,
        ) != (
            parent.creator_sequence,
            parent.creator_generation,
            parent.creator_claim_attempt,
            parent.creator_operation_id,
            parent.creator_operation_epoch,
        ):
            raise AuditIntegrityError("iteration completion cannot adopt another actual paid owner")
        if record.stage in (
            "early_dispatched",
            "classify_dispatched",
            "end_dispatched",
            "reply_refusal_ready",
            "reply_refusal_dispatched",
        ) and (record.compose != before_compose or record.batch != batch):
            raise AuditIntegrityError("iteration dispatch cannot change actual captured Compose or batch data")
    expected_root = before_root.model_copy(
        update={"cursor": current.cursor, "frames": (record.compose, record.batch), "effect_frontier": _frontier(before_root, record, key)}
    )
    if current.payload_json != encode_continuation_snapshot(expected_root):
        raise AuditIntegrityError("iteration stage changed its complete captured root")
    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == advance_effect_key(batch.response)
        )
    ).one_or_none()
    returned_from_end = record.stage == "returned" and previous is not None and previous[0].stage == "end_dispatched"
    returned_from_refusal = record.stage == "returned" and previous is not None and previous[0].stage == "reply_refusal_dispatched"
    if (
        record.stage in ("classify_dispatched", "end_ready", "reply_refusal_ready")
        or (record.stage == "returned" and not returned_from_end and not returned_from_refusal)
    ) and (
        effect is None
        or effect.owner != ITERATION_EFFECT_OWNER
        or effect.input_sha256 != stable_hash({"schema": "composer.post-batch.v1", "batch": source_batch.accumulator.digest})
        or effect.creator_sequence != record.creator_sequence
        or effect.creator_generation != record.creator_generation
        or effect.provider_attempt_id is not None
        or effect.action_json is not None
    ):
        raise AuditIntegrityError("iteration stage lost its actual original classifier effect")
    if (
        record.stage in ("end_ready", "reply_refusal_ready", "returned")
        and not returned_from_end
        and not returned_from_refusal
        and (effect is None or effect.disposition != "committed" or effect.receipt_key != key or effect.result_chunk_key != key)
    ):
        raise AuditIntegrityError("iteration return changed its committed original result pointers")
    if record.stage == "end_ready":
        _prove_pending_source(conn, record, before)
    if record.stage == "reply_refusal_ready":
        _prove_reply_refusal_ready(conn, record)
    if record.stage == "reply_refusal_dispatched" or returned_from_refusal:
        if previous is None or previous[0].result_json is None:
            raise AuditIntegrityError("reply refusal lost its actual original ready DATA")
        pending_json = previous[0].result_json
        if record.stage == "reply_refusal_dispatched" and record.result_json != pending_json:
            raise AuditIntegrityError("reply refusal begin changed its original ready DATA")
        execution = record if record.stage == "reply_refusal_dispatched" else previous[0]
        refusal = conn.execute(
            select(effects).where(
                effects.c.session_id == sid,
                effects.c.operation_id == operation,
                effects.c.effect_key == _reply_refusal_key(pending_json),
            )
        ).one_or_none()
        if (
            refusal is None
            or refusal.owner != ITERATION_EFFECT_OWNER
            or refusal.input_sha256 != _reply_refusal_input(execution)
            or refusal.creator_sequence != record.creator_sequence
            or refusal.creator_generation != record.creator_generation
            or refusal.provider_attempt_id is not None
            or refusal.action_json is not None
            or (
                returned_from_refusal
                and (refusal.disposition != "committed" or refusal.receipt_key != key or refusal.result_chunk_key != key)
            )
        ):
            raise AuditIntegrityError("reply refusal changed its original SQL execution source")
    if record.stage == "end_dispatched" or returned_from_end:
        if previous is None or previous[0].result_json is None:
            raise AuditIntegrityError("iteration END execution lost its original ready source")
        pending_json = previous[0].result_json
        if record.stage == "end_dispatched" and record.result_json != pending_json:
            raise AuditIntegrityError("iteration END begin changed its original pending DATA")
        end_key = _end_key(pending_json)
        input_source = _read_end_input(conn, record, pending_json)
        end = conn.execute(
            select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == end_key)
        ).one_or_none()
        if (
            end is None
            or end.owner != ITERATION_EFFECT_OWNER
            or end.input_sha256 != stable_hash(input_source.model_dump(mode="json"))
            or end.creator_sequence != record.creator_sequence
            or end.creator_generation != record.creator_generation
            or end.provider_attempt_id is not None
            or end.action_json is not None
            or (returned_from_end and (end.disposition != "committed" or end.receipt_key != key or end.result_chunk_key != key))
        ):
            raise AuditIntegrityError("iteration END changed its exact actual execution source")
    if record.stage == "early_dispatched" or (record.stage == "classify_ready" and record.early_required):
        early = conn.execute(
            select(effects).where(
                effects.c.session_id == sid,
                effects.c.operation_id == operation,
                effects.c.effect_key == f"advance-early:{batch.response.digest}",
            )
        ).one_or_none()
        if (
            early is None
            or early.owner != ITERATION_EFFECT_OWNER
            or early.input_sha256 != stable_hash({"schema": "composer.iteration-early.v1", "batch": source_batch.accumulator.digest})
            or early.creator_sequence != record.creator_sequence
            or early.creator_generation != record.creator_generation
            or early.provider_attempt_id is not None
            or early.action_json is not None
        ):
            raise AuditIntegrityError("iteration stage lost its exact original EARLY owner")
        if record.stage == "classify_ready" and (
            early.disposition != "committed" or early.receipt_key != key or early.result_chunk_key != key
        ):
            raise AuditIntegrityError("EARLY completion changed its committed original result pointers")
    if (record.stage in ("end_ready", "end_dispatched", "reply_refusal_ready", "reply_refusal_dispatched", "returned")) != (
        record.result_json is not None
    ):
        raise AuditIntegrityError("iteration stage changed its complete returned-result disposition")
    if record.stage in ("early_ready", "classify_ready", "end_ready", "reply_refusal_ready", "returned"):
        _prove_captured_data(conn, current, record)


def read_iteration_stage_data_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str, selected: StoredContinuationSnapshot
) -> IterationStageData | None:
    """Prove stage DATA before claim admission, without constructing Running."""
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot

    if type(session_id) is not UUID or type(operation_id) is not str or type(selected) is not StoredContinuationSnapshot:
        raise TypeError("iteration stage DATA requires exact original selected identity")
    sid, operation = str(session_id), operation_id
    current = _historical(conn, sid, operation, checkpoint_id=selected.checkpoint_id)
    if current != selected:
        raise AuditIntegrityError("iteration stage substituted its actual current checkpoint")
    root = decode_continuation_snapshot(current.payload_json)
    _compose, batch = applied_batch_cut(root.frames)
    main = advance_effect_key(batch.response)
    actual = set(
        conn.execute(
            select(effects.c.effect_key, effects.c.disposition, effects.c.receipt_key).where(
                effects.c.session_id == sid, effects.c.operation_id == operation
            )
        ).tuples()
    )
    expected = {(item.effect_id, item.disposition, item.receipt_id) for item in root.effect_frontier}
    if actual != expected:
        raise AuditIntegrityError("iteration stage lost its complete physical effects inventory")
    value = _stage_source_record(conn, current, sid, operation)
    if value is None:
        existing = conn.execute(
            select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == main)
        ).one_or_none()
        if existing is not None:
            if (
                existing.disposition == "committed"
                and type(existing.result_chunk_key) is str
                and existing.result_chunk_key.startswith("iteration-stage:")
            ):
                from elspeth.web.sessions.continuation_semantic_advisor import read_returned_iteration_descendant_data_on_connection

                original = read_returned_iteration_descendant_data_on_connection(
                    conn, session_id=session_id, operation_id=operation, selected=current, effect_key=main
                )
                receipt = _stage_record(conn, original.completed, sid, operation)
                if receipt is None or receipt[0].stage != "returned":
                    raise AuditIntegrityError("iteration descendant lost its original returned source")
                creator = receipt[0]
                return IterationStageData(
                    original.current_stage,
                    current,
                    original.original_source,
                    main,
                    f"advance-early:{batch.response.digest}",
                    creator.early_required,
                    creator.creator_sequence,
                    creator.creator_generation,
                )
            raise AuditIntegrityError("unversioned post-batch owner requires reconciliation, never paid replay")
        return None
    record, key, source_selected = value
    _prove_record(conn, source_selected, record, key)
    if record.stage in ("early_ready", "classify_ready"):
        future = conn.execute(
            select(effects.c.effect_key).where(
                effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == main
            )
        ).scalar_one_or_none()
        if future is not None:
            raise AuditIntegrityError("unpaid ready source cannot precreate a future classifier owner")
    if record.stage == "classify_dispatched":
        main_result = conn.execute(
            select(effects.c.result_chunk_key).where(
                effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == main
            )
        ).scalar_one()
        if main_result is not None:
            raise AuditIntegrityError("uncompleted classifier cannot name a committed result chunk")
    if record.stage == "early_dispatched":
        early_result = conn.execute(
            select(effects.c.result_chunk_key).where(
                effects.c.session_id == sid,
                effects.c.operation_id == operation,
                effects.c.effect_key == f"advance-early:{batch.response.digest}",
            )
        ).scalar_one()
        if early_result is not None:
            raise AuditIntegrityError("uncompleted EARLY owner cannot name a committed result chunk")
    return IterationStageData(
        record.stage,
        current,
        _historical(conn, sid, operation, checkpoint_id=record.source_checkpoint_id),
        main,
        f"advance-early:{batch.response.digest}",
        record.early_required,
        record.creator_sequence,
        record.creator_generation,
        _decode_pending(record.result_json) if record.stage in ("end_ready", "end_dispatched") and record.result_json is not None else None,
    )


def read_iteration_stage_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: StoredContinuationSnapshot
) -> IterationStageData | None:
    """Add actual current claim/Actor admission to the same stage DATA proof."""
    _require_current_continuation_running(conn, running)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    if load_snapshot(conn, session_id=sid, operation_id=operation) != selected:
        raise AuditIntegrityError("iteration stage substituted its actual current checkpoint")
    data = read_iteration_stage_data_on_connection(conn, session_id=running.claim.session_id, operation_id=operation, selected=selected)
    if data is not None and data.stage in ("early_dispatched", "classify_dispatched", "end_dispatched", "reply_refusal_dispatched"):
        actual = _stage_source_record(conn, selected, sid, operation)
        if actual is None:
            raise AuditIntegrityError("entered iteration stage lost its original execution source")
        record = actual[0]
        if (
            record.creator_sequence,
            record.creator_generation,
            record.creator_claim_attempt,
            record.creator_operation_id,
            record.creator_operation_epoch,
        ) != (
            running.claim.segment_sequence,
            running.claim.ownership_generation,
            running.claim.attempt,
            running.session_operation_context.fence.operation_id,
            running.session_operation_context.fence.operation_epoch,
        ):
            raise AuditIntegrityError("iteration paid stage cannot replay under a successor or another actual lease")
    return data


def read_original_classifier_stage_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str, selected: StoredContinuationSnapshot
) -> IterationStageData:
    """Prove original classifier-stage DATA without granting current SDK work."""
    if type(session_id) is not UUID or type(operation_id) is not str or type(selected) is not StoredContinuationSnapshot:
        raise TypeError("original classifier stage requires exact immutable SQL identity")
    sid = str(session_id)
    original = _historical(conn, sid, operation_id, checkpoint_id=selected.checkpoint_id)
    if original != selected:
        raise AuditIntegrityError("original classifier stage substituted its complete selected root")
    found = _stage_record(conn, original, sid, operation_id)
    if found is None or found[0].stage != "classify_dispatched":
        raise AuditIntegrityError("original classifier stage has no actual dispatched source")
    record, key = found
    _prove_record(conn, original, record, key)
    batch = _source_batch(conn, record)
    return IterationStageData(
        record.stage,
        original,
        _historical(conn, sid, operation_id, checkpoint_id=record.source_checkpoint_id),
        advance_effect_key(batch.response),
        f"advance-early:{batch.response.digest}",
        record.early_required,
        record.creator_sequence,
        record.creator_generation,
    )


def read_iteration_allowance_data_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str, selected: StoredContinuationSnapshot
) -> ContinuationActionPolicy | None:
    """Select original stage bounds for queued/claim DATA; no fabricated Running."""
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
    from elspeth.web.composer.continuation_next_action import iteration_stage_action_policy

    if type(session_id) is not UUID or type(operation_id) is not str or type(selected) is not StoredContinuationSnapshot:
        raise TypeError("iteration allowance requires exact selected SQL DATA")
    sid = str(session_id)
    original = _historical(conn, sid, operation_id, checkpoint_id=selected.checkpoint_id)
    if original != selected:
        raise AuditIntegrityError("iteration allowance substituted its original selected root")
    root = decode_continuation_snapshot(original.payload_json)
    try:
        applied_batch_cut(root.frames)
    except AuditIntegrityError:
        return None
    data = read_iteration_stage_data_on_connection(conn, session_id=session_id, operation_id=operation_id, selected=selected)
    if data is None:
        return None
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    return iteration_stage_action_policy(selected=selected, stage=data, policy=policy)


def _current(
    conn: Connection, running: ComposerOperationRunning, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory
) -> StoredContinuationSnapshot:
    _require_current_continuation_running(conn, running)
    require_current_env_inventory_on_connection(
        conn, running, inventory=inventory, expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.stack.cursor
    )
    actual = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
    if actual.checkpoint_id != selected.checkpoint_id or actual.payload_json != encode_continuation_snapshot(selected.stack):
        raise AuditIntegrityError("iteration writer changed its actual current selected source")
    if selected.stack.pending_owner_results or selected.stack.pending_settlement_ids or selected.stack.active_action_json is not None:
        raise AuditIntegrityError("iteration writer cannot replace pending work or a prescribed action")
    if applied_batch_cut(selected.stack.frames)[0].slice_sequence != running.claim.segment_sequence:
        raise AuditIntegrityError("iteration writer changed its actual current segment/slice")
    return actual


def _capture(selected: SelectedContinuationState, captured: CapturedContinuationRuntime) -> ComposeFrame:
    compose, batch = applied_batch_cut(selected.stack.frames)
    following, following_batch = applied_batch_cut(captured.stack.frames)
    if (
        captured.selected is not selected
        or captured.stack.model_copy(update={"frames": selected.stack.frames}) != selected.stack
        or following_batch.model_copy(update={"accumulator": batch.accumulator}) != batch
    ):
        raise AuditIntegrityError("iteration capture changed its actual selected/P4 owners")
    _require_compose_capture(compose, following)
    return following


def read_iteration_applied_p4_result_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
) -> AppliedIterationP4Data:
    """Return the original P4 DATA after the actual current journal proof."""
    from elspeth.web.sessions.continuation_p4_application_store import require_p4_application_journal_data_on_connection

    current = _current(conn, running, selected, inventory)
    stage = read_iteration_stage_on_connection(conn, running, selected=current)
    _compose, batch = applied_batch_cut(selected.stack.frames)
    admitted = read_selected_turn_evidence_graph_on_connection(conn, running, selected=selected, names=inventory.names)
    results = require_p4_application_journal_data_on_connection(
        conn,
        session_id=running.claim.session_id,
        operation_id=running.claim.operation_id,
        selected=selected,
        evidence=admitted,
        env_ref_names=inventory.names,
    )
    key = f"batch:{batch.response.digest}:p4"
    matching = tuple(result for result in results if result.receipt.effect_key == key)
    if len(matching) != 1:
        raise AuditIntegrityError("iteration cold batch lost its exact selected original P4 receipt")
    return AppliedIterationP4Data(matching[0], stage)


def _retain(
    conn: Connection,
    running: ComposerOperationRunning,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    record: _StageRecord,
    captured: CapturedContinuationRuntime | None,
    *,
    end_input: _EndInputRecord | None = None,
) -> StoredContinuationSnapshot:
    current = _current(conn, running, selected, inventory)
    canonical = canonical_json(record.model_dump(mode="json"))
    if len(canonical.encode("utf-8")) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("iteration stage exceeds its complete private document bound")
    require_no_credential_material(
        canonical,
        surface="composer_continuation_iteration_stage",
        env_ref_names=inventory.names,
        max_string_bytes=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
    )
    key = _key(canonical)
    frontier = _frontier(selected.stack, record, key)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    if captured is not None:
        for chunk in captured.chunks:
            put_chunk(
                conn,
                session_id=sid,
                operation_id=operation,
                key=private_chunk_key(chunk),
                payload=chunk.canonical.encode("utf-8"),
                codec=chunk.reference.codec,
            )
    put_chunk(conn, session_id=sid, operation_id=operation, key=key, payload=canonical.encode("utf-8"), codec=record.schema_version)
    _compose, batch = applied_batch_cut(selected.stack.frames)
    source_batch = _source_batch(conn, record)
    main, early = advance_effect_key(batch.response), f"advance-early:{batch.response.digest}"
    initial = record.before_checkpoint_id == record.source_checkpoint_id
    if record.stage in ("early_dispatched", "classify_dispatched", "end_dispatched", "reply_refusal_dispatched"):
        if record.stage == "reply_refusal_dispatched":
            if record.result_json is None:
                raise AuditIntegrityError("reply refusal dispatch lost its original ready DATA")
            owner_key = _reply_refusal_key(record.result_json)
        elif record.stage == "end_dispatched":
            if record.result_json is None:
                raise AuditIntegrityError("iteration END dispatch lost its exact ready source")
            owner_key = _end_key(record.result_json)
        else:
            owner_key = early if record.stage == "early_dispatched" else main
        if record.stage == "classify_dispatched":
            input_hash = stable_hash({"schema": "composer.post-batch.v1", "batch": source_batch.accumulator.digest})
        elif record.stage == "early_dispatched":
            input_hash = stable_hash({"schema": "composer.iteration-early.v1", "batch": source_batch.accumulator.digest})
        elif record.stage == "reply_refusal_dispatched":
            input_hash = _reply_refusal_input(record)
        else:
            if record.result_json is None or end_input is None:
                raise AuditIntegrityError("iteration END dispatch lost its original source bytes")
            input_canonical = canonical_json(end_input.model_dump(mode="json"))
            put_chunk(
                conn,
                session_id=sid,
                operation_id=operation,
                key=_end_input_key(record.result_json),
                payload=input_canonical.encode("utf-8"),
                codec=end_input.schema_version,
            )
            input_hash = stable_hash(end_input.model_dump(mode="json"))
        conn.execute(
            insert(effects).values(
                session_id=sid,
                operation_id=operation,
                effect_key=owner_key,
                input_sha256=input_hash,
                owner=ITERATION_EFFECT_OWNER,
                disposition="dispatched",
                creator_sequence=record.creator_sequence,
                creator_generation=record.creator_generation,
            )
        )
    elif not initial:
        owner_key = early if record.stage == "classify_ready" else main
        if record.stage == "returned":
            original_stage = _stage_source_record(conn, current, sid, operation)
            if original_stage is not None and original_stage[0].stage == "end_dispatched":
                if original_stage[0].result_json is None:
                    raise AuditIntegrityError("iteration END completion lost its immutable pending source")
                owner_key = _end_key(original_stage[0].result_json)
            elif original_stage is not None and original_stage[0].stage == "reply_refusal_dispatched":
                if original_stage[0].result_json is None:
                    raise AuditIntegrityError("reply refusal completion lost its original ready DATA")
                owner_key = _reply_refusal_key(original_stage[0].result_json)
        complete = record.stage in ("classify_ready", "end_ready", "reply_refusal_ready", "returned")
        changed = conn.execute(
            update(effects)
            .where(
                effects.c.session_id == sid,
                effects.c.operation_id == operation,
                effects.c.effect_key == owner_key,
                effects.c.owner == ITERATION_EFFECT_OWNER,
                effects.c.creator_sequence == record.creator_sequence,
                effects.c.creator_generation == record.creator_generation,
                effects.c.disposition == ("dispatched" if complete else "prepared"),
                effects.c.receipt_key.is_(None),
                effects.c.result_chunk_key.is_(None),
            )
            .values(
                disposition="committed" if complete else "dispatched",
                receipt_key=key if complete else None,
                result_chunk_key=key if complete else None,
            )
        )
        if changed.rowcount != 1:
            raise AuditIntegrityError("iteration stage lost its original prepared/dispatched effect")
    policy = decode_continuation_policy(selected.stack.admission_policy_json, selected.stack.policy_hash)
    conn.execute(
        update(segments)
        .where(
            segments.c.session_id == sid,
            segments.c.operation_id == operation,
            segments.c.sequence == running.claim.segment_sequence,
            segments.c.generation == running.claim.ownership_generation,
        )
        .values(
            next_action_json=(
                policy.actions.session_sql
                if record.stage.startswith("reply_refusal")
                else policy.actions.advisor
                if record.stage.startswith(("early", "end")) or record.stage == "returned"
                else policy.actions.primary
            ).model_dump_json()
        )
    )
    after = selected.stack.model_copy(update={"frames": (record.compose, record.batch), "effect_frontier": frontier})
    retained = append_complete_snapshot(
        conn,
        session_id=sid,
        operation_id=operation,
        expected_checkpoint_id=current.checkpoint_id,
        expected_cursor=current.cursor,
        snapshot_json=encode_continuation_snapshot(after),
        event_batch=(ContinuationEvent("checkpoint", _stage_event(record, key)),),
        handoff=False,
    )
    _prove_record(conn, retained, record, key)
    if captured is not None:
        from elspeth.web.composer.continuation_codec import decode_continuation_snapshot

        root = decode_continuation_snapshot(retained.payload_json)
        actual = SelectedContinuationState(retained.checkpoint_id, root, _selected_chunks(root, captured.chunks, selected.chunks))
        admitted = read_selected_turn_evidence_graph_on_connection(conn, running, selected=actual, names=inventory.names)
        from elspeth.web.sessions.continuation_p4_application_store import require_p4_application_journal_on_connection

        require_p4_application_journal_on_connection(conn, running, selected=actual, evidence=admitted, env_ref_names=inventory.names)
    return retained


def _prove_reply_refusal_ready(conn: Connection, record: _StageRecord) -> ClassifyReplyRefusalReady:
    from elspeth.web.composer.continuation_classifier_owned_source import decode_classify_reply_refusal_ready

    if record.result_json is None:
        raise AuditIntegrityError("reply refusal lost its original complete ready DATA")
    pending = decode_classify_reply_refusal_ready(record.result_json)
    original = _read_owned_observation(conn, record, pending_json=None)
    observation, source, _evidence = _prove_owned_observation(conn, record, original)
    if (
        observation.kind != "reply_deadline_exhausted"
        or pending.observation_json != original.observation_json
        or (pending.source_checkpoint_id, pending.source_cursor) != (record.before_checkpoint_id, record.before_cursor)
        or (pending.composition_turns_used, pending.discovery_turns_used)
        != (source.compose.composition_slice, source.compose.discovery_slice)
    ):
        raise AuditIntegrityError("reply refusal changed its exact original unpaid source")
    return pending


def retain_iteration_reply_deadline_ready(
    authority: ComposerAsyncOperationAuthority,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    observed: ObservedClassifierReplyDeadline,
    action_clock: ContinuationActionClock,
    phases: ContinuationLivePhases,
) -> StoredContinuationSnapshot:
    """Retain only actual never-invoked refusal DATA in the original reserve."""
    from elspeth.web.composer.continuation_classifier_owned_source import (
        ClassifyReplyRefusalReady,
        encode_classifier_owned_source,
        encode_classify_reply_refusal_ready,
    )
    from elspeth.web.composer.continuation_semantic_advisor_return import require_original_classifier_reply_deadline

    with locked_session_transaction(authority._engine, str(running.claim.session_id)) as conn:
        actual = require_original_classifier_reply_deadline(observed, phases=phases, running=running, action_clock=action_clock)
        if actual.selected is not selected or actual.inventory is not inventory:
            raise AuditIntegrityError("reply deadline disposition substituted its actual original selected source")
        current = _current(conn, running, selected, inventory)
        stage = read_iteration_stage_on_connection(conn, running, selected=current)
        previous = _stage_source_record(conn, current, str(running.claim.session_id), running.claim.operation_id)
        if stage is None or stage.stage != "classify_dispatched" or previous is None:
            raise AuditIntegrityError("reply deadline disposition lost its actual original classifier")
        parent = previous[0]
        encoded_observation = encode_classifier_owned_source(actual.token.observation)
        pending = ClassifyReplyRefusalReady(
            source_checkpoint_id=current.checkpoint_id,
            source_cursor=current.cursor,
            observation_json=encoded_observation,
            runtime_preflight_json=actual.token.runtime_preflight_json,
            composition_turns_used=parent.compose.composition_slice,
            discovery_turns_used=parent.compose.discovery_slice,
        )
        record = parent.model_copy(
            update={
                "before_checkpoint_id": current.checkpoint_id,
                "before_cursor": current.cursor,
                "stage": "reply_refusal_ready",
                "result_json": encode_classify_reply_refusal_ready(pending),
            }
        )
        _store_owned_observation(conn, record, encoded_observation, pending_json=None)
        _prove_reply_refusal_ready(conn, record)
        return _retain(conn, running, selected, inventory, record, None)


def read_iteration_reply_refusal(
    authority: ComposerAsyncOperationAuthority,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
) -> ClassifyReplyRefusalReady:
    with locked_session_transaction(authority._engine, str(running.claim.session_id)) as conn:
        current = _current(conn, running, selected, inventory)
        stage = read_iteration_stage_on_connection(conn, running, selected=current)
        source = _stage_source_record(conn, current, str(running.claim.session_id), running.claim.operation_id)
        if stage is None or source is None or stage.stage not in ("reply_refusal_ready", "reply_refusal_dispatched"):
            raise AuditIntegrityError("reply refusal has no actual current ready/executing source")
        record = source[0]
        if record.stage == "reply_refusal_dispatched":
            before = _historical(conn, str(running.claim.session_id), running.claim.operation_id, checkpoint_id=record.before_checkpoint_id)
            ready = _stage_source_record(conn, before, str(running.claim.session_id), running.claim.operation_id)
            if ready is None or ready[0].stage != "reply_refusal_ready" or ready[0].result_json != record.result_json:
                raise AuditIntegrityError("reply refusal execution changed its immutable original ready source")
            record = ready[0]
        return _prove_reply_refusal_ready(conn, record)


def begin_iteration_reply_refusal(
    authority: ComposerAsyncOperationAuthority,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
) -> StoredContinuationSnapshot:
    with locked_session_transaction(authority._engine, str(running.claim.session_id)) as conn:
        current = _current(conn, running, selected, inventory)
        stage = read_iteration_stage_on_connection(conn, running, selected=current)
        source = _stage_source_record(conn, current, str(running.claim.session_id), running.claim.operation_id)
        if stage is None or stage.stage != "reply_refusal_ready" or source is None:
            raise AuditIntegrityError("reply refusal begin cannot adopt or replay an entered execution")
        _prove_reply_refusal_ready(conn, source[0])
        compose, batch = applied_batch_cut(selected.stack.frames)
        record = source[0].model_copy(
            update={
                "before_checkpoint_id": current.checkpoint_id,
                "before_cursor": current.cursor,
                "stage": "reply_refusal_dispatched",
                "compose": compose,
                "batch": batch,
                "creator_sequence": running.claim.segment_sequence,
                "creator_generation": running.claim.ownership_generation,
                "creator_claim_attempt": running.claim.attempt,
                "creator_operation_id": running.session_operation_context.fence.operation_id,
                "creator_operation_epoch": running.session_operation_context.fence.operation_epoch,
            }
        )
        return _retain(conn, running, selected, inventory, record, None)


def retain_iteration_prepared(
    authority: ComposerAsyncOperationAuthority,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    captured: CapturedContinuationRuntime,
) -> StoredContinuationSnapshot:
    """Retain the pure actual batch fold before selecting any paid action."""
    compose = _capture(selected, captured)
    with locked_session_transaction(authority._engine, str(running.claim.session_id)) as conn:
        current = _current(conn, running, selected, inventory)
        if read_iteration_stage_on_connection(conn, running, selected=current) is not None:
            raise AuditIntegrityError("iteration fold already has an original stage owner")
        evidence = read_selected_turn_evidence_graph_on_connection(conn, running, selected=selected, names=inventory.names)
        graph = evidence.graph
        if graph is None or graph.selected.batch.phase != "complete":
            raise AuditIntegrityError("iteration fold lost its actual complete applied P4 graph")
        early = state_is_structurally_empty(graph.selected.batch.entry_state) and not state_is_structurally_empty(
            graph.selected.batch.state
        )
        fence = running.session_operation_context.fence
        record = _StageRecord(
            session_id=running.claim.session_id,
            operation_id=running.claim.operation_id,
            source_checkpoint_id=current.checkpoint_id,
            source_cursor=current.cursor,
            before_checkpoint_id=current.checkpoint_id,
            before_cursor=current.cursor,
            stage="early_ready" if early else "classify_ready",
            early_required=early,
            creator_sequence=running.claim.segment_sequence,
            creator_generation=running.claim.ownership_generation,
            creator_claim_attempt=running.claim.attempt,
            creator_operation_id=fence.operation_id,
            creator_operation_epoch=fence.operation_epoch,
            env_ref_names=tuple(sorted(inventory.names)),
            compose=compose,
            batch=applied_batch_cut(captured.stack.frames)[1],
        )
        return _retain(conn, running, selected, inventory, record, captured)


def begin_iteration_paid_stage(
    authority: ComposerAsyncOperationAuthority,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    stage: Literal["early", "classify", "end"],
    end_input: ObservedIterationEndInput | None = None,
) -> StoredContinuationSnapshot:
    with locked_session_transaction(authority._engine, str(running.claim.session_id)) as conn:
        current = _current(conn, running, selected, inventory)
        data = read_iteration_stage_on_connection(conn, running, selected=current)
        if data is None or data.stage != f"{stage}_ready":
            raise AuditIntegrityError("iteration paid begin has no exact current ready source")
        previous = _stage_source_record(conn, current, str(running.claim.session_id), running.claim.operation_id)
        if previous is None:
            raise AuditIntegrityError("iteration paid begin lost its original stage record")
        record = previous[0].model_copy(
            update={
                "before_checkpoint_id": current.checkpoint_id,
                "before_cursor": current.cursor,
                "stage": f"{stage}_dispatched",
                "compose": applied_batch_cut(selected.stack.frames)[0],
                "batch": applied_batch_cut(selected.stack.frames)[1],
                "creator_sequence": running.claim.segment_sequence,
                "creator_generation": running.claim.ownership_generation,
                "creator_claim_attempt": running.claim.attempt,
                "creator_operation_id": running.session_operation_context.fence.operation_id,
                "creator_operation_epoch": running.session_operation_context.fence.operation_epoch,
            }
        )
        original_input = None
        if stage == "end":
            from elspeth.web.composer.continuation_semantic_advisor_return import require_original_iteration_end_input

            if end_input is None or record.result_json is None:
                raise AuditIntegrityError("iteration END requires its original live candidate observation")
            binding = require_original_iteration_end_input(end_input, running, selected, pending_json=record.result_json)
            last_message = conn.execute(
                select(func.max(messages.c.sequence_no)).where(messages.c.session_id == str(record.session_id))
            ).scalar_one()
            original_input = _EndInputRecord(
                session_id=record.session_id,
                operation_id=record.operation_id,
                ready_stage_key=previous[1],
                before_checkpoint_id=current.checkpoint_id,
                pending_sha256=sha256(record.result_json.encode("utf-8")).hexdigest(),
                publication_binding=binding,
                messages_before=-1 if last_message is None else last_message,
                creator_sequence=record.creator_sequence,
                creator_generation=record.creator_generation,
                creator_claim_attempt=record.creator_claim_attempt,
                creator_operation_id=record.creator_operation_id,
                creator_operation_epoch=record.creator_operation_epoch,
            )
        elif end_input is not None:
            raise AuditIntegrityError("only the actual END owner accepts an END input observation")
        return _retain(conn, running, selected, inventory, record, None, end_input=original_input)


def retain_iteration_early_completed(
    authority: ComposerAsyncOperationAuthority,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    observed: ObservedIterationEarly,
    captured: CapturedContinuationRuntime,
) -> StoredContinuationSnapshot:
    from elspeth.web.composer.continuation_semantic_advisor_return import require_original_iteration_early

    require_original_iteration_early(observed, running, selected, captured)
    compose = _capture(selected, captured)
    with locked_session_transaction(authority._engine, str(running.claim.session_id)) as conn:
        current = _current(conn, running, selected, inventory)
        data = read_iteration_stage_on_connection(conn, running, selected=current)
        previous = _stage_record(conn, current, str(running.claim.session_id), running.claim.operation_id)
        if data is None or data.stage != "early_dispatched" or previous is None:
            raise AuditIntegrityError("EARLY completion lost its exact actual dispatched stage")
        record = previous[0].model_copy(
            update={
                "before_checkpoint_id": current.checkpoint_id,
                "before_cursor": current.cursor,
                "stage": "classify_ready",
                "compose": compose,
                "batch": applied_batch_cut(captured.stack.frames)[1],
            }
        )
        return _retain(conn, running, selected, inventory, record, captured)


def retain_iteration_pending_end(
    authority: ComposerAsyncOperationAuthority,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    observed: ObservedClassification,
    captured: CapturedContinuationRuntime,
) -> StoredContinuationSnapshot:
    """Close actual classifier work to immutable DATA before a new END action."""
    from elspeth.web.composer.continuation_classifier_handoff import ClassifySourceProofStop
    from elspeth.web.composer.continuation_semantic_advisor_return import (
        require_original_classification_owned_source,
        require_original_classification_pending,
    )

    pending = require_original_classification_pending(observed, running, selected, captured)
    observation_json = require_original_classification_owned_source(observed, running, selected, captured)
    compose = _capture(selected, captured)
    encoded = _encode_pending(pending)
    with locked_session_transaction(authority._engine, str(running.claim.session_id)) as conn:
        current = _current(conn, running, selected, inventory)
        data = read_iteration_stage_on_connection(conn, running, selected=current)
        previous = _stage_source_record(conn, current, str(running.claim.session_id), running.claim.operation_id)
        if data is None or data.stage != "classify_dispatched" or previous is None:
            raise AuditIntegrityError("pending END lost its actual original classifier execution")
        record = previous[0].model_copy(
            update={
                "before_checkpoint_id": current.checkpoint_id,
                "before_cursor": current.cursor,
                "stage": "end_ready",
                "compose": compose,
                "batch": applied_batch_cut(captured.stack.frames)[1],
                "result_json": encoded,
            }
        )
        if type(pending) is ClassifySourceProofStop:
            if observation_json is None:
                raise AuditIntegrityError("source-proof stop lost its actual private observation")
            _store_owned_observation(conn, record, observation_json, pending_json=encoded)
        elif observation_json is not None:
            raise AuditIntegrityError("paid classifier END cannot borrow a source-proof observation")
        _prove_pending_source(conn, record, current)
        return _retain(conn, running, selected, inventory, record, captured)


def read_iteration_pending_end_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
) -> IterationPendingEndData:
    current = _current(conn, running, selected, inventory)
    stage = read_iteration_stage_on_connection(conn, running, selected=current)
    found = _stage_source_record(conn, current, str(running.claim.session_id), running.claim.operation_id)
    if stage is None or stage.stage not in ("end_ready", "end_dispatched") or found is None:
        raise AuditIntegrityError("iteration END has no actual current pending source")
    record, key, _original = found
    if record.stage == "end_dispatched":
        predecessor = _historical(
            conn, str(running.claim.session_id), running.claim.operation_id, checkpoint_id=record.before_checkpoint_id
        )
        ready = _stage_source_record(conn, predecessor, str(running.claim.session_id), running.claim.operation_id)
        if ready is None or ready[0].stage != "end_ready":
            raise AuditIntegrityError("iteration END lost its original unpaid ready source")
        record, key, _original = ready
    if record.stage != "end_ready" or record.result_json is None:
        raise AuditIntegrityError("iteration END lost its immutable complete phase result")
    response = _prove_pending_source(
        conn,
        record,
        _historical(conn, str(running.claim.session_id), running.claim.operation_id, checkpoint_id=record.before_checkpoint_id),
    )
    from elspeth.web.composer.continuation_classifier_handoff import ClassifySourceProofStop

    pending = _decode_pending(record.result_json)
    observation_json = (
        _read_owned_observation(conn, record, pending_json=record.result_json).observation_json
        if type(pending) is ClassifySourceProofStop
        else None
    )
    return IterationPendingEndData(stage, pending, key, _end_key(record.result_json), response, observation_json)


def read_iteration_pending_end(
    authority: ComposerAsyncOperationAuthority,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
) -> IterationPendingEndData:
    with locked_session_transaction(authority._engine, str(running.claim.session_id)) as conn:
        return read_iteration_pending_end_on_connection(conn, running, selected=selected, inventory=inventory)


def _original_end_pass_id(conn: Connection, record: _StageRecord, publication_binding: str) -> str:
    from elspeth.web.composer.advisor_audit import ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
    from elspeth.web.sessions.continuation_end_pass_source import read_original_accepted_end_pass_on_connection
    from elspeth.web.sessions.models import chat_messages_table as messages

    ids = (
        conn.execute(
            select(messages.c.id).where(
                messages.c.session_id == str(record.session_id),
                messages.c.role == "audit",
                messages.c.writer_principal == "compose_loop",
                messages.c.tool_calls[0]["_kind"].as_string() == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND,
                messages.c.tool_calls[0]["pass"]["phase"].as_string() == "end",
                messages.c.tool_calls[0]["pass"]["source"].as_string() == "model",
                messages.c.tool_calls[0]["pass"]["publication_binding"].as_string() == publication_binding,
                messages.c.tool_calls[0]["pass"]["pass_index"].as_integer() == record.compose.advisor_checkpoint_passes,
            )
        )
        .scalars()
        .all()
    )
    if len(ids) != 1:
        raise AuditIntegrityError("iteration return lost its unique original accepted model END pass")
    actual = read_original_accepted_end_pass_on_connection(
        conn,
        session_id=record.session_id,
        message_id=ids[0],
        pass_index=record.compose.advisor_checkpoint_passes,
        publication_binding=publication_binding,
    )
    return actual.message_id


def require_clean_iteration_return_comparands(
    *, result: ComposerResult, captured: ComposeFrame, state_chunk: PrivateChunk, before: ComposeFrame, pass_index: int
) -> None:
    """Check actual terminal DATA; this issues no END/publication authority."""
    from elspeth.web.composer.continuation_codec import verify_private_chunk

    verify_private_chunk(captured.state, state_chunk.canonical)
    if state_chunk.reference != captured.state or canonical_json(result.state.to_dict()) != state_chunk.canonical:
        raise AuditIntegrityError("clean iteration result substituted its actual captured state")
    if type(pass_index) is not int or pass_index != captured.advisor_checkpoint_passes or pass_index <= before.advisor_checkpoint_passes:
        raise AuditIntegrityError("clean iteration result reused a model pass from before its END execution")


def require_clean_iteration_end_pass_source(
    *, binding: str, original_binding: str | None, pass_sequence: int | None, messages_before: int
) -> None:
    """Compare source DATA only; callers still independently prove SQL custody."""
    if binding != original_binding or pass_sequence is None or pass_sequence <= messages_before:
        raise AuditIntegrityError("clean iteration result substituted a pass from another END input")


def _historical_value(conn: Connection, record: _StageRecord, reference: PrivateChunkRef) -> PrivateChunk:
    from elspeth.web.composer.continuation_codec import PrivateChunk, verify_private_chunk

    sid, operation = str(record.session_id), record.operation_id
    key = f"value:{reference.codec}:{reference.digest}"
    codec = conn.execute(
        select(chunks.c.codec).where(chunks.c.session_id == sid, chunks.c.operation_id == operation, chunks.c.chunk_key == key)
    ).scalar_one_or_none()
    canonical = read_chunk(conn, session_id=sid, operation_id=operation, key=key).decode("utf-8", errors="strict")
    if codec != reference.codec:
        raise AuditIntegrityError("iteration terminal changed its original source value codec")
    verify_private_chunk(reference, canonical)
    return PrivateChunk(reference, canonical)


def _owned_observation_key(record: _StageRecord) -> str:
    return f"iteration-owned-observation:{record.batch.response.digest}"


def _owned_completion_key(record: _StageRecord) -> str:
    return f"iteration-owned-completion:{record.batch.response.digest}"


def _owned_text(conn: Connection, record: _StageRecord, key: str, codec: str) -> str:
    stored_codec = conn.execute(
        select(chunks.c.codec).where(
            chunks.c.session_id == str(record.session_id),
            chunks.c.operation_id == record.operation_id,
            chunks.c.chunk_key == key,
        )
    ).scalar_one_or_none()
    if stored_codec != codec:
        raise AuditIntegrityError("owned classifier terminal lost its original observation codec")
    return read_chunk(conn, session_id=str(record.session_id), operation_id=record.operation_id, key=key).decode("utf-8", errors="strict")


def _prove_owned_observation(
    conn: Connection,
    record: _StageRecord,
    value: _OwnedObservationRecord,
) -> tuple[ClassifierOwnedSourceObservation, _StageRecord, AdmittedTurnEvidence]:
    from elspeth.web.composer.continuation_classifier_owned_source import decode_classifier_owned_source

    observation = decode_classifier_owned_source(value.observation_json)
    snapshot = _historical(conn, str(record.session_id), record.operation_id, checkpoint_id=observation.source_checkpoint_id)
    source = _stage_record(conn, snapshot, str(record.session_id), record.operation_id)
    if (
        source is None
        or source[0].stage != "classify_dispatched"
        or snapshot.cursor != observation.source_cursor
        or source[1] != value.source_record_key
        or value.session_id != record.session_id
        or value.operation_id != record.operation_id
        or value.owner_effect_key != advance_effect_key(record.batch.response)
        or source[0].batch.response != record.batch.response
        or (observation.physical_operation_id, observation.physical_operation_epoch)
        != (source[0].creator_operation_id, source[0].creator_operation_epoch)
    ):
        raise AuditIntegrityError("owned classifier refusal substituted its original stage or physical observation")
    _prove_record(conn, snapshot, source[0], source[1])
    state = _historical_value(conn, source[0], source[0].compose.state)
    if observation.state_sha256 != sha256(state.canonical.encode("utf-8")).hexdigest():
        raise AuditIntegrityError("owned classifier refusal changed its original observed state")
    evidence = _prove_captured_data(conn, snapshot, source[0])
    graph = evidence.graph
    if graph is None or graph.selected.batch.phase != "complete":
        raise AuditIntegrityError("owned classifier refusal lost its immutable complete P4 observation")
    from elspeth.web.composer.service import _tool_batch_staged_terminal_interpretation_review_handoff

    batch = graph.selected.batch
    staged = _tool_batch_staged_terminal_interpretation_review_handoff(tuple(batch.tool_outcomes))
    if observation.kind == "source_proof_refusal":
        from elspeth.web.composer.continuation_classifier_handoff import (
            decode_classifier_source_stop_proof,
            encode_classifier_source_stop_proof,
        )
        from elspeth.web.composer.discovery_response import admit_discovery_result, preview_source_proof_stop
        from elspeth.web.composer.tools import ToolResult
        from elspeth.web.composer.tools.generation import MalformedSourceProofClaim, ProofUnavailable, _admit_source_proof_claims

        if observation.proof_json is None:
            raise AuditIntegrityError("owned source refusal lost its actual proof")
        observed_proof = decode_classifier_source_stop_proof(observation.proof_json)
        try:
            claims = {name: blob for name, _source, blob in _admit_source_proof_claims(batch.state)}
        except MalformedSourceProofClaim as actual_claim:
            if (
                type(observed_proof) is not MalformedSourceProofClaim
                or encode_classifier_source_stop_proof(actual_claim) != observation.proof_json
            ):
                raise AuditIntegrityError("source refusal changed its actual malformed source claim") from actual_claim
        else:
            if type(observed_proof) is MalformedSourceProofClaim:
                raise AuditIntegrityError("source refusal invented a malformed source claim")
            if type(observed_proof) is not ProofUnavailable:
                raise AuditIntegrityError("source refusal lost its original unavailable observation")
            for diagnostic in observed_proof.diagnostics:
                locator = diagnostic["evidence_locator"]
                if diagnostic["code"] == "source_blob_unavailable":
                    source_name, blob_id = locator.get("source_name"), locator.get("blob_id")
                    if type(source_name) is not str or type(blob_id) is not str or claims.get(source_name) != blob_id:
                        raise AuditIntegrityError("source refusal changed its original claimed blob scope")
        if observation.proof_origin == "preview":
            terminal = batch.tool_outcomes[-1] if batch.tool_outcomes else None
            if (
                terminal is None
                or terminal.call.id != observation.terminal_tool_call_id
                or terminal.call.function.name != "preview_pipeline"
                or terminal.error_class is not None
                or not isinstance(terminal.response, ToolResult)
            ):
                raise AuditIntegrityError("source refusal substituted its original terminal preview")
            proof = preview_source_proof_stop(admit_discovery_result("preview_pipeline", terminal.response), batch.state)
            if proof is None or encode_classifier_source_stop_proof(proof) != observation.proof_json:
                raise AuditIntegrityError("source refusal changed its original P4 proof observation")
        elif observation.assessment_trigger == "staged_review":
            if not staged:
                raise AuditIntegrityError("source assessment invented a staged-review boundary")
        elif (
            observation.assessment_trigger != "composition_limit"
            or observation.composition_limit is None
            or not batch.turn_has_mutation
            or source[0].compose.composition_slice + 1 < observation.composition_limit
        ):
            raise AuditIntegrityError("source assessment changed its original composition boundary")
        # Assessment availability is an original point-in-time owned fact.
        # Re-reading a now-ready blob would change the historical decision.
    elif not staged:
        raise AuditIntegrityError("reply refusal lost its actual staged P4 handoff")
    if observation.kind in ("source_proof_refusal", "reply_deadline_exhausted"):
        anchor = conn.execute(
            select(chunks.c.chunk_key).where(
                chunks.c.session_id == str(record.session_id),
                chunks.c.operation_id == record.operation_id,
                chunks.c.chunk_key == f"classifier-primary-stage:{value.owner_effect_key}",
            )
        ).scalar_one_or_none()
        if anchor is not None:
            raise AuditIntegrityError("unpaid classifier refusal cannot relabel an entered or unknown provider source")
    if observation.kind == "reply_deadline_exhausted":
        from elspeth.web.composer.continuation_codec import decode_continuation_snapshot

        root = decode_continuation_snapshot(snapshot.payload_json)
        policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
        if observation.clock_policy_json != canonical_json(policy.actions.primary.model_dump(mode="json")):
            raise AuditIntegrityError("reply time exhaustion substituted its original admitted action policy")
    return observation, source[0], evidence


def _store_owned_observation(
    conn: Connection,
    record: _StageRecord,
    observation_json: str,
    *,
    pending_json: str | None,
) -> _OwnedObservationRecord:
    from elspeth.web.composer.continuation_classifier_owned_source import decode_classifier_owned_source

    observation = decode_classifier_owned_source(observation_json)
    original = _historical(conn, str(record.session_id), record.operation_id, checkpoint_id=observation.source_checkpoint_id)
    source = _stage_record(conn, original, str(record.session_id), record.operation_id)
    if source is None:
        raise AuditIntegrityError("owned classifier observation has no original stage")
    value = _OwnedObservationRecord(
        session_id=record.session_id,
        operation_id=record.operation_id,
        owner_effect_key=advance_effect_key(record.batch.response),
        source_record_key=source[1],
        observation_json=observation_json,
        pending_sha256=None if pending_json is None else sha256(pending_json.encode("utf-8")).hexdigest(),
    )
    _prove_owned_observation(conn, record, value)
    canonical = canonical_json(value.model_dump(mode="json"))
    require_no_credential_material(
        canonical,
        surface="composer_classifier_owned_observation",
        env_ref_names=frozenset(record.env_ref_names),
        max_string_bytes=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
    )
    put_chunk(
        conn,
        session_id=str(record.session_id),
        operation_id=record.operation_id,
        key=_owned_observation_key(record),
        payload=canonical.encode("utf-8"),
        codec=value.schema_version,
    )
    return value


def _read_owned_observation(conn: Connection, record: _StageRecord, *, pending_json: str | None) -> _OwnedObservationRecord:
    canonical = _owned_text(conn, record, _owned_observation_key(record), "composer.iteration-owned-observation.v1")
    try:
        value = _OwnedObservationRecord.model_validate_json(canonical, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("owned classifier refusal lost its closed original observation") from error
    expected_pending = None if pending_json is None else sha256(pending_json.encode("utf-8")).hexdigest()
    if canonical_json(value.model_dump(mode="json")) != canonical or value.pending_sha256 != expected_pending:
        raise AuditIntegrityError("owned classifier refusal changed its exact pending observation")
    _prove_owned_observation(conn, record, value)
    return value


def _store_owned_completion(
    conn: Connection,
    record: _StageRecord,
    *,
    observation_json: str,
    result_json: str,
    end_effect_key: str | None,
    pending_stage_key: str | None,
    end_disposition: Literal["clean", "blocked"] | None,
) -> None:
    from elspeth.web.composer.continuation_classifier_owned_source import decode_classifier_owned_source

    observation = decode_classifier_owned_source(observation_json)
    if observation.kind == "source_proof_refusal":
        if end_effect_key is None or pending_stage_key is None or end_disposition != "blocked":
            raise AuditIntegrityError("source refusal requires its actual original blocked END execution")
        pending_canonical = read_chunk(
            conn,
            session_id=str(record.session_id),
            operation_id=record.operation_id,
            key=pending_stage_key,
        ).decode("utf-8", errors="strict")
        pending_record = _StageRecord.model_validate_json(pending_canonical, strict=True)
        if _key(pending_canonical) != pending_stage_key or pending_record.stage != "end_ready" or pending_record.result_json is None:
            raise AuditIntegrityError("source refusal completion changed its original pending stage")
        original = _read_owned_observation(conn, record, pending_json=pending_record.result_json)
        if original.observation_json != observation_json:
            raise AuditIntegrityError("source refusal completion substituted its original observation")
    else:
        if end_effect_key is not None or pending_stage_key is not None or end_disposition is not None:
            raise AuditIntegrityError("reply refusal cannot invent an END execution or clean verdict")
        original = _store_owned_observation(conn, record, observation_json, pending_json=None)
    observation_canonical = canonical_json(original.model_dump(mode="json"))
    value = _OwnedCompletionRecord(
        observation_key=_owned_observation_key(record),
        observation_sha256=sha256(observation_canonical.encode("utf-8")).hexdigest(),
        result_sha256=sha256(result_json.encode("utf-8")).hexdigest(),
        before_checkpoint_id=record.before_checkpoint_id,
        end_effect_key=end_effect_key,
        pending_stage_key=pending_stage_key,
        end_disposition=end_disposition,
    )
    put_chunk(
        conn,
        session_id=str(record.session_id),
        operation_id=record.operation_id,
        key=_owned_completion_key(record),
        payload=canonical_json(value.model_dump(mode="json")).encode("utf-8"),
        codec=value.schema_version,
    )


def _prove_owned_recovery_source(
    conn: Connection,
    record: _StageRecord,
    envelope: IterationReturnEnvelope,
    *,
    captured_state: PrivateChunk | None = None,
    captured_recorder: PrivateChunk | None = None,
) -> None:
    from elspeth.web.composer.continuation_audit_buffer import decode_recorder_snapshot
    from elspeth.web.composer.recovery_controls import parse_recovery_tool_batch
    from elspeth.web.sessions.continuation_classifier_primary import read_original_classifier_primary_on_connection

    if (
        envelope.source_kind != "classifier_primary"
        or envelope.primary_effect_key is None
        or envelope.end_disposition is not None
        or envelope.retained.publication_binding is not None
        or envelope.end_effect_key is not None
        or envelope.pending_stage_key is not None
        or envelope.end_pass_message_id is not None
        or envelope.end_pass_index is not None
    ):
        raise AuditIntegrityError("owned graph recovery lost its sole actual classifier PRIMARY source")
    paid = read_original_classifier_primary_on_connection(
        conn,
        session_id=record.session_id,
        operation_id=record.operation_id,
        effect_key=envelope.primary_effect_key,
    )
    if (
        paid.snapshot.checkpoint_id != record.before_checkpoint_id
        or paid.snapshot.cursor != record.before_cursor
        or paid.input_sha256 != envelope.primary_input_sha256
        or paid.response.reference != envelope.primary_response
    ):
        raise AuditIntegrityError("owned graph recovery substituted another classifier invocation")
    try:
        intent = parse_recovery_tool_batch(paid.completion.tool_batch)
    except (TypeError, ValueError) as error:
        raise AuditIntegrityError("owned graph recovery changed its original provider control") from error
    result = envelope.retained.result
    state = captured_state if captured_state is not None else _historical_value(conn, record, record.compose.state)
    recorder_chunk = captured_recorder if captured_recorder is not None else _historical_value(conn, record, record.compose.audit_buffer)
    if state.reference != record.compose.state or recorder_chunk.reference != record.compose.audit_buffer:
        raise AuditIntegrityError("owned graph recovery substituted its actual captured source")
    recorder = decode_recorder_snapshot(recorder_chunk)
    if intent is None or canonical_json(result.state.to_dict()) != state.canonical:
        raise AuditIntegrityError("owned graph recovery lost its actual original intent or captured state")
    expected = ComposerResult(
        message=intent.message,
        state=result.state,
        graph_recovery_intent=intent,
        tool_invocations=() if record.compose.persisted_tool_call_turn else recorder.invocations,
        llm_calls=recorder.llm_calls,
    )
    if result != expected:
        raise AuditIntegrityError("owned graph recovery changed its complete original provider-authored result")


def _prove_return_envelope(
    conn: Connection,
    record: _StageRecord,
    envelope: IterationReturnEnvelope,
    *,
    captured_state: PrivateChunk | None = None,
    captured_recorder: PrivateChunk | None = None,
) -> None:
    from elspeth.web.composer.continuation_classifier_handoff import ClassifyPostPrimary, ClassifySourceProofStop
    from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_snapshot
    from elspeth.web.composer.continuation_end_disposition import classify_end_review_result
    from elspeth.web.sessions.continuation_classifier_primary import read_original_classifier_primary_on_connection
    from elspeth.web.sessions.continuation_end_pass_source import read_original_accepted_end_pass_on_connection

    sid, operation = str(record.session_id), record.operation_id
    if envelope.terminal_kind == "owned_recovery":
        _prove_owned_recovery_source(conn, record, envelope, captured_state=captured_state, captured_recorder=captured_recorder)
    if envelope.primary_effect_key is not None:
        paid = read_original_classifier_primary_on_connection(
            conn, session_id=record.session_id, operation_id=operation, effect_key=envelope.primary_effect_key
        )
        if (paid.input_sha256, paid.response.reference) != (envelope.primary_input_sha256, envelope.primary_response):
            raise AuditIntegrityError("iteration result changed its actual original classifier PRIMARY")
    if envelope.end_effect_key is not None:
        if envelope.pending_stage_key is None:
            raise AuditIntegrityError("iteration result lost its original pending END source")
        canonical = read_chunk(conn, session_id=sid, operation_id=operation, key=envelope.pending_stage_key).decode(
            "utf-8", errors="strict"
        )
        try:
            pending_record = _StageRecord.model_validate_json(canonical, strict=True)
        except ValidationError as error:
            raise AuditIntegrityError("iteration result changed its closed original pending END") from error
        if (
            pending_record.stage != "end_ready"
            or pending_record.result_json is None
            or _key(canonical) != envelope.pending_stage_key
            or _end_key(pending_record.result_json) != envelope.end_effect_key
            or pending_record.session_id != record.session_id
            or pending_record.operation_id != operation
            or pending_record.source_checkpoint_id != record.source_checkpoint_id
        ):
            raise AuditIntegrityError("iteration result substituted its original pending phase result")
        pending_snapshot = _historical(conn, sid, operation, cursor=pending_record.before_cursor + 1)
        actual = _stage_record(conn, pending_snapshot, sid, operation)
        if actual != (pending_record, envelope.pending_stage_key):
            raise AuditIntegrityError("iteration result changed its original pending checkpoint")
        _prove_record(conn, pending_snapshot, pending_record, envelope.pending_stage_key)
        pending = _decode_pending(pending_record.result_json)
        if type(pending) is ClassifyPostPrimary:
            if envelope.source_kind != "classifier_primary" or (
                pending.primary_effect_key,
                pending.primary_input_sha256,
                pending.response,
            ) != (envelope.primary_effect_key, envelope.primary_input_sha256, envelope.primary_response):
                raise AuditIntegrityError("iteration END result changed its original physical branch")
        elif type(pending) is ClassifySourceProofStop and envelope.source_kind != "source_proof_stop":
            raise AuditIntegrityError("iteration source refusal cannot invent a classifier PRIMARY")
        before = _historical(conn, sid, operation, checkpoint_id=record.before_checkpoint_id)
        parent = _stage_source_record(conn, before, sid, operation)
        if parent is None or parent[0].stage != "end_dispatched" or parent[0].result_json != pending_record.result_json:
            raise AuditIntegrityError("iteration result has no exact original END execution")
        main = conn.execute(
            select(effects).where(
                effects.c.session_id == sid,
                effects.c.operation_id == operation,
                effects.c.effect_key == advance_effect_key(record.batch.response),
            )
        ).one()
        if main.result_chunk_key != envelope.pending_stage_key or main.receipt_key != envelope.pending_stage_key:
            raise AuditIntegrityError("iteration main phase rewrote its immutable pending receipt")
    if envelope.end_disposition == "clean":
        binding = envelope.retained.publication_binding
        if (
            binding is None
            or envelope.end_pass_message_id is None
            or envelope.end_pass_index is None
            or envelope.end_pass_index != record.compose.advisor_checkpoint_passes
            or classify_end_review_result(envelope.retained.result, publication_binding=binding) != "clean"
        ):
            raise AuditIntegrityError("iteration return lost its complete actual clean END result")
        before = _historical(conn, sid, operation, checkpoint_id=record.before_checkpoint_id)
        before_compose, _batch = applied_batch_cut(decode_continuation_snapshot(before.payload_json).frames)
        if captured_state is None:
            reference = record.compose.state
            canonical = read_chunk(conn, session_id=sid, operation_id=operation, key=f"value:{reference.codec}:{reference.digest}").decode(
                "utf-8", errors="strict"
            )
            captured_state = PrivateChunk(reference, canonical)
        require_clean_iteration_return_comparands(
            result=envelope.retained.result,
            captured=record.compose,
            state_chunk=captured_state,
            before=before_compose,
            pass_index=envelope.end_pass_index,
        )
        end_source = _stage_source_record(conn, before, sid, operation)
        if end_source is None or end_source[0].stage != "end_dispatched" or end_source[0].result_json is None:
            raise AuditIntegrityError("clean iteration result lost its actual original END input")
        original_input = _read_end_input(conn, end_source[0], end_source[0].result_json)
        pass_sequence = conn.execute(
            select(messages.c.sequence_no).where(
                messages.c.session_id == sid,
                messages.c.id == envelope.end_pass_message_id,
            )
        ).scalar_one_or_none()
        require_clean_iteration_end_pass_source(
            binding=binding,
            original_binding=original_input.publication_binding,
            pass_sequence=pass_sequence,
            messages_before=original_input.messages_before,
        )
        read_original_accepted_end_pass_on_connection(
            conn,
            session_id=record.session_id,
            message_id=envelope.end_pass_message_id,
            pass_index=envelope.end_pass_index,
            publication_binding=binding,
        )


def retain_iteration_returned(
    authority: ComposerAsyncOperationAuthority,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    observed: ObservedIterationReturn,
    captured: CapturedContinuationRuntime,
) -> StoredContinuationSnapshot:
    from elspeth.web.composer.continuation_semantic_advisor_return import require_original_iteration_return

    value = require_original_iteration_return(observed, running, selected, captured)
    compose = _capture(selected, captured)
    recorder = next((chunk for chunk in (*captured.chunks, *selected.chunks) if chunk.reference == compose.audit_buffer), None)
    if recorder is None or value.recorder_source != recorder:
        raise AuditIntegrityError("iteration return lost its actual postexecution recorder source")
    encoded = encode_retained_composer_result(RetainedComposerResult(value.result, value.publication_binding), recorder_source=recorder)
    if value.result_json != encoded:
        raise AuditIntegrityError("iteration return changed its original complete classifier result")
    with locked_session_transaction(authority._engine, str(running.claim.session_id)) as conn:
        current = _current(conn, running, selected, inventory)
        data = read_iteration_stage_on_connection(conn, running, selected=current)
        previous = _stage_source_record(conn, current, str(running.claim.session_id), running.claim.operation_id)
        if data is None or data.stage not in ("classify_dispatched", "end_dispatched", "reply_refusal_dispatched") or previous is None:
            raise AuditIntegrityError("iteration return lost its actual dispatched classifier source")
        record = previous[0].model_copy(
            update={
                "before_checkpoint_id": current.checkpoint_id,
                "before_cursor": current.cursor,
                "stage": "returned",
                "compose": compose,
                "batch": applied_batch_cut(captured.stack.frames)[1],
                "result_json": encoded,
            }
        )
        if value.terminal_kind is not None:
            from elspeth.web.composer.continuation_classifier_handoff import ClassifySourceProofStop
            from elspeth.web.composer.continuation_iteration_return_codec import IterationReturnEnvelope, encode_iteration_return_envelope

            pending_source = (
                read_iteration_pending_end_on_connection(conn, running, selected=selected, inventory=inventory)
                if data.stage == "end_dispatched"
                else None
            )
            binding = value.publication_binding
            pass_id = _original_end_pass_id(conn, record, binding) if value.end_disposition == "clean" and binding is not None else None
            source_kind: Literal["classifier_primary", "source_proof_stop", "unclassified"] = (
                "classifier_primary"
                if value.primary_effect_key is not None
                else "source_proof_stop"
                if pending_source is not None and type(pending_source.pending) is ClassifySourceProofStop
                else "unclassified"
            )
            envelope = IterationReturnEnvelope(
                retained=RetainedComposerResult(value.result, binding),
                result_json=encoded,
                source_kind=source_kind,
                terminal_kind=value.terminal_kind,
                end_disposition=value.end_disposition,
                primary_effect_key=value.primary_effect_key,
                primary_input_sha256=value.primary_input_sha256,
                primary_response=value.primary_response,
                end_effect_key=None if pending_source is None else pending_source.end_effect_key,
                pending_stage_key=None if pending_source is None else pending_source.pending_stage_key,
                end_pass_message_id=pass_id,
                end_pass_index=None if pass_id is None else compose.advisor_checkpoint_passes,
            )
            if value.terminal_kind == "owned_refusal":
                if value.owned_source_json is None:
                    raise AuditIntegrityError("owned classifier refusal lost its actual original source observation")
                _store_owned_completion(
                    conn,
                    record,
                    observation_json=value.owned_source_json,
                    result_json=encoded,
                    end_effect_key=envelope.end_effect_key,
                    pending_stage_key=envelope.pending_stage_key,
                    end_disposition=value.end_disposition,
                )
            elif value.owned_source_json is not None:
                raise AuditIntegrityError("classifier terminal cannot borrow a different owned refusal source")
            state_source = next((chunk for chunk in (*captured.chunks, *selected.chunks) if chunk.reference == compose.state), None)
            if state_source is None:
                raise AuditIntegrityError("iteration return lost its exact actual captured state chunk")
            _prove_return_envelope(conn, record, envelope, captured_state=state_source, captured_recorder=recorder)
            record = record.model_copy(update={"result_json": encode_iteration_return_envelope(envelope, recorder_source=recorder)})
        completed = _retain(conn, running, selected, inventory, record, captured)
        cold = read_original_iteration_return_on_connection(
            conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, effect_key=data.owner_effect_key
        )
        if cold.completed != completed or cold.result != value.result or cold.publication_binding != value.publication_binding:
            raise AuditIntegrityError("iteration return failed its original complete cold readback")
        if value.terminal_kind == "owned_refusal":
            owned = read_original_owned_iteration_return_on_connection(
                conn,
                session_id=running.claim.session_id,
                operation_id=running.claim.operation_id,
                effect_key=data.owner_effect_key,
            )
            if owned.returned != cold:
                raise AuditIntegrityError("owned refusal failed its independent original cold source readback")
        return completed


def read_original_iteration_return_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str, effect_key: str
) -> IterationReturnCommitted:
    """Replay immutable original return bytes, stage chain and physical effect."""
    from elspeth.contracts.json_parser import parse_json_strict
    from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_snapshot, verify_private_chunk
    from elspeth.web.composer.continuation_iteration_return_codec import decode_iteration_return_envelope

    if type(session_id) is not UUID or type(operation_id) is not str or type(effect_key) is not str:
        raise TypeError("iteration return requires exact original SQL identity")
    sid = str(session_id)
    row = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == effect_key)
    ).one_or_none()
    if (
        row is None
        or row.owner != ITERATION_EFFECT_OWNER
        or row.disposition != "committed"
        or type(row.result_chunk_key) is not str
        or row.receipt_key != row.result_chunk_key
    ):
        raise AuditIntegrityError("iteration return lost its committed original effect/result")
    canonical = read_chunk(conn, session_id=sid, operation_id=operation_id, key=row.result_chunk_key).decode("utf-8", errors="strict")
    result_key = row.result_chunk_key
    try:
        record = _StageRecord.model_validate_json(canonical, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("iteration return lost its closed original result record") from error
    if record.stage in ("end_ready", "reply_refusal_ready"):
        if record.result_json is None or _key(canonical) != result_key:
            raise AuditIntegrityError("iteration return lost its immutable original pending result")
        end_key = _end_key(record.result_json) if record.stage == "end_ready" else _reply_refusal_key(record.result_json)
        end = conn.execute(
            select(effects).where(
                effects.c.session_id == sid,
                effects.c.operation_id == operation_id,
                effects.c.effect_key == end_key,
            )
        ).one_or_none()
        if (
            end is None
            or end.owner != ITERATION_EFFECT_OWNER
            or end.disposition != "committed"
            or type(end.result_chunk_key) is not str
            or end.receipt_key != end.result_chunk_key
        ):
            raise AuditIntegrityError("iteration pending DATA has no original completed END result")
        result_key = end.result_chunk_key
        canonical = read_chunk(conn, session_id=sid, operation_id=operation_id, key=result_key).decode("utf-8", errors="strict")
        try:
            record = _StageRecord.model_validate_json(canonical, strict=True)
        except ValidationError as error:
            raise AuditIntegrityError("iteration END lost its closed original returned result") from error
    if (
        record.stage != "returned"
        or record.result_json is None
        or str(record.session_id) != sid
        or record.operation_id != operation_id
        or _key(canonical) != result_key
    ):
        raise AuditIntegrityError("iteration return changed its original result identity")
    completed = _historical(conn, sid, operation_id, cursor=record.before_cursor + 1)
    actual = _stage_record(conn, completed, sid, operation_id)
    if actual is None or actual != (record, result_key):
        raise AuditIntegrityError("iteration return changed its complete original checkpoint event")
    _prove_record(conn, completed, record, result_key)
    root = decode_continuation_snapshot(completed.payload_json)
    compose, batch = applied_batch_cut(root.frames)
    if advance_effect_key(batch.response) != effect_key:
        raise AuditIntegrityError("iteration return replaced its original paid batch")
    reference = compose.audit_buffer
    recorder_text = read_chunk(conn, session_id=sid, operation_id=operation_id, key=f"value:{reference.codec}:{reference.digest}").decode(
        "utf-8", errors="strict"
    )
    verify_private_chunk(reference, recorder_text)
    recorder = PrivateChunk(reference, recorder_text)
    body, parse_error = parse_json_strict(record.result_json)
    if parse_error is not None:
        raise AuditIntegrityError("iteration return lost its strict original phase result")
    if type(body) is dict and body.get("schema_version") == "composer.iteration-return.v1":
        envelope = decode_iteration_return_envelope(record.result_json, recorder_source=recorder)
        _prove_return_envelope(conn, record, envelope)
        return IterationReturnCommitted(
            completed=completed,
            result=envelope.retained.result,
            publication_binding=envelope.retained.publication_binding,
            owner_effect_key=effect_key,
            original_source=_historical(conn, sid, operation_id, checkpoint_id=record.source_checkpoint_id),
            recorder_source=recorder,
            result_json=envelope.result_json,
            terminal_kind=envelope.terminal_kind,
            end_disposition=envelope.end_disposition,
            primary_effect_key=envelope.primary_effect_key,
            primary_input_sha256=envelope.primary_input_sha256,
            primary_response=envelope.primary_response,
            end_pass_message_id=envelope.end_pass_message_id,
            end_pass_index=envelope.end_pass_index,
            end_effect_key=envelope.end_effect_key,
            pending_stage_key=envelope.pending_stage_key,
        )
    # Older complete neutral records remain inspectable DATA. They carry no
    # clean END stamp or accepted-pipeline publication authority.
    retained = decode_retained_composer_result(record.result_json, recorder_source=recorder)
    return IterationReturnCommitted(
        completed,
        retained.result,
        retained.publication_binding,
        effect_key,
        _historical(conn, sid, operation_id, checkpoint_id=record.source_checkpoint_id),
        recorder,
        record.result_json,
    )


def read_original_owned_iteration_return_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str, effect_key: str
) -> OwnedIterationReturnSource:
    """Replay a supported original terminal kind; coarse stamps grant nothing."""
    from elspeth.web.composer.continuation_bootstrap import bootstrap_values, decode_continuation_bootstrap
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
    from elspeth.web.composer.continuation_prompt_producers import require_original_bootstrap_transcript_on_connection

    returned = read_original_iteration_return_on_connection(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        effect_key=effect_key,
    )
    found = _stage_record(conn, returned.completed, str(session_id), operation_id)
    if found is None or found[0].stage != "returned":
        raise AuditIntegrityError("owned terminal lost its original completed source")
    record = found[0]
    if returned.terminal_kind == "owned_refusal":
        return _read_owned_refusal_source(conn, record, returned)
    if returned.terminal_kind != "owned_recovery" or returned.result.graph_recovery_intent is None:
        raise AuditIntegrityError("iteration return has no supported independently proved owned terminal source")
    source_root = decode_continuation_snapshot(returned.original_source.payload_json)
    source_compose, _batch = applied_batch_cut(source_root.frames)
    bootstrap = decode_continuation_bootstrap(_historical_value(conn, record, source_compose.bootstrap))
    values = bootstrap_values(bootstrap)
    if (
        bootstrap.logical != source_root.logical
        or bootstrap.logical.session_id != str(session_id)
        or bootstrap.logical.operation_id != operation_id
        or bootstrap.initial_state_json != _historical_value(conn, record, source_compose.initial_state).canonical
    ):
        raise AuditIntegrityError("owned recovery changed its original bootstrap request or initial state")
    require_original_bootstrap_transcript_on_connection(conn, bootstrap)
    return OwnedIterationReturnSource("graph_recovery", returned, values.recovery_context)


def _read_owned_refusal_source(
    conn: Connection,
    record: _StageRecord,
    returned: IterationReturnCommitted,
) -> OwnedIterationReturnSource:
    from elspeth.web.composer.continuation_classifier_handoff import decode_classifier_source_stop_proof
    from elspeth.web.composer.continuation_classifier_owned_source import original_protocol_history_has_tool_calls
    from elspeth.web.composer.service import _reply_only_protocol_echo
    from elspeth.web.sessions.continuation_classifier_primary import read_original_classifier_primary_on_connection

    canonical = _owned_text(conn, record, _owned_completion_key(record), "composer.iteration-owned-completion.v1")
    try:
        completion = _OwnedCompletionRecord.model_validate_json(canonical, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("owned refusal lost its closed original completion observation") from error
    pending_json = None
    if completion.pending_stage_key is not None:
        pending_canonical = read_chunk(
            conn,
            session_id=str(record.session_id),
            operation_id=record.operation_id,
            key=completion.pending_stage_key,
        ).decode("utf-8", errors="strict")
        try:
            pending_record = _StageRecord.model_validate_json(pending_canonical, strict=True)
        except ValidationError as error:
            raise AuditIntegrityError("owned refusal changed its original pending source") from error
        if _key(pending_canonical) != completion.pending_stage_key or pending_record.stage != "end_ready":
            raise AuditIntegrityError("owned refusal substituted its pending checkpoint")
        pending_json = pending_record.result_json
    original = _read_owned_observation(conn, record, pending_json=pending_json)
    observation, source, evidence = _prove_owned_observation(conn, record, original)
    if (
        canonical_json(completion.model_dump(mode="json")) != canonical
        or completion.observation_key != _owned_observation_key(record)
        or completion.observation_sha256 != sha256(canonical_json(original.model_dump(mode="json")).encode("utf-8")).hexdigest()
        or completion.result_sha256 != sha256(returned.result_json.encode("utf-8")).hexdigest()
        or completion.before_checkpoint_id != record.before_checkpoint_id
        or (completion.end_effect_key, completion.pending_stage_key, completion.end_disposition)
        != (returned.end_effect_key, returned.pending_stage_key, returned.end_disposition)
    ):
        raise AuditIntegrityError("owned refusal changed its complete original source or returned result")
    result = returned.result
    state = _historical_value(conn, record, record.compose.state)
    if (
        canonical_json(result.state.to_dict()) != state.canonical
        or result.pipeline_commit_intent is not None
        or result.graph_recovery_intent is not None
        or returned.end_disposition == "clean"
        or returned.end_pass_message_id is not None
        or returned.end_pass_index is not None
    ):
        raise AuditIntegrityError("owned refusal cannot claim a different state, recovery or clean model permission")
    if observation.kind == "source_proof_refusal":
        from elspeth.web.composer.composition_completion import _source_proof_unavailable_validation

        if (
            returned.primary_effect_key is not None
            or returned.primary_input_sha256 is not None
            or returned.primary_response is not None
            or returned.end_effect_key is None
            or returned.pending_stage_key is None
            or returned.end_disposition != "blocked"
            or observation.proof_json is None
            or evidence.graph is None
            or result.runtime_preflight is None
        ):
            raise AuditIntegrityError("source refusal requires original unpaid proof and actual blocked END")
        proof = decode_classifier_source_stop_proof(observation.proof_json)
        batch = evidence.graph.selected.batch
        try:
            expected = _source_proof_unavailable_validation(batch.state, proof, batch.last_runtime_preflight)
        except (KeyError, ValueError) as error:
            raise AuditIntegrityError("source refusal changed its original proof scope") from error
        actual = result.runtime_preflight
        if (
            actual.is_valid
            or actual.readiness.execution_ready
            or actual.readiness.completion_ready
            or actual.readiness.authoring_valid != expected.readiness.authoring_valid
            or any(item not in actual.errors for item in expected.errors)
            or any(item not in actual.checks for item in expected.checks)
            or any(item not in actual.warnings for item in expected.warnings)
            or any(item not in actual.readiness.blockers for item in expected.readiness.blockers)
        ):
            raise AuditIntegrityError("source refusal erased its original blocked proof or authoring scope")
    else:
        if (
            returned.end_effect_key is not None
            or returned.publication_binding is not None
            or result.raw_assistant_content not in (None, "")
        ):
            raise AuditIntegrityError("unavailable staged reply cannot invent END or model-authored prose")
        if observation.kind == "reply_protocol_echo":
            if returned.primary_effect_key is None:
                raise AuditIntegrityError("protocol echo refusal lost its actual physical response")
            paid = read_original_classifier_primary_on_connection(
                conn,
                session_id=record.session_id,
                operation_id=record.operation_id,
                effect_key=returned.primary_effect_key,
            )
            shelf = evidence.snapshot.transcript
            messages_json = _historical_value(conn, source, source.compose.messages).canonical
            if messages_json != canonical_json({"messages_json": shelf.messages_json, "current_indexes": shelf.current_indexes}):
                raise AuditIntegrityError("protocol refusal changed its original selected message shelf")
            history_has_protocol = original_protocol_history_has_tool_calls(shelf.messages_json, shelf.current_indexes)
            if (
                paid.branch != "staged_review"
                or paid.source_checkpoint_id != observation.source_checkpoint_id
                or paid.source_cursor != observation.source_cursor
                or paid.snapshot.checkpoint_id != record.before_checkpoint_id
                or paid.input_sha256 != returned.primary_input_sha256
                or paid.response.reference != returned.primary_response
                or not _reply_only_protocol_echo(
                    paid.completion.message.content or "",
                    history_has_tool_protocol=history_has_protocol,
                    user_message=evidence.snapshot.transcript.request,
                )
            ):
                raise AuditIntegrityError("protocol refusal substituted its original paid echo or user request")
        elif (
            returned.primary_effect_key is not None
            or returned.primary_input_sha256 is not None
            or returned.primary_response is not None
            or observation.deadline is None
            or observation.observed_monotonic is None
            or observation.observed_monotonic < observation.deadline
        ):
            raise AuditIntegrityError("reply time exhaustion cannot relabel an entered or unknown provider failure")
        else:
            executing = _historical(conn, str(record.session_id), record.operation_id, checkpoint_id=record.before_checkpoint_id)
            execution = _stage_source_record(conn, executing, str(record.session_id), record.operation_id)
            if execution is None or execution[0].stage != "reply_refusal_dispatched":
                raise AuditIntegrityError("reply time exhaustion lost its separately admitted original SQL tail")
            ready_snapshot = _historical(conn, str(record.session_id), record.operation_id, checkpoint_id=execution[0].before_checkpoint_id)
            ready = _stage_source_record(conn, ready_snapshot, str(record.session_id), record.operation_id)
            if ready is None or ready[0].stage != "reply_refusal_ready":
                raise AuditIntegrityError("reply time exhaustion lost its actual original unpaid disposition")
            pending = _prove_reply_refusal_ready(conn, ready[0])
            if pending.observation_json != original.observation_json or execution[0].result_json != ready[0].result_json:
                raise AuditIntegrityError("reply time exhaustion substituted its original elapsed observation")
            main = conn.execute(
                select(effects).where(
                    effects.c.session_id == str(record.session_id),
                    effects.c.operation_id == record.operation_id,
                    effects.c.effect_key == advance_effect_key(record.batch.response),
                )
            ).one()
            if main.receipt_key != ready[1] or main.result_chunk_key != ready[1]:
                raise AuditIntegrityError("reply time exhaustion rewrote its original immutable no-SDK disposition")
    return OwnedIterationReturnSource(observation.kind, returned)
