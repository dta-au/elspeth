"""/api/auth/admin/{identities,roles,relationships} -- the admission path (identity sprint step D).

The authority under the routes is REAL (a file-backed sessions store), so the
refusals these tests observe are the authority's own, translated. The audit
writer is a recording fake: what these tests pin is that every mutation hands
the authority a record callback and that the callback fires with the fields
the trail needs; that the row is written INSIDE the transaction is the
authority's contract, pinned where it lives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from elspeth.web.auth.identity_admin_routes import create_identity_admin_router
from elspeth.web.auth.models import IdentityClaims
from elspeth.web.auth.routes import create_auth_router
from elspeth.web.config import WebSettings
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.middleware.request_id import RequestIdMiddleware
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import initialize_session_schema

from .conftest import build_local_auth_provider

pytestmark = pytest.mark.asyncio

_QUOTA_TOKENS = 100_000
_QUOTA_BYTES = 1_000_000


@dataclass
class _AuditCall:
    method: str
    request_bound: bool
    kwargs: dict[str, Any]


@dataclass
class _RecordingAuditWriter:
    """Every ``AuthAuditWriter`` member, explicit: the admin ones record, the login ones are inert here."""

    calls: list[_AuditCall] = field(default_factory=list)

    def _note(self, method: str, request: Request | None, kwargs: dict[str, Any]) -> None:
        self.calls.append(_AuditCall(method=method, request_bound=request is not None, kwargs=kwargs))

    def record_login_success_and_token_issued(self, request: Request, **kwargs: Any) -> None:
        return None

    def record_login_success(self, request: Request, **kwargs: Any) -> None:
        return None

    def record_login_failure(self, request: Request, **kwargs: Any) -> None:
        return None

    def record_token_issued(self, request: Request, **kwargs: Any) -> None:
        return None

    def record_auth_failure(self, request: Request, **kwargs: Any) -> None:
        return None

    def record_identity_admitted(self, **kwargs: Any) -> None:
        return None

    def record_identity_retired(self, **kwargs: Any) -> None:
        return None

    def record_logout(self, request: Request, **kwargs: Any) -> None:
        self._note("record_logout", request, kwargs)

    def record_identity_activated(self, request: Request | None, **kwargs: Any) -> None:
        self._note("record_identity_activated", request, kwargs)

    def record_identity_enabled(self, request: Request | None, **kwargs: Any) -> None:
        self._note("record_identity_enabled", request, kwargs)

    def record_identity_disabled(self, request: Request | None, **kwargs: Any) -> None:
        self._note("record_identity_disabled", request, kwargs)

    def record_role_changed(self, request: Request | None, **kwargs: Any) -> None:
        self._note("record_role_changed", request, kwargs)

    def record_relationship_changed(self, request: Request | None, **kwargs: Any) -> None:
        self._note("record_relationship_changed", request, kwargs)

    def only(self, method: str) -> _AuditCall:
        matches = [call for call in self.calls if call.method == method]
        assert len(matches) == 1, [call.method for call in self.calls]
        return matches[0]


@dataclass(frozen=True)
class _Harness:
    app: FastAPI
    authority: RepositoryIdentityAuthority
    audit: _RecordingAuditWriter
    root_identity_id: str


def _local_claims(username: str) -> IdentityClaims:
    return IdentityClaims(provider="local", subject=username, username=username)


def _build(tmp_path: Path) -> _Harness:
    """A local-auth app with a REAL identity substrate and ``root`` bootstrapped as the first admin."""
    from elspeth.web.middleware.rate_limit import ComposerRateLimiter

    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(engine)
    authority = RepositoryIdentityAuthority(engine)
    provider = build_local_auth_provider(tmp_path / "auth.db", session_engine=engine, registration_open=True)
    for username in ("root", "alice", "bob", "carol"):
        provider.create_user(username, "password123", display_name=username.title())

    bootstrapped = authority.bootstrap_admin(
        claims=_local_claims("root"),
        note="test bootstrap",
        quota_tokens_per_day=None,
        quota_storage_bytes=None,
        record=lambda _event: None,
    )

    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)
    app.state.auth_provider = provider
    app.state.settings = WebSettings(
        auth_provider="local",
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
        quota_default_tokens_per_day=_QUOTA_TOKENS,
        quota_default_storage_bytes=_QUOTA_BYTES,
    )
    app.state.oidc_authorization_endpoint = None
    app.state.oidc_token_endpoint = None
    app.state.identity_authority = authority
    audit = _RecordingAuditWriter()
    app.state.auth_audit_recorder = audit
    app.state.auth_rate_limiter = ComposerRateLimiter(limit=100)
    app.include_router(create_auth_router())
    app.include_router(create_identity_admin_router())
    return _Harness(app=app, authority=authority, audit=audit, root_identity_id=bootstrapped.record.identity_id)


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _bearer(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post("/api/auth/login", json={"username": username, "password": "password123"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _pending(harness: _Harness, username: str) -> str:
    """Create ``username``'s identity row at the D12 wall without logging in (login would admit under open registration)."""
    outcome = harness.authority.ensure_identity(
        claims=_local_claims(username),
        activate=False,
        quota_tokens_per_day=None,
        quota_storage_bytes=None,
        record_admission=lambda *_args: None,
    )
    assert outcome.record.access_state == "pending"
    return outcome.record.identity_id


