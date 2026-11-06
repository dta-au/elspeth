"""Same-transaction custody for a reply superseded by END repair."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.composer.visible_tool_event import validate_visible_audit_row
from elspeth.web.composer.withheld_replies import withheld_reply_envelope
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.time_normalization import restore_utc


@dataclass(frozen=True, slots=True)
class PreparedEndRepairWithheldReply:
    """One original model reply and its preallocated cold locator."""

    message_id: str
    content: str


def prepare_end_repair_withheld_reply(content: str) -> PreparedEndRepairWithheldReply | None:
    """Match the legacy nonblank rule before measuring the complete batch."""
    if type(content) is not str:
        raise TypeError("END repair withheld reply requires original model text")
    if not content.strip():
        return None
    return PreparedEndRepairWithheldReply(
        message_id=str(uuid4()),
        content=content,
    )


def write_end_repair_withheld_reply_on_connection(
    conn: Connection,
    *,
    service: object,
    context: SessionOperationContext,
    prepared: PreparedEndRepairWithheldReply,
    created_at: datetime,
) -> int:
    """Insert exactly the legacy audit row under the caller's session lock.

    This is called inside the same locked transaction as the original paid
    settlement. The caller must have admitted the entire consequence batch.
    """
    from elspeth.web.sessions.service import SessionServiceImpl

    if (
        type(service) is not SessionServiceImpl
        or type(context) is not SessionOperationContext
        or context.operation_kind is not SessionOperationKind.COMPOSE
        or type(prepared) is not PreparedEndRepairWithheldReply
        or type(created_at) is not datetime
        or not prepared.content.strip()
    ):
        raise AuditIntegrityError("END repair withheld reply changed its original fenced row")
    sid = context.fence.session_id
    envelope = withheld_reply_envelope("repair_gate_superseded", prepared.content)
    service._assert_session_write_lock_held(conn, sid, caller="end_repair_withheld_reply")
    validate_visible_audit_row(
        envelopes=(envelope,),
        content=prepared.content,
        role="audit",
        writer_principal="compose_loop",
        session_id=sid,
        context=context,
    )
    sequence = service._reserve_sequence_range(conn, sid, count=1)
    inserted = service._insert_chat_message(
        conn,
        session_id=sid,
        role="audit",
        content=prepared.content,
        raw_content=None,
        tool_calls=[envelope],
        sequence_no=sequence,
        writer_principal="compose_loop",
        composition_state_id=None,
        tool_call_id=None,
        parent_assistant_id=None,
        created_at=created_at,
        session_operation_context=context,
        message_id=prepared.message_id,
    )
    if inserted != prepared.message_id:
        raise AuditIntegrityError("END repair withheld reply changed its preallocated row identity")
    with service._session_mutations(conn, session_id=sid, session_operation_context=context) as mutations:
        mutations.mark_session_updated(updated_at=created_at)
    require_end_repair_withheld_reply_on_connection(conn, session_id=sid, prepared=prepared, sequence_no=sequence, created_at=created_at)
    return sequence


def require_end_repair_withheld_reply_on_connection(
    conn: Connection,
    *,
    session_id: str,
    prepared: PreparedEndRepairWithheldReply,
    sequence_no: int,
    created_at: datetime,
) -> None:
    """Cold-check the one non-rendered audit row against original text."""
    if (
        type(session_id) is not str
        or not session_id
        or type(prepared) is not PreparedEndRepairWithheldReply
        or type(sequence_no) is not int
        or sequence_no < 1
        or type(created_at) is not datetime
    ):
        raise AuditIntegrityError("END repair withheld cold read lacks its exact row identity")
    row = conn.execute(select(messages).where(messages.c.session_id == session_id, messages.c.id == prepared.message_id)).one_or_none()
    if (
        row is None
        or row.sequence_no != sequence_no
        or row.role != "audit"
        or row.content != prepared.content
        or row.raw_content is not None
        or row.tool_calls != [withheld_reply_envelope("repair_gate_superseded", prepared.content)]
        or row.writer_principal != "compose_loop"
        or row.composition_state_id is not None
        or row.tool_call_id is not None
        or row.parent_assistant_id is not None
        or restore_utc(row.created_at) != restore_utc(created_at)
    ):
        raise AuditIntegrityError("END repair lost its exact superseded model reply")
