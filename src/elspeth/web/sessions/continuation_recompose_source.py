"""Adopt the existing authenticated Recompose human row as immutable SQL source."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.continuation import InitializeFrame
from elspeth.web.composer.continuation_bootstrap import _canonical_records_json
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.sessions.chat_message_decoder import chat_message_record_from_row
from elspeth.web.sessions.composer_operations import ComposerOperationRunning, composer_operation_admission_request_hash
from elspeth.web.sessions.continuation import (
    ContinuationCASConflict,
    ContinuationEvent,
    StoredContinuationSnapshot,
    _require_current_continuation_running,
    append_complete_snapshot,
    load_snapshot,
    read_chunk,
)
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import message_ingress_receipts_table as ingress_receipts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import sessions_table as sessions
from elspeth.web.sessions.protocol import ChatMessageRecord
from elspeth.web.sessions.schemas import RecomposeRequest
from elspeth.web.sessions.transcript_reads import non_private_message_predicate, validate_transcript_private_custody


class RecomposeOriginalReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["composer.recompose-original.v1"] = "composer.recompose-original.v1"
    session_id: str
    operation_id: str
    original_user_message_id: str
    original_last_sequence: int = Field(ge=1)
    transcript_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_session_title: str
    source_checkpoint_id: str
    source_cursor: int = Field(ge=0)


@dataclass(frozen=True, slots=True)
class RecomposeOriginalSource:
    selected: StoredContinuationSnapshot
    request: RecomposeRequest
    actor_user_id: str
    records: tuple[ChatMessageRecord, ...]
    original_session_title: str
    receipt: RecomposeOriginalReceipt | None


def _receipt(text: str) -> RecomposeOriginalReceipt:
    try:
        value = RecomposeOriginalReceipt.model_validate_json(text, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("Recompose lost its complete original-source receipt") from error
    if canonical_json(value.model_dump(mode="json")) != text:
        raise AuditIntegrityError("Recompose original-source receipt changed encoding")
    return value


def _event(value: RecomposeOriginalReceipt) -> str:
    return canonical_json({"owner": "composer_recompose_original", "receipt": value.model_dump(mode="json")})


def _checkpoint(conn: Connection, *, sid: str, operation: str, cursor: int) -> StoredContinuationSnapshot:
    row = conn.execute(
        select(checkpoints).where(checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation, checkpoints.c.cursor == cursor)
    ).one_or_none()
    if row is None or row.kind != "transition" or row.frame_stack_version != 1:
        raise AuditIntegrityError("Recompose original source lost its actual transition checkpoint")
    try:
        text = read_chunk(conn, session_id=sid, operation_id=operation, key=row.snapshot_chunk_key).decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise AuditIntegrityError("Recompose original checkpoint is not strict UTF-8") from error
    root = decode_continuation_snapshot(text)
    if root.cursor != cursor or encode_continuation_snapshot(root) != text:
        raise AuditIntegrityError("Recompose original checkpoint changed complete root")
    return StoredContinuationSnapshot(text, cursor, row.checkpoint_id)


def _records(conn: Connection, sid: str, *, cutoff: int | None) -> tuple[ChatMessageRecord, ...]:
    validate_transcript_private_custody(conn, sid)
    rows = conn.execute(
        select(messages, ingress_receipts.c.operation_id)
        .select_from(
            messages.outerjoin(
                ingress_receipts,
                (ingress_receipts.c.user_message_id == messages.c.id) & (ingress_receipts.c.session_id == messages.c.session_id),
            )
        )
        .where(
            messages.c.session_id == sid,
            messages.c.sequence_no <= cutoff if cutoff is not None else messages.c.sequence_no >= 1,
            non_private_message_predicate(conn.dialect.name),
        )
        .order_by(messages.c.sequence_no)
    ).all()
    return tuple(chat_message_record_from_row(row, operation_id=row.operation_id) for row in rows)


def read_recompose_original_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
    current: bool = True,
) -> RecomposeOriginalSource:
    """Read current source or a caller-authorized historical source; never insert."""
    if type(running) is not ComposerOperationRunning or type(selected) is not StoredContinuationSnapshot:
        raise AuditIntegrityError("Recompose source requires actual running/selected owners")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    if current:
        _require_current_continuation_running(conn, running)
        if load_snapshot(conn, session_id=sid, operation_id=operation) != selected:
            raise ContinuationCASConflict("Recompose original source changed its complete selected CAS")
    if _checkpoint(conn, sid=sid, operation=operation, cursor=selected.cursor) != selected:
        raise AuditIntegrityError("Recompose source lost its actual stored selected checkpoint")
    root = decode_continuation_snapshot(selected.payload_json)
    frame = root.frames[0]
    if (
        len(root.frames) != 1
        or type(frame) is not InitializeFrame
        or root.logical.kind != "compose_recompose"
        or frame.ingress_phase not in ("reserved", "inserted", "ready")
    ):
        raise AuditIntegrityError("Recompose source has no closed original Initialize cut")
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
    principal = conn.execute(select(sessions.c.user_id, sessions.c.archived_at, sessions.c.title).where(sessions.c.id == sid)).one()
    try:
        request = RecomposeRequest.model_validate_json(parent.request_json, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("Recompose original parent request changed admitted shape") from error
    original_id = str(request.expected_user_message_id)
    if (
        parent.kind != "compose_recompose"
        or parent.ingress_phase != "committed"
        or parent.user_message_id != original_id
        or parent.reserved_user_message_id != original_id
        or parent.actor_user_id != principal.user_id
        or principal.archived_at is not None
        or frame.request_json != parent.request_json
        or request.operation_id != operation
        or (str(request.state_id) if request.state_id is not None else None) != parent.base_state_id
        or composer_operation_admission_request_hash(session_id=UUID(sid), kind="compose_recompose", request=request) != parent.request_hash
        or root.logical.original_user_message_id != original_id
        or root.logical.request_hash != parent.request_hash
        or root.logical.base_state_id != parent.base_state_id
    ):
        raise AuditIntegrityError("Recompose original actor/request/base/user binding changed")
    if (
        conn.execute(
            select(ingress_receipts.c.operation_id).where(
                ingress_receipts.c.session_id == sid, ingress_receipts.c.operation_id == operation
            )
        ).one_or_none()
        is not None
    ):
        raise AuditIntegrityError("Recompose cannot own a newly inserted user-message receipt")
    receipt = None if frame.ingress_phase == "reserved" else _receipt(frame.ingress_receipt_json or "")
    records = _records(conn, sid, cutoff=receipt.original_last_sequence if receipt is not None else None)
    conversation = tuple(row for row in records if row.role not in ("audit", "tool"))
    user = next((row for row in reversed(conversation) if row.role == "user"), None)
    if (
        user is None
        or str(user.id) != original_id
        or user.writer_principal not in ("route_user_message", "session_fork")
        or any(row.role == "assistant" and not row.tool_calls for row in conversation[conversation.index(user) + 1 :])
    ):
        raise AuditIntegrityError("Recompose changed its original authenticated human turn")
    if frame.ingress_phase == "reserved":
        if frame.ingress_receipt_json is not None or frame.bootstrap is not None:
            raise AuditIntegrityError("Recompose reserved source already claims publication")
    else:
        if frame.ingress_receipt_json is None or type(receipt) is not RecomposeOriginalReceipt:
            raise AuditIntegrityError("Recompose inserted source lost its receipt")
        predecessor = _checkpoint(conn, sid=sid, operation=operation, cursor=receipt.source_cursor)
        prior = decode_continuation_snapshot(predecessor.payload_json)
        published = _checkpoint(conn, sid=sid, operation=operation, cursor=receipt.source_cursor + 1)
        event = conn.execute(
            select(events).where(
                events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == receipt.source_cursor + 1
            )
        ).one_or_none()
        if event is None or event.tag != "checkpoint":
            raise AuditIntegrityError("Recompose source lost its actual original publication event")
        event_text = read_chunk(conn, session_id=sid, operation_id=operation, key=event.payload_chunk_key).decode("utf-8", errors="strict")
        if (
            receipt.session_id != sid
            or receipt.operation_id != operation
            or receipt.original_user_message_id != original_id
            or receipt.source_checkpoint_id != predecessor.checkpoint_id
            or prior.logical != root.logical
            or len(prior.frames) != 1
            or type(prior.frames[0]) is not InitializeFrame
            or prior.frames[0].ingress_phase != "reserved"
            or published.payload_json
            != encode_continuation_snapshot(
                prior.model_copy(
                    update={
                        "cursor": receipt.source_cursor + 1,
                        "frames": (
                            prior.frames[0].model_copy(
                                update={
                                    "ingress_phase": "inserted",
                                    "ingress_receipt_json": canonical_json(receipt.model_dump(mode="json")),
                                }
                            ),
                        ),
                    }
                )
            )
            or event_text != _event(receipt)
            or receipt.original_last_sequence != records[-1].sequence_no
            or receipt.transcript_sha256 != stable_hash(_canonical_records_json(records))
            or receipt.original_session_title != principal.title
        ):
            raise AuditIntegrityError("Recompose original publication/source changed")
    if current and frame.ingress_phase in ("reserved", "inserted", "ready"):
        prior_fences = select(attempts.c.session_operation_id).where(attempts.c.session_id == sid, attempts.c.operation_id == operation)
        if (
            conn.execute(select(effects.c.effect_key).where(effects.c.session_id == sid, effects.c.operation_id == operation)).first()
            is not None
            or conn.execute(
                select(physical.c.attempt_id).where(physical.c.session_id == sid, physical.c.operation_id.in_(prior_fences))
            ).first()
            is not None
        ):
            raise AuditIntegrityError("Recompose source refuses prior provider/effect work")
    return RecomposeOriginalSource(selected, request, parent.actor_user_id, records, principal.title, receipt)


def publish_recompose_original_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: StoredContinuationSnapshot
) -> RecomposeOriginalSource:
    source = read_recompose_original_on_connection(conn, running, selected=selected)
    root = decode_continuation_snapshot(selected.payload_json)
    frame = root.frames[0]
    if type(frame) is not InitializeFrame or frame.ingress_phase != "reserved" or not source.records:
        raise AuditIntegrityError("Recompose publication requires original reserved source")
    last_sequence = source.records[-1].sequence_no
    if type(last_sequence) is not int or last_sequence < 1:
        raise AuditIntegrityError("Recompose publication lost its actual positive transcript sequence")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    receipt = RecomposeOriginalReceipt(
        session_id=sid,
        operation_id=operation,
        original_user_message_id=str(source.request.expected_user_message_id),
        original_last_sequence=last_sequence,
        transcript_sha256=stable_hash(_canonical_records_json(source.records)),
        original_session_title=source.original_session_title,
        source_checkpoint_id=selected.checkpoint_id,
        source_cursor=selected.cursor,
    )
    following = root.model_copy(
        update={
            "frames": (
                frame.model_copy(
                    update={"ingress_phase": "inserted", "ingress_receipt_json": canonical_json(receipt.model_dump(mode="json"))}
                ),
            )
        }
    )
    published = append_complete_snapshot(
        conn,
        session_id=sid,
        operation_id=operation,
        expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.cursor,
        snapshot_json=encode_continuation_snapshot(following),
        event_batch=(ContinuationEvent("checkpoint", _event(receipt)),),
        handoff=False,
    )
    return read_recompose_original_on_connection(conn, running, selected=published)
