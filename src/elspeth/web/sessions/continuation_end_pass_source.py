"""Read one original accepted model END pass from its durable audit row.

The row is a historical source fact. It grants no publication by itself;
callers must separately reprove the original END execution, complete result,
physical effects, selected checkpoint and later paid whole-request Stop.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.advisor_audit import ADVISOR_CHECKPOINT_PASS_AUDIT_KIND, AdvisorCheckpointPassRecord
from elspeth.web.composer.continuation_end_disposition import require_end_publication_pass
from elspeth.web.sessions.models import chat_messages_table


@dataclass(frozen=True, slots=True)
class OriginalAcceptedEndPass:
    message_id: str
    record: AdvisorCheckpointPassRecord


def read_original_accepted_end_pass_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    message_id: str,
    pass_index: int,
    publication_binding: str,
) -> OriginalAcceptedEndPass:
    """Reparse the exact persisted pass envelope, never a caller's verdict."""
    if (
        type(conn) is not Connection
        or type(session_id) is not UUID
        or type(message_id) is not str
        or not message_id
        or type(pass_index) is not int
        or pass_index < 0
        or type(publication_binding) is not str
        or len(publication_binding) != 64
        or any(char not in "0123456789abcdef" for char in publication_binding)
    ):
        raise AuditIntegrityError("original END pass lost its exact SQL identity")
    rows = conn.execute(
        select(chat_messages_table).where(
            chat_messages_table.c.session_id == str(session_id),
            chat_messages_table.c.id == message_id,
            chat_messages_table.c.role == "audit",
            chat_messages_table.c.writer_principal == "compose_loop",
            chat_messages_table.c.tool_calls[0]["_kind"].as_string() == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND,
            chat_messages_table.c.tool_calls[0]["pass"]["phase"].as_string() == "end",
            chat_messages_table.c.tool_calls[0]["pass"]["source"].as_string() == "model",
            chat_messages_table.c.tool_calls[0]["pass"]["publication_binding"].as_string() == publication_binding,
            chat_messages_table.c.tool_calls[0]["pass"]["pass_index"].as_integer() == pass_index,
        )
    ).all()
    if len(rows) != 1:
        raise AuditIntegrityError("original END pass lost its unique persisted model audit row")
    row = rows[0]
    if type(row.tool_calls) is not list or len(row.tool_calls) != 1:
        raise AuditIntegrityError("original END pass lost its complete one-envelope audit row")
    envelope = row.tool_calls[0]
    if type(envelope) is not dict or set(envelope) != {"_kind", "pass"} or envelope["_kind"] != ADVISOR_CHECKPOINT_PASS_AUDIT_KIND:
        raise AuditIntegrityError("original END pass changed its closed audit envelope")
    try:
        record = AdvisorCheckpointPassRecord.from_dict(envelope["pass"])
    except (TypeError, ValueError, KeyError) as error:
        raise AuditIntegrityError("original END pass changed its original model record") from error
    require_end_publication_pass(record, publication_binding=publication_binding)
    if record.pass_index != pass_index:
        raise AuditIntegrityError("original END pass changed its selected pass index")
    return OriginalAcceptedEndPass(message_id, record)
