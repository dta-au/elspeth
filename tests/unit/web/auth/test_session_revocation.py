"""Real shared-store revocation, retained families, and reset races."""

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import delete

from elspeth.contracts.auth import AuthProviderType
from elspeth.web.auth.models import AuthenticationError
from elspeth.web.auth.session_token import SessionTokenIssuer
from elspeth.web.coordination.auth_session_authority import RepositoryAuthSessionAuthority
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import auth_session_families_table
from elspeth.web.sessions.schema import initialize_session_schema
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web.auth.conftest import build_local_auth_provider


@pytest.mark.parametrize("provider", ["local", "oidc", "entra", "vanguard", "google"])
def test_revocation_survives_new_authority_and_does_not_revoke_other_families(tmp_path, provider: AuthProviderType):
    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="victim", provider=provider)
    try:

        def issuer() -> SessionTokenIssuer:
            return SessionTokenIssuer(
                signing_key=b"shared-session-test-signing-key!!",
                provider=provider,
                audience="test",
                token_expiry_hours=24,
                max_refresh_chain_hours=168,
                principal_is_active=lambda _: True,
                sessions=RepositoryAuthSessionAuthority(engine),
            )

        first, second = issuer(), issuer()
        original = first.mint(identity_id="victim", username="victim")
        successor = second.refresh(original)
        independent = second.mint(identity_id="victim", username="victim")
        assert first.authenticate(original).binding.family_id == second.authenticate(successor).binding.family_id
        second.revoke(successor)
        second.revoke(successor)
        for token in (original, successor):
            with pytest.raises(AuthenticationError):
                issuer().authenticate(token)
            with pytest.raises(AuthenticationError):
                issuer().refresh(token)
        assert issuer().authenticate(independent).identity_id == "victim"
        with engine.begin() as conn:
            conn.execute(delete(auth_session_families_table))
        with pytest.raises(AuthenticationError):
            issuer().authenticate(independent)
    finally:
        engine.dispose()


def test_refresh_that_passed_authentication_cannot_escape_concurrent_logout(tmp_path, monkeypatch):
    provider = build_local_auth_provider(tmp_path / "auth.db")
    provider.create_user("alice", "old-password", "Alice")
    original = provider._login_sync("alice", "old-password")
    issuer = provider._issuer
    mint = issuer.mint
    entered, released = Event(), Event()

    def paused_mint(**kwargs):
        entered.set()
        assert released.wait(10)
        return mint(**kwargs)

    monkeypatch.setattr(issuer, "mint", paused_mint)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(provider._refresh_sync, original)
            assert entered.wait(10)
            issuer.revoke(original)
            released.set()
            successor = future.result(timeout=10)
        with pytest.raises(AuthenticationError):
            provider._authenticate_sync(successor)
    finally:
        released.set()


def test_old_password_cannot_acquire_the_generation_of_a_concurrent_reset(tmp_path, monkeypatch):
    provider = build_local_auth_provider(tmp_path / "auth.db")
    provider.create_user("alice", "old-password", "Alice")
    original_admission = provider._admitted_identity_id

    def reset_before_admission(username):
        provider.set_password(username, "replacement-password")
        return original_admission(username)

    monkeypatch.setattr(provider, "_admitted_identity_id", reset_before_admission)
    with pytest.raises(AuthenticationError):
        provider._login_sync("alice", "old-password")
    monkeypatch.setattr(provider, "_admitted_identity_id", original_admission)
    assert provider._authenticate_sync(provider._login_sync("alice", "replacement-password")).username == "alice"


def test_derived_binding_expires_with_its_original_bearer(tmp_path):
    provider = build_local_auth_provider(tmp_path / "auth.db")
    provider.create_user("alice", "password", "Alice")
    token = provider._login_sync("alice", "password")
    claims = provider._issuer.decode(token)
    user = provider._authenticate_sync(token)
    from elspeth.web.auth.session_token import SessionBinding

    expired = SessionBinding(claims.binding.family_id, 0, int(time.time()) - 1)
    with pytest.raises(AuthenticationError):
        provider.authorize_binding(user, expired)
