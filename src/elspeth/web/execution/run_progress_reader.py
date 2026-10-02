"""Authorized, bounded run-event reads without execution ownership."""

from typing import cast
from uuid import UUID

from sqlalchemy import Engine, func, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.execution.errors import RunSessionIntegrityError
from elspeth.web.sessions.models import identities_table, run_events_table, runs_table, sessions_table
from elspeth.web.sessions.protocol import RunEventRecord, SessionRunEventType


class RepositoryRunProgressReader:
    """Read committed progress on any replica; never acquire a writer fence.

    Every call uses a fresh repeatable-read snapshot so authorization, sequence
    validation and the bounded page describe one database state. No database
    handles escape the call. PostgreSQL timestamps retain their timezone.
    """

    def __init__(self, engine: Engine) -> None:
        self.__engine = engine

    def read_after(self, *, identity_id: str, run_id: UUID, after_sequence: int, limit: int = 256) -> tuple[RunEventRecord, ...] | None:
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("after_sequence must be a non-negative exact integer")
        if type(limit) is not int or not 1 <= limit <= 256:
            raise ValueError("limit must be between 1 and 256")
        with self.__engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn, conn.begin():
            owner = conn.execute(
                select(
                    runs_table.c.session_id,
                    sessions_table.c.id.label("existing_session_id"),
                    sessions_table.c.user_id,
                    sessions_table.c.auth_provider_type,
                    sessions_table.c.archived_at,
                )
                .select_from(runs_table.outerjoin(sessions_table, runs_table.c.session_id == sessions_table.c.id))
                .where(runs_table.c.id == str(run_id))
            ).one_or_none()
            if owner is None:
                return None
            if owner.existing_session_id is None:
                raise RunSessionIntegrityError(run_id=str(run_id), session_id=owner.session_id)
            identity = conn.execute(
                select(identities_table.c.provider, identities_table.c.access_state).where(identities_table.c.identity_id == identity_id)
            ).one_or_none()
            if (
                identity is None
                or identity.access_state != "active"
                or owner.user_id != identity_id
                or owner.auth_provider_type != identity.provider
                or owner.archived_at is not None
            ):
                return None
            count, minimum, maximum = conn.execute(
                select(func.count(), func.min(run_events_table.c.sequence), func.max(run_events_table.c.sequence)).where(
                    run_events_table.c.run_id == str(run_id)
                )
            ).one()
            if count and (minimum != 1 or maximum != count):
                raise AuditIntegrityError("Tier 1: run_events.sequence is noncontiguous")
            if after_sequence > count:
                raise ValueError("after_sequence exceeds committed run history")
            rows = conn.execute(
                select(run_events_table)
                .where(run_events_table.c.run_id == str(run_id), run_events_table.c.sequence > after_sequence)
                .order_by(run_events_table.c.sequence)
                .limit(limit)
            ).all()
            try:
                return tuple(
                    RunEventRecord(
                        id=UUID(row.id),
                        run_id=UUID(row.run_id),
                        sequence=row.sequence,
                        timestamp=row.timestamp,
                        event_type=cast(SessionRunEventType, row.event_type),
                        data=row.data,
                    )
                    for row in rows
                )
            except ValueError as exc:
                raise AuditIntegrityError("Tier 1: persisted run event identifiers are invalid") from exc