def _active(harness: _Harness, username: str) -> str:
    outcome = harness.authority.ensure_identity(
        claims=_local_claims(username),
        activate=True,
        quota_tokens_per_day=None,
        quota_storage_bytes=None,
        record_admission=lambda *_args: None,
    )
    assert outcome.record.access_state == "active"
    return outcome.record.identity_id


@pytest.fixture
def harness(tmp_path: Path) -> _Harness:
    return _build(tmp_path)


# ── who may call it ──────────────────────────────────────────────────────


async def test_unauthenticated_and_non_admin_callers_see_nothing(harness: _Harness) -> None:
    _active(harness, "bob")
    async with _client(harness.app) as client:
        anonymous = await client.get("/api/auth/admin/identities")
        assert anonymous.status_code == 401
        bob = await _bearer(client, "bob")
        for method, path, body in (
            ("GET", "/api/auth/admin/identities", None),
            ("GET", "/api/auth/admin/roles", None),
            ("GET", "/api/auth/admin/relationships", None),
            ("POST", "/api/auth/admin/identities/whoever/activate", {"role": "user", "note": "n"}),
            ("POST", "/api/auth/admin/roles", {"identity_id": "whoever", "role": "user"}),
        ):
            response = await client.request(method, path, headers=bob, json=body)
            assert response.status_code == 404, (method, path, response.text)
    assert harness.audit.calls == []


async def test_a_revoked_admin_is_refused_on_the_next_request(harness: _Harness) -> None:
    """Membership is checked per request, never cached (spec §Routes)."""
    carol_id = _active(harness, "carol")
    async with _client(harness.app) as client:
        root = await _bearer(client, "root")
        granted = await client.post("/api/auth/admin/roles", headers=root, json={"identity_id": carol_id, "role": "admin"})
        assert granted.status_code == 201, granted.text
        carol = await _bearer(client, "carol")
        assert (await client.get("/api/auth/admin/identities", headers=carol)).status_code == 200
        revoked = await client.post(f"/api/auth/admin/roles/{granted.json()['role_id']}/revoke", headers=root, json={})
        assert revoked.status_code == 200, revoked.text
        assert (await client.get("/api/auth/admin/identities", headers=carol)).status_code == 404


async def test_console_provenance_from_a_human_is_refused_and_leaves_no_row(harness: _Harness) -> None:
    alice_id = _pending(harness, "alice")
    async with _client(harness.app) as client:
        root = await _bearer(client, "root")
        response = await client.post(
            f"/api/auth/admin/identities/{alice_id}/activate",
            headers=root,
            json={"role": "user", "note": "n", "on_behalf_of": "someone", "console_request_id": "req-1"},
        )
    assert response.status_code == 404
    assert harness.audit.calls == []
    assert harness.authority.read_identity(identity_id=alice_id).access_state == "pending"


# ── identities ───────────────────────────────────────────────────────────


async def test_the_queue_lists_pending_rows_as_subject_and_organisation_only(harness: _Harness) -> None:
    _pending(harness, "alice")
    async with _client(harness.app) as client:
        root = await _bearer(client, "root")
        response = await client.get("/api/auth/admin/identities", headers=root)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["access_state"] == "pending"
    assert body["active_human_admin_count"] == 1
    (row,) = body["identities"]
    assert row["subject"] == "alice"
    assert row["access_state"] == "pending"
    assert row["username"] is None and row["display_name"] is None and row["email"] is None
    assert row["last_login_at"] is None
    assert "raw_claims_json" not in row


