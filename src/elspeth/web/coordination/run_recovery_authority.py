"""Durable, handle-free authority for cross-session run recovery writes."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from typing import Any, cast, final
from uuid import UUID

from sqlalchemy import ColumnElement, Connection, Engine, select, update

from elspeth.contracts.advisory_locks import ELSPETH_SESSIONS_LOCK_CLASSID
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.locking import locked_session_transaction, process_session_lock
from elspeth.web.sessions.models import (
    runs_table,
    session_operation_fences_table,
    sessions_table,
    web_instances_table,
)
from elspeth.web.sessions.protocol import (
    LANDSCAPE_RECONCILIATION_ABSENT_SUFFIX,
    LANDSCAPE_RECONCILIATION_COMPLETE_SUFFIX,
    LANDSCAPE_RECONCILIATION_PENDING_SUFFIX,
    RunRecord,
    SessionRunStatus,
)


def _ensure_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _database_now(conn: Connection) -> datetime:
    dialect = conn.dialect.name
    if dialect == "postgresql":
        value = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
    elif dialect == "sqlite":
        value = conn.exec_driver_sql("SELECT CURRENT_TIMESTAMP").scalar_one()
    else:
        raise NotImplementedError(f"sessions database time not implemented for {dialect}")
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise RuntimeError("sessions database clock returned a non-datetime value")
    return _ensure_utc(value)


def _run_record_from_row(row: Any) -> RunRecord:
    """Construct the immutable run snapshot returned by recovery authority."""
    return RunRecord(
        id=UUID(row.id),
        session_id=UUID(row.session_id),
        state_id=UUID(row.state_id),
        status=cast("SessionRunStatus", row.status),
        started_at=_ensure_utc(row.started_at),
        finished_at=_ensure_utc(row.finished_at) if row.finished_at is not None else None,
        rows_processed=row.rows_processed,
        rows_succeeded=row.rows_succeeded,
        rows_failed=row.rows_failed,
        rows_routed_success=row.rows_routed_success,
        rows_routed_failure=row.rows_routed_failure,
        rows_quarantined=row.rows_quarantined,
        error=row.error,
        landscape_run_id=row.landscape_run_id,
        pipeline_yaml=row.pipeline_yaml,
    )


@final
class RepositoryGlobalRunRecoveryAuthority:
    """Cross-session recovery writer with durable per-session fencing.

    Candidate discovery is intentionally optimistic. Every cancellation is
    re-evaluated under the same per-session lock used by ordinary run writes,
    against database time, the current session fence, and PostgreSQL instance
    membership. Reconciliation markers use one globally ordered
    multi-session transaction so the public batch remains atomic.
    """

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name not in {"postgresql", "sqlite"}:
            raise NotImplementedError(f"global run recovery authority not implemented for {engine.dialect.name}")
        self._engine = engine

    @staticmethod
    def _validate_cancel_request(
        *,
        max_age_seconds: int | None,
        exclude_run_ids: frozenset[str],
        reason: str | None,
    ) -> None:
        if max_age_seconds is not None and (type(max_age_seconds) is not int or max_age_seconds < 0):
            raise ValueError("max_age_seconds must be a non-negative exact integer or None")
        if type(exclude_run_ids) is not frozenset or any(type(run_id) is not str for run_id in exclude_run_ids):
            raise TypeError("exclude_run_ids must be a frozenset of exact strings")
        if type(reason) not in {str, type(None)}:
            raise TypeError("reason must be an exact string or None")

    def _session_allows_recovery(self, conn: Connection, *, session_id: str, database_now: datetime) -> bool:
        session = conn.execute(select(sessions_table.c.id).where(sessions_table.c.id == session_id).with_for_update()).one_or_none()
        if session is None:
            return False
        fence = conn.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == session_id).with_for_update()
        ).one_or_none()
        if fence is None:
            raise AuditIntegrityError("Active run has no durable session-operation fence")
        if fence.released_at is not None:
            return True
        if type(fence.lease_expires_at) is not datetime:
            raise AuditIntegrityError("Session-operation fence has an invalid lease expiry")
        if _ensure_utc(fence.lease_expires_at) > database_now:
            return False
        if self._engine.dialect.name == "sqlite":
            return True

        owner = conn.execute(
            select(web_instances_table.c.lease_expires_at)
            .where(web_instances_table.c.instance_id == fence.owner_instance_id)
            .with_for_update()
        ).one_or_none()
        if owner is None:
            return False
        if type(owner.lease_expires_at) is not datetime:
            raise AuditIntegrityError("Web-instance membership has an invalid lease expiry")
        return _ensure_utc(owner.lease_expires_at) <= database_now

    def _cancel_candidate(
        self,
        *,
        run_id: str,
        session_id: str,
        max_age_seconds: int | None,
        exclude_run_ids: frozenset[str],
        reason: str | None,
    ) -> RunRecord | None:
        with locked_session_transaction(self._engine, session_id) as conn:
            database_now = _database_now(conn)
            if not self._session_allows_recovery(conn, session_id=session_id, database_now=database_now):
                return None
            row = conn.execute(select(runs_table).where(runs_table.c.id == run_id).with_for_update()).one_or_none()
            if row is None or row.session_id != session_id or row.status not in {"pending", "running"}:
                return None
            if run_id in exclude_run_ids:
                return None
            cutoff: datetime | None = None
            if max_age_seconds is not None and max_age_seconds > 0:
                if type(row.started_at) is not datetime:
                    raise AuditIntegrityError("Active run has an invalid start timestamp")
                cutoff = database_now - timedelta(seconds=max_age_seconds)
                if _ensure_utc(row.started_at) > cutoff:
                    return None

            values: dict[str, Any] = {
                "status": "cancelled",
                "finished_at": database_now,
            }
            if reason is not None:
                values["error"] = reason
            predicates: list[ColumnElement[bool]] = [
                runs_table.c.id == run_id,
                runs_table.c.session_id == session_id,
                runs_table.c.status.in_(("pending", "running")),
            ]
            if cutoff is not None:
                predicates.append(runs_table.c.started_at <= cutoff)
            updated = conn.execute(update(runs_table).where(*predicates).values(**values).returning(runs_table)).one_or_none()
            return _run_record_from_row(updated) if updated is not None else None

    def cancel_orphaned_run_records(
        self,
        *,
        max_age_seconds: int | None,
        exclude_run_ids: frozenset[str],
        reason: str | None,
    ) -> tuple[RunRecord, ...]:
        self._validate_cancel_request(
            max_age_seconds=max_age_seconds,
            exclude_run_ids=exclude_run_ids,
            reason=reason,
        )
        with self._engine.connect() as conn:
            candidates = conn.execute(
                select(runs_table.c.id, runs_table.c.session_id)
                .where(runs_table.c.status.in_(("pending", "running")))
                .order_by(runs_table.c.session_id, runs_table.c.started_at, runs_table.c.id)
            ).all()

        cancelled: list[RunRecord] = []
        for candidate in candidates:
            record = self._cancel_candidate(
                run_id=candidate.id,
                session_id=candidate.session_id,
                max_age_seconds=max_age_seconds,
                exclude_run_ids=exclude_run_ids,
                reason=reason,
            )
            if record is not None:
                cancelled.append(record)
        return tuple(cancelled)

    def mark_landscape_reconciliation_outcomes(
        self,
        *,
        complete_run_ids: frozenset[UUID],
        absent_run_ids: frozenset[UUID],
    ) -> None:
        overlap = complete_run_ids & absent_run_ids
        if overlap:
            raise ValueError("Landscape reconciliation outcome sets overlap")

        outcomes = (
            (complete_run_ids, LANDSCAPE_RECONCILIATION_COMPLETE_SUFFIX),
            (absent_run_ids, LANDSCAPE_RECONCILIATION_ABSENT_SUFFIX),
        )
        requested_ids = frozenset(str(run_id) for run_ids, _suffix in outcomes for run_id in run_ids)
        if not requested_ids:
            return
        with self._engine.connect() as conn:
            discovered = conn.execute(select(runs_table.c.id, runs_table.c.session_id).where(runs_table.c.id.in_(requested_ids))).all()
        session_ids = tuple(sorted({row.session_id for row in discovered}))

        with ExitStack() as process_stack:
            for session_id in session_ids:
                process_stack.enter_context(process_session_lock(self._engine, session_id))
            with self._engine.begin() as conn:
                if self._engine.dialect.name == "postgresql":
                    for session_id in session_ids:
                        conn.exec_driver_sql(
                            "SELECT pg_catalog.pg_advisory_xact_lock(%s, pg_catalog.hashtext(%s))",
                            (ELSPETH_SESSIONS_LOCK_CLASSID, session_id),
                        )
                pending_updates: list[tuple[str, str, str]] = []
                for run_ids, closed_suffix in outcomes:
                    for run_id in sorted(run_ids, key=str):
                        run_id_text = str(run_id)
                        row = conn.execute(
                            select(runs_table.c.status, runs_table.c.error).where(runs_table.c.id == run_id_text).with_for_update()
                        ).one_or_none()
                        if row is None or row.status != "cancelled" or type(row.error) is not str:
                            raise ValueError("Run is not an exact pending Landscape reconciliation candidate")
                        if row.error.endswith(closed_suffix):
                            continue
                        if not row.error.endswith(LANDSCAPE_RECONCILIATION_PENDING_SUFFIX):
                            raise ValueError("Run is not an exact pending Landscape reconciliation candidate")
                        updated_error = row.error[: -len(LANDSCAPE_RECONCILIATION_PENDING_SUFFIX)] + closed_suffix
                        pending_updates.append((run_id_text, row.error, updated_error))
                for run_id_text, prior_error, updated_error in pending_updates:
                    result = conn.execute(
                        update(runs_table)
                        .where(
                            runs_table.c.id == run_id_text,
                            runs_table.c.status == "cancelled",
                            runs_table.c.error == prior_error,
                        )
                        .values(error=updated_error)
                    )
                    if result.rowcount != 1:
                        raise RuntimeError("Landscape reconciliation marker update lost its compare-and-swap")
