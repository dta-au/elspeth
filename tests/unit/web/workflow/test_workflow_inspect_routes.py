"""A live request authorizes a frozen public state read and an access row."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import event, insert, select, update
from sqlalchemy.exc import SQLAlchemyError

from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.coordination.audit_access_log_authority import RepositoryAuditAccessLogAuthority
from elspeth.web.coordination.review_authority import RepositoryReviewAuthority
from elspeth.web.sessions.models import (
    approvals_table,
    audit_access_log_table,
    composition_states_table,
    identity_roles_table,
    sessions_table,
)
from elspeth.web.sessions.routes.workflow.inspect import create_workflow_inspect_router
from elspeth.web.sessions.state_envelope import envelope_state_column
from tests.fixtures.identities import ensure_test_identity

SESSION = "11111111-1111-4111-8111-111111111111"
STATE = "22222222-2222-4222-8222-222222222222"
URL = f"/api/workflow/inspect/{SESSION}/{STATE}"


@pytest.fixture
def app(closed_local_app: Any) -> Any:
    engine = closed_local_app.app.state.phase3_engine
    now = datetime.now(UTC)
    with engine.begin() as conn:
        for identity_id in ("alice", "bob", "carol"):
            ensure_test_identity(conn, identity_id=identity_id)
        for identity_id in ("bob", "carol"):
            conn.execute(
                insert(identity_roles_table).values(
                    role_id=f"role-{identity_id}",
                    identity_id=identity_id,
                    role="approver",
                    granted_at=now,
                    granted_by_identity_id="alice",
                )
            )
        conn.execute(
            insert(sessions_table).values(
                id=SESSION,
                user_id="alice",
                auth_provider_type="local",
                title="inspect me",
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            insert(composition_states_table).values(
                id=STATE,
                session_id=SESSION,
                version=1,
                provenance="session_seed",
                created_at=now,
                sources=envelope_state_column({}),
                nodes=envelope_state_column([]),
                edges=envelope_state_column([]),
                outputs=envelope_state_column([]),
                metadata_=envelope_state_column({"name": "demo", "description": ""}),
            )
        )
        conn.execute(
            insert(approvals_table).values(
                approval_id="approval-1",
                session_id=SESSION,
                state_id=STATE,
                binding_json={},
                requested_by_identity_id="alice",
                approver_identity_id="bob",
                requested_at=now,
            )
        )
    closed_local_app.app.include_router(create_workflow_inspect_router())
    closed_local_app.app.state.audit_access_log_authority = RepositoryAuditAccessLogAuthority(engine)
    closed_local_app.app.state.review_authority = RepositoryReviewAuthority(engine)
    return closed_local_app


def _as(app: Any, identity_id: str) -> None:
    async def user() -> UserIdentity:
        return UserIdentity(user_id=identity_id, username=identity_id)

    app.app.dependency_overrides[get_current_user] = user


def _access_rows(app: Any) -> list[Any]:
    with app.app.state.phase3_engine.connect() as conn:
        return list(conn.execute(select(audit_access_log_table)).all())


def test_non_addressee_with_approver_role_reads_public_projection_and_access_is_logged(app: Any) -> None:
    _as(app, "carol")

    response = app.get(URL)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["composition_snapshot"]["metadata"] == {"name": "demo", "description": ""}
    assert body["attestations"] == []
    assert "token" not in body
    assert response.headers["cache-control"] == "no-store"
    rows = _access_rows(app)
    assert [(row.id, row.requesting_principal, row.writer_principal, row.query_args) for row in rows] == [
        (body["access_log_id"], "carol", "workflow_inspect", {})
    ]


@pytest.mark.parametrize("change", ["author", "closed", "governance_off"])
def test_denial_is_hidden_or_explicit_governance_refusal_and_writes_nothing(app: Any, change: str) -> None:
    _as(app, "alice" if change == "author" else "carol")
    if change == "closed":
        with app.app.state.phase3_engine.begin() as conn:
            conn.execute(update(approvals_table).where(approvals_table.c.approval_id == "approval-1").values(decision="approved"))
    elif change == "governance_off":
        app.app.state.settings = app.app.state.settings.model_copy(update={"workflow_governance": "off"})

    response = app.get(URL)

    assert response.status_code == (409 if change == "governance_off" else 404), response.text
    if change == "governance_off":
        assert response.json()["detail"]["error_type"] == "workflow_governance_off"
    else:
        assert response.json() == {"detail": "Not found"}
    assert _access_rows(app) == []


def test_inspect_returns_no_composition_if_access_log_cannot_commit(app: Any) -> None:
    _as(app, "carol")
    engine = app.app.state.phase3_engine

    def fail_audit_insert(_conn, _cursor, statement: str, _params, _context, _executemany) -> None:
        if statement.startswith("INSERT INTO audit_access_log"):
            raise SQLAlchemyError("audit store unavailable")

    event.listen(engine, "before_cursor_execute", fail_audit_insert)
    try:
        response = app.get(URL)
    finally:
        event.remove(engine, "before_cursor_execute", fail_audit_insert)

    assert response.status_code == 500
    assert response.json()["error_type"] == "audit_access_log_write_failed"
    assert "composition_snapshot" not in response.json()
    assert _access_rows(app) == []
