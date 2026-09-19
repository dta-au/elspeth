"""Mailbox projections and seen stamps over the real Sessions authorities."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, update

from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.coordination.approval_authority import ApprovalBinding, ApprovalTransactionAuthority, RepositoryApprovalAuthority
from elspeth.web.coordination.approval_lifecycle_authority import RepositoryApprovalLifecycleAuthority
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.coordination.review_authority import RepositoryReviewAuthority, ReviewerRoleRequired, ReviewParticipantNotActive
from elspeth.web.sessions.models import composition_states_table, identities_table, identity_relationships_table, identity_roles_table
from elspeth.web.sessions.routes.workflow.mailbox import create_mailbox_router, decision_is_unseen
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web.conftest import _make_session

_BINDING = ApprovalBinding(
    config_hash="config",
    canonical_version="canonical",
    runtime_val_manifest_sha256="manifest",
    openrouter_catalog_sha256="catalog",
    binding_generation_fingerprint="generation",
    policy_hash="policy",
)


def _principal(client: TestClient, identity_id: str) -> None:
    async def current() -> UserIdentity:
        return UserIdentity(user_id=identity_id, username=identity_id)

    client.app.dependency_overrides[get_current_user] = current


@pytest.fixture
def mailbox_app(closed_local_app: TestClient) -> TestClient:
    engine = closed_local_app.app.state.phase3_engine
    with engine.begin() as conn:
        now = database_now(conn)
        for identity_id in ("bob", "carol", "erin", "bystander"):
            ensure_test_identity(conn, identity_id=identity_id)
        for identity_id, role in (("bob", "approver"), ("carol", "approver"), ("erin", "reviewer")):
            conn.execute(
                insert(identity_roles_table).values(
                    role_id=f"role-{identity_id}",
                    identity_id=identity_id,
                    role=role,
                    granted_by_identity_id="alice",
                    granted_at=now - timedelta(minutes=1),
                )
            )
        conn.execute(
            insert(identity_relationships_table).values(
                relationship_id="carol-over-alice",
                from_identity_id="carol",
                to_identity_id="alice",
                relationship_type="approver",
                asserted_by_identity_id="alice",
                asserted_at=now,
            )
        )
    closed_local_app.app.state.identity_authority = RepositoryIdentityAuthority(
        engine, lifecycle_effect=RepositoryApprovalLifecycleAuthority().apply
    )
    closed_local_app.app.state.approval_authority = ApprovalTransactionAuthority(engine)
    closed_local_app.app.state.review_authority = RepositoryReviewAuthority(engine)
    closed_local_app.app.include_router(create_mailbox_router())
    return closed_local_app


def _request(client: TestClient, *, addressed_to: str) -> tuple[str, str, str]:
    engine = client.app.state.phase3_engine
    session_id = str(uuid4())
    state_id = str(uuid4())
    with engine.begin() as conn:
        now = database_now(conn)
        _make_session(conn, session_id=session_id, user_id="alice", created_at=now, updated_at=now)
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

    def create(token: str) -> str:
        record = RepositoryApprovalAuthority.request(
            token,
            session_id=session_id,
            state_id=state_id,
            binding=_BINDING,
            requested_by="alice",
            approver=addressed_to,
            note="inspect this state",
            record=lambda _record: None,
        )
        return record.approval_id

    approval_id = client.app.state.approval_authority.run(session_id, create)
    return session_id, state_id, approval_id


def test_role_based_inbox_orders_addressed_first_and_conceals_author(mailbox_app: TestClient) -> None:
    _, _, cover_id = _request(mailbox_app, addressed_to="carol")
    _, _, addressed_id = _request(mailbox_app, addressed_to="bob")
    _principal(mailbox_app, "bob")
    inbox = mailbox_app.get("/api/workflow/mailbox/inbox")
    assert inbox.status_code == 200, inbox.text
    assert inbox.headers["Cache-Control"] == "no-store"
    assert [row["approval_id"] for row in inbox.json()["approvals"]] == [addressed_id, cover_id]
    summary = mailbox_app.get("/api/workflow/mailbox/summary")
    assert summary.json()["approvals_to_decide"] == 2
    assert summary.json()["roles"] == ["approver"]
    _principal(mailbox_app, "alice")
    assert mailbox_app.get("/api/workflow/mailbox/inbox").json()["approvals"] == []
    _principal(mailbox_app, "bystander")
    assert mailbox_app.get("/api/workflow/mailbox/inbox").json()["approvals"] == []


def test_sent_seen_and_directory_follow_current_grants(mailbox_app: TestClient) -> None:
    session_id, _, approval_id = _request(mailbox_app, addressed_to="bob")
    _principal(mailbox_app, "alice")
    directory = mailbox_app.get("/api/workflow/mailbox/approvers")
    assert directory.json() == {
        "approvers": [{"identity_id": "bob", "username": "bob"}, {"identity_id": "carol", "username": "carol"}],
        "suggested_identity_ids": ["carol"],
    }
    assert mailbox_app.post(f"/api/workflow/mailbox/{approval_id}/seen").json()["decision_seen_at"] is None

    def decide(token: str) -> None:
        RepositoryApprovalAuthority.decide(
            token,
            approval_id=approval_id,
            decided_by="bob",
            decision="rejected",
            note="missing owner",
            record=lambda _record: None,
        )

    mailbox_app.app.state.approval_authority.run(session_id, decide)
    assert mailbox_app.get("/api/workflow/mailbox/summary").json()["decisions_unseen"] == 1
    (sent,) = mailbox_app.get("/api/workflow/mailbox/sent").json()["approvals"]
    assert (sent["decision"], sent["decided_by_identity_id"], sent["decision_note"]) == ("rejected", "bob", "missing owner")
    _principal(mailbox_app, "bob")
    hidden = mailbox_app.post(f"/api/workflow/mailbox/{approval_id}/seen")
    assert hidden.status_code == 404
    _principal(mailbox_app, "alice")
    seen = mailbox_app.post(f"/api/workflow/mailbox/{approval_id}/seen")
    assert seen.status_code == 200, seen.text
    assert seen.json()["decision_seen_at"] is not None
    assert mailbox_app.get("/api/workflow/mailbox/summary").json()["decisions_unseen"] == 0
    assert mailbox_app.post(f"/api/workflow/mailbox/{approval_id}/seen").json()["decision_seen_at"] == seen.json()["decision_seen_at"]

    with mailbox_app.app.state.phase3_engine.begin() as conn:
        conn.execute(
            update(identity_roles_table).where(identity_roles_table.c.role_id == "role-carol").values(revoked_at=database_now(conn))
        )
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "bob").values(access_state="disabled"))
    assert mailbox_app.get("/api/workflow/mailbox/approvers").json() == {"approvers": [], "suggested_identity_ids": []}


def test_governance_off_keeps_roles_but_empties_folders(mailbox_app: TestClient) -> None:
    _, _, approval_id = _request(mailbox_app, addressed_to="bob")
    mailbox_app.app.state.settings = mailbox_app.app.state.settings.model_copy(update={"workflow_governance": "off"})
    _principal(mailbox_app, "bob")
    assert mailbox_app.get("/api/workflow/mailbox/summary").json() == {
        "governance": "off",
        "roles": ["approver"],
        "approvals_to_decide": 0,
        "reviews_to_attest": 0,
        "decisions_unseen": 0,
    }
    assert mailbox_app.get("/api/workflow/mailbox/inbox").json() == {"approvals": [], "reviews": []}
    assert mailbox_app.get("/api/workflow/mailbox/sent").json() == {"approvals": [], "reviews": []}
    assert mailbox_app.get("/api/workflow/mailbox/approvers").json() == {"approvers": [], "suggested_identity_ids": []}
    refusal = mailbox_app.post(f"/api/workflow/mailbox/{approval_id}/seen")
    assert refusal.status_code == 409
    assert refusal.json()["detail"]["error_type"] == "workflow_governance_off"


def test_review_request_moves_from_reviewer_inbox_to_requester_sent(mailbox_app: TestClient) -> None:
    session_id, state_id, _ = _request(mailbox_app, addressed_to="bob")
    authority = mailbox_app.app.state.review_authority
    requested = authority.request(
        session_id=session_id,
        state_id=state_id,
        requested_by="alice",
        reviewer="erin",
        note="check this graph",
        record=lambda _record: None,
    )
    _principal(mailbox_app, "erin")
    inbox = mailbox_app.get("/api/workflow/mailbox/inbox").json()
    assert [row["request_id"] for row in inbox["reviews"]] == [requested.request_id]
    assert mailbox_app.get("/api/workflow/mailbox/summary").json()["reviews_to_attest"] == 1
    authority.attest(
        session_id=session_id,
        state_id=state_id,
        payload_digest="sha256:" + "ab" * 32,
        reviewer="erin",
        verdict="changes_requested",
        note="rename the sink",
        record=lambda _record: None,
    )
    assert mailbox_app.get("/api/workflow/mailbox/inbox").json()["reviews"] == []
    _principal(mailbox_app, "alice")
    (sent,) = mailbox_app.get("/api/workflow/mailbox/sent").json()["reviews"]
    assert sent["request"]["request_id"] == requested.request_id
    assert sent["request"]["open"] is False
    assert [(row["reviewer_identity_id"], row["verdict"], row["note"]) for row in sent["attestations"]] == [
        ("erin", "changes_requested", "rename the sink")
    ]
    assert mailbox_app.get("/api/workflow/mailbox/summary").json()["decisions_unseen"] == 0


def test_historical_service_identity_is_not_offered_as_approver(mailbox_app: TestClient) -> None:
    _, _, _approval_id = _request(mailbox_app, addressed_to="bob")
    with mailbox_app.app.state.phase3_engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "bob").values(provider="service", kind="human"))
    _principal(mailbox_app, "alice")
    assert [row["identity_id"] for row in mailbox_app.get("/api/workflow/mailbox/approvers").json()["approvers"]] == ["carol"]
    _principal(mailbox_app, "bob")
    assert mailbox_app.get("/api/workflow/mailbox/summary").json()["roles"] == []
    assert mailbox_app.get("/api/workflow/mailbox/inbox").json()["approvals"] == []
    assert mailbox_app.get("/api/workflow/mailbox/approvers").json() == {"approvers": [], "suggested_identity_ids": []}


def test_summary_exposes_only_a_live_user_grant_for_library_publish(mailbox_app: TestClient) -> None:
    _principal(mailbox_app, "bystander")
    assert mailbox_app.get("/api/workflow/mailbox/summary").json()["roles"] == []
    with mailbox_app.app.state.phase3_engine.begin() as conn:
        conn.execute(
            insert(identity_roles_table).values(
                role_id="role-bystander-user",
                identity_id="bystander",
                role="user",
                granted_by_identity_id="alice",
                granted_at=database_now(conn),
            )
        )
    assert mailbox_app.get("/api/workflow/mailbox/summary").json()["roles"] == ["user"]
    with mailbox_app.app.state.phase3_engine.begin() as conn:
        conn.execute(
            update(identity_roles_table)
            .where(identity_roles_table.c.role_id == "role-bystander-user")
            .values(revoked_at=database_now(conn))
        )
    assert mailbox_app.get("/api/workflow/mailbox/summary").json()["roles"] == []


def test_unseen_badge_excludes_outcomes_caused_by_the_requester(mailbox_app: TestClient) -> None:
    _, _, approval_id = _request(mailbox_app, addressed_to="bob")
    (record,) = mailbox_app.app.state.approval_authority.sent(requested_by_identity_id="alice")
    assert record.approval_id == approval_id
    assert decision_is_unseen(record, caller="alice") is False
    assert decision_is_unseen(replace(record, decision="superseded"), caller="alice") is False
    own_withdrawal = replace(record, decision="revoked", revocation_actor_kind="identity", revoked_by_identity_id="alice")
    assert decision_is_unseen(own_withdrawal, caller="alice") is False
    assert decision_is_unseen(replace(own_withdrawal, revocation_actor_kind="system", revoked_by_identity_id=None), caller="alice")
    assert decision_is_unseen(replace(record, decision="approved"), caller="bystander") is False


@pytest.mark.parametrize(
    ("refusal", "path"),
    [
        (ReviewerRoleRequired("reviewer role revoked"), "/api/workflow/mailbox/inbox"),
        (ReviewParticipantNotActive("reviewer disabled"), "/api/workflow/mailbox/summary"),
    ],
)
def test_reviewer_authority_loss_during_mailbox_read_is_explicit(mailbox_app: TestClient, refusal: Exception, path: str) -> None:
    class _RevokedReviewAuthority:
        def open_for(self, *, reviewer: str) -> None:
            assert reviewer == "erin"
            raise refusal

    mailbox_app.app.state.review_authority = _RevokedReviewAuthority()
    _principal(mailbox_app, "erin")
    response = mailbox_app.get(path)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_type"] == "reviewer_scope_changed"
