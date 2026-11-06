"""Transaction-local reproof of the pending interpretation cohort at H."""

from __future__ import annotations

from pydantic import TypeAdapter
from sqlalchemy import Connection, select

from elspeth.contracts.composer_interpretation import InterpretationChoice, InterpretationEventRecord
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_tool_batch import _dump_owned
from elspeth.web.sessions.continuation_checkpoint_end_owed import VerifiedCheckpointEndOwed
from elspeth.web.sessions.models import interpretation_events_table as interpretation_events

_PENDING_EVENTS = TypeAdapter(tuple[InterpretationEventRecord, ...])


def require_end_pending_cohort_on_connection(conn: Connection, *, owed: VerifiedCheckpointEndOwed) -> None:
    """Compare the current SQL cohort with the deep-H-proved original gate."""
    from elspeth.web.sessions.service import _interpretation_event_record_from_row

    if type(owed) is not VerifiedCheckpointEndOwed or owed.end_input is None:
        raise AuditIntegrityError("END pending cohort lacks its complete retained gate source")
    rows = conn.execute(
        select(interpretation_events)
        .where(
            interpretation_events.c.session_id == owed.session_id,
            interpretation_events.c.choice == InterpretationChoice.PENDING.value,
        )
        .order_by(interpretation_events.c.created_at, interpretation_events.c.id)
    ).all()
    events = tuple(_interpretation_event_record_from_row(row) for row in rows)
    if _dump_owned(_PENDING_EVENTS, events) != owed.end_input.orphan_events_json:
        raise AuditIntegrityError("cold END pending interpretation cohort changed after gate reproof")
