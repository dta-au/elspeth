"""Filter already checkpointed provider evidence from later audit cohorts."""

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.visible_tool_event import VISIBLE_RESPONSE_GAP_KIND
from elspeth.web.coordination.quota_authority import (
    llm_call_usage_entries,
    settle_provider_attempt_on_connection,
    settled_provider_attempt_ids_on_connection,
)
from elspeth.web.sessions.models import chat_messages_table


def uncheckpointed_envelopes(
    connection: Connection, *, session_id: str, envelopes: Sequence[Mapping[str, Any]]
) -> tuple[Mapping[str, Any], ...]:
    """Keep new evidence and validate every skipped terminal replay."""
    entries = llm_call_usage_entries(envelopes)
    settled = settled_provider_attempt_ids_on_connection(
        connection, session_id=session_id, attempt_ids=tuple(entry.call_id for entry in entries if entry.call_id is not None)
    )
    for entry in entries:
        if entry.call_id in settled:
            assert entry.call_id is not None
            settle_provider_attempt_on_connection(connection, session_id=session_id, attempt_id=entry.call_id, entry=entry)
    gaps = tuple(envelope for envelope in envelopes if "_kind" in envelope and envelope["_kind"] == VISIBLE_RESPONSE_GAP_KIND)
    if gaps and any(entry.call_id in settled for entry in entries):
        if len(envelopes) != 2 or len(gaps) != 1 or len(entries) != 1:
            raise AuditIntegrityError("Provider checkpoint companion replay lacks one exact call pair")
        rows = connection.execute(
            select(chat_messages_table.c.tool_calls).where(
                chat_messages_table.c.session_id == session_id,
                chat_messages_table.c.role == "audit",
                chat_messages_table.c.writer_principal == "compose_loop",
                chat_messages_table.c.tool_calls[0]["_kind"].as_string() == "llm_call_audit",
                chat_messages_table.c.tool_calls[0]["call"]["call_id"].as_string() == entries[0].call_id,
            )
        ).all()
        if len(rows) != 1 or deep_thaw(rows[0].tool_calls) != deep_thaw(envelopes):
            raise AuditIntegrityError("Provider checkpoint companion differs from its durable same-row evidence")
        return ()
    return tuple(
        envelope
        for envelope in envelopes
        if "_kind" not in envelope or envelope["_kind"] != "llm_call_audit" or envelope["call"]["call_id"] not in settled
    )