async def test_activation_admits_with_a_role_and_a_quota_and_records_it_before_answering(harness: _Harness) -> None:
    alice_id = _pending(harness, "alice")
    async with _client(harness.app) as client:
        root = await _bearer(client, "root")
        response = await client.post(
            f"/api/auth/admin/identities/{alice_id}/activate",
            headers={**root, "x-request-id": "activate-alice"},
            json={"role": "user", "note": "approved by the section lead"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["identity"]["access_state"] == "active"
        assert body["identity"]["username"] == "alice"
        assert body["identity"]["activated_by_identity_id"] == harness.root_identity_id
        assert body["role"]["role"] == "user"
        assert body["quota_written"] is True

        # She can log in now, and the active list shows her in full.
        await _bearer(client, "alice")
        active = await client.get("/api/auth/admin/identities", headers=root, params={"access_state": "active"})
        assert {row["username"] for row in active.json()["identities"]} == {"root", "alice"}

    call = harness.audit.only("record_identity_activated")
    assert call.request_bound
    assert call.kwargs["identity_id"] == alice_id
    assert call.kwargs["username"] == "alice"
    assert call.kwargs["actor_identity_id"] == harness.root_identity_id
    assert call.kwargs["cause"] == "admin_activation"
    assert call.kwargs["note"] == "approved by the section lead"
    assert call.kwargs["role"] == "user" and call.kwargs["role_id"] == body["role"]["role_id"]
    assert (call.kwargs["tokens_per_day"], call.kwargs["storage_bytes"]) == (_QUOTA_TOKENS, _QUOTA_BYTES)
    assert (call.kwargs["on_behalf_of"], call.kwargs["console_request_id"]) == (None, None)


async def test_activating_an_active_or_unknown_identity_is_refused_by_the_authority(harness: _Harness) -> None:
    bob_id = _active(harness, "bob")
    async with _client(harness.app) as client:
        root = await _bearer(client, "root")
        already = await client.post(f"/api/auth/admin/identities/{bob_id}/activate", headers=root, json={"role": "user", "note": "n"})
        assert already.status_code == 409
        assert already.json()["detail"]["refusal"] == "identity_not_pending"
        missing = await client.post("/api/auth/admin/identities/no-such-id/activate", headers=root, json={"role": "user", "note": "n"})
        assert missing.status_code == 404
        assert missing.json()["detail"]["refusal"] == "identity_not_found"
    assert harness.audit.calls == []


async def test_pre_provision_creates_an_active_row_before_first_login(harness: _Harness) -> None:
    async with _client(harness.app) as client:
        root = await _bearer(client, "root")
        response = await client.post(
            "/api/auth/admin/identities",
            headers=root,
            json={"provider": "local", "subject": "dave", "role": "reviewer", "note": "joins next week", "organisation_id": "12345678901"},
        )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["identity"]["access_state"] == "active"
    assert body["identity"]["subject"] == "dave"
    assert body["identity"]["organisation_id"] == "12345678901"
    assert body["identity"]["pre_provisioned_at"] is not None
    assert body["role"]["role"] == "reviewer"
    call = harness.audit.only("record_identity_activated")
    assert call.kwargs["cause"] == "pre_provision"


async def test_disable_and_enable_round_trip_with_their_rows(harness: _Harness) -> None:
    bob_id = _active(harness, "bob")
    async with _client(harness.app) as client:
        root = await _bearer(client, "root")
        disabled = await client.post(f"/api/auth/admin/identities/{bob_id}/disable", headers=root, json={"reason": "left the agency"})
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["identity"]["access_state"] == "disabled"
        assert disabled.json()["identity"]["disable_reason"] == "left the agency"
        assert disabled.json()["revoked_relationship_ids"] == []
        enabled = await client.post(f"/api/auth/admin/identities/{bob_id}/enable", headers=root, json={"note": "came back"})
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["access_state"] == "active"
        assert enabled.json()["disabled_at"] is None
    disable_call = harness.audit.only("record_identity_disabled")
    assert disable_call.kwargs["reason"] == "left the agency"
    assert disable_call.kwargs["revoked_relationship_ids"] == ()
    enable_call = harness.audit.only("record_identity_enabled")
    assert enable_call.kwargs["note"] == "came back"
    assert enable_call.kwargs["actor_identity_id"] == harness.root_identity_id


async def test_an_admin_cannot_disable_themselves(harness: _Harness) -> None:
    async with _client(harness.app) as client:
        root = await _bearer(client, "root")
        response = await client.post(
            f"/api/auth/admin/identities/{harness.root_identity_id}/disable", headers=root, json={"reason": "oops"}
        )
    assert response.status_code == 409
    assert response.json()["detail"]["refusal"] == "cannot_disable_self"
    assert harness.audit.calls == []
    assert harness.authority.read_identity(identity_id=harness.root_identity_id).access_state == "active"


# ── roles ────────────────────────────────────────────────────────────────


async def test_roles_are_granted_listed_and_revoked_with_rows(harness: _Harness) -> None:
    bob_id = _active(harness, "bob")
    async with _client(harness.app) as client:
        root = await _bearer(client, "root")
        granted = await client.post(
            "/api/auth/admin/roles",
            headers=root,
            json={"identity_id": bob_id, "role": "approver", "note": "section lead", "expires_at": "2027-01-01T00:00:00Z"},
        )
        assert granted.status_code == 201, granted.text
        grant = granted.json()
        assert grant["role"] == "approver" and grant["identity_id"] == bob_id
        assert grant["granted_by_identity_id"] == harness.root_identity_id
        assert grant["expires_at"].startswith("2027-01-01")

        listed = await client.get("/api/auth/admin/roles", headers=root, params={"identity_id": bob_id})
        assert [row["role"] for row in listed.json()["roles"]] == ["approver"]

        revoked = await client.post(f"/api/auth/admin/roles/{grant['role_id']}/revoke", headers=root, json={"note": "rotated"})
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["revoked_at"] is not None

        after = await client.get("/api/auth/admin/roles", headers=root, params={"identity_id": bob_id})
        assert after.json()["roles"] == []
        with_revoked = await client.get("/api/auth/admin/roles", headers=root, params={"identity_id": bob_id, "include_revoked": "true"})
        assert len(with_revoked.json()["roles"]) == 1

    changes = [call for call in harness.audit.calls if call.method == "record_role_changed"]
    assert [call.kwargs["change"] for call in changes] == ["granted", "revoked"]
    assert all(call.kwargs["role_id"] == grant["role_id"] and call.kwargs["identity_id"] == bob_id for call in changes)
    assert changes[0].kwargs["note"] == "section lead"
    assert changes[1].kwargs["note"] == "rotated"


async def test_a_workload_role_on_an_admin_is_refused(harness: _Harness) -> None:
    """R8: admin is never combined with a workload role."""
    async with _client(harness.app) as client:
        root = await _bearer(client, "root")
        response = await client.post("/api/auth/admin/roles", headers=root, json={"identity_id": harness.root_identity_id, "role": "user"})
    assert response.status_code == 409
    assert response.json()["detail"]["refusal"] == "role_forbidden_for_identity"


# ── relationships ────────────────────────────────────────────────────────


async def test_relationships_are_asserted_listed_and_revoked_with_rows(harness: _Harness) -> None:
    bob_id = _active(harness, "bob")
    carol_id = _active(harness, "carol")
    async with _client(harness.app) as client:
        root = await _bearer(client, "root")
        approver = await client.post("/api/auth/admin/roles", headers=root, json={"identity_id": bob_id, "role": "approver"})
        assert approver.status_code == 201, approver.text
        asserted = await client.post(
            "/api/auth/admin/relationships",
            headers=root,
            json={"from_identity_id": bob_id, "to_identity_id": carol_id, "relationship_type": "approver", "note": "org chart"},
        )
        assert asserted.status_code == 201, asserted.text
        edge = asserted.json()
        assert (edge["from_identity_id"], edge["to_identity_id"]) == (bob_id, carol_id)
        assert edge["asserted_by_identity_id"] == harness.root_identity_id

        listed = await client.get("/api/auth/admin/relationships", headers=root, params={"identity_id": carol_id})
        assert [row["relationship_id"] for row in listed.json()["relationships"]] == [edge["relationship_id"]]

        revoked = await client.post(f"/api/auth/admin/relationships/{edge['relationship_id']}/revoke", headers=root, json={})
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["revoked_by_identity_id"] == harness.root_identity_id

    changes = [call for call in harness.audit.calls if call.method == "record_relationship_changed"]
    assert [call.kwargs["change"] for call in changes] == ["asserted", "revoked"]
    assert all(call.kwargs["to_identity_id"] == carol_id and call.kwargs["from_identity_id"] == bob_id for call in changes)


async def test_an_approver_edge_needs_the_approver_role_and_two_distinct_identities(harness: _Harness) -> None:
    bob_id = _active(harness, "bob")
    carol_id = _active(harness, "carol")
    async with _client(harness.app) as client:
        root = await _bearer(client, "root")
        no_role = await client.post(
            "/api/auth/admin/relationships",
            headers=root,
            json={"from_identity_id": bob_id, "to_identity_id": carol_id, "relationship_type": "approver"},
        )
        assert no_role.status_code == 409
        assert no_role.json()["detail"]["refusal"] == "approver_role_required"
        self_edge = await client.post(
            "/api/auth/admin/relationships",
            headers=root,
            json={"from_identity_id": bob_id, "to_identity_id": bob_id, "relationship_type": "approver"},
        )
        assert self_edge.status_code == 409
        assert self_edge.json()["detail"]["refusal"] == "relationship_self_edge"
    assert harness.audit.calls == []


async def test_bodies_are_strict(harness: _Harness) -> None:
    async with _client(harness.app) as client:
        root = await _bearer(client, "root")
        extra = await client.post("/api/auth/admin/roles", headers=root, json={"identity_id": "x", "role": "user", "surprise": 1})
        assert extra.status_code == 422
        bad_role = await client.post("/api/auth/admin/roles", headers=root, json={"identity_id": "x", "role": "superuser"})
        assert bad_role.status_code == 422
        empty_note = await client.post("/api/auth/admin/identities/x/activate", headers=root, json={"role": "user", "note": ""})
        assert empty_note.status_code == 422
    assert harness.audit.calls == []
