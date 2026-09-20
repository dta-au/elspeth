"""Local account management: configured dev admin or live local-auth administrator."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from elspeth.web.auth.admin_routes import create_dev_admin_router
from elspeth.web.auth.local import LocalAuthProvider
from elspeth.web.auth.models import IdentityClaims
from elspeth.web.auth.routes import create_auth_router
from elspeth.web.config import WebSettings
from elspeth.web.coordination.approval_lifecycle_authority import RepositoryApprovalLifecycleAuthority
from elspeth.web.coordination.identity_authority import IdentityAdminActor, RepositoryIdentityAuthority
from elspeth.web.middleware.request_id import RequestIdMiddleware
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import initialize_session_schema

from .conftest import build_local_auth_provider


class _NoopAuthAuditRecorder:
    def record_login_success_and_token_issued(self, *args, **kwargs) -> None:
        return None

    def record_login_failure(self, *args, **kwargs) -> None:
        return None

    def record_token_issued(self, *args, **kwargs) -> None:
        return None

    def record_auth_failure(self, *args, **kwargs) -> None:
        return None


def _create_test_app(provider, *, authority: RepositoryIdentityAuthority | None = None, **settings_overrides) -> FastAPI:
    """Create a FastAPI app with the auth + dev-admin routers mounted."""
    from elspeth.web.middleware.rate_limit import ComposerRateLimiter

    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)
    app.state.auth_provider = provider
    if authority is None:
        # Match build_local_auth_provider's default identity substrate.
        authority = RepositoryIdentityAuthority(
            create_session_engine(f"sqlite:///{provider._db_path.parent / 'identity-substrate.db'}"),
            lifecycle_effect=RepositoryApprovalLifecycleAuthority().apply,
        )
    app.state.identity_authority = authority
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
    provider = build_local_auth_provider(tmp_path / "auth.db")
    provider.create_user("john", "admin-password-1", display_name="John")
    return provider


async def _bearer(client: AsyncClient, username: str, password: str) -> dict[str, str]:
    response = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.asyncio
class TestDevAdminGuard:
    async def test_all_routes_404_for_non_admin_when_flag_unset(self, tmp_path) -> None:
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
            assert (
                await client.request("DELETE", "/api/auth/admin/users/john", headers=headers, json={"reason": "left the team"})
            ).status_code == 404

    async def test_routes_401_without_credentials_when_flag_unset(self, tmp_path) -> None:
        provider = _provider_with_admin(tmp_path)
        app = _create_test_app(provider)

        async with _client_for(app) as client:
            assert (await client.get("/api/auth/admin/users")).status_code == 401

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
            response = await client.request("DELETE", "/api/auth/admin/users/alice", headers=headers, json={"reason": "left the team"})
            assert response.status_code == 204

            login = await client.post("/api/auth/login", json={"username": "alice", "password": "user-password-1"})
            assert login.status_code == 401

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"json": {}},
            {"json": {"reason": ""}},
            {"json": {"reason": "   "}},
            {"json": {"reason": "x" * 481}},
            {"json": {"reason": "left the team", "extra": "field"}},
        ],
    )
    async def test_delete_without_a_usable_reason_is_refused_and_deletes_nothing(self, tmp_path, kwargs) -> None:
        # Disabling requires a reason; deleting is the graver act and must not
        # cost less (ruling D3, 2026-09-20).
        provider = _provider_with_admin(tmp_path)
        provider.create_user("alice", "user-password-1", display_name="Alice")
        app = _create_test_app(provider, dev_admin_user="john")

        async with _client_for(app) as client:
            headers = await _bearer(client, "john", "admin-password-1")
            response = await client.request("DELETE", "/api/auth/admin/users/alice", headers=headers, **kwargs)
            assert response.status_code == 422
            login = await client.post("/api/auth/login", json={"username": "alice", "password": "user-password-1"})
            assert login.status_code == 200

    async def test_admin_cannot_delete_own_account(self, tmp_path) -> None:
        provider = _provider_with_admin(tmp_path)
        app = _create_test_app(provider, dev_admin_user="john")

        async with _client_for(app) as client:
            headers = await _bearer(client, "john", "admin-password-1")
            response = await client.request("DELETE", "/api/auth/admin/users/john", headers=headers, json={"reason": "left the team"})
        assert response.status_code == 400
        assert provider.list_users()[0].user_id == "john"

    async def test_delete_unknown_user_404s(self, tmp_path) -> None:
        provider = _provider_with_admin(tmp_path)
        app = _create_test_app(provider, dev_admin_user="john")

        async with _client_for(app) as client:
            headers = await _bearer(client, "john", "admin-password-1")
            response = await client.request("DELETE", "/api/auth/admin/users/ghost", headers=headers, json={"reason": "left the team"})
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


def _substrate(tmp_path) -> tuple[LocalAuthProvider, RepositoryIdentityAuthority]:
    """A provider and an authority over ONE identity substrate.

    The dev admin's power is a CREDENTIAL flag; the last-administrator rule
    (R5) lives in the identity store. A test of where they meet needs both
    halves bound to the same rows, which the default fixture hides.
    """
    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(engine)
    authority = RepositoryIdentityAuthority(engine, lifecycle_effect=RepositoryApprovalLifecycleAuthority().apply)
    provider = build_local_auth_provider(tmp_path / "auth.db", session_engine=engine)
    provider.create_user("john", "admin-password-1", display_name="John")
    return provider, authority


def _bootstrap_identity_admin(provider: LocalAuthProvider, authority: RepositoryIdentityAuthority, username: str) -> str:
    provider.create_user(username, "user-password-1", display_name=username.title())
    event = authority.bootstrap_admin(
        claims=IdentityClaims(provider="local", subject=username, username=username),
        note="test bootstrap",
        quota_tokens_per_day=None,
        quota_storage_bytes=None,
        record=lambda _event: None,
    )
    return event.record.identity_id


@pytest.mark.asyncio
class TestDeleteUserLastAdministrator:
    """Deleting a local account retires its identity, so R5 must hold here too.

    The dev admin is named by configuration and need not hold the identity
    ``admin`` role, so ``disable_identity``'s own-identity and last-admin
    refusals never see this path: before the repair the deletion below
    answered 204 and left the container with no administrator at all.
    """

    async def test_refuses_to_delete_the_last_active_human_administrator(self, tmp_path) -> None:
        provider, authority = _substrate(tmp_path)
        _bootstrap_identity_admin(provider, authority, "alice")
        assert authority.count_active_human_admins() == 1
        app = _create_test_app(provider, authority=authority, dev_admin_user="john")

        async with _client_for(app) as client:
            headers = await _bearer(client, "john", "admin-password-1")
            response = await client.request("DELETE", "/api/auth/admin/users/alice", headers=headers, json={"reason": "left the team"})
            assert response.status_code == 409, response.text
            assert response.json()["detail"]["refusal"] == "last_active_admin_protected"
            # The refusal is decided BEFORE the credential goes: a refused
            # deletion that had already removed the password would leave the
            # only administrator with an identity and no way to sign in.
            await _bearer(client, "alice", "user-password-1")

        assert authority.count_active_human_admins() == 1
        assert authority.read_identity_by_natural_key(provider="local", subject="alice") is not None

    async def test_deletes_an_administrator_when_another_remains(self, tmp_path) -> None:
        provider, authority = _substrate(tmp_path)
        alice_id = _bootstrap_identity_admin(provider, authority, "alice")
        provider.create_user("bob", "user-password-1", display_name="Bob")
        bob = authority.ensure_identity(
            claims=IdentityClaims(provider="local", subject="bob", username="bob"),
            activate=True,
            quota_tokens_per_day=None,
            quota_storage_bytes=None,
            identity_dormancy_days=90,
            record_admission=lambda *_args: None,
            record_rebound=lambda *_args: None,
            record_dormant=lambda *_args: None,
        )
        authority.grant_role(
            actor=IdentityAdminActor(identity_id=alice_id, on_behalf_of=None, console_request_id=None),
            identity_id=bob.record.identity_id,
            role="admin",
            scope=None,
            expires_at=None,
            note=None,
            record=lambda _event: None,
        )
        assert authority.count_active_human_admins() == 2
        app = _create_test_app(provider, authority=authority, dev_admin_user="john")

        async with _client_for(app) as client:
            headers = await _bearer(client, "john", "admin-password-1")
            assert (
                await client.request("DELETE", "/api/auth/admin/users/alice", headers=headers, json={"reason": "left the team"})
            ).status_code == 204

        assert authority.count_active_human_admins() == 1
        assert authority.read_identity_by_natural_key(provider="local", subject="alice") is None
