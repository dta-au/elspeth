"""The approver audit view reads only identities in active oversight scope."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import insert

from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import auth_events_table, run_attributions_table, runs_table
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.coordination.workflow_scope_reader import RepositoryWorkflowScopeReader
from elspeth.web.sessions.models import approvals_table, identity_relationships_table, identity_roles_table, sessions_table
from elspeth.web.sessions.routes.workflow.audit_view import create_workflow_audit_view_router
from tests.fixtures.identities import ensure_test_identity


@pytest.fixture
def app(closed_local_app: Any) -> Any:
    engine = closed_local_app.app.state.phase3_engine
    now = datetime.now(UTC)
    with engine.begin() as conn:
        for identity_id in ("root", "lead", "alice", "outsider"):
            ensure_test_identity(conn, identity_id=identity_id)
        conn.execute(
            insert(identity_roles_table).values(
                role_id="lead-approver",
                identity_id="lead",
                role="approver",
                granted_at=now,
                granted_by_identity_id="root",
            )
        )
        conn.execute(
            insert(identity_relationships_table).values(
                relationship_id="lead-alice",
                from_identity_id="lead",
                to_identity_id="alice",
                relationship_type="approver",
                asserted_by_identity_id="root",
                asserted_at=now,
            )
        )
        for identity_id in ("alice", "outsider"):
            conn.execute(
                insert(sessions_table).values(
                    id=f"session-{identity_id}",
                    user_id=identity_id,
                    auth_provider_type="local",
                    title="audit",
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                insert(approvals_table).values(
                    approval_id=f"approval-{identity_id}",
                    session_id=f"session-{identity_id}",
                    state_id=f"state-{identity_id}",
                    binding_json={},
                    requested_by_identity_id=identity_id,
                    approver_identity_id="lead",
                    requested_at=now,
                    decision="approved",
                )
            )

    settings = closed_local_app.app.state.settings
    (settings.data_dir / "runs").mkdir(parents=True, exist_ok=True)
    with (
        LandscapeDB.from_url(settings.get_landscape_url(), passphrase=settings.landscape_passphrase) as db,
        db.write_connection() as conn,
    ):
        for identity_id in ("alice", "outsider"):
            conn.execute(
                insert(runs_table).values(
                    run_id=f"run-{identity_id}",
                    started_at=now,
                    completed_at=now,
                    config_hash="0" * 64,
                    settings_json="{}",
                    canonical_version="v1",
                    status="completed",
                    openrouter_catalog_sha256="0" * 64,
                    openrouter_catalog_source="bundled",
                )
            )
            conn.execute(
                insert(run_attributions_table).values(
                    run_id=f"run-{identity_id}",
                    recorded_at=now,
                    initiated_by_user_id=identity_id,
                    auth_provider_type="local",
                )
            )
            conn.execute(
                insert(auth_events_table).values(
                    event_id=f"event-{identity_id}",
                    occurred_at=now,
                    event_type="login",
                    outcome="success",
                    provider="local",
                    user_id=identity_id,
                    username=identity_id,
                    identity_id=identity_id,
                    metadata_json="{}",
                )
            )
    closed_local_app.app.state.workflow_scope_reader = RepositoryWorkflowScopeReader(engine)
    closed_local_app.app.include_router(create_workflow_audit_view_router())
    return closed_local_app


def _as(app: Any, identity_id: str) -> None:
    async def user() -> UserIdentity:
        return UserIdentity(user_id=identity_id, username=identity_id)

    app.app.dependency_overrides[get_current_user] = user


def test_approver_view_contains_only_scoped_history_from_both_stores(app: Any) -> None:
    _as(app, "lead")

    response = app.get("/api/workflow/audit-view")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["identity_ids"] == ["alice"]
    assert [row["run_id"] for row in body["runs"]] == ["run-alice"]
    assert [row["approval_id"] for row in body["approvals"]] == ["approval-alice"]
    assert [row["event_id"] for row in body["auth_events"]] == ["event-alice"]
    assert response.headers["cache-control"] == "no-store"


def test_roleless_caller_is_hidden_and_governance_off_refuses(app: Any) -> None:
    _as(app, "outsider")
    assert app.get("/api/workflow/audit-view").status_code == 404
    _as(app, "lead")
    app.app.state.settings = app.app.state.settings.model_copy(update={"workflow_governance": "off"})
    response = app.get("/api/workflow/audit-view")
    assert response.status_code == 409
    assert response.json()["detail"]["error_type"] == "workflow_governance_off"
