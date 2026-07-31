"""Handle-free repository authority for audit-grade transcript views."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import final
from uuid import uuid4

from sqlalchemy import Connection, Engine, insert, select

from elspeth.contracts.auth import AuthProviderType
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import audit_access_log_table, sessions_table
from elspeth.web.sessions.protocol import (
    AUDIT_GRADE_VIEW_QUERY_ARG_ALLOWLIST,
    AUDIT_GRADE_VIEW_WRITER_PRINCIPAL,
    AuditAccessLogRecord,
    AuditAccessLogWriteError,
)


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
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


@final
class RepositoryAuditAccessLogAuthority:
    """Append one audit row only for the exact live session subject."""

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name not in {"postgresql", "sqlite"}:
            raise NotImplementedError(f"audit access log authority not implemented for {engine.dialect.name}")
        self._engine = engine

    def record_audit_grade_view(
        self,
        *,
        session_id: str,
        requesting_principal: str,
        auth_provider_type: AuthProviderType,
        request_path: str,
        query_args: Mapping[str, str],
        ip_address: str | None,
    ) -> AuditAccessLogRecord:
        """Commit one access row after re-proving the live session subject."""
        if type(session_id) is not str or not session_id:
            raise TypeError("session_id must be a non-empty exact string")
        if type(requesting_principal) is not str or not requesting_principal:
            raise TypeError("requesting_principal must be a non-empty exact string")
        if type(auth_provider_type) is not str or auth_provider_type not in {"local", "oidc", "entra"}:
            raise ValueError("auth_provider_type must be a supported exact provider")
        if type(request_path) is not str or not request_path:
            raise TypeError("request_path must be a non-empty exact string")
        if type(ip_address) not in {str, type(None)}:
            raise TypeError("ip_address must be an exact string or None")
        allowed_query_args = dict(query_args)
        unexpected = frozenset(allowed_query_args) - AUDIT_GRADE_VIEW_QUERY_ARG_ALLOWLIST
        if unexpected:
            raise ValueError(f"unallowlisted audit-grade query args: {sorted(unexpected)}")
        if any(type(key) is not str or type(value) is not str for key, value in allowed_query_args.items()):
            raise TypeError("audit-grade query args must contain exact strings")

        with locked_session_transaction(self._engine, session_id) as conn:
            subject = conn.execute(
                select(sessions_table.c.id)
                .where(
                    sessions_table.c.id == session_id,
                    sessions_table.c.user_id == requesting_principal,
                    sessions_table.c.auth_provider_type == auth_provider_type,
                    sessions_table.c.archived_at.is_(None),
                )
                .with_for_update()
            ).one_or_none()
            if subject is None:
                raise AuditAccessLogWriteError("audit-grade transcript access subject is not live and owned")
            record = AuditAccessLogRecord(
                id=str(uuid4()),
                timestamp=_database_now(conn),
                session_id=session_id,
                requesting_principal=requesting_principal,
                request_path=request_path,
                query_args=allowed_query_args,
                ip_address=ip_address,
                writer_principal=AUDIT_GRADE_VIEW_WRITER_PRINCIPAL,
            )
            conn.execute(
                insert(audit_access_log_table).values(
                    id=record.id,
                    timestamp=record.timestamp,
                    session_id=record.session_id,
                    requesting_principal=record.requesting_principal,
                    request_path=record.request_path,
                    query_args=dict(record.query_args),
                    ip_address=record.ip_address,
                    writer_principal=record.writer_principal,
                )
            )
        return record
