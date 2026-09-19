"""Approval HTTP routes against a real Sessions authority and a compiled binding stub."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.coordination.approval_authority import (
    ApprovalBinding,
    ApprovalRecord,
    ApprovalSupersession,
    ApprovalTransactionAuthority,
    supersede_open_approvals,
)
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.execution.errors import BlobSourcePathMismatchError, ExecutionReadinessError
from elspeth.web.sessions.models import (
    approvals_table,
    composition_states_table,
    identity_roles_table,
    session_operation_fences_table,
    sessions_table,
)
from elspeth.web.sessions.routes.workflow.approvals import create_approvals_router
from tests.fixtures.identities import ensure_test_identity

_BINDING = ApprovalBinding(
    config_hash="config",
    canonical_version="canonical",
    runtime_val_manifest_sha256="manifest",
    openrouter_catalog_sha256="catalog",
    binding_generation_fingerprint="generation",
    policy_hash="policy",
)


@dataclass
class _Audit:
    requested: list[ApprovalRecord] = field(default_factory=list)
    decided: list[ApprovalRecord] = field(default_factory=list)
    superseded: list[ApprovalSupersession] = field(default_factory=list)
    fail: bool = False

    def record_approval_requested(self, _request: Any, *, provider: str, approval: ApprovalRecord) -> None:
        assert provider == "local"
        if self.fail:
            raise RuntimeError("Landscape unavailable")
        self.requested.append(approval)

    def record_approval_decided(self, _request: Any, *, provider: str, approval: ApprovalRecord, actor_identity_id: str) -> None:
        assert provider == "local" and actor_identity_id
        if self.fail:
            raise RuntimeError("Landscape unavailable")
        self.decided.append(approval)

    def record_approval_superseded(self, outcome: ApprovalSupersession) -> None:
        if self.fail:
            raise RuntimeError("Landscape unavailable")
        self.superseded.append(outcome)

    def record_approval_rejection_with_supersessions(
        self,
        _request: Any,
        *,
        provider: str,
        approval: ApprovalRecord,
        actor_identity_id: str,
        supersessions: tuple[ApprovalSupersession, ...],
    ) -> None:
        assert provider == "local" and actor_identity_id
        if self.fail:
            raise RuntimeError("Landscape unavailable")
        self.decided.append(approval)
        self.superseded.extend(supersessions)


class _CompiledExecution:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, UUID, str]] = []
        self.error: Exception | None = None

    async def compile_approval_binding(
        self, session_id: UUID, state_id: UUID, *, user_id: str, session_operation_context: Any
    ) -> ApprovalBinding:
        assert session_operation_context.fence.session_id == str(session_id)
        self.calls.append((session_id, state_id, user_id))
        if self.error is not None:
            raise self.error
        return _BINDING


def _principal(client: TestClient, identity_id: str) -> None:
    async def current() -> UserIdentity:
        return UserIdentity(user_id=identity_id, username=identity_id)

    client.app.dependency_overrides[get_current_user] = current


def _seed(client: TestClient) -> tuple[str, str, _Audit, _CompiledExecution]:
    engine = client.app.state.phase3_engine
    session_id = str(uuid4())
    state_id = str(uuid4())
    with engine.begin() as conn:
        now = database_now(conn)
        for identity_id in ("addressed", "cover", "outsider"):
            ensure_test_identity(conn, identity_id=identity_id)
        conn.execute(
            insert(sessions_table).values(
                id=session_id,
                user_id="alice",
                auth_provider_type="local",
                title="approval test",
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            insert(composition_states_table).values(
                id=state_id,
                session_id=session_id,
                version=1,
                is_valid=True,
                provenance="session_seed",
                created_at=now,
            )
        )
        conn.execute(
            insert(session_operation_fences_table).values(
                session_id=session_id,
                operation_id=f"seed-{session_id}",
                lease_token=f"seed-token-{session_id}",
                operation_kind="create",
                owner_instance_id="approval-route-seed",
                operation_epoch=1,
                lease_expires_at=now + timedelta(hours=1),
                released_at=now,
            )
        )
        for identity_id in ("addressed", "cover"):
            conn.execute(
                insert(identity_roles_table).values(
                    role_id=f"approver-{identity_id}",
                    identity_id=identity_id,
                    role="approver",
                    granted_by_identity_id="alice",
                    granted_at=now - timedelta(days=1),
                )
            )
    audit = _Audit()
    compiled = _CompiledExecution()
    client.app.state.approval_authority = ApprovalTransactionAuthority(engine)
    client.app.state.auth_audit_recorder = audit
    client.app.state.execution_service = compiled
    client.app.include_router(create_approvals_router())
    _principal(client, "alice")
    return session_id, state_id, audit, compiled


def test_request_inbox_cover_decision_and_sent_round_trip(closed_local_app: TestClient) -> None:
    client = closed_local_app
    session_id, state_id, audit, compiled = _seed(client)
    created = client.post(
        f"/api/sessions/{session_id}/approvals",
        json={"state_id": state_id, "approver_identity_id": "addressed", "note": "please approve"},
    )
    assert created.status_code == 201, created.text
    approval_id = created.json()["approval_id"]
    assert created.json()["binding"] == _BINDING.as_json()
    assert compiled.calls == [(UUID(session_id), UUID(state_id), "alice")]
    assert audit.requested[0].approval_id == approval_id
    assert created.headers["Cache-Control"] == "no-store"

    _principal(client, "cover")
    inbox = client.get("/api/approvals/inbox")
    assert inbox.status_code == 200
    assert [row["approval_id"] for row in inbox.json()["approvals"]] == [approval_id]
    decided = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approved"})
    assert decided.status_code == 200, decided.text
    assert decided.json()["decided_by_identity_id"] == "cover"
    assert audit.decided[0].approval_id == approval_id
    loser = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "rejected", "note": "late"})
    assert loser.status_code == 409
    assert loser.json()["detail"]["current_state"] == "approved"
    assert client.get("/api/approvals/inbox").json()["approvals"] == []

    _principal(client, "alice")
    sent = client.get("/api/approvals/sent")
    assert sent.status_code == 200
    assert sent.json()["approvals"][0]["decision"] == "approved"
    assert sent.headers["Cache-Control"] == "no-store"


def test_invalid_execution_readiness_refuses_approval_request_without_a_row(closed_local_app: TestClient) -> None:
    client = closed_local_app
    session_id, state_id, audit, compiled = _seed(client)
    compiled.error = ExecutionReadinessError(blockers=())

    response = client.post(
        f"/api/sessions/{session_id}/approvals",
        json={"state_id": state_id, "approver_identity_id": "addressed"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error_type"] == "execution_not_ready"
    assert audit.requested == []
    with client.app.state.phase3_engine.connect() as conn:
        assert conn.execute(select(approvals_table.c.approval_id)).all() == []


def test_blob_source_path_drift_does_not_disclose_paths_in_approval_response(closed_local_app: TestClient) -> None:
    client = closed_local_app
    session_id, state_id, audit, compiled = _seed(client)
    compiled.error = BlobSourcePathMismatchError(
        blob_id="private-blob", session_id=session_id, stored_path="/private/stored", canonical_path="/private/canonical"
    )

    response = client.post(
        f"/api/sessions/{session_id}/approvals",
        json={"state_id": state_id, "approver_identity_id": "addressed"},
    )

    assert response.status_code == 500
    assert response.json()["detail"]["error_type"] == "blob_source_path_mismatch"
    assert "/private/" not in response.text
    assert audit.requested == []


def test_later_rejection_retires_prior_approval_and_emits_supersession(closed_local_app: TestClient) -> None:
    client = closed_local_app
    session_id, state_id, audit, _compiled = _seed(client)
    first = client.post(f"/api/sessions/{session_id}/approvals", json={"approver_identity_id": "addressed"})
    assert first.status_code == 201, first.text
    first_id = first.json()["approval_id"]
    _principal(client, "addressed")
    assert client.post(f"/api/approvals/{first_id}/decide", json={"decision": "approved"}).status_code == 200
    _principal(client, "alice")
    second = client.post(f"/api/sessions/{session_id}/approvals", json={"approver_identity_id": "addressed"})
    assert second.status_code == 201, second.text
    _principal(client, "cover")
    blank = client.post(f"/api/approvals/{second.json()['approval_id']}/decide", json={"decision": "rejected", "note": "  "})
    assert blank.status_code == 409 and blank.json()["detail"]["error_type"] == "approval_note_required"
    rejected = client.post(
        f"/api/approvals/{second.json()['approval_id']}/decide",
        json={"decision": "rejected", "note": "needs changes"},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["decision"] == "rejected"
    assert len(audit.superseded) == 1
    assert audit.superseded[0].approval.approval_id == first_id
    assert audit.superseded[0].cause == "later_rejection"
    with client.app.state.phase3_engine.connect() as conn:
        rows = conn.execute(select(approvals_table).where(approvals_table.c.state_id == state_id)).all()
    assert {row.approval_id: row.decision for row in rows} == {
        first_id: "superseded",
        second.json()["approval_id"]: "rejected",
    }


def test_closed_request_reports_terminal_state_after_new_state(closed_local_app: TestClient) -> None:
    client = closed_local_app
    session_id, _state_id, audit, _compiled = _seed(client)
    created = client.post(f"/api/sessions/{session_id}/approvals", json={"approver_identity_id": "addressed"})
    assert created.status_code == 201
    approval_id = created.json()["approval_id"]

    with client.app.state.phase3_engine.begin() as conn:
        now = database_now(conn)
        assert supersede_open_approvals(conn, session_id=session_id, now=now, record=audit.record_approval_superseded) == (approval_id,)
        conn.execute(
            insert(composition_states_table).values(
                id=str(uuid4()),
                session_id=session_id,
                version=2,
                is_valid=True,
                provenance="session_seed",
                created_at=now,
            )
        )

    _principal(client, "cover")
    stale = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approved"})
    assert stale.status_code == 409
    assert stale.json()["detail"]["error_type"] == "approval_already_decided"
    assert stale.json()["detail"]["current_state"] == "superseded"
    assert audit.decided == []

    _principal(client, "alice")
    replacement = client.post(f"/api/sessions/{session_id}/approvals", json={"approver_identity_id": "addressed"})
    assert replacement.status_code == 201
    replacement_id = replacement.json()["approval_id"]
    withdrawn = client.post(f"/api/approvals/{replacement_id}/withdraw")
    assert withdrawn.status_code == 200 and withdrawn.json()["decision"] == "revoked"
    _principal(client, "cover")
    revoked = client.post(f"/api/approvals/{replacement_id}/decide", json={"decision": "approved"})
    assert revoked.status_code == 409
    assert revoked.json()["detail"]["error_type"] == "approval_already_decided"
    assert revoked.json()["detail"]["current_state"] == "revoked"


def test_withdraw_and_audit_failure_rollback(closed_local_app: TestClient) -> None:
    client = closed_local_app
    session_id, _state_id, audit, _compiled = _seed(client)
    created = client.post(f"/api/sessions/{session_id}/approvals", json={"approver_identity_id": "addressed"})
    approval_id = created.json()["approval_id"]
    audit.fail = True
    try:
        client.post(f"/api/approvals/{approval_id}/withdraw")
    except RuntimeError as exc:
        assert str(exc) == "Landscape unavailable"
    else:
        raise AssertionError("audit failure did not propagate")
    with client.app.state.phase3_engine.connect() as conn:
        assert conn.execute(select(approvals_table.c.decision).where(approvals_table.c.approval_id == approval_id)).scalar_one() is None
    audit.fail = False
    withdrawn = client.post(f"/api/approvals/{approval_id}/withdraw")
    assert withdrawn.status_code == 200 and withdrawn.json()["decision"] == "revoked"
    assert audit.decided[-1].approval_id == approval_id


def test_request_refusals_and_off_switch(closed_local_app: TestClient) -> None:
    client = closed_local_app
    session_id, state_id, audit, compiled = _seed(client)
    foreign = client.post(f"/api/sessions/{uuid4()}/approvals", json={"approver_identity_id": "addressed"})
    assert foreign.status_code == 404
    wrong_state = client.post(f"/api/sessions/{session_id}/approvals", json={"state_id": str(uuid4()), "approver_identity_id": "addressed"})
    assert wrong_state.status_code == 404 and wrong_state.json()["detail"]["error_type"] == "state_not_found"
    invalid = client.post(f"/api/sessions/{session_id}/approvals", json={"approver_identity_id": "addressed", "binding": {}})
    assert invalid.status_code == 422
    too_long = client.post(
        f"/api/sessions/{session_id}/approvals", json={"state_id": state_id, "approver_identity_id": "addressed", "note": "é" * 3000}
    )
    assert too_long.status_code == 409 and too_long.json()["detail"]["error_type"] == "approval_note_too_long"
    assert audit.requested == []
    assert compiled.calls[-1] == (UUID(session_id), UUID(state_id), "alice")
    client.app.state.settings = client.app.state.settings.model_copy(update={"workflow_governance": "off"})
    off = client.post(f"/api/sessions/{session_id}/approvals", json={"approver_identity_id": "addressed"})
    assert off.status_code == 409 and off.json()["detail"]["error_type"] == "workflow_governance_off"
    assert client.get("/api/approvals/inbox").json()["approvals"] == []
    assert client.get("/api/approvals/sent").json()["approvals"] == []


def test_revoked_approver_and_author_cannot_decide(closed_local_app: TestClient) -> None:
    client = closed_local_app
    session_id, _state_id, audit, _compiled = _seed(client)
    created = client.post(f"/api/sessions/{session_id}/approvals", json={"approver_identity_id": "addressed"})
    assert created.status_code == 201
    approval_id = created.json()["approval_id"]
    author = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approved"})
    assert author.status_code == 409 and author.json()["detail"]["error_type"] == "approval_author_is_approver"
    _principal(client, "outsider")
    outsider = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approved"})
    unknown = client.post(f"/api/approvals/{uuid4()}/decide", json={"decision": "approved"})
    assert outsider.status_code == unknown.status_code == 404
    assert outsider.content == unknown.content
    for body in ({"decision": "rejected", "note": " "}, {"decision": "approved", "note": "é" * 3000}):
        existing_with_bad_note = client.post(f"/api/approvals/{approval_id}/decide", json=body)
        unknown_with_bad_note = client.post(f"/api/approvals/{uuid4()}/decide", json=body)
        assert existing_with_bad_note.status_code == unknown_with_bad_note.status_code == 404
        assert existing_with_bad_note.content == unknown_with_bad_note.content
    assert client.get("/api/approvals/inbox").json()["approvals"] == []
    with client.app.state.phase3_engine.begin() as conn:
        conn.execute(
            update(identity_roles_table).where(identity_roles_table.c.role_id == "approver-addressed").values(revoked_at=database_now(conn))
        )
    _principal(client, "addressed")
    assert client.get("/api/approvals/inbox").json()["approvals"] == []
    revoked = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approved"})
    assert revoked.status_code == unknown.status_code
    assert revoked.content == unknown.content
    assert audit.decided == []


def test_non_requester_withdraw_does_not_reveal_approval(closed_local_app: TestClient) -> None:
    client = closed_local_app
    session_id, _state_id, audit, _compiled = _seed(client)
    created = client.post(f"/api/sessions/{session_id}/approvals", json={"approver_identity_id": "addressed"})
    assert created.status_code == 201
    _principal(client, "outsider")
    existing = client.post(f"/api/approvals/{created.json()['approval_id']}/withdraw")
    unknown = client.post(f"/api/approvals/{uuid4()}/withdraw")
    assert existing.status_code == unknown.status_code == 404
    assert existing.content == unknown.content
    _principal(client, "cover")
    role_granted_non_requester = client.post(f"/api/approvals/{created.json()['approval_id']}/withdraw")
    assert role_granted_non_requester.status_code == unknown.status_code
    assert role_granted_non_requester.content == unknown.content
    assert audit.decided == []
