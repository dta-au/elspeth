"""Tests for the dev-admin user management routes -- /api/auth/admin/users.

The surface exists only when WebSettings.dev_admin_user names a local-auth
user; every other configuration must 404 exactly like the hidden /login and
/register arms so probes cannot learn the surface exists.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from elspeth.web.auth.admin_routes import create_dev_admin_router
from elspeth.web.auth.local import LocalAuthProvider
from elspeth.web.auth.routes import create_auth_router
from elspeth.web.config import WebSettings
from elspeth.web.middleware.request_id import RequestIdMiddleware


class _NoopAuthAuditRecorder:
    def record_login_success_and_token_issued(self, *args, **kwargs) -> None:
        return None

    def record_login_failure(self, *args, **kwargs) -> None:
        return None

    def record_token_issued(self, *args, **kwargs) -> None:
        return None

    def record_auth_failure(self, *args, **kwargs) -> None:
        return None


def _create_test_app(provider, **settings_overrides) -> FastAPI:
    """Create a FastAPI app with the auth + dev-admin routers mounted."""
    from elspeth.web.middleware.rate_limit import ComposerRateLimiter

    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)
    app.state.auth_provider = provider
    app.state.settings = WebSettings(
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
        **settings_overrides,
    )
    app.state.oidc_authorization_endpoint = None
    app.state.oidc_token_endpoint = None
    app.state.auth_audit_recorder = _NoopAuthAuditRecorder()
    app.state.auth_rate_limiter = ComposerRateLimiter(limit=100)
    app.include_router(create_auth_router())
    app.include_router(create_dev_admin_router())
    return app


def _client_for(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _provider_with_admin(tmp_path) -> LocalAuthProvider:
    provider = LocalAuthProvider(db_path=tmp_path / "auth.db", secret_key="test-key")
    provider.create_user("john", "admin-password-1", display_name="John")
    return provider


async def _bearer(client: AsyncClient, username: str, password: str) -> dict[str, str]:
    response = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.asyncio
class TestDevAdminGuard:
    async def test_all_routes_404_when_flag_unset(self, tmp_path) -> None:
        provider = _provider_with_admin(tmp_path)
        app = _create_test_app(provider)

        async with _client_for(app) as client:
            headers = await _bearer(client, "john", "admin-password-1")
            assert (await client.get("/api/auth/admin/users", headers=headers)).status_code == 404
            assert (
                await client.post("/api/auth/admin/users", headers=headers, json={"username": "a", "display_name": "A"})
            ).status_code == 404
            assert (await client.post("/api/auth/admin/users/a/reset-password", headers=headers)).status_code == 404
            assert (await client.delete("/api/auth/admin/users/a", headers=headers)).status_code == 404

    async def test_routes_404_for_authenticated_non_admin(self, tmp_path) -> None:
        provider = _provider_with_admin(tmp_path)
        provider.create_user("mallory", "user-password-1", display_name="Mallory")
        app = _create_test_app(provider, dev_admin_user="john")

        async with _client_for(app) as client:
            headers = await _bearer(client, "mallory", "user-password-1")
            assert (await client.get("/api/auth/admin/users", headers=headers)).status_code == 404
            assert (
                await client.post("/api/auth/admin/users", headers=headers, json={"username": "a", "display_name": "A"})
            ).status_code == 404
            assert (await client.post("/api/auth/admin/users/john/reset-password", headers=headers)).status_code == 404
            assert (await client.delete("/api/auth/admin/users/john", headers=headers)).status_code == 404

    async def test_routes_404_without_credentials_when_disabled(self, tmp_path) -> None:
        provider = _provider_with_admin(tmp_path)
        app = _create_test_app(provider)

        async with _client_for(app) as client:
            assert (await client.get("/api/auth/admin/users")).status_code == 404

    async def test_routes_401_without_credentials_when_enabled(self, tmp_path) -> None:
        provider = _provider_with_admin(tmp_path)
        app = _create_test_app(provider, dev_admin_user="john")

        async with _client_for(app) as client:
            assert (await client.get("/api/auth/admin/users")).status_code == 401


@pytest.mark.asyncio
class TestListUsers:
    async def test_admin_lists_accounts(self, tmp_path) -> None:
        provider = _provider_with_admin(tmp_path)
        provider.create_user("alice", "user-password-1", display_name="Alice", email="alice@example.com")
        app = _create_test_app(provider, dev_admin_user="john")

        async with _client_for(app) as client:
            headers = await _bearer(client, "john", "admin-password-1")
            response = await client.get("/api/auth/admin/users", headers=headers)

        assert response.status_code == 200
        users = response.json()["users"]
        assert [user["user_id"] for user in users] == ["alice", "john"]
        assert users[0] == {
            "user_id": "alice",
            "display_name": "Alice",
            "email": "alice@example.com",
            "email_verified": True,
        }


@pytest.mark.asyncio
class TestCreateUser:
    async def test_creates_user_and_returns_generated_password_once(self, tmp_path) -> None:
        provider = _provider_with_admin(tmp_path)
        app = _create_test_app(provider, dev_admin_user="john")

        async with _client_for(app) as client:
            headers = await _bearer(client, "john", "admin-password-1")
            response = await client.post(
                "/api/auth/admin/users",
                headers=headers,
                json={"username": "alice", "display_name": "Alice", "email": "alice@example.com"},
            )
            assert response.status_code == 201
            body = response.json()
            assert body["user_id"] == "alice"
            generated = body["password"]
            assert len(generated) >= 16
            assert response.headers["Cache-Control"] == "no-store"

            # The generated password is live immediately.
            login = await client.post("/api/auth/login", json={"username": "alice", "password": generated})
            assert login.status_code == 200

    async def test_duplicate_username_conflicts(self, tmp_path) -> None:
        provider = _provider_with_admin(tmp_path)
        app = _create_test_app(provider, dev_admin_user="john")

        async with _client_for(app) as client:
            headers = await _bearer(client, "john", "admin-password-1")
            response = await client.post(
                "/api/auth/admin/users",
                headers=headers,
                json={"username": "john", "display_name": "Imposter"},
            )
        assert response.status_code == 409

    async def test_blank_username_rejected(self, tmp_path) -> None:
        provider = _provider_with_admin(tmp_path)
        app = _create_test_app(provider, dev_admin_user="john")

        async with _client_for(app) as client:
            headers = await _bearer(client, "john", "admin-password-1")
            response = await client.post(
                "/api/auth/admin/users",
                headers=headers,
                json={"username": "   ", "display_name": "A"},
            )
        assert response.status_code == 422


@pytest.mark.asyncio
class TestResetPassword:
    async def test_reset_invalidates_old_password_and_returns_new_one_once(self, tmp_path) -> None:
        provider = _provider_with_admin(tmp_path)
        provider.create_user("alice", "user-password-1", display_name="Alice")
        app = _create_test_app(provider, dev_admin_user="john")

        async with _client_for(app) as client:
            headers = await _bearer(client, "john", "admin-password-1")
            response = await client.post("/api/auth/admin/users/alice/reset-password", headers=headers)
            assert response.status_code == 200
            body = response.json()
            assert body["user_id"] == "alice"
            generated = body["password"]
            assert response.headers["Cache-Control"] == "no-store"

            old_login = await client.post("/api/auth/login", json={"username": "alice", "password": "user-password-1"})
            assert old_login.status_code == 401
            new_login = await client.post("/api/auth/login", json={"username": "alice", "password": generated})
            assert new_login.status_code == 200

    async def test_reset_unknown_user_404s(self, tmp_path) -> None:
        provider = _provider_with_admin(tmp_path)
        app = _create_test_app(provider, dev_admin_user="john")

        async with _client_for(app) as client:
            headers = await _bearer(client, "john", "admin-password-1")
            response = await client.post("/api/auth/admin/users/ghost/reset-password", headers=headers)
        assert response.status_code == 404


@pytest.mark.asyncio
class TestDeleteUser:
    async def test_delete_removes_account(self, tmp_path) -> None:
        provider = _provider_with_admin(tmp_path)
        provider.create_user("alice", "user-password-1", display_name="Alice")
        app = _create_test_app(provider, dev_admin_user="john")

        async with _client_for(app) as client:
            headers = await _bearer(client, "john", "admin-password-1")
            response = await client.delete("/api/auth/admin/users/alice", headers=headers)
            assert response.status_code == 204

            login = await client.post("/api/auth/login", json={"username": "alice", "password": "user-password-1"})
            assert login.status_code == 401

    async def test_admin_cannot_delete_own_account(self, tmp_path) -> None:
        provider = _provider_with_admin(tmp_path)
        app = _create_test_app(provider, dev_admin_user="john")

        async with _client_for(app) as client:
            headers = await _bearer(client, "john", "admin-password-1")
            response = await client.delete("/api/auth/admin/users/john", headers=headers)
        assert response.status_code == 400
        assert provider.list_users()[0].user_id == "john"

    async def test_delete_unknown_user_404s(self, tmp_path) -> None:
        provider = _provider_with_admin(tmp_path)
        app = _create_test_app(provider, dev_admin_user="john")

        async with _client_for(app) as client:
            headers = await _bearer(client, "john", "admin-password-1")
            response = await client.delete("/api/auth/admin/users/ghost", headers=headers)
        assert response.status_code == 404


@pytest.mark.asyncio
class TestMeDevAdminFlag:
    async def test_me_reports_dev_admin_for_the_flagged_user(self, tmp_path) -> None:
        provider = _provider_with_admin(tmp_path)
        app = _create_test_app(provider, dev_admin_user="john")

        async with _client_for(app) as client:
            headers = await _bearer(client, "john", "admin-password-1")
            response = await client.get("/api/auth/me", headers=headers)
        assert response.status_code == 200
        assert response.json()["dev_admin"] is True

    async def test_me_reports_false_for_other_users(self, tmp_path) -> None:
        provider = _provider_with_admin(tmp_path)
        provider.create_user("alice", "user-password-1", display_name="Alice")
        app = _create_test_app(provider, dev_admin_user="john")

        async with _client_for(app) as client:
            headers = await _bearer(client, "alice", "user-password-1")
            response = await client.get("/api/auth/me", headers=headers)
        assert response.status_code == 200
        assert response.json()["dev_admin"] is False

    async def test_me_reports_false_when_flag_unset(self, tmp_path) -> None:
        provider = _provider_with_admin(tmp_path)
        app = _create_test_app(provider)

        async with _client_for(app) as client:
            headers = await _bearer(client, "john", "admin-password-1")
            response = await client.get("/api/auth/me", headers=headers)
        assert response.status_code == 200
        assert response.json()["dev_admin"] is False
