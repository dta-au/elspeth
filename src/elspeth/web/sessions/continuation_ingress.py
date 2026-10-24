"""Current reserved-ID ingress DATA at the existing session writer boundary.

The real SessionServiceImpl owns the inserter, transcript and transaction.
These functions admit/bind/retain that one write; they are not another writer
and a retained checkpoint is never current authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Connection, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.continuation import ContinuationFrameStack, InitializeFrame, reserved_user_message_id
from elspeth.web.composer.continuation_bootstrap import _canonical_records_json
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.sessions.chat_message_decoder import chat_message_record_from_row
from elspeth.web.sessions.composer_operations import ComposerOperationRunning, composer_operation_v2_request_hash
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    StoredContinuationSnapshot,
    _effect_snapshot,
    _require_current_continuation_running,
    append_complete_snapshot,
    load_snapshot,
    read_chunk,
)
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import message_ingress_receipts_table as receipts
from elspeth.web.sessions.models import sessions_table as sessions
from elspeth.web.sessions.protocol import ChatMessageRecord
from elspeth.web.sessions.schemas import SendMessageRequest
from elspeth.web.sessions.time_normalization import restore_utc
from elspeth.web.sessions.transcript_reads import non_private_message_predicate, validate_transcript_private_custody


class ContinuationIngressReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["composer.ingress-receipt.v1"] = "composer.ingress-receipt.v1"
    session_id: str
    operation_id: str
    user_message_id: str
    requested_state_id: str | None
    composition_state_id: str | None
    created_at: str
    original_last_sequence: int = Field(ge=1)
    transcript_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_session_title: str
    source_checkpoint_id: str
    source_cursor: int = Field(ge=0)


@dataclass(frozen=True, slots=True)
class ContinuationIngressPrepared:
    selected: StoredContinuationSnapshot
    reserved_id: str
    original_session_title: str


@dataclass(frozen=True, slots=True)
class ContinuationIngressCheckpoint:
    operation_id: UUID
    message: ChatMessageRecord
    transcript: tuple[ChatMessageRecord, ...]
    snapshot: StoredContinuationSnapshot
    disposition: Literal["inserted", "replayed"]


def _receipt(text: str) -> ContinuationIngressReceipt:
    try:
        value = ContinuationIngressReceipt.model_validate_json(text, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("initializer lost its complete ingress receipt") from error
    if canonical_json(value.model_dump(mode="json")) != text:
        raise AuditIntegrityError("initializer ingress receipt lost or normalized a field")
    return value


def _event(value: ContinuationIngressReceipt) -> str:
    return canonical_json({"owner": "composer_ingress", "receipt": value.model_dump(mode="json")})


def _records(conn: Connection, *, session_id: str, operation_id: str, message_id: str, cutoff: int) -> tuple[ChatMessageRecord, ...]:
    validate_transcript_private_custody(conn, session_id)
    rows = conn.execute(
        select(messages)
        .where(messages.c.session_id == session_id, messages.c.sequence_no <= cutoff, non_private_message_predicate(conn.dialect.name))
        .order_by(messages.c.sequence_no)
    ).all()
    return tuple(chat_message_record_from_row(row, operation_id=operation_id if row.id == message_id else None) for row in rows)


def prepare_ingress_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
    content: str,
    requested_state_id: UUID | None,
    composition_state_id: UUID | None,
) -> ContinuationIngressPrepared | ContinuationIngressCheckpoint:
    if type(selected) is not StoredContinuationSnapshot or type(running) is not ComposerOperationRunning:
        raise AuditIntegrityError("initializer ingress requires actual selected/current owners")
    _require_current_continuation_running(conn, running)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    actual = load_snapshot(conn, session_id=sid, operation_id=operation)
    if actual != selected:
        from elspeth.web.sessions.continuation import ContinuationCASConflict

        raise ContinuationCASConflict("initializer ingress changed its complete selected CAS")
    root = decode_continuation_snapshot(actual.payload_json)
    if len(root.frames) != 1 or type(root.frames[0]) is not InitializeFrame:
        raise AuditIntegrityError("initializer ingress requires its one actual Initialize frame")
    frame = root.frames[0]
    if (
        frame.ingress_phase not in ("reserved", "inserted")
        or frame.title_attempt_id is not None
        or frame.bootstrap is not None
        or frame.title_response is not None
        or frame.provider_effect_key is not None
        or frame.provider_input_sha256 is not None
        or root.effect_frontier
        or root.pending_owner_results
        or root.pending_settlement_ids
        or root.active_action_json is not None
        or root.active_action_source is not None
        or root.semantic_frontier is not None
    ):
        raise AuditIntegrityError("initializer ingress has no closed unadvanced SQL-only cut")
    return _read_ingress_source_on_connection(
        conn,
        running,
        actual=actual,
        content=content,
        requested_state_id=requested_state_id,
        composition_state_id=composition_state_id,
    )


def _read_ingress_source_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    actual: StoredContinuationSnapshot,
    content: str,
    requested_state_id: UUID | None,
    composition_state_id: UUID | None,
) -> ContinuationIngressPrepared | ContinuationIngressCheckpoint:
    """Original phase-owner path; source and current selection are identical."""
    return _read_ingress_values_with_source_on_connection(
        conn,
        running,
        actual=actual,
        source=actual,
        content=content,
        requested_state_id=requested_state_id,
        composition_state_id=composition_state_id,
    )


def _actual_ingress_source_root_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    source: StoredContinuationSnapshot,
) -> ContinuationFrameStack:
    if type(source) is not StoredContinuationSnapshot:
        raise AuditIntegrityError("initializer ingress source requires its actual stored checkpoint")
    row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == str(running.claim.session_id),
            checkpoints.c.operation_id == running.claim.operation_id,
            checkpoints.c.checkpoint_id == source.checkpoint_id,
        )
    ).one_or_none()
    if row is None or row.cursor != source.cursor or row.kind != "transition" or row.frame_stack_version != 1:
        raise AuditIntegrityError("initializer ingress source lost its actual SQL checkpoint")
    try:
        text = read_chunk(
            conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id, key=row.snapshot_chunk_key
        ).decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise AuditIntegrityError("initializer ingress source checkpoint is not strict UTF-8") from error
    root = decode_continuation_snapshot(text)
    if text != source.payload_json or root.cursor != source.cursor or encode_continuation_snapshot(root) != text:
        raise AuditIntegrityError("initializer ingress source changed its complete actual checkpoint")
    return root


def _read_ingress_values_with_source_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    actual: StoredContinuationSnapshot,
    source: StoredContinuationSnapshot,
    content: str,
    requested_state_id: UUID | None,
    composition_state_id: UUID | None,
) -> ContinuationIngressPrepared | ContinuationIngressCheckpoint:
    """Current SQL values plus the actual retained ingress source DATA."""
    _require_current_continuation_running(conn, running)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    if load_snapshot(conn, session_id=sid, operation_id=operation) != actual:
        from elspeth.web.sessions.continuation import ContinuationCASConflict

        raise ContinuationCASConflict("initializer ingress changed its complete selected CAS")
    root = decode_continuation_snapshot(actual.payload_json)
    frame = root.frames[0]
    if len(root.frames) != 1 or type(frame) is not InitializeFrame:
        raise AuditIntegrityError("initializer source lost its exact Initialize frame")
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
    principal = conn.execute(select(sessions.c.user_id, sessions.c.archived_at, sessions.c.title).where(sessions.c.id == sid)).one()
    if parent.lifecycle_version != 2 or parent.kind != "compose_message":
        raise AuditIntegrityError("initializer ingress cannot insert for another lifecycle/request kind")
    try:
        request = SendMessageRequest.model_validate_json(parent.request_json, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("initializer parent request violates its admitted owned shape") from error
    reserved = reserved_user_message_id(running.claim.session_id, operation)
    requested = str(requested_state_id) if requested_state_id is not None else None
    state = str(composition_state_id) if composition_state_id is not None else None
    if (
        parent.lifecycle_version != 2
        or parent.kind != "compose_message"
        or principal.archived_at is not None
        or principal.user_id != parent.actor_user_id
        or request.operation_id != operation
        or frame.request_json != parent.request_json
        or request.content != content
        or composer_operation_v2_request_hash(session_id=running.claim.session_id, kind="compose_message", request=request)
        != parent.request_hash
        or (
            root.logical.session_id,
            root.logical.operation_id,
            root.logical.kind,
            root.logical.request_hash,
            root.logical.original_user_message_id,
            root.logical.base_state_id,
        )
        != (sid, operation, "compose_message", parent.request_hash, reserved, parent.base_state_id)
        or parent.reserved_user_message_id != reserved
        or (str(request.state_id) if request.state_id is not None else None) != requested
        or requested != parent.base_state_id
        or state != parent.base_state_id
    ):
        raise AuditIntegrityError("initializer ingress changed its original request/actor/base/reserved identity")
    row = conn.execute(select(receipts).where(receipts.c.session_id == sid, receipts.c.operation_id == operation)).one_or_none()
    if frame.ingress_phase == "reserved":
        collision = conn.execute(select(messages.c.id).where(messages.c.id == reserved)).one_or_none()
        if (
            frame.ingress_receipt_json is not None
            or parent.ingress_phase != "pending"
            or parent.user_message_id is not None
            or row is not None
            or collision is not None
        ):
            raise AuditIntegrityError("reserved initializer cannot adopt a preexisting user row or receipt")
        return ContinuationIngressPrepared(actual, reserved, principal.title)
    if frame.ingress_receipt_json is None:
        raise AuditIntegrityError("inserted initializer lost its actual ingress receipt")
    value = _receipt(frame.ingress_receipt_json)
    original_event = conn.execute(
        select(events).where(events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == value.source_cursor + 1)
    ).one_or_none()
    predecessor = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation,
            checkpoints.c.checkpoint_id == value.source_checkpoint_id,
        )
    ).one_or_none()
    if (
        original_event is None
        or original_event.tag != "checkpoint"
        or predecessor is None
        or predecessor.cursor != value.source_cursor
        or not value.source_cursor < root.cursor
    ):
        raise AuditIntegrityError("inserted initializer lost its actual ingress predecessor/event")
    try:
        event_text = read_chunk(conn, session_id=sid, operation_id=operation, key=original_event.payload_chunk_key).decode(
            "utf-8", errors="strict"
        )
        predecessor_text = read_chunk(conn, session_id=sid, operation_id=operation, key=predecessor.snapshot_chunk_key).decode(
            "utf-8", errors="strict"
        )
    except UnicodeDecodeError as error:
        raise AuditIntegrityError("initializer ingress storage is not strict UTF-8") from error
    source_root = _actual_ingress_source_root_on_connection(conn, running, source)
    source_frame = source_root.frames[0]
    if len(source_root.frames) != 1 or type(source_frame) is not InitializeFrame:
        raise AuditIntegrityError("initializer ingress source lost its exact Initialize frame")
    before = decode_continuation_snapshot(predecessor_text)
    expected_before = source_frame.model_copy(update={"ingress_phase": "reserved", "ingress_receipt_json": None, "bootstrap": None})
    if (
        event_text != _event(value)
        or before.logical != source_root.logical
        or before.mutation_ceiling != source_root.mutation_ceiling
        or before.policy_hash != source_root.policy_hash
        or before.admission_policy_json != source_root.admission_policy_json
        or before.frames != (expected_before,)
        or before.cursor != value.source_cursor
    ):
        raise AuditIntegrityError("inserted initializer changed its exact original ingress source/event")
    if (
        parent.ingress_phase != "committed"
        or parent.user_message_id != reserved
        or row is None
        or (value.session_id, value.operation_id, value.user_message_id, value.requested_state_id, value.composition_state_id)
        != (sid, operation, reserved, requested, state)
        or (row.user_message_id, row.requested_state_id, restore_utc(row.created_at).isoformat()) != (reserved, requested, value.created_at)
    ):
        raise AuditIntegrityError("inserted initializer changed its original SQL ingress receipt")
    transcript = _records(conn, session_id=sid, operation_id=operation, message_id=reserved, cutoff=value.original_last_sequence)
    if (
        not transcript
        or str(transcript[-1].id) != reserved
        or transcript[-1].sequence_no != value.original_last_sequence
        or transcript[-1].role != "user"
        or transcript[-1].writer_principal != "route_user_message"
        or transcript[-1].content != content
        or transcript[-1].composition_state_id != composition_state_id
        or transcript[-1].raw_content is not None
        or transcript[-1].tool_calls is not None
        or transcript[-1].tool_call_id is not None
        or transcript[-1].parent_assistant_id is not None
        or stable_hash(_canonical_records_json(transcript)) != value.transcript_sha256
    ):
        raise AuditIntegrityError("inserted initializer changed its complete original SQL user/transcript source")
    return ContinuationIngressCheckpoint(UUID(operation), transcript[-1], transcript, actual, "replayed")


def read_prepared_title_ingress_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    actual: StoredContinuationSnapshot,
    source: StoredContinuationSnapshot,
    content: str,
    requested_state_id: UUID | None,
    composition_state_id: UUID | None,
) -> ContinuationIngressCheckpoint:
    """Read a real prepared TITLE extension of its real published source.

    Both snapshots are actual SQL DATA. Current authorization always uses
    actual. The TITLE owner separately checks the full attributed stage chain.
    """
    if type(actual) is not StoredContinuationSnapshot or type(running) is not ComposerOperationRunning:
        raise AuditIntegrityError("prepared TITLE ingress requires actual current/source owners")
    _require_current_continuation_running(conn, running)
    if load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id) != actual:
        from elspeth.web.sessions.continuation import ContinuationCASConflict

        raise ContinuationCASConflict("prepared TITLE ingress changed its complete current CAS")
    root = decode_continuation_snapshot(actual.payload_json)
    frame = root.frames[0]
    source_root = _actual_ingress_source_root_on_connection(conn, running, source)
    source_frame = source_root.frames[0]
    if (
        len(root.frames) != 1
        or type(frame) is not InitializeFrame
        or frame.ingress_phase != "title_pending"
        or frame.provider_effect_key != "provider:3:0"
        or frame.provider_input_sha256 is None
        or frame.title_attempt_id is not None
        or frame.title_response is not None
        or len(source_root.frames) != 1
        or type(source_frame) is not InitializeFrame
        or source_frame.ingress_phase != "title_pending"
        or source_frame.bootstrap is None
        or source_frame.provider_effect_key is not None
        or source_frame.provider_input_sha256 is not None
        or source_frame.title_attempt_id is not None
        or source_frame.title_response is not None
        or source_root.effect_frontier
        or source_root.pending_owner_results
        or source_root.pending_settlement_ids
        or source_root.semantic_frontier is not None
        or source_root.active_action_json is not None
        or source_root.active_action_source is not None
    ):
        raise AuditIntegrityError("prepared TITLE ingress has no actual provider-free publication predecessor")
    expected_frame = source_frame.model_copy(
        update={"provider_effect_key": frame.provider_effect_key, "provider_input_sha256": frame.provider_input_sha256}
    )
    expected_root = source_root.model_copy(update={"cursor": root.cursor, "frames": (expected_frame,)})
    expected = _effect_snapshot(
        encode_continuation_snapshot(expected_root), effect_key="provider:3:0", disposition="prepared", receipt_id=None
    )
    if actual.cursor != source.cursor + 1 or expected != actual.payload_json:
        raise AuditIntegrityError("prepared TITLE ingress changed its full actual forward source transition")
    result = _read_ingress_values_with_source_on_connection(
        conn,
        running,
        actual=actual,
        source=source,
        content=content,
        requested_state_id=requested_state_id,
        composition_state_id=composition_state_id,
    )
    if type(result) is not ContinuationIngressCheckpoint:
        raise AuditIntegrityError("prepared TITLE ingress lost its actual committed user source")
    return result


def read_bootstrapped_ingress_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
    content: str,
    requested_state_id: UUID | None,
    composition_state_id: UUID | None,
) -> ContinuationIngressCheckpoint:
    """Reprove original ingress at one closed, provider-free pre-title cut.

    This read never routes through the inserter and does not manufacture an
    inserted selection. The separate bootstrap owner must also prove its own
    original publication predecessor, event and complete private source.
    """
    if type(selected) is not StoredContinuationSnapshot or type(running) is not ComposerOperationRunning:
        raise AuditIntegrityError("bootstrap ingress read requires actual selected/current owners")
    root = decode_continuation_snapshot(selected.payload_json)
    frame = root.frames[0]
    if (
        len(root.frames) != 1
        or type(frame) is not InitializeFrame
        or frame.ingress_phase not in ("title_pending", "ready")
        or frame.bootstrap is None
        or frame.bootstrap.codec != "composer.bootstrap.v1"
        or frame.ingress_receipt_json is None
        or frame.title_attempt_id is not None
        or frame.title_response is not None
        or frame.provider_effect_key is not None
        or frame.provider_input_sha256 is not None
        or root.effect_frontier
        or root.pending_owner_results
        or root.pending_settlement_ids
        or root.active_action_json is not None
        or root.active_action_source is not None
        or root.semantic_frontier is not None
    ):
        raise AuditIntegrityError("bootstrap ingress read has no closed published SQL-only cut")
    result = _read_ingress_source_on_connection(
        conn,
        running,
        actual=selected,
        content=content,
        requested_state_id=requested_state_id,
        composition_state_id=composition_state_id,
    )
    if type(result) is not ContinuationIngressCheckpoint:
        raise AuditIntegrityError("bootstrap ingress read lost its actual committed human source")
    return result


def bind_ingress_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, prepared: ContinuationIngressPrepared, message_id: str
) -> None:
    _require_current_continuation_running(conn, running)
    if type(prepared) is not ContinuationIngressPrepared or message_id != prepared.reserved_id:
        raise AuditIntegrityError("initializer writer changed its actual reserved message identity")
    if load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id) != prepared.selected:
        raise AuditIntegrityError("initializer ingress source changed before its actual parent binding")
    result = conn.execute(
        update(jobs)
        .where(
            jobs.c.session_id == str(running.claim.session_id),
            jobs.c.operation_id == running.claim.operation_id,
            jobs.c.lifecycle_version == 2,
            jobs.c.ingress_phase == "pending",
            jobs.c.user_message_id.is_(None),
            jobs.c.reserved_user_message_id == message_id,
            jobs.c.checkpoint_id == prepared.selected.checkpoint_id,
            jobs.c.journal_cursor == prepared.selected.cursor,
        )
        .values(user_message_id=message_id, ingress_phase="committed")
    )
    if result.rowcount != 1:
        raise AuditIntegrityError("initializer ingress parent was not the actual fresh reserved owner")


def retain_ingress_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, prepared: ContinuationIngressPrepared, transcript: tuple[ChatMessageRecord, ...]
) -> ContinuationIngressCheckpoint:
    _require_current_continuation_running(conn, running)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    if type(prepared) is not ContinuationIngressPrepared or not transcript:
        raise AuditIntegrityError("initializer ingress lost its actual completed writer values")
    message = transcript[-1]
    if str(message.id) != prepared.reserved_id or type(message.sequence_no) is not int:
        raise AuditIntegrityError("initializer transcript does not end at its actual reserved human row")
    row = conn.execute(select(receipts).where(receipts.c.session_id == sid, receipts.c.operation_id == operation)).one()
    value = ContinuationIngressReceipt(
        session_id=sid,
        operation_id=operation,
        user_message_id=prepared.reserved_id,
        requested_state_id=row.requested_state_id,
        composition_state_id=str(message.composition_state_id) if message.composition_state_id is not None else None,
        created_at=restore_utc(row.created_at).isoformat(),
        original_last_sequence=message.sequence_no,
        transcript_sha256=stable_hash(_canonical_records_json(transcript)),
        original_session_title=prepared.original_session_title,
        source_checkpoint_id=prepared.selected.checkpoint_id,
        source_cursor=prepared.selected.cursor,
    )
    root = decode_continuation_snapshot(prepared.selected.payload_json)
    frame = root.frames[0]
    if type(frame) is not InitializeFrame or frame.ingress_phase != "reserved":
        raise AuditIntegrityError("initializer completed writer lost its exact reserved predecessor")
    receipt_json = canonical_json(value.model_dump(mode="json"))
    following = root.model_copy(
        update={"frames": (frame.model_copy(update={"ingress_phase": "inserted", "ingress_receipt_json": receipt_json}),)}
    )
    snapshot = append_complete_snapshot(
        conn,
        session_id=sid,
        operation_id=operation,
        expected_checkpoint_id=prepared.selected.checkpoint_id,
        expected_cursor=prepared.selected.cursor,
        snapshot_json=encode_continuation_snapshot(following),
        event_batch=(ContinuationEvent("checkpoint", _event(value)),),
        handoff=False,
    )
    # Reuse the exact current read admission before the transaction can commit.
    checked = prepare_ingress_on_connection(
        conn,
        running,
        selected=snapshot,
        content=message.content,
        requested_state_id=UUID(row.requested_state_id) if row.requested_state_id is not None else None,
        composition_state_id=message.composition_state_id,
    )
    if type(checked) is not ContinuationIngressCheckpoint:
        raise AuditIntegrityError("initializer did not retain its actual complete ingress source")
    return ContinuationIngressCheckpoint(UUID(operation), message, transcript, snapshot, "inserted")
