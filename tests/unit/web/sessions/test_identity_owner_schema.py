"""D6 owners and D27 reserved writer vocabulary are database contracts."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import CheckConstraint, delete, insert
from sqlalchemy.exc import IntegrityError

from elspeth.web.coordination.audit_access_log_authority import RepositoryAuditAccessLogAuthority
from elspeth.web.sessions import models
from elspeth.web.sessions.protocol import AuditAccessLogWriteError
from tests.unit.web.conftest import _make_session


@pytest.mark.parametrize("table", [models.sessions_table, models.user_secrets_table, models.user_preferences_table])
def test_owner_column_restricts_identity_deletion(table) -> None:
    assert [(fk.target_fullname, fk.ondelete) for fk in table.c.user_id.foreign_keys] == [("identities.identity_id", "RESTRICT")]


def test_workflow_inspect_is_reserved_in_closed_principal_check() -> None:
    check = next(c for c in models.audit_access_log_table.constraints if isinstance(c, CheckConstraint))
    assert str(check.sqltext) == "writer_principal IN ('audit_grade_view', 'admin_tool', 'workflow_inspect')"


@pytest.mark.parametrize("provider", ["local", "oidc", "entra", "vanguard", "google"])
def test_audit_grade_owner_read_accepts_every_login_provider(engine, provider: str) -> None:
    with engine.begin() as conn:
        _make_session(conn, session_id="owned", user_id="owner", auth_provider_type=provider)
    authority = RepositoryAuditAccessLogAuthority(engine)
    record = authority.record_audit_grade_view(
        session_id="owned",
        requesting_principal="owner",
        auth_provider_type=provider,
        request_path="/api/sessions/owned/messages",
        query_args={},
        ip_address=None,
    )
    assert record.writer_principal == "audit_grade_view"
    with pytest.raises(AuditAccessLogWriteError, match="live and owned"):
        authority.record_audit_grade_view(
            session_id="owned",
            requesting_principal="another-identity",
            auth_provider_type=provider,
            request_path="/api/sessions/owned/messages",
            query_args={},
            ip_address=None,
        )


def test_audit_grade_read_rejects_unknown_provider_before_sql(engine) -> None:
    with pytest.raises(ValueError, match="supported exact provider"):
        RepositoryAuditAccessLogAuthority(engine).record_audit_grade_view(
            session_id="absent",
            requesting_principal="owner",
            auth_provider_type="unknown",
            request_path="/api/sessions/absent/messages",
            query_args={},
            ip_address=None,
        )


@pytest.mark.parametrize("table_name", ["sessions", "user_secrets", "user_preferences"])
def test_orphan_owner_insert_is_refused(engine, table_name: str) -> None:
    now = datetime.now(UTC)
    table = models.metadata.tables[table_name]
    values = {"user_id": "missing", "updated_at": now}
    if table_name == "sessions":
        values.update(id="s", auth_provider_type="local", title="test", created_at=now)
    elif table_name == "user_secrets":
        values.update(id="secret", name="test", auth_provider_type="local", encrypted_value=b"ciphertext", salt=b"salt", created_at=now)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(insert(table).values(**values))


def test_identity_with_owned_session_cannot_be_deleted(engine) -> None:
    with engine.begin() as conn:
        now = datetime.now(UTC)
        conn.execute(
            insert(models.identities_table).values(
                identity_id="owner", provider="local", subject="owner", username="owner", first_seen_at=now
            )
        )
        _make_session(conn, session_id="owned", user_id="owner")
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(delete(models.identities_table).where(models.identities_table.c.identity_id == "owner"))
