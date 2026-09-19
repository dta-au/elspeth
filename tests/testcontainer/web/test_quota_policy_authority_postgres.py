"""Quota policy replacement and suspension on the production SQL dialect."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import Engine, insert, select
from sqlalchemy.engine import make_url
from tests.fixtures.identities import ensure_test_identity

from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.identity_authority import IdentityAdminActor
from elspeth.web.coordination.quota_policy_authority import RepositoryQuotaPolicyAuthority
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import blobs_table, identity_roles_table, quota_policies_table, sessions_table
from elspeth.web.sessions.schema import initialize_session_schema

pytestmark = pytest.mark.testcontainer


@pytest.fixture
def pg_engine(external_deployment_postgres_url: str) -> Iterator[Engine]:
    database = f"quota_admin_{uuid.uuid4().hex}"
    control = create_session_engine(external_deployment_postgres_url, isolation_level="AUTOCOMMIT")
    with control.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
    engine = create_session_engine(make_url(external_deployment_postgres_url).set(database=database).render_as_string(hide_password=False))
    try:
        initialize_session_schema(engine)
        with engine.begin() as conn:
            now = database_now(conn)
            ensure_test_identity(conn, identity_id="root")
            ensure_test_identity(conn, identity_id="alice")
            conn.execute(
                insert(identity_roles_table).values(
                    role_id="root-admin",
                    identity_id="root",
                    role="admin",
                    granted_at=now - timedelta(days=1),
                    granted_by_identity_id="root",
                )
            )
        yield engine
    finally:
        engine.dispose()
        with control.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
        control.dispose()


def test_policy_replace_then_revoke_keeps_single_live_row_and_audits(pg_engine: Engine) -> None:
    authority = RepositoryQuotaPolicyAuthority(pg_engine)
    actor = IdentityAdminActor(identity_id="root", on_behalf_of=None, console_request_id=None)
    changes = []
    with pg_engine.begin() as conn:
        now = database_now(conn)
        conn.execute(
            insert(sessions_table).values(
                id="alice-session",
                user_id="alice",
                auth_provider_type="local",
                title="held bytes",
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            insert(blobs_table).values(
                id="alice-blob",
                session_id="alice-session",
                filename="held.csv",
                mime_type="text/csv",
                size_bytes=125,
                content_hash="a" * 64,
                storage_path="blobs/alice-blob",
                created_at=now,
                created_by="user",
                status="ready",
            )
        )
    first = authority.set_identity_policy(
        actor=actor,
        identity_id="alice",
        dimension="tokens",
        value=2**40,
        default_tokens_per_day=None,
        default_storage_bytes=2000,
        record=changes.append,
    )
    second = authority.set_identity_policy(
        actor=actor,
        identity_id="alice",
        dimension="storage",
        value=3000,
        default_tokens_per_day=None,
        default_storage_bytes=None,
        record=changes.append,
    )
    assert first.policy is not None and second.policy is not None
    assert first.storage_bytes_used == second.storage_bytes_used == 125
    assert second.previous == first.policy
    assert (second.policy.tokens_per_day, second.policy.storage_bytes) == (2**40, 3000)
    with pg_engine.begin() as conn:
        rows = conn.execute(select(quota_policies_table).where(quota_policies_table.c.identity_id == "alice")).all()
    assert len(rows) == 2
    assert [(row.policy_id, row.revoked_at is None) for row in rows if row.revoked_at is None] == [(second.policy.policy_id, True)]
    assert changes == [first, second]

    revoked = authority.revoke_identity_policy(actor=actor, identity_id="alice", record=changes.append)
    assert revoked.previous == second.policy and revoked.policy is None
    assert authority.status(identity_id="alice").identity_policy is None
    with pg_engine.begin() as conn:
        rows = conn.execute(select(quota_policies_table).where(quota_policies_table.c.identity_id == "alice")).all()
    assert len(rows) == 2 and all(row.revoked_at is not None for row in rows)
    assert changes == [first, second, revoked]
