"""The wired / unwired decision, and what a wired deployment binds.

``build_sso_wiring`` returning ``None`` is the ONE fact both the app factory
(legacy bearer path, SSO routes refuse) and readiness (missing fields named)
read. These tests pin that the decision derives from the profile registry's
required settings and nothing else, and that a wired deployment's runtime is
built from the profile rather than from a per-provider branch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretBytes

from elspeth.web.auth.claims import IdTokenClaims
from elspeth.web.auth.sso import SsoRuntime
from elspeth.web.config import WebSettings
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sso_wiring import build_sso_wiring, resolve_sso_runtime, sso_missing_settings
from tests.helpers.fake_idp import FakeIdP

_COMPOSER: dict[str, Any] = {
    "composer_max_composition_turns": 15,
    "composer_max_discovery_turns": 10,
    "composer_timeout_seconds": 85.0,
    "composer_rate_limit_per_minute": 10,
    "shareable_link_signing_key": SecretBytes(b"\x00" * 32),
    "operator_metrics_bearer_token": "operator-metrics-token-for-tests-0001",
    "secret_key": "dev-secret",
}


def _oidc_wired(tmp_path: Path, idp: FakeIdP, **overrides: Any) -> WebSettings:
    base: dict[str, Any] = {
        "data_dir": tmp_path,
        "auth_provider": "oidc",
        # Transitional: the oidc arm still demands the legacy fields (elspeth-2094379035).
        "oidc_issuer": idp.issuer,
        "oidc_audience": idp.client_id,
        "oidc_client_id": idp.client_id,
        "sso_issuer": idp.issuer,
        "sso_client_id": idp.client_id,
        "sso_client_secret": idp.client_secret,
        "sso_transaction_secret": "t" * 40,
        "public_base_url": "https://elspeth.example.gov.au",
        "compartment_id": "example-compartment",
        "quota_default_tokens_per_day": 100_000,
        "quota_default_storage_bytes": 1_000_000,
        **_COMPOSER,
    }
    base.update(overrides)
    return WebSettings(**base)


def _oidc_legacy_only(tmp_path: Path) -> WebSettings:
    return WebSettings(
        data_dir=tmp_path,
        auth_provider="oidc",
        oidc_issuer="https://issuer.example.com",
        oidc_audience="audience",
        oidc_client_id="client",
        **_COMPOSER,
    )


@pytest.fixture
def substrate(tmp_path: Path):
    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(engine)
    return engine, RepositoryIdentityAuthority(engine)


def test_missing_settings_are_the_profiles_required_settings_not_configured(tmp_path: Path) -> None:
    assert sso_missing_settings(_oidc_legacy_only(tmp_path)) == (
        "sso_client_id",
        "sso_client_secret",
        "sso_transaction_secret",
        "public_base_url",
        "compartment_id",
        "quota_default_tokens_per_day",
        "quota_default_storage_bytes",
        "sso_issuer",
    )
    assert sso_missing_settings(_oidc_wired(tmp_path, FakeIdP())) == ()


def test_local_and_unwired_deployments_build_nothing(tmp_path: Path, substrate) -> None:
    engine, authority = substrate
    local = WebSettings(data_dir=tmp_path, auth_provider="local", **_COMPOSER)
    assert build_sso_wiring(local, session_engine=engine, identity_authority=authority, resolved_state_mode="sqlite-single") is None
    assert (
        build_sso_wiring(
            _oidc_legacy_only(tmp_path), session_engine=engine, identity_authority=authority, resolved_state_mode="sqlite-single"
        )
        is None
    )


@pytest.mark.asyncio
async def test_a_wired_deployment_resolves_its_runtime_by_discovery_under_the_profiles_policy(tmp_path: Path, substrate) -> None:
    engine, authority = substrate
    idp = FakeIdP()
    wiring = build_sso_wiring(
        _oidc_wired(tmp_path, idp), session_engine=engine, identity_authority=authority, resolved_state_mode="sqlite-single"
    )
    assert wiring is not None
    assert wiring.token_issuer.audience == "https://elspeth.example.gov.au"
    assert wiring.token_issuer.provider == "oidc"

    runtime = await resolve_sso_runtime(wiring, _oidc_wired(tmp_path, idp), transport=idp.transport())
    assert isinstance(runtime, SsoRuntime)
    assert runtime.client.endpoints.authorization_endpoint == idp.authorization_endpoint
    assert runtime.client.endpoints.jwks_uri == idp.jwks_uri
    assert runtime.client.redirect_uri == "https://elspeth.example.gov.au/api/auth/sso/callback"
    assert runtime.client.start_url == "https://elspeth.example.gov.au/api/auth/sso/start"
    assert runtime.client.userinfo is False
    assert runtime.validator._algorithms == ("RS256",)


@pytest.mark.asyncio
async def test_a_wired_deployment_takes_the_break_glass_override_without_discovery(tmp_path: Path, substrate) -> None:
    engine, authority = substrate
    idp = FakeIdP()
    settings = _oidc_wired(
        tmp_path,
        idp,
        sso_authorization_endpoint=f"{idp.issuer}/authorize",
        sso_token_endpoint=f"{idp.issuer}/token",
        sso_jwks_uri=f"{idp.issuer}/keys",
    )
    wiring = build_sso_wiring(settings, session_engine=engine, identity_authority=authority, resolved_state_mode="sqlite-single")
    assert wiring is not None

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no request expected under the override, got {request.url}")

    runtime = await resolve_sso_runtime(wiring, settings, transport=httpx.MockTransport(refuse))
    assert runtime.client.endpoints.token_endpoint == f"{idp.issuer}/token"
    assert runtime.client.endpoints.userinfo_endpoint is None


@pytest.mark.asyncio
async def test_a_wired_deployments_first_login_lands_pending(tmp_path: Path, substrate) -> None:
    """D12: the IdP verified who the person is, not whether this container admits them."""
    engine, authority = substrate
    idp = FakeIdP()
    wiring = build_sso_wiring(
        _oidc_wired(tmp_path, idp), session_engine=engine, identity_authority=authority, resolved_state_mode="sqlite-single"
    )
    assert wiring is not None
    claims = wiring.map_identity(
        IdTokenClaims(issuer=idp.issuer, subject="ada", audience=idp.client_id, issued_at=1_700_000_000, expires_at=1_700_000_300), None
    )
    admitted = wiring.upsert_identity(claims)
    assert admitted.access_state == "pending"
    assert wiring.read_identity(admitted.identity_id) is not None
    assert wiring.token_issuer.mint(identity_id=admitted.identity_id, username=admitted.username)
