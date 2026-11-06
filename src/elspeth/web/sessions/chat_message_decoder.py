"""Decode owned chat-message storage rows without granting read authority."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.protocol import ChatMessageRecord
from elspeth.web.sessions.time_normalization import restore_utc


def chat_message_record_from_row(row: Any, *, operation_id: str | None = None) -> ChatMessageRecord:
    if operation_id is not None and (row.role != "user" or row.writer_principal != "route_user_message"):
        raise AuditIntegrityError("Tier 1: message ingress receipt is attached to a non-user message")
    return ChatMessageRecord(
        id=UUID(row.id),
        session_id=UUID(row.session_id),
        role=row.role,
        content=row.content,
        raw_content=row.raw_content,
        tool_calls=row.tool_calls,
        created_at=restore_utc(row.created_at),
        sequence_no=row.sequence_no,
        composition_state_id=UUID(row.composition_state_id) if row.composition_state_id else None,
        writer_principal=row.writer_principal,
        tool_call_id=row.tool_call_id,
        parent_assistant_id=UUID(row.parent_assistant_id) if row.parent_assistant_id else None,
        operation_id=UUID(operation_id) if operation_id is not None else None,
    )
