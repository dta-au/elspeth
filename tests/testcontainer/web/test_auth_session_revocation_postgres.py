"""A refresh already past authentication stays revoked across PostgreSQL clients."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web.auth.conftest import build_local_auth_provider

from elspeth.web.auth.models import AuthenticationError
from elspeth.web.auth.session_token import SessionTokenIssuer
from elspeth.web.coordination.auth_session_authority import RepositoryAuthSessionAuthority
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import initialize_session_schema

pytestmark = pytest.mark.testcontainer


def test_refresh_logout_interleaving_preserves_revocation_across_clients(external_deployment_postgres_url, monkeypatch):
    first_engine = create_session_engine(external_deployment_postgres_url)
    second_engine = create_session_engine(external_deployment_postgres_url)
    initialize_session_schema(first_engine)
    identity = f"auth-race-{uuid4()}"
    with first_engine.begin() as conn:
        ensure_test_identity(conn, identity_id=identity)

    def issuer(engine):
        return SessionTokenIssuer(
            signing_key=b"postgres-auth-revocation-test-key!",
            provider="local",
            audience="race-test",
            token_expiry_hours=24,
            max_refresh_chain_hours=168,
            principal_is_active=lambda _: True,
            sessions=RepositoryAuthSessionAuthority(engine),
        )

    first, second = issuer(first_engine), issuer(second_engine)
    original = first.mint(identity_id=identity, username=identity)
    independent = second.mint(identity_id=identity, username=identity)
    entered, release = Event(), Event()
    mint = first.mint

    def paused_mint(**kwargs):
        entered.set()
        assert release.wait(15)
        return mint(**kwargs)

    monkeypatch.setattr(first, "mint", paused_mint)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            refreshed = pool.submit(first.refresh, original)
            assert entered.wait(15)
            second.revoke(original)
            release.set()
            successor = refreshed.result(timeout=15)
        for token in (original, successor):
            with pytest.raises(AuthenticationError):
                first.authenticate(token)
            with pytest.raises(AuthenticationError):
                second.refresh(token)
        assert second.authenticate(independent).identity_id == identity
    finally:
        release.set()
        first_engine.dispose()
        second_engine.dispose()


def test_password_reset_during_postgres_identity_admission_cannot_issue_current_credentials(
    external_deployment_postgres_url,
    tmp_path,
    monkeypatch,
):
    engine = create_session_engine(external_deployment_postgres_url)
    initialize_session_schema(engine)
    provider = build_local_auth_provider(tmp_path / "auth.db", session_engine=engine)
    username = f"reset-race-{uuid4()}"
    provider.create_user(username, "old-password", "Reset race")
    original = provider._login_sync(username, "old-password")
    entered, release = Event(), Event()
    admit = provider._admitted_identity_id

    def paused_admission(candidate):
        entered.set()
        assert release.wait(15)
        return admit(candidate)

    monkeypatch.setattr(provider, "_admitted_identity_id", paused_admission)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(provider._login_sync, username, "old-password")
            assert entered.wait(15)
            provider.set_password(username, "replacement-password")
            release.set()
            with pytest.raises(AuthenticationError):
                pending.result(timeout=15)
        with pytest.raises(AuthenticationError):
            provider._authenticate_sync(original)
        monkeypatch.setattr(provider, "_admitted_identity_id", admit)
        fresh = provider._login_sync(username, "replacement-password")
        assert provider._authenticate_sync(fresh).username == username
    finally:
        release.set()
        engine.dispose()
