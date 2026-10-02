"""Explicit identity setup for tests which create identity-owned rows."""

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI
from sqlalchemy import Connection, insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from elspeth.web.coordination.approval_lifecycle_authority import RepositoryApprovalLifecycleAuthority
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import identities_table, identity_roles_table
from elspeth.web.sessions.schema import initialize_session_schema


def ensure_test_identity(conn: Connection, *, identity_id: str, provider: str = "local") -> None:
    """Create a test owner if absent; preserve any existing identity's state.

    This is fixture setup, never production admission or automatic role grant.
    Existing rows may deliberately be pending, disabled or bound differently.
    """
    if conn.execute(select(identities_table.c.identity_id).where(identities_table.c.identity_id == identity_id)).first() is not None:
        return
    conn.execute(
        insert(identities_table).values(
            identity_id=identity_id,
            provider=provider,
            subject=identity_id,
            username=identity_id,
            first_seen_at=datetime.now(UTC),
            access_state="active",
            activated_at=datetime.now(UTC),
        )
    )


def grant_test_pipeline_user(conn: Connection, *, identity_id: str) -> None:
    """Grant the live unscoped ``user`` role expected by workload routes."""
    existing = conn.execute(
        select(identity_roles_table.c.role_id).where(
            identity_roles_table.c.identity_id == identity_id,
            identity_roles_table.c.role == "user",
            identity_roles_table.c.scope.is_(None),
            identity_roles_table.c.revoked_at.is_(None),
        )
    ).first()
    if existing is not None:
        return
    conn.execute(
        insert(identity_roles_table).values(
            role_id=str(uuid4()),
            identity_id=identity_id,
            role="user",
            scope=None,
            granted_by_identity_id=identity_id,
            granted_at=datetime.now(UTC),
        )
    )


def wire_test_pipeline_user_authority(
    app: FastAPI,
    *,
    identity_id: str,
    provider: str = "local",
    engine: Engine | None = None,
) -> Engine:
    """Attach a real identity authority seeded for one admitted pipeline user.

    Route tests sometimes use service doubles and therefore have no Sessions
    engine of their own. In that case this helper owns a small, live identity
    database solely for the same admission proof production performs.
    """
    authority_engine = engine
    if authority_engine is None:
        authority_engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(authority_engine)
    with authority_engine.begin() as conn:
        ensure_test_identity(conn, identity_id=identity_id, provider=provider)
        grant_test_pipeline_user(conn, identity_id=identity_id)
    app.state.identity_authority = RepositoryIdentityAuthority(
        authority_engine,
        lifecycle_effect=RepositoryApprovalLifecycleAuthority().apply,
    )
    app.state.pipeline_user_identity_engine = authority_engine
    return authority_engine
