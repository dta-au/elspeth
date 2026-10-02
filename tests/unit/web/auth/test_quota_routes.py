"""Quota status and administration through the real HTTP router."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import insert, update

from elspeth.web.auth.quota_routes import create_quota_router
from elspeth.web.coordination.approval_lifecycle_authority import RepositoryApprovalLifecycleAuthority
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.coordination.quota_policy_authority import QuotaPolicyChange, RepositoryQuotaPolicyAuthority
from elspeth.web.sessions.models import identity_roles_table
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
    assert own.status_code == 200 and own.json()["storage_bytes_used"] == 0
    assert own.headers["Cache-Control"] == "no-store"
    first = client.post("/api/workflow/quota/identities/bob", json={"dimension": "storage", "value": 2**40})
    assert first.status_code == 200 and first.json()["storage_bytes"] == 2**40
    assert first.json()["tokens_per_day"] == 100_000
    assert len(recorder.changes) == 1 and recorder.changes[0].dimension == "storage"
    read = client.get("/api/workflow/quota/identities/bob")
    assert read.status_code == 200 and read.json()["storage_bytes"] == 2**40
    revoked = client.post("/api/workflow/quota/identities/bob/revoke", json={})
    assert revoked.status_code == 200 and revoked.json()["storage_bytes"] is None
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
