"""Review HTTP routes over the real Sessions review authority."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import Request
from sqlalchemy import insert, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.coordination.review_authority import RepositoryReviewAuthority, ReviewAttestationRecord
from elspeth.web.sessions.models import (
    composition_states_table,
    identity_roles_table,
    review_attestations_table,
    review_requests_table,
    sessions_table,
)
from elspeth.web.sessions.routes.workflow.reviews import create_reviews_router
from elspeth.web.sessions.state_envelope import envelope_state_column
from tests.fixtures.identities import ensure_test_identity

SESSION = "11111111-1111-4111-8111-111111111111"
STATE = "22222222-2222-4222-8222-222222222222"
OTHER_STATE = "33333333-3333-4333-8333-333333333333"


@dataclass
class _AuditRecorder:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    fail_request: bool = False

    def record_review_requested(self, request: Request, **kwargs: Any) -> None:
        assert isinstance(request, Request)
        if self.fail_request:
            raise RuntimeError("audit unavailable")
        self.calls.append(("requested", kwargs))

    def record_review_request_cancelled(self, request: Request, **kwargs: Any) -> None:
        assert isinstance(request, Request)
        self.calls.append(("cancelled", kwargs))

    def record_review_attested(self, request: Request, **kwargs: Any) -> None:
        assert isinstance(request, Request)
        self.calls.append(("attested", kwargs))


@pytest.fixture
def app(closed_local_app: Any) -> Any:
    engine = closed_local_app.app.state.phase3_engine
    now = datetime.now(UTC)
    with engine.begin() as conn:
        for identity_id in ("alice", "bob", "carol", "dave"):
            ensure_test_identity(conn, identity_id=identity_id)
        for role_id, identity_id in (("role-bob", "bob"), ("role-carol", "carol")):
            conn.execute(
                insert(identity_roles_table).values(
                    role_id=role_id,
                    identity_id=identity_id,
                    role="reviewer",
                    granted_at=now,
                    granted_by_identity_id="alice",
                )
            )
        conn.execute(
            insert(sessions_table).values(
                id=SESSION,
                user_id="alice",
                auth_provider_type="local",
                title="review me",
                created_at=now,
                updated_at=now,
            )
        )
        for state_id, version in ((STATE, 1), (OTHER_STATE, 2)):
            conn.execute(
                insert(composition_states_table).values(
                    id=state_id,
                    session_id=SESSION,
                    version=version,
                    provenance="session_seed",
                    created_at=now,
                    sources=envelope_state_column({"source": {"plugin": "csv", "options": {"path": "in.csv"}}}),
                    nodes=envelope_state_column([{"id": "n1", "plugin": "passthrough", "options": {}}]),
                    edges=envelope_state_column([]),
                    outputs=envelope_state_column([]),
                    metadata_=envelope_state_column({"name": "demo", "description": ""}),
                )
            )
    closed_local_app.app.state.review_authority = RepositoryReviewAuthority(engine)
    closed_local_app.app.state.auth_audit_recorder = _AuditRecorder()
    closed_local_app.app.include_router(create_reviews_router())
    return closed_local_app


def _as(app: Any, identity_id: str) -> None:
    async def user() -> UserIdentity:
        return UserIdentity(user_id=identity_id, username=identity_id)

    app.app.dependency_overrides[get_current_user] = user


def _request(app: Any, *, reviewer: str | None = "bob", state_id: str = STATE) -> Any:
    _as(app, "alice")
    return app.post(
        f"/api/sessions/{SESSION}/reviews", json={"state_id": state_id, "reviewer_identity_id": reviewer, "note": "please look"}
    )


def _attest(app: Any, request_id: str, *, reviewer: str = "bob", verdict: str = "signed_off", note: str | None = None) -> Any:
    _as(app, reviewer)
    return app.post(f"/api/reviews/{request_id}/attest", json={"verdict": verdict, "note": note})


def test_request_and_attest_bind_audit_to_open_exact_request(app: Any) -> None:
    requested = _request(app)
    assert requested.status_code == 201, requested.text
    request_id = requested.json()["request_id"]
    assert requested.json()["open"] is True
    assert requested.headers["cache-control"] == "no-store"
    attested = _attest(app, request_id)
    assert attested.status_code == 201, attested.text
    assert attested.json()["session_id"] == SESSION
    assert attested.json()["state_id"] == STATE
    assert attested.json()["author_identity_id"] == "alice"
    expected_digest = (
        "sha256:"
        + hashlib.sha256(
            canonical_json(
                {
                    "version": 1,
                    "sources": {"source": {"plugin": "csv", "options": {"path": "in.csv"}}},
                    "source": None,
                    "nodes": [{"id": "n1", "plugin": "passthrough", "options": {}}],
                    "edges": [],
                    "outputs": [],
                    "metadata": {"name": "demo", "description": ""},
                }
            ).encode("utf-8")
        ).hexdigest()
    )
    assert attested.json()["payload_digest"] == expected_digest
    assert [name for name, _ in app.app.state.auth_audit_recorder.calls] == ["requested", "attested"]
    assert app.app.state.auth_audit_recorder.calls[1][1]["authorizing_request_id"] == request_id
    with app.app.state.phase3_engine.connect() as conn:
        rows = conn.execute(select(review_attestations_table)).all()
    assert len(rows) == 1 and rows[0].payload_digest == attested.json()["payload_digest"]


def test_audit_failure_prevents_request_response_and_rolls_back_row(app: Any) -> None:
    app.app.state.auth_audit_recorder.fail_request = True
    with pytest.raises(RuntimeError, match="audit unavailable"):
        _request(app)
    with app.app.state.phase3_engine.connect() as conn:
        assert conn.execute(select(review_requests_table)).all() == []


def test_missing_authorizing_request_id_refuses_before_audit_and_rolls_back(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    request_id = _request(app).json()["request_id"]
    original_attest = RepositoryReviewAuthority.attest

    def missing_authorizer(self: RepositoryReviewAuthority, **kwargs: Any) -> ReviewAttestationRecord:
        record = kwargs["record"]

        def without_authorizer(attested: ReviewAttestationRecord) -> None:
            record(replace(attested, authorizing_request_id=None))

        return original_attest(self, **{**kwargs, "record": without_authorizer})

    monkeypatch.setattr(RepositoryReviewAuthority, "attest", missing_authorizer)
    with pytest.raises(AuditIntegrityError, match="authorizing request"):
        _attest(app, request_id)
    assert [name for name, _ in app.app.state.auth_audit_recorder.calls] == ["requested"]
    with app.app.state.phase3_engine.connect() as conn:
        assert conn.execute(select(review_attestations_table)).all() == []


def test_attestation_requires_open_request_addressed_to_reviewer(app: Any) -> None:
    requested = _request(app)
    request_id = requested.json()["request_id"]
    wrong_reviewer = _attest(app, request_id, reviewer="carol")
    assert wrong_reviewer.status_code == 404
    assert wrong_reviewer.json()["detail"]["error_type"] == "review_request_not_found"
    author = _attest(app, request_id, reviewer="alice")
    assert author.status_code == 409
    assert author.json()["detail"]["error_type"] == "reviewer_is_author"
    first = _attest(app, request_id)
    assert first.status_code == 201, first.text
    closed = _attest(app, request_id)
    assert closed.status_code == 409
    assert closed.json()["detail"]["error_type"] == "open_review_request_required"
    assert [name for name, _ in app.app.state.auth_audit_recorder.calls] == ["requested", "attested"]


def test_addressed_request_id_is_concealed_from_another_active_reviewer(app: Any) -> None:
    request_id = _request(app).json()["request_id"]
    hidden = _attest(app, request_id, reviewer="carol")
    absent = _attest(app, "missing", reviewer="carol")
    assert hidden.status_code == absent.status_code == 404
    assert hidden.json() == absent.json()
    assert _attest(app, request_id, reviewer="bob").status_code == 201


def test_old_url_uses_the_actual_new_open_request_as_its_audit_authority(app: Any) -> None:
    old_id = _request(app).json()["request_id"]
    _as(app, "alice")
    assert app.post(f"/api/reviews/{old_id}/cancel").status_code == 200
    new_id = _request(app).json()["request_id"]
    attested = _attest(app, old_id)
    assert attested.status_code == 201, attested.text
    assert app.app.state.auth_audit_recorder.calls[-1][1]["authorizing_request_id"] == new_id


def test_open_request_for_another_state_does_not_authorize_attestation(app: Any) -> None:
    old_id = _request(app).json()["request_id"]
    _as(app, "alice")
    assert app.post(f"/api/reviews/{old_id}/cancel").status_code == 200
    assert _request(app, state_id=OTHER_STATE).status_code == 201
    refused = _attest(app, old_id)
    assert refused.status_code == 409
    assert refused.json()["detail"]["error_type"] == "open_review_request_required"


def test_unaddressed_request_admits_any_active_reviewer_and_inbox_is_role_scoped(app: Any) -> None:
    request_id = _request(app, reviewer=None).json()["request_id"]
    for identity_id in ("bob", "carol"):
        _as(app, identity_id)
        inbox = app.get("/api/reviews/inbox")
        assert inbox.status_code == 200
        assert [row["request_id"] for row in inbox.json()["requests"]] == [request_id]
    _as(app, "dave")
    assert app.get("/api/reviews/inbox").json() == {"requests": []}
    assert _attest(app, request_id, reviewer="carol", verdict="changes_requested", note="please fix").status_code == 201
    _as(app, "bob")
    assert app.get("/api/reviews/inbox").json() == {"requests": []}


def test_changes_requested_needs_note_and_invalid_body_is_rejected(app: Any) -> None:
    request_id = _request(app).json()["request_id"]
    missing_note = _attest(app, request_id, verdict="changes_requested", note=" ")
    assert missing_note.status_code == 409
    assert missing_note.json()["detail"]["error_type"] == "changes_requested_needs_note"
    assert _attest(app, request_id, verdict="approved").status_code == 422
    _as(app, "bob")
    assert (
        app.post(f"/api/reviews/{request_id}/attest", json={"verdict": "signed_off", "note": None, "payload_digest": "forged"}).status_code
        == 422
    )


def test_governance_off_refuses_mutations_and_returns_empty_inbox(app: Any) -> None:
    app.app.state.settings = app.app.state.settings.model_copy(update={"workflow_governance": "off"})
    assert _request(app).json()["detail"]["error_type"] == "workflow_governance_off"
    _as(app, "bob")
    assert app.get("/api/reviews/inbox").json() == {"requests": []}
    assert _attest(app, "missing").json()["detail"]["error_type"] == "workflow_governance_off"
    assert app.post("/api/reviews/missing/cancel").json()["detail"]["error_type"] == "workflow_governance_off"
    assert app.app.state.auth_audit_recorder.calls == []


def test_cancel_only_requester_and_missing_request_returns_404(app: Any) -> None:
    request_id = _request(app).json()["request_id"]
    _as(app, "bob")
    assert app.post(f"/api/reviews/{request_id}/cancel").status_code == 404
    _as(app, "alice")
    cancelled = app.post(f"/api/reviews/{request_id}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["open"] is False
    assert app.post(f"/api/reviews/{request_id}/cancel").json()["detail"]["error_type"] == "review_request_already_closed"
    assert _attest(app, "missing").status_code == 404
