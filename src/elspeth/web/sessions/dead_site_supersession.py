"""Dead-site supersession of pending interpretation reviews.

One implementation, two composition-state writers. The locked session head
owns supersession authority, so the head advance and the retirement of the
reviews it extinguished must settle in the SAME transaction. Before this
module the sweep lived only on ``SessionServiceImpl._insert_composition_state``
while the session-operation authority's own writer
(``_RepositoryCompositionStateMutations.append_state``) appended a head
without it — a split that merges cleanly and leaves the zombie card
mainline's ``elspeth-d73139155a`` fix exists to retire.

The module holds no logging: ``web/coordination/repository.py`` is
deliberately log-free and the durable ``interpretation_events`` row is the
audit record. Retirements are RETURNED so a caller that does carry a logger
can emit its telemetry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

from sqlalchemy import select, update

from elspeth.contracts.composer_interpretation import InterpretationChoice, InterpretationKind, InterpretationSource
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.models import interpretation_events_table
from elspeth.web.sessions.protocol import CompositionStateRecord, InterpretationResolveError

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy import Connection


@final
@dataclass(frozen=True, slots=True)
class RetiredDeadSiteReview:
    """One pending review terminally retired because its site is gone."""

    event_id: str
    kind: str
    affected_node_id: str
    reason: str


def supersede_dead_site_pending_interpretation_events(
    connection: Connection,
    *,
    session_id: str,
    state_record: CompositionStateRecord,
    now: datetime,
) -> tuple[RetiredDeadSiteReview, ...]:
    """Terminally retire pending reviews whose site the new head extinguished.

    ``state_record`` is the head this transaction just appended; ``connection``
    must be the same connection that inserted it, so the advance and the
    retirement commit together.

    The predicate is deliberately "identity derivation RAISES", never
    "identity differs": a differing identity means the site still exists and
    the surfacing dedup path owns reconciliation (supersede + fresh card),
    while a raising derivation means no future surfacing can occur — the tool
    boundary refuses review requests for a site the live state no longer
    carries, so nothing downstream would ever retire the row. Left pending,
    such a row is a zombie card: resolve raises drift forever and the Run gate
    blocks on it (elspeth-d73139155a). ``SUPERSEDED`` records the retirement
    honestly; ``ABANDONED`` is adjudicated wrong for supersession — the
    session continues (elspeth-dbc39dd367).
    """
    from elspeth.web.sessions.pending_interpretation import _reviewed_content_identity

    if type(state_record) is not CompositionStateRecord:
        raise TypeError("dead-site supersession requires an exact CompositionStateRecord head")
    pending_rows = connection.execute(
        select(interpretation_events_table)
        .where(interpretation_events_table.c.session_id == session_id)
        .where(interpretation_events_table.c.choice == InterpretationChoice.PENDING.value)
        .where(interpretation_events_table.c.interpretation_source == InterpretationSource.USER_APPROVED.value)
    ).all()
    if not pending_rows:
        return ()

    def _extinguished_site_error(kind: InterpretationKind, affected_node_id: str, user_term: str) -> str | None:
        """Return the derivation error when the new head extinguished the site, else None."""
        try:
            _reviewed_content_identity(
                state_record,
                kind=kind,
                affected_node_id=affected_node_id,
                user_term=user_term,
                context="supersede_dead_site_pending_interpretation_events",
            )
        except InterpretationResolveError as exc:
            return str(exc)
        return None

    retired: list[RetiredDeadSiteReview] = []
    for pending_row in pending_rows:
        kind_value = pending_row.kind
        user_term = pending_row.user_term
        affected_node_id = pending_row.affected_node_id
        if type(kind_value) is not str or type(user_term) is not str or type(affected_node_id) is not str:
            raise AuditIntegrityError(
                "supersede_dead_site_pending_interpretation_events: pending user_approved row "
                f"{pending_row.id!r} violates the row-shape contract"
            )
        extinguished_reason = _extinguished_site_error(InterpretationKind(kind_value), affected_node_id, user_term)
        if extinguished_reason is None:
            continue
        retired.append(
            RetiredDeadSiteReview(
                event_id=pending_row.id,
                kind=kind_value,
                affected_node_id=affected_node_id,
                reason=extinguished_reason,
            )
        )
    if not retired:
        return ()
    connection.execute(
        update(interpretation_events_table)
        .where(interpretation_events_table.c.id.in_([entry.event_id for entry in retired]))
        .where(interpretation_events_table.c.session_id == session_id)
        .where(interpretation_events_table.c.choice == InterpretationChoice.PENDING.value)
        .values(
            choice=InterpretationChoice.SUPERSEDED.value,
            resolved_at=now,
        )
    )
    return tuple(retired)
