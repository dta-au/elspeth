"""The people directory -- /api/auth/admin/people.

Two independent capabilities front two stores. Most of what can go wrong here
is a capability leaking across that seam, so the cases are organised around
the four callers: neither, local accounts only, identity admin only, both.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError

from elspeth.web.auth.admin_routes import create_dev_admin_router
from elspeth.web.auth.identity_admin_routes import create_identity_admin_router
from elspeth.web.auth.local import LocalAuthProvider
from elspeth.web.auth.models import IdentityClaims
from elspeth.web.auth.people_routes import create_people_router
from elspeth.web.auth.routes import create_auth_router
from elspeth.web.config import WebSettings
from elspeth.web.coordination.approval_lifecycle_authority import RepositoryApprovalLifecycleAuthority
from elspeth.web.coordination.identity_authority import IdentityAdminActor, RepositoryIdentityAuthority
from elspeth.web.middleware.request_id import RequestIdMiddleware
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import initialize_session_schema

from .conftest import build_local_auth_provider

pytestmark = pytest.mark.asyncio

_PASSWORD = "password123"


class _InertAuditWriter:
    """The directory is read-only; login is the only path here that audits."""

    def record_login_success_and_token_issued(self, *args: Any, **kwargs: Any) -> None:
        return None

    def record_login_failure(self, *args: Any, **kwargs: Any) -> None:
        return None

    def record_token_issued(self, *args: Any, **kwargs: Any) -> None:
        return None

    def record_auth_failure(self, *args: Any, **kwargs: Any) -> None:
        return None

    def record_identity_activated(self, *args: Any, **kwargs: Any) -> None:
        # One case sets access up through the existing pre-provision route.
        return None


@dataclass(frozen=True)
class _Harness:
    app: FastAPI
    authority: RepositoryIdentityAuthority
    provider: LocalAuthProvider
    root_identity_id: str


def _claims(username: str, **overrides: Any) -> IdentityClaims:
    values: dict[str, Any] = {"provider": "local", "subject": username, "username": username}
    values.update(overrides)
    return IdentityClaims(**values)


def _admit(authority: RepositoryIdentityAuthority, claims: IdentityClaims, *, activate: bool) -> str:
    return authority.ensure_identity(
        claims=claims,
        activate=activate,
        quota_tokens_per_day=None,
        quota_storage_bytes=None,
        identity_dormancy_days=90,
        record_admission=lambda *_args: None,
        record_rebound=lambda *_args: None,
        record_dormant=lambda *_args: None,
    ).record.identity_id


def _build(tmp_path: Path, *, dev_admin_user: str | None = "devadmin") -> _Harness:
    """``root`` holds identity admin, ``devadmin`` the credential flag, ``both`` each, ``nobody`` neither."""
    from elspeth.web.middleware.rate_limit import ComposerRateLimiter

    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(engine)
    authority = RepositoryIdentityAuthority(engine, lifecycle_effect=RepositoryApprovalLifecycleAuthority().apply)
    provider = build_local_auth_provider(tmp_path / "auth.db", session_engine=engine, registration_open=True)
    for username, display in (("root", "Root Admin"), ("devadmin", "Dev Admin"), ("nobody", "No Body"), ("jane", "Jane Doe")):
        provider.create_user(username, _PASSWORD, display_name=display, email=f"{username}@corp.example")
    root = authority.bootstrap_admin(
        claims=_claims("root", display_name="Root Admin"),
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
        dev_admin_user=dev_admin_user,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
    )
    app.state.oidc_authorization_endpoint = None
    app.state.oidc_token_endpoint = None
    app.state.identity_authority = authority
    app.state.auth_audit_recorder = _InertAuditWriter()
    app.state.auth_rate_limiter = ComposerRateLimiter(limit=100)
    app.include_router(create_auth_router())
    app.include_router(create_dev_admin_router())
    app.include_router(create_identity_admin_router())
    app.include_router(create_people_router())
    return _Harness(app=app, authority=authority, provider=provider, root_identity_id=root.record.identity_id)


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _bearer(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _make_both(harness: _Harness) -> None:
    """Give the dev admin a live identity ``admin`` role as well."""
    dev_id = _admit(harness.authority, _claims("devadmin", display_name="Dev Admin"), activate=True)
    harness.authority.grant_role(
        actor=IdentityAdminActor(identity_id=harness.root_identity_id, on_behalf_of=None, console_request_id=None),
        identity_id=dev_id,
        role="admin",
        scope=None,
        expires_at=None,
        note=None,
        record=lambda _event: None,
    )


def _keys(body: dict[str, Any]) -> list[str]:
    return [person["key"] for person in body["people"]]


# ── Neither capability ───────────────────────────────────────────────────


async def test_a_caller_with_neither_capability_sees_no_directory(tmp_path) -> None:
    harness = _build(tmp_path)
    async with _client(harness.app) as client:
        headers = await _bearer(client, "nobody")
        for path in ("", "/labels?identity_id=x", f"/identity/{harness.root_identity_id}", "/local/jane"):
            assert (await client.get(f"/api/auth/admin/people{path}", headers=headers)).status_code == 404, path
        # Their OWN capabilities are theirs to read, and say exactly that.
        capabilities = await client.get("/api/auth/admin/people/capabilities", headers=headers)
        assert capabilities.status_code == 200
        assert capabilities.headers["cache-control"] == "no-store"
        assert (capabilities.json()["identity_admin"], capabilities.json()["local_accounts"]) == (False, False)
        # No quota setting is configured in this harness, and the panel is told so.
        assert capabilities.json()["quotas_enabled"] is False
        assert (await client.get("/api/auth/admin/people")).status_code == 401


# ── Local accounts only ──────────────────────────────────────────────────


async def test_a_local_only_administrator_sees_accounts_and_no_identity_data(tmp_path) -> None:
    harness = _build(tmp_path)
    async with _client(harness.app) as client:
        headers = await _bearer(client, "devadmin")
        response = await client.get("/api/auth/admin/people", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["capabilities"]["local_accounts"] is True
        assert body["capabilities"]["identity_admin"] is False
        assert body["active_human_admin_count"] is None
        assert sorted(_keys(body)) == ["local:devadmin", "local:jane", "local:nobody", "local:root"]
        for person in body["people"]:
            assert person["record_type"] == "local_account"
            # Nothing is CLAIMED about access the caller cannot read: root is an
            # administrator, and this caller must not learn it from a label.
            assert person["access"] == "not_visible"
            assert person["actions"] == {
                "manage_access": False,
                "manage_credentials": True,
                "set_up_access": False,
                "is_self": person["key"] == "local:devadmin",
            }
            assert "identity" not in person

        assert (await client.get(f"/api/auth/admin/people/identity/{harness.root_identity_id}", headers=headers)).status_code == 404
        assert (
            await client.get(f"/api/auth/admin/people/labels?identity_id={harness.root_identity_id}", headers=headers)
        ).status_code == 404
        refused = await client.get("/api/auth/admin/people?status=active", headers=headers)
        assert refused.status_code == 422
        assert refused.json()["detail"]["capability"] == "identity_admin"
        selected = await client.get("/api/auth/admin/people/local/root", headers=headers)
        assert selected.json()["person"]["access"] == "not_visible"


# ── Identity admin only ──────────────────────────────────────────────────


async def test_an_identity_only_administrator_sees_no_credential_inventory(tmp_path) -> None:
    harness = _build(tmp_path)
    async with _client(harness.app) as client:
        headers = await _bearer(client, "root")
        response = await client.get("/api/auth/admin/people", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["active_human_admin_count"] == 1
        # ``jane`` has an account and has never signed in: no identity, so an
        # identity-only caller is not shown her at all.
        assert _keys(body) == [f"identity:{harness.root_identity_id}"]
        person = body["people"][0]
        assert person["local_account"] is None
        assert person["actions"]["manage_credentials"] is False
        assert person["actions"]["is_self"] is True

        assert (await client.get("/api/auth/admin/people/local/jane", headers=headers)).status_code == 404
        assert (await client.get("/api/auth/admin/people?status=not_set_up", headers=headers)).status_code == 422
        assert "password" not in response.text.lower()


async def test_pending_redaction_holds_through_search_labels_and_selection(tmp_path) -> None:
    harness = _build(tmp_path)
    pending_id = _admit(
        harness.authority,
        _claims("sub-771", provider="oidc", display_name="Sam Lee", email="sam@secret.example"),
        activate=False,
    )
    async with _client(harness.app) as client:
        headers = await _bearer(client, "root")
        assert _keys((await client.get("/api/auth/admin/people?q=Sam", headers=headers)).json()) == []
        assert _keys((await client.get("/api/auth/admin/people?q=secret.example", headers=headers)).json()) == []
        found = (await client.get("/api/auth/admin/people?q=sub-77&status=pending", headers=headers)).json()
        assert _keys(found) == [f"identity:{pending_id}"]
        assert found["people"][0]["identity"]["display_name"] is None

        label = (await client.get(f"/api/auth/admin/people/labels?identity_id={pending_id}", headers=headers)).json()["labels"][0]
        assert label["label"] == "sub-771"
        assert "Sam" not in label["detail"]
        selected = (await client.get(f"/api/auth/admin/people/identity/{pending_id}", headers=headers)).json()["person"]
        assert selected["identity"]["email"] is None


# ── Both ─────────────────────────────────────────────────────────────────


async def test_both_capabilities_merge_by_exact_local_correlation(tmp_path) -> None:
    harness = _build(tmp_path)
    _make_both(harness)
    # Same NAME, different provider: a different person, never merged with the local jane.
    oidc_jane = _admit(harness.authority, _claims("jane", provider="oidc", display_name="Jane Doe"), activate=True)
    async with _client(harness.app) as client:
        headers = await _bearer(client, "devadmin")
        body = (await client.get("/api/auth/admin/people", headers=headers)).json()
        by_key = {person["key"]: person for person in body["people"]}

        assert by_key["local:jane"]["access"] == "not_set_up"
        assert by_key["local:jane"]["actions"]["set_up_access"] is True
        assert by_key[f"identity:{oidc_jane}"]["local_account"] is None
        assert by_key[f"identity:{harness.root_identity_id}"]["local_account"]["username"] == "root"
        assert by_key[f"identity:{harness.root_identity_id}"]["actions"]["manage_credentials"] is True
        # Every local account appears exactly once: linked ones under their identity.
        assert "local:root" not in by_key and "local:devadmin" not in by_key

        only_unlinked = (await client.get("/api/auth/admin/people?status=not_set_up", headers=headers)).json()
        assert sorted(_keys(only_unlinked)) == ["local:jane", "local:nobody"]


async def test_setting_access_up_moves_a_person_to_their_identity_key(tmp_path) -> None:
    harness = _build(tmp_path)
    _make_both(harness)
    async with _client(harness.app) as client:
        headers = await _bearer(client, "devadmin")
        before = (await client.get("/api/auth/admin/people/local/jane", headers=headers)).json()["person"]
        assert before["key"] == "local:jane"
        provisioned = await client.post(
            "/api/auth/admin/identities",
            headers=headers,
            json={"provider": "local", "subject": "jane", "username": "jane", "role": "user", "note": "set up access"},
        )
        assert provisioned.status_code == 201, provisioned.text
        after = (await client.get("/api/auth/admin/people/local/jane", headers=headers)).json()["person"]
        assert after["key"] == f"identity:{provisioned.json()['identity']['identity_id']}"
        assert after["local_account"]["username"] == "jane"


async def test_a_label_borrows_the_linked_account_name_only_where_nothing_is_withheld(tmp_path) -> None:
    harness = _build(tmp_path)
    _make_both(harness)
    async with _client(harness.app) as client:
        headers = await _bearer(client, "devadmin")
        prepared = await client.post(
            "/api/auth/admin/identities",
            headers=headers,
            json={"provider": "local", "subject": "jane", "username": "jane", "role": "user", "note": "set up access"},
        )
        jane_id = prepared.json()["identity"]["identity_id"]
        # ``nobody`` has an account named "No Body" and a NEVER-ADMITTED pending identity.
        pending_id = _admit(harness.authority, _claims("nobody"), activate=False)

        labels = {
            label["identity_id"]: label
            for label in (
                await client.get(f"/api/auth/admin/people/labels?identity_id={jane_id}&identity_id={pending_id}", headers=headers)
            ).json()["labels"]
        }
        # Prepared ahead of first sign-in: no profile yet, so the account names her.
        assert labels[jane_id]["label"] == "Jane Doe"
        # Withheld on purpose: the account must not put the name back.
        assert labels[pending_id]["label"] == "nobody"
        assert "No Body" not in str(labels[pending_id])

        # An identity-only caller is never shown an account-sourced name.
        root_headers = await _bearer(client, "root")
        root_view = (await client.get(f"/api/auth/admin/people/labels?identity_id={jane_id}", headers=root_headers)).json()["labels"][0]
        assert root_view["label"] == "jane"


async def test_a_recycled_username_keeps_its_retired_history_separate(tmp_path) -> None:
    harness = _build(tmp_path)
    _make_both(harness)
    old_id = _admit(harness.authority, _claims("jane", display_name="Jane Doe"), activate=True)
    async with _client(harness.app) as client:
        headers = await _bearer(client, "devadmin")
        assert (await client.delete("/api/auth/admin/users/jane", headers=headers)).status_code == 204
        created = await client.post("/api/auth/admin/users", headers=headers, json={"username": "jane", "display_name": "Jane Newcomer"})
        assert created.status_code == 201

        body = (await client.get("/api/auth/admin/people?q=jane&status=all", headers=headers)).json()
        by_key = {person["key"]: person for person in body["people"]}
        # The newcomer has no access yet and inherits nothing; the old row is history.
        assert by_key["local:jane"]["access"] == "not_set_up"
        retired = by_key[f"identity:{old_id}"]
        assert retired["retired"] is True
        assert retired["local_account"] is None
        assert retired["identity"]["access_state"] == "disabled"


async def test_the_merged_roster_pages_without_repeating_or_dropping_anyone(tmp_path) -> None:
    harness = _build(tmp_path)
    _make_both(harness)
    for index in range(5):
        _admit(harness.authority, _claims(f"sso-{index}", provider="oidc", display_name=f"Person {index}"), activate=True)
    async with _client(harness.app) as client:
        headers = await _bearer(client, "devadmin")
        whole = (await client.get("/api/auth/admin/people?limit=100", headers=headers)).json()
        assert whole["has_more"] is False
        paged: list[str] = []
        offset = 0
        while True:
            page = (await client.get(f"/api/auth/admin/people?limit=2&offset={offset}", headers=headers)).json()
            paged.extend(_keys(page))
            if not page["has_more"]:
                break
            offset += 2
        assert paged == _keys(whole)
        assert len(set(paged)) == len(paged) == 9


async def test_search_reaches_past_the_first_page(tmp_path) -> None:
    harness = _build(tmp_path)
    for index in range(6):
        _admit(harness.authority, _claims(f"sso-{index}", provider="oidc", display_name=f"Aaron {index}"), activate=True)
    target = _admit(harness.authority, _claims("zed", provider="oidc", display_name="Zed Last"), activate=True)
    async with _client(harness.app) as client:
        headers = await _bearer(client, "root")
        first_page = (await client.get("/api/auth/admin/people?limit=2", headers=headers)).json()
        assert f"identity:{target}" not in _keys(first_page)
        assert _keys((await client.get("/api/auth/admin/people?limit=2&q=zed%20l", headers=headers)).json()) == [f"identity:{target}"]


# ── Source failure and authority loss ────────────────────────────────────


async def test_an_unreadable_credential_store_is_an_error_not_an_empty_source(tmp_path, monkeypatch) -> None:
    harness = _build(tmp_path)
    _make_both(harness)

    def broken() -> list[Any]:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(harness.provider, "list_users", broken)
    async with _client(harness.app) as client:
        headers = await _bearer(client, "devadmin")
        response = await client.get("/api/auth/admin/people", headers=headers)
    assert response.status_code == 503
    assert response.json()["detail"]["unavailable_source"] == "local_accounts"


async def test_an_unreadable_identity_store_is_an_error_not_an_empty_source(tmp_path, monkeypatch) -> None:
    harness = _build(tmp_path)

    def broken(_self: RepositoryIdentityAuthority, **_kwargs: Any) -> tuple[Any, ...]:
        raise OperationalError("SELECT", {}, Exception("connection refused"))

    async with _client(harness.app) as client:
        headers = await _bearer(client, "root")
        # The authority is slotted, so the seam is the class, not the instance.
        monkeypatch.setattr(RepositoryIdentityAuthority, "search_identities", broken)
        response = await client.get("/api/auth/admin/people", headers=headers)
    assert response.status_code == 503
    assert response.json()["detail"]["unavailable_source"] == "identities"


async def test_a_revoked_administrator_loses_the_identity_half_at_once(tmp_path) -> None:
    harness = _build(tmp_path)
    _make_both(harness)
    async with _client(harness.app) as client:
        headers = await _bearer(client, "devadmin")
        assert (await client.get("/api/auth/admin/people", headers=headers)).json()["capabilities"]["identity_admin"] is True
        role = next(
            grant
            for grant in harness.authority.list_roles(identity_id=None, include_revoked=False, limit=50, offset=0)
            if grant.role == "admin" and grant.identity_id != harness.root_identity_id
        )
        harness.authority.revoke_role(
            actor=IdentityAdminActor(identity_id=harness.root_identity_id, on_behalf_of=None, console_request_id=None),
            role_id=role.role_id,
            note=None,
            record=lambda _event: None,
        )
        after = (await client.get("/api/auth/admin/people", headers=headers)).json()
        # The same token, the next request: the permitted half stays, the other is gone.
        assert after["capabilities"] == {**after["capabilities"], "identity_admin": False, "local_accounts": True}
        assert all(person["record_type"] == "local_account" and person["access"] == "not_visible" for person in after["people"])
