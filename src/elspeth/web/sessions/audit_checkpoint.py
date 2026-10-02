"""Filter already checkpointed provider evidence from later audit cohorts."""

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.engine import Connection

from elspeth.web.coordination.quota_authority import (
    llm_call_usage_entries,
    settle_provider_attempt_on_connection,
    settled_provider_attempt_ids_on_connection,
)


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
    return tuple(
        envelope
        for envelope in envelopes
        if "_kind" not in envelope or envelope["_kind"] != "llm_call_audit" or envelope["call"]["call_id"] not in settled
    )
