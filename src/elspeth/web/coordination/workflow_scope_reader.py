"""Read the approver's bounded audit scope from the Sessions store.

Oversight relationships grant audit visibility only. Approval eligibility
comes from role grants, independently of this tree. A visited set bounds
pre-existing cycles, and the depth limit reports when the view is truncated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, final

from sqlalchemy import bindparam, select
from sqlalchemy.engine import Engine

from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.models import (
    approvals_table,
    identities_table,
    identity_relationships_table,
    identity_roles_table,
    review_attestations_table,
)

MAX_APPROVER_SCOPE_DEPTH: Final = 8
MAX_AUDIT_VIEW_ROWS: Final = 200

_CALLER: Final = select(identities_table.c.access_state, identities_table.c.kind, identities_table.c.provider).where(
    identities_table.c.identity_id == bindparam("caller")
)
_CALLER_APPROVER_GRANTS: Final = select(identity_roles_table.c.expires_at).where(
    identity_roles_table.c.identity_id == bindparam("caller"),
    identity_roles_table.c.role == "approver",
    identity_roles_table.c.scope.is_(None),
    identity_roles_table.c.revoked_at.is_(None),
)
_ACTIVE_OUTGOING_EDGES: Final = (
    select(identity_relationships_table.c.to_identity_id)
    .where(
        identity_relationships_table.c.from_identity_id == bindparam("from_identity_id"),
        identity_relationships_table.c.relationship_type == "approver",
        identity_relationships_table.c.revoked_at.is_(None),
    )
    .order_by(identity_relationships_table.c.to_identity_id)
)
_APPROVALS_REQUESTED_BY_SCOPE: Final = (
    select(approvals_table)
    .where(approvals_table.c.requested_by_identity_id.in_(bindparam("identity_ids", expanding=True)))
    .order_by(approvals_table.c.requested_at.desc(), approvals_table.c.approval_id)
    .limit(MAX_AUDIT_VIEW_ROWS)
)
_ATTESTATIONS_AUTHORED_BY_SCOPE: Final = (
    select(review_attestations_table)
    .where(review_attestations_table.c.author_identity_id.in_(bindparam("identity_ids", expanding=True)))
    .order_by(review_attestations_table.c.attested_at.desc(), review_attestations_table.c.attestation_id)
    .limit(MAX_AUDIT_VIEW_ROWS)
)


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _utc_or_none(value: datetime | None) -> datetime | None:
    return None if value is None else _utc(value)


@final
@dataclass(frozen=True, slots=True)
class ScopedApproval:
    approval_id: str
    session_id: str
    state_id: str
    requested_by_identity_id: str
    approver_identity_id: str
    requested_at: datetime
    decided_at: datetime | None
    decision: str | None


@final
@dataclass(frozen=True, slots=True)
class ScopedAttestation:
    attestation_id: str
    session_id: str
    state_id: str
    payload_digest: str
    reviewer_identity_id: str
    author_identity_id: str
    attested_at: datetime
    verdict: str


@final
@dataclass(frozen=True, slots=True)
class WorkflowAuditScope:
    caller_is_approver: bool
    identity_ids: tuple[str, ...]
    truncated: bool
    approvals: tuple[ScopedApproval, ...]
    attestations: tuple[ScopedAttestation, ...]


_NO_SCOPE: Final = WorkflowAuditScope(caller_is_approver=False, identity_ids=(), truncated=False, approvals=(), attestations=())


@final
class RepositoryWorkflowScopeReader:
    """One Sessions read connection per call; no Landscape or write handle."""

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name not in {"postgresql", "sqlite"}:
            raise NotImplementedError(f"workflow scope reader not implemented for {engine.dialect.name}")
        self._engine = engine

    def read_audit_scope(self, *, caller: str) -> WorkflowAuditScope:
        if type(caller) is not str or not caller:
            raise TypeError("caller must be a non-empty exact string")
        with self._engine.connect() as conn:
            identity = conn.execute(_CALLER, {"caller": caller}).one_or_none()
            if identity is None or identity.access_state != "active" or identity.kind != "human" or identity.provider == "service":
                return _NO_SCOPE
            now = database_now(conn)
            grants = conn.execute(_CALLER_APPROVER_GRANTS, {"caller": caller}).all()
            if not any(row.expires_at is None or _utc(row.expires_at) > now for row in grants):
                return _NO_SCOPE

            visited: set[str] = {caller}
            ordered: list[str] = []
            frontier: list[str] = [caller]
            for _hop in range(MAX_APPROVER_SCOPE_DEPTH):
                next_frontier: list[str] = []
                for parent in frontier:
                    for child in conn.execute(_ACTIVE_OUTGOING_EDGES, {"from_identity_id": parent}).scalars():
                        if child not in visited:
                            visited.add(child)
                            ordered.append(child)
                            next_frontier.append(child)
                frontier = next_frontier
                if not frontier:
                    break
            truncated = any(
                child not in visited
                for parent in frontier
                for child in conn.execute(_ACTIVE_OUTGOING_EDGES, {"from_identity_id": parent}).scalars()
            )
            if not ordered:
                return WorkflowAuditScope(caller_is_approver=True, identity_ids=(), truncated=False, approvals=(), attestations=())

            approval_rows = conn.execute(_APPROVALS_REQUESTED_BY_SCOPE, {"identity_ids": ordered}).all()
            attestation_rows = conn.execute(_ATTESTATIONS_AUTHORED_BY_SCOPE, {"identity_ids": ordered}).all()

        return WorkflowAuditScope(
            caller_is_approver=True,
            identity_ids=tuple(ordered),
            truncated=truncated,
            approvals=tuple(
                ScopedApproval(
                    approval_id=row.approval_id,
                    session_id=row.session_id,
                    state_id=row.state_id,
                    requested_by_identity_id=row.requested_by_identity_id,
                    approver_identity_id=row.approver_identity_id,
                    requested_at=_utc(row.requested_at),
                    decided_at=_utc_or_none(row.decided_at),
                    decision=row.decision,
                )
                for row in approval_rows
            ),
            attestations=tuple(
                ScopedAttestation(
                    attestation_id=row.attestation_id,
                    session_id=row.session_id,
                    state_id=row.state_id,
                    payload_digest=row.payload_digest,
                    reviewer_identity_id=row.reviewer_identity_id,
                    author_identity_id=row.author_identity_id,
                    attested_at=_utc(row.attested_at),
                    verdict=row.verdict,
                )
                for row in attestation_rows
            ),
        )
