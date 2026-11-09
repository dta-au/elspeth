"""Handle-free repository authority for audit-grade transcript views."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import final, get_args
from uuid import uuid4

from sqlalchemy import Connection, Engine, exists, insert, or_, select
from sqlalchemy.exc import SQLAlchemyError

from elspeth.contracts.auth import AuthProviderType
from elspeth.web.coordination.approval_authority import active_non_author_approver
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import (
    approvals_table,
    audit_access_log_table,
    composition_states_table,
    identities_table,
    identity_roles_table,
    review_attestations_table,
    review_requests_table,
    sessions_table,
)
from elspeth.web.sessions.protocol import (
    AUDIT_GRADE_VIEW_QUERY_ARG_ALLOWLIST,
    AUDIT_GRADE_VIEW_WRITER_PRINCIPAL,
    WORKFLOW_INSPECT_REQUEST_PATH_TEMPLATE,
    WORKFLOW_INSPECT_WRITER_PRINCIPAL,
    AuditAccessLogRecord,
    AuditAccessLogWriteError,
    WorkflowInspectDenied,
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


_ATTESTED_SINCE_REQUEST = exists().where(
    review_attestations_table.c.session_id == review_requests_table.c.session_id,
    review_attestations_table.c.state_id == review_requests_table.c.state_id,
    review_attestations_table.c.attested_at >= review_requests_table.c.requested_at,
)


@contextmanager
def _locked_workflow_inspect_transaction(engine: Engine, session_id: str) -> Iterator[Connection]:
    """Expose a typed failure when the audit write or commit cannot persist."""
    try:
        with locked_session_transaction(engine, session_id) as conn:
            yield conn
    except SQLAlchemyError as exc:
        raise AuditAccessLogWriteError("workflow inspection audit access could not be recorded") from exc


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
        auth_provider_candidate: object = auth_provider_type
        if type(auth_provider_candidate) is not str or auth_provider_candidate not in get_args(AuthProviderType):
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

    def record_workflow_inspect(
        self,
        *,
        session_id: str,
        state_id: str,
        requesting_principal: str,
        ip_address: str | None,
    ) -> AuditAccessLogRecord:
        """Log a cross-identity read only while its exact request is open.

        Session locking serializes request closure and state changes with the
        read. The caller and role rows are locked before database time is read,
        so a concurrent revocation cannot finish before this audit append.
        """
        for name, value in (("session_id", session_id), ("state_id", state_id), ("requesting_principal", requesting_principal)):
            if type(value) is not str or not value:
                raise TypeError(f"{name} must be a non-empty exact string")
        if type(ip_address) not in {str, type(None)}:
            raise TypeError("ip_address must be an exact string or None")

        with _locked_workflow_inspect_transaction(self._engine, session_id) as conn:
            session = conn.execute(select(sessions_table).where(sessions_table.c.id == session_id).with_for_update()).one_or_none()
            if session is None or session.archived_at is not None:
                raise WorkflowInspectDenied("session is not live")
            state = conn.execute(
                select(composition_states_table.c.id).where(
                    composition_states_table.c.id == state_id,
                    composition_states_table.c.session_id == session_id,
                )
            ).one_or_none()
            if state is None:
                raise WorkflowInspectDenied("state is not a composition state of the session")

            # Lock the same active human admin population as identity mutations,
            # before locking this caller's identity or role grants.
            conn.execute(
                select(identity_roles_table.c.identity_id, identity_roles_table.c.expires_at, identity_roles_table.c.revoked_at)
                .select_from(
                    identity_roles_table.join(identities_table, identity_roles_table.c.identity_id == identities_table.c.identity_id)
                )
                .where(
                    identity_roles_table.c.role == "admin",
                    identity_roles_table.c.scope.is_(None),
                    identities_table.c.kind == "human",
                    identities_table.c.access_state == "active",
                )
                .with_for_update()
            ).all()
            identity = conn.execute(
                select(identities_table).where(identities_table.c.identity_id == requesting_principal).with_for_update()
            ).one_or_none()
            if identity is None or identity.access_state != "active" or identity.kind != "human" or identity.provider == "service":
                raise WorkflowInspectDenied("caller is not an active identity")
            grants = conn.execute(
                select(identity_roles_table)
                .where(
                    identity_roles_table.c.identity_id == requesting_principal,
                    identity_roles_table.c.role.in_(("approver", "reviewer")),
                    identity_roles_table.c.scope.is_(None),
                )
                .order_by(identity_roles_table.c.role_id)
                .with_for_update()
            ).all()
            now = _database_now(conn)
            approver_grants = tuple(row for row in grants if row.role == "approver")
            approved_to_inspect = False
            if approver_grants:
                approval = conn.execute(
                    select(approvals_table.c.requested_by_identity_id).where(
                        approvals_table.c.session_id == session_id,
                        approvals_table.c.state_id == state_id,
                        approvals_table.c.decision.is_(None),
                    )
                ).one_or_none()
                if approval is not None:
                    approved_to_inspect = active_non_author_approver(
                        actor_identity_id=requesting_principal,
                        author_identity_id=approval.requested_by_identity_id,
                        access_state=identity.access_state,
                        identity_kind=identity.kind,
                        provider=identity.provider,
                        role_rows=approver_grants,
                        now=now,
                    )
            reviewer_grants = tuple(row for row in grants if row.role == "reviewer")
            reviewed_to_inspect = False
            if reviewer_grants and any(
                row.revoked_at is None and (row.expires_at is None or _database_aware(row.expires_at) > now) for row in reviewer_grants
            ):
                reviewed_to_inspect = (
                    conn.execute(
                        select(review_requests_table.c.request_id).where(
                            review_requests_table.c.session_id == session_id,
                            review_requests_table.c.state_id == state_id,
                            review_requests_table.c.cancelled_at.is_(None),
                            ~_ATTESTED_SINCE_REQUEST,
                            or_(
                                review_requests_table.c.reviewer_identity_id == requesting_principal,
                                review_requests_table.c.reviewer_identity_id.is_(None),
                            ),
                            review_requests_table.c.requested_by_identity_id != requesting_principal,
                        )
                    ).first()
                    is not None
                )
            if not approved_to_inspect and not reviewed_to_inspect:
                raise WorkflowInspectDenied("no live approval or review request authorises this read")

            record = AuditAccessLogRecord(
                id=str(uuid4()),
                timestamp=now,
                session_id=session_id,
                requesting_principal=requesting_principal,
                request_path=WORKFLOW_INSPECT_REQUEST_PATH_TEMPLATE.format(session_id=session_id, state_id=state_id),
                query_args={},
                ip_address=ip_address,
                writer_principal=WORKFLOW_INSPECT_WRITER_PRINCIPAL,
            )
            conn.execute(
                insert(audit_access_log_table).values(
                    id=record.id,
                    timestamp=record.timestamp,
                    session_id=record.session_id,
                    requesting_principal=record.requesting_principal,
                    request_path=record.request_path,
                    query_args={},
                    ip_address=record.ip_address,
                    writer_principal=record.writer_principal,
                )
            )
        return record


def _database_aware(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
