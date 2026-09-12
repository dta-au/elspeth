"""PostgreSQL FK and principal enforcement for the paired identity residual."""

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import Engine, delete, insert
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from tests.fixtures.identities import ensure_test_identity

from elspeth.web.coordination.audit_access_log_authority import RepositoryAuditAccessLogAuthority
from elspeth.web.sessions import models
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import initialize_session_schema

pytestmark = pytest.mark.testcontainer


@pytest.fixture
def owner_engine(external_deployment_postgres_url: str) -> Iterator[Engine]:
    database = f"identity_owner_{uuid4().hex}"
    control = create_session_engine(external_deployment_postgres_url, isolation_level="AUTOCOMMIT")
    with control.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
    engine = create_session_engine(make_url(external_deployment_postgres_url).set(database=database).render_as_string(hide_password=False))
    try:
        initialize_session_schema(engine)
        yield engine
    finally:
        engine.dispose()
        with control.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
        control.dispose()


@pytest.mark.parametrize("table_name", ["sessions", "user_secrets", "user_preferences"])
def test_owner_fk_refuses_orphans_and_preserves_owned_data(owner_engine: Engine, table_name: str) -> None:
    table = models.metadata.tables[table_name]
    now = datetime.now(UTC)
    values = {"user_id": "owner", "updated_at": now}
    if table_name == "sessions":
        values.update(id="session", auth_provider_type="local", title="test", created_at=now)
    elif table_name == "user_secrets":
        values.update(id="secret", name="test", auth_provider_type="local", encrypted_value=b"ciphertext", salt=b"salt", created_at=now)
    with pytest.raises(IntegrityError), owner_engine.begin() as conn:
        conn.execute(insert(table).values(**values))
    with owner_engine.begin() as conn:
        ensure_test_identity(conn, identity_id="owner")
        conn.execute(insert(table).values(**values))
    with pytest.raises(IntegrityError), owner_engine.begin() as conn:
        conn.execute(delete(models.identities_table).where(models.identities_table.c.identity_id == "owner"))


@pytest.mark.parametrize("provider", ["local", "oidc", "entra", "vanguard", "google"])
def test_owned_audit_grade_view_accepts_every_login_provider(owner_engine: Engine, provider: str) -> None:
    now = datetime.now(UTC)
    with owner_engine.begin() as conn:
        ensure_test_identity(conn, identity_id="owner", provider=provider)
        conn.execute(
            insert(models.sessions_table).values(
                id="session", user_id="owner", auth_provider_type=provider, title="test", created_at=now, updated_at=now
            )
        )
    record = RepositoryAuditAccessLogAuthority(owner_engine).record_audit_grade_view(
        session_id="session",
        requesting_principal="owner",
        auth_provider_type=provider,
        request_path="/api/sessions/session/messages",
        query_args={},
        ip_address=None,
    )
    assert record.writer_principal == "audit_grade_view"


def test_reserved_workflow_principal_is_closed(owner_engine: Engine) -> None:
    now = datetime.now(UTC)
    with owner_engine.begin() as conn:
        ensure_test_identity(conn, identity_id="owner")
        conn.execute(
            insert(models.sessions_table).values(
                id="session", user_id="owner", auth_provider_type="local", title="test", created_at=now, updated_at=now
            )
        )
        for principal in ("audit_grade_view", "admin_tool", "workflow_inspect"):
            conn.execute(
                insert(models.audit_access_log_table).values(
                    id=principal,
                    timestamp=now,
                    session_id="session",
                    requesting_principal="owner",
                    request_path="/test",
                    query_args={},
                    writer_principal=principal,
                )
            )
    with pytest.raises(IntegrityError, match="ck_audit_access_log_writer_principal"), owner_engine.begin() as conn:
        conn.execute(
            insert(models.audit_access_log_table).values(
                id="unknown",
                timestamp=now,
                session_id="session",
                requesting_principal="owner",
                request_path="/test",
                query_args={},
                writer_principal="unknown",
            )
        )
