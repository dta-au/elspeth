"""Local credential administration follows live deployment-wide authority."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import OperationalError

from elspeth.web.coordination import identity_authority
from elspeth.web.coordination.identity_authority import IdentityAdminActor, RepositoryIdentityAuthority

from .test_people_routes import _admit, _bearer, _build, _claims, _client

pytestmark = pytest.mark.asyncio


async def _assert_credential_routes_hidden(client: AsyncClient, headers: dict[str, str]) -> None:
    responses = [
        await client.get("/api/auth/admin/users", headers=headers),
        await client.post("/api/auth/admin/users", headers=headers, json={"username": "new-person", "display_name": "New Person"}),
        await client.post("/api/auth/admin/users/jane/reset-password", headers=headers),
        await client.request("DELETE", "/api/auth/admin/users/jane", headers=headers, json={"reason": "remove account"}),
    ]
    for response in responses:
        assert response.status_code == 404, response.text


@pytest.mark.parametrize("dev_admin_user", [None, "devadmin"])
async def test_ordinary_local_administrator_can_reset_and_delete_credentials(tmp_path: Path, dev_admin_user: str | None) -> None:
    harness = _build(tmp_path, dev_admin_user=dev_admin_user)
    async with _client(harness.app) as client:
        headers = await _bearer(client, "root")
        reset = await client.post("/api/auth/admin/users/jane/reset-password", headers=headers)
        assert reset.status_code == 200, reset.text
        assert reset.headers["cache-control"] == "no-store"
        assert reset.json()["user_id"] == "jane"
        password = reset.json()["password"]
        logged_in = await client.post("/api/auth/login", json={"username": "jane", "password": password})
        assert logged_in.status_code == 200, logged_in.text

        deleted = await client.request("DELETE", "/api/auth/admin/users/jane", headers=headers, json={"reason": "left the team"})
        assert deleted.status_code == 204, deleted.text
        after = await client.post("/api/auth/login", json={"username": "jane", "password": password})
        assert after.status_code == 401, after.text


async def test_local_nonadministrator_cannot_manage_credentials(tmp_path: Path) -> None:
    harness = _build(tmp_path)
    async with _client(harness.app) as client:
        headers = await _bearer(client, "nobody")
        capabilities = await client.get("/api/auth/admin/people/capabilities", headers=headers)
        assert capabilities.status_code == 200
        assert capabilities.json()["local_accounts"] is False
        await _assert_credential_routes_hidden(client, headers)


async def test_revocation_removes_local_account_authority_on_the_same_token(tmp_path: Path) -> None:
    harness = _build(tmp_path)
    actor = IdentityAdminActor(identity_id=harness.root_identity_id, on_behalf_of=None, console_request_id=None)
    administrator = _admit(harness.authority, _claims("nobody"), activate=True)
    grant = harness.authority.grant_role(
        actor=actor, identity_id=administrator, role="admin", scope=None, expires_at=None, note=None, record=lambda _event: None
    )
    async with _client(harness.app) as client:
        headers = await _bearer(client, "nobody")
        before = await client.get("/api/auth/admin/people/capabilities", headers=headers)
        assert before.status_code == 200
        assert before.json()["local_accounts"] is True
        assert (await client.get("/api/auth/admin/users", headers=headers)).status_code == 200

        harness.authority.revoke_role(actor=actor, role_id=grant.role_id, note=None, record=lambda _event: None)

        after = await client.get("/api/auth/admin/people/capabilities", headers=headers)
        assert after.status_code == 200
        assert after.json()["identity_admin"] is False
        assert after.json()["local_accounts"] is False
        await _assert_credential_routes_hidden(client, headers)


async def test_scoped_administrator_cannot_manage_local_credentials(tmp_path: Path) -> None:
    harness = _build(tmp_path)
    administrator = _admit(harness.authority, _claims("nobody"), activate=True)
    harness.authority.grant_role(
        actor=IdentityAdminActor(identity_id=harness.root_identity_id, on_behalf_of=None, console_request_id=None),
        identity_id=administrator,
        role="admin",
        scope="team:example",
        expires_at=None,
        note=None,
        record=lambda _event: None,
    )
    async with _client(harness.app) as client:
        headers = await _bearer(client, "nobody")
        capabilities = await client.get("/api/auth/admin/people/capabilities", headers=headers)
        assert capabilities.status_code == 200
        assert capabilities.json()["local_accounts"] is False
        await _assert_credential_routes_hidden(client, headers)


async def test_expired_admin_grant_cannot_manage_local_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _build(tmp_path)
    administrator = _admit(harness.authority, _claims("nobody"), activate=True)
    expires_at = datetime.now(UTC) + timedelta(hours=1)
    harness.authority.grant_role(
        actor=IdentityAdminActor(identity_id=harness.root_identity_id, on_behalf_of=None, console_request_id=None),
        identity_id=administrator,
        role="admin",
        scope=None,
        expires_at=expires_at,
        note=None,
        record=lambda _event: None,
    )
    async with _client(harness.app) as client:
        headers = await _bearer(client, "nobody")
        before = await client.get("/api/auth/admin/people/capabilities", headers=headers)
        assert before.status_code == 200
        assert before.json()["local_accounts"] is True

        # Advance the authority's database-clock boundary, leaving the bearer valid.
        monkeypatch.setattr(identity_authority, "_database_clock_value", lambda _value: expires_at + timedelta(seconds=1))
        after = await client.get("/api/auth/admin/people/capabilities", headers=headers)
        assert after.status_code == 200
        assert after.json()["local_accounts"] is False
        await _assert_credential_routes_hidden(client, headers)


@pytest.mark.parametrize("provider", ["oidc", "entra", "google", "vanguard"])
async def test_external_auth_deployment_hides_credentials_even_from_admin(tmp_path: Path, provider: str) -> None:
    harness = _build(tmp_path, dev_admin_user=None)
    async with _client(harness.app) as client:
        headers = await _bearer(client, "root")
        # Keep real principal authentication while exercising the deployment gate.
        harness.app.state.settings = harness.app.state.settings.model_copy(update={"auth_provider": provider})
        capabilities = await client.get("/api/auth/admin/people/capabilities", headers=headers)
        assert capabilities.status_code == 200
        assert capabilities.json()["identity_admin"] is True
        assert capabilities.json()["local_accounts"] is False
        await _assert_credential_routes_hidden(client, headers)


async def test_disabled_administrator_cannot_reuse_bearer_for_credentials(tmp_path: Path) -> None:
    harness = _build(tmp_path)
    actor = IdentityAdminActor(identity_id=harness.root_identity_id, on_behalf_of=None, console_request_id=None)
    administrator = _admit(harness.authority, _claims("nobody"), activate=True)
    harness.authority.grant_role(
        actor=actor, identity_id=administrator, role="admin", scope=None, expires_at=None, note=None, record=lambda _event: None
    )
    async with _client(harness.app) as client:
        headers = await _bearer(client, "nobody")
        before = await client.get("/api/auth/admin/people/capabilities", headers=headers)
        assert before.status_code == 200
        assert before.json()["local_accounts"] is True
        harness.authority.disable_identity(actor=actor, identity_id=administrator, reason="access removed", record=lambda _event: None)
        denied = await client.post("/api/auth/admin/users", headers=headers, json={"username": "new-person", "display_name": "New Person"})
        assert denied.status_code == 401, denied.text


async def test_ordinary_administrator_cannot_delete_own_credentials(tmp_path: Path) -> None:
    harness = _build(tmp_path)
    async with _client(harness.app) as client:
        headers = await _bearer(client, "root")
        response = await client.request("DELETE", "/api/auth/admin/users/root", headers=headers, json={"reason": "remove account"})
        assert response.status_code == 400, response.text
        assert (await client.get("/api/auth/admin/users", headers=headers)).status_code == 200


async def test_unavailable_authority_never_grants_local_account_creation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _build(tmp_path)

    def unavailable(_self: RepositoryIdentityAuthority) -> frozenset[str]:
        raise OperationalError("SELECT", {}, Exception("connection refused"))

    async with _client(harness.app) as client:
        headers = await _bearer(client, "root")
        monkeypatch.setattr(RepositoryIdentityAuthority, "active_human_admin_ids", unavailable)
        capabilities = await client.get("/api/auth/admin/people/capabilities", headers=headers)
        assert capabilities.status_code == 503, capabilities.text
        accounts = await client.get("/api/auth/admin/users", headers=headers)
        assert accounts.status_code == 503, accounts.text
        created = await client.post("/api/auth/admin/users", headers=headers, json={"username": "new-person", "display_name": "New Person"})
        assert created.status_code == 503, created.text
        assert "new-person" not in {account.user_id for account in harness.provider.list_users()}
