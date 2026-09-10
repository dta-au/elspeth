"""Authenticated durable cancellation intent independent of the worker lease."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import Engine, func, insert, select, update

from elspeth.contracts.auth import AuthProviderType
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.repository import SessionDerivedCustodyError
from elspeth.web.coordination.run_recovery_authority import _database_now, _run_record_from_row
from elspeth.web.execution.schemas import CancelledData
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import run_events_table, run_start_permits_table, runs_table, sessions_table
from elspeth.web.sessions.protocol import RunRecord


class RepositoryRunCancellationAuthority:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def request(self, run_id: UUID, *, session_id: UUID, user_id: str, auth_provider_type: AuthProviderType) -> RunRecord:
        with locked_session_transaction(self._engine, str(session_id)) as conn:
            session = conn.execute(select(sessions_table).where(sessions_table.c.id == str(session_id)).with_for_update()).one_or_none()
            if (
                session is None
                or session.user_id != user_id
                or session.auth_provider_type != auth_provider_type
                or session.archived_at is not None
            ):
                raise SessionDerivedCustodyError
            run = conn.execute(select(runs_table).where(runs_table.c.id == str(run_id)).with_for_update()).one_or_none()
            if run is None or run.session_id != str(session_id):
                raise SessionDerivedCustodyError
            if run.status not in {"pending", "running"} or run.cancel_requested_at is not None:
                return _run_record_from_row(run)
            now = _database_now(conn)
            permit = conn.execute(
                select(run_start_permits_table).where(run_start_permits_table.c.run_id == str(run_id)).with_for_update()
            ).one_or_none()
            before_permit = permit is not None and permit.start_state == "pending"
            if before_permit and (
                run.status != "pending"
                or run.landscape_run_id is not None
                or any(
                    (
                        run.rows_processed,
                        run.rows_succeeded,
                        run.rows_failed,
                        run.rows_quarantined,
                        run.rows_routed_success,
                        run.rows_routed_failure,
                    )
                )
            ):
                raise AuditIntegrityError("Run without a start permit already reports execution")
            conn.execute(
                update(runs_table)
                .where(runs_table.c.id == str(run_id))
                .values(
                    cancel_requested_at=now,
                    cancellation_source="user",
                    saga_state="terminal_cancelled" if before_permit else "cancel_pending",
                    status="cancelled" if before_permit else run.status,
                    finished_at=now if before_permit else run.finished_at,
                )
            )
            if before_permit:
                conn.execute(
                    update(run_start_permits_table)
                    .where(run_start_permits_table.c.run_id == str(run_id))
                    .values(
                        start_state="cancelled_before_permit",
                        cancelled_at=now,
                    )
                )
                sequence = (
                    conn.execute(
                        select(func.coalesce(func.max(run_events_table.c.sequence), 0)).where(
                            run_events_table.c.run_id == str(run_id),
                        )
                    ).scalar_one()
                    + 1
                )
                conn.execute(
                    insert(run_events_table).values(
                        id=str(uuid4()),
                        run_id=str(run_id),
                        sequence=sequence,
                        timestamp=now,
                        event_type="cancelled",
                        data=CancelledData(
                            source_rows_processed=0,
                            tokens_succeeded=0,
                            tokens_failed=0,
                            tokens_quarantined=0,
                            tokens_routed_success=0,
                            tokens_routed_failure=0,
                        ).model_dump(mode="json"),
                    )
                )
            return _run_record_from_row(conn.execute(select(runs_table).where(runs_table.c.id == str(run_id))).one())
