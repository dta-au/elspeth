"""Payload-free private custody checks for ordinary transcript readers."""

from sqlalchemy import ColumnElement, Connection, case, exists, func, or_, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import JSONB

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.diagnostic_evidence import DIAGNOSTIC_EVIDENCE_KIND
from elspeth.web.sessions.models import chat_messages_table


def non_private_message_predicate(dialect_name: str) -> ColumnElement[bool]:
    """Recognize private kind in every slot before hydrating payload columns."""
    calls = chat_messages_table.c.tool_calls
    if dialect_name == "sqlite":
        slots = func.json_each(calls).table_valued("value", "type")
        kind = case((slots.c.type == "object", func.json_extract(slots.c.value, "$._kind")), else_=None)
    elif dialect_name == "postgresql":
        document = sql_cast(calls, JSONB)
        array = case((func.jsonb_typeof(document) == "array", document), else_=func.jsonb_build_array())
        slots = func.jsonb_array_elements(array).table_valued("value")
        kind = sql_cast(slots.c.value, JSONB)["_kind"].as_string()
    else:
        raise AuditIntegrityError("Private evidence reads require a qualified SQLite or PostgreSQL dialect")
    return ~exists(select(1).select_from(slots).where(kind == DIAGNOSTIC_EVIDENCE_KIND))


def validate_transcript_private_custody(conn: Connection, session_id: str) -> None:
    """Keep corrupt custody fatal using scalar metadata, without payloads."""
    calls = chat_messages_table.c.tool_calls
    count: ColumnElement[int]
    if conn.dialect.name == "sqlite":
        count = func.json_array_length(calls)
    elif conn.dialect.name == "postgresql":
        document = sql_cast(calls, JSONB)
        count = case((func.jsonb_typeof(document) == "array", func.jsonb_array_length(document)), else_=0)
    else:
        raise AuditIntegrityError("Private evidence reads require a qualified SQLite or PostgreSQL dialect")
    damaged = conn.execute(
        select(chat_messages_table.c.id)
        .where(
            chat_messages_table.c.session_id == session_id,
            ~non_private_message_predicate(conn.dialect.name),
            or_(chat_messages_table.c.role != "audit", chat_messages_table.c.writer_principal != "compose_loop", count != 1),
        )
        .limit(1)
    ).first()
    if damaged is not None:
        raise AuditIntegrityError("Private Composer evidence requires a dedicated Composer audit row")
