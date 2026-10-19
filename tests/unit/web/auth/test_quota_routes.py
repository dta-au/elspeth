"""Quota status and administration through the real HTTP router."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, update

from elspeth.web.auth.quota_routes import create_quota_router
from elspeth.web.coordination import quota_policy_authority
from elspeth.web.coordination.approval_lifecycle_authority import RepositoryApprovalLifecycleAuthority
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.coordination.quota_policy_authority import QuotaPolicyChange, RepositoryQuotaPolicyAuthority
from elspeth.web.sessions.models import (
    blobs_table,
    identity_roles_table,
    quota_provider_attempts_table,
    sessions_table,
    token_usage_ledger_table,
)
from tests.fixtures.identities import ensure_test_identity


class _QuotaAudit:
    def __init__(self) -> None:
        self.changes: list[QuotaPolicyChange] = []
        self.fail = False

    def record_quota_set(self, _request: Any, *, provider: str, change: QuotaPolicyChange) -> None:
        assert provider == "local"
        if self.fail:
            raise RuntimeError("Landscape unavailable")
        self.changes.append(change)


def _wire(client: TestClient) -> _QuotaAudit:
    engine = client.app.state.phase3_engine
    with engine.begin() as conn:
        now = database_now(conn)
        ensure_test_identity(conn, identity_id="bob")
        conn.execute(
            insert(identity_roles_table).values(
                role_id="alice-admin",
                identity_id="alice",
                role="admin",
                granted_by_identity_id="alice",
                granted_at=now - timedelta(days=1),
            )
        )
    recorder = _QuotaAudit()
    client.app.state.identity_authority = RepositoryIdentityAuthority(engine, lifecycle_effect=RepositoryApprovalLifecycleAuthority().apply)
    client.app.state.quota_policy_authority = RepositoryQuotaPolicyAuthority(engine)
    client.app.state.auth_audit_recorder = recorder
    client.app.include_router(create_quota_router())
    return recorder


def test_admin_can_read_set_and_revoke_policy(closed_local_app: TestClient) -> None:
    client = closed_local_app
    recorder = _wire(client)
    own = client.get("/api/workflow/quota/me")
    assert own.status_code == 200 and own.json()["storage_bytes_used"] == "0"
    assert own.json()["policy_state"] == "never_issued"
    assert own.headers["Cache-Control"] == "no-store"
    first = client.post("/api/workflow/quota/identities/bob", json={"dimension": "storage", "value": 2**40})
    assert first.status_code == 200 and first.json()["storage_bytes"] == str(2**40)
    assert first.json()["tokens_per_day"] == "100000"
    assert len(recorder.changes) == 1 and recorder.changes[0].dimension == "storage"
    read = client.get("/api/workflow/quota/identities/bob")
    assert read.status_code == 200 and read.json()["storage_bytes"] == str(2**40)
    revoked = client.post("/api/workflow/quota/identities/bob/revoke", json={})
    assert revoked.status_code == 200 and revoked.json()["storage_bytes"] is None
    assert revoked.json()["policy_state"] == "suspended"
    assert revoked.json()["can_edit"] is True
    assert len(recorder.changes) == 2 and recorder.changes[1].action == "revoke"


def test_policy_write_reproves_admin_and_fails_closed_on_audit(closed_local_app: TestClient) -> None:
    client = closed_local_app
    recorder = _wire(client)
    recorder.fail = True
    try:
        client.post("/api/workflow/quota/identities/bob", json={"dimension": "tokens", "value": 600})
    except RuntimeError as exc:
        assert str(exc) == "Landscape unavailable"
    else:
        raise AssertionError("audit failure was not propagated")
    assert client.get("/api/workflow/quota/identities/bob").json()["tokens_per_day"] is None
    with client.app.state.phase3_engine.begin() as conn:
        conn.execute(
            update(identity_roles_table).where(identity_roles_table.c.role_id == "alice-admin").values(revoked_at=database_now(conn))
        )
    hidden = client.post("/api/workflow/quota/identities/bob", json={"dimension": "tokens", "value": 600})
    assert hidden.status_code == 404
    assert client.get("/api/workflow/quota/identities/bob").status_code == 404


def test_invalid_wire_quantity_refused_without_policy_write(closed_local_app: TestClient) -> None:
    client = closed_local_app
    recorder = _wire(client)
    invalid = client.post("/api/workflow/quota/identities/bob", json={"dimension": "tokens", "value": True})
    overflow = client.post("/api/workflow/quota/identities/bob", json={"dimension": "tokens", "value": 2**63})
    assert invalid.status_code == 422 and overflow.status_code == 422
    assert recorder.changes == []


def test_full_range_uses_exact_strings_and_unsafe_numeric_requests_are_refused(closed_local_app: TestClient) -> None:
    client = closed_local_app
    recorder = _wire(client)
    maximum = str(2**63 - 1)
    result = client.post("/api/workflow/quota/identities/bob", json={"dimension": "storage", "value": maximum})
    assert result.status_code == 200
    assert result.json()["storage_bytes"] == maximum
    assert len(recorder.changes) == 1
    for invalid in (2**53, str(2**63), "1.0", "-1", "\uff11\uff12", True):
        response = client.post("/api/workflow/quota/identities/bob", json={"dimension": "tokens", "value": invalid})
        assert response.status_code == 422
    assert len(recorder.changes) == 1


def test_persisted_policy_can_be_edited_and_restored_without_defaults(
    closed_local_app: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = closed_local_app
    _wire(client)
    with client.app.state.phase3_engine.begin() as conn:
        frozen_now = database_now(conn)
    monkeypatch.setattr(quota_policy_authority, "database_now", lambda _conn: frozen_now)
    first = client.post("/api/workflow/quota/identities/bob", json={"dimension": "storage", "value": "5368709120"})
    assert first.status_code == 200
    assert client.post("/api/workflow/quota/identities/bob", json={"dimension": "storage", "value": "6000000000"}).status_code == 200
    settings = client.app.state.settings
    client.app.state.settings = settings.model_copy(update={"quota_default_tokens_per_day": None, "quota_default_storage_bytes": None})
    assert client.get("/api/workflow/quota/identities/bob").json()["can_edit"] is True
    assert client.post("/api/workflow/quota/identities/bob/revoke", json={}).status_code == 200
    restored = client.post("/api/workflow/quota/identities/bob", json={"dimension": "tokens", "value": "777"})
    assert restored.status_code == 200
    assert restored.json()["policy_state"] == "active"
    assert restored.json()["storage_bytes"] == "6000000000"
    assert restored.json()["tokens_per_day"] == "777"
    assert client.get("/api/workflow/quota/me").json()["can_edit"] is False


def test_status_reports_aggregate_usage_and_unknown_container_accounting(closed_local_app: TestClient) -> None:
    client = closed_local_app
    _wire(client)
    engine = client.app.state.phase3_engine
    with engine.begin() as conn:
        now = database_now(conn)
        for owner, size, tokens in (("alice", 10, 20), ("bob", 30, 40)):
            session_id = f"quota-{owner}"
            conn.execute(
                insert(sessions_table).values(
                    id=session_id,
                    user_id=owner,
                    auth_provider_type="local",
                    title="quota",
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                insert(blobs_table).values(
                    id=f"blob-{owner}",
                    session_id=session_id,
                    filename="data.csv",
                    mime_type="text/csv",
                    size_bytes=size,
                    storage_path=f"/nonexistent/{owner}.csv",
                    created_at=now,
                    created_by="user",
                    status="pending",
                )
            )
            conn.execute(
                insert(token_usage_ledger_table).values(
                    entry_id=f"entry-{owner}",
                    identity_id=owner,
                    source="composer",
                    session_id=session_id,
                    model="test",
                    prompt_tokens=tokens,
                    completion_tokens=1,
                    recorded_at=now,
                )
            )
        conn.execute(
            insert(token_usage_ledger_table).values(
                entry_id="system-probe",
                identity_id=None,
                source="system",
                session_id=None,
                model="probe",
                prompt_tokens=None,
                completion_tokens=None,
                recorded_at=now,
            )
        )
    status = client.get("/api/workflow/quota/me").json()
    assert status["tokens_used_today"] == "21"
    assert status["container_tokens_used_today"] == "62"
    assert status["storage_bytes_used"] == "10"
    assert status["container_storage_bytes_used"] == "40"
    with engine.begin() as conn:
        conn.execute(
            insert(quota_provider_attempts_table).values(
                attempt_id="pending",
                identity_id="bob",
                session_id="quota-bob",
                source="composer",
                started_at=database_now(conn),
                operation_id="operation",
                operation_epoch=1,
                lease_token="lease",
            )
        )
    unknown = client.get("/api/workflow/quota/me").json()
    assert unknown["tokens_used_today"] == "21"
    assert unknown["container_tokens_used_today"] is None
