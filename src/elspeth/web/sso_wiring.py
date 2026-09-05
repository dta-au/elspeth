"""Wire the SSO login walk into the application, once, at startup.

The pieces already exist and none of them reads ``WebSettings``: the profile
registry says what an IdP is, ``web/auth/sso.py`` performs the walk against
an ``SsoRuntime``, and the identity substrate is reached only through
``RepositoryIdentityAuthority``. This module is the one place that knows all
of them and the settings, so the app factory binds ONE object
(``app.state.sso``) and the three routes read that and nothing else.

Two phases, because the app factory is synchronous and discovery is not:

- :func:`build_sso_wiring` runs in the factory. It decides whether the
  deployment is wired at all (the active profile has every setting it
  requires, by the same rule readiness reports on) and assembles everything
  that needs no network: the session-token issuer, the handoff store, the
  admission and read callables, the profile's claim checks.
- :func:`resolve_sso_runtime` runs in lifespan. It resolves the IdP
  endpoints -- the operator's break-glass override, else discovery under the
  profile's origin policy -- and binds the validator, the client and the
  runtime. Discovery failing is a boot failure, deliberately: a deployment
  that cannot reach its IdP cannot log anyone in, and saying so at startup
  is better than saying it to every user at the callback.

An ``oidc`` or ``entra`` deployment that is NOT wired keeps the legacy bearer
path for now (identity sprint step E deletes it); its SSO routes refuse and
``/api/auth/config`` reports no start URL, which is the same fact.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from sqlalchemy import Engine

from elspeth.contracts.auth import AuthProviderType
from elspeth.web.auth.audit import AuthAuditRecorder
from elspeth.web.auth.id_token import JWKSTokenValidator
from elspeth.web.auth.models import IdentityClaims
from elspeth.web.auth.providers import PROFILE_REGISTRY, IdPProfile
from elspeth.web.auth.session_token import (
    DEFAULT_MAX_REFRESH_CHAIN_HOURS,
    DEFAULT_TOKEN_EXPIRY_HOURS,
    SessionTokenIssuer,
    session_token_audience,
)
from elspeth.web.auth.sso import (
    AdmittedIdentity,
    SsoClient,
    SsoRuntime,
    configured_endpoint_override,
    fetch_discovery_endpoints,
)
from elspeth.web.config import WebSettings, configured_auth_settings
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.key_derivation import derive_session_token_key
from elspeth.web.sessions.sso_handoff_repository import SsoHandoffRepository

SSO_CALLBACK_PATH = "/api/auth/sso/callback"


def sso_missing_settings(settings: WebSettings) -> tuple[str, ...]:
    """The active profile's required settings that are not configured; empty means wired."""
    if settings.auth_provider == "local" or settings.auth_provider not in PROFILE_REGISTRY:
        return ()
    configured = configured_auth_settings(settings)
    return tuple(name for name in PROFILE_REGISTRY[settings.auth_provider].required_settings if not configured[name])


@dataclass(frozen=True, slots=True)
class SsoWiring:
    """Everything the runtime needs that needs no network to build.

    Frozen and slotted like ``SsoRuntime``: it is read by lifespan exactly
    once and must not grow attributes on the way.
    """

    profile: IdPProfile
    provider: AuthProviderType
    issuer_url: str
    expected_origins: frozenset[str]
    client_id: str
    client_secret: str
    transaction_secret: str
    public_base_url: str
    token_issuer: SessionTokenIssuer
    handoffs: SsoHandoffRepository
    upsert_identity: Callable[[IdentityClaims], AdmittedIdentity]
    read_identity: Callable[[str], AdmittedIdentity | None]
    claim_checks: Callable[[Mapping[str, Any]], None]
    map_identity: Callable[[Mapping[str, Any], Mapping[str, Any] | None], IdentityClaims]


def build_sso_wiring(
    settings: WebSettings,
    *,
    session_engine: Engine,
    identity_authority: RepositoryIdentityAuthority,
    resolved_state_mode: Literal["sqlite-single", "external-postgresql"],
) -> SsoWiring | None:
    """Assemble the network-free half, or ``None`` when the deployment is not wired.

    ``None`` is a decision, not an error: readiness reports the missing
    fields by name, and the routes refuse closed. A partially configured
    profile must never produce a partially working login.
    """
    if settings.auth_provider == "local" or sso_missing_settings(settings):
        return None
    profile = PROFILE_REGISTRY[settings.auth_provider]
    # required_settings guarantees these are present; the asserts make the
    # narrowing visible rather than trusting a tuple of names.
    assert settings.sso_client_id is not None
    assert settings.sso_client_secret is not None
    assert settings.sso_transaction_secret is not None
    assert settings.public_base_url is not None
    issuer_url = profile.resolve_issuer(settings)
    audit_recorder = AuthAuditRecorder.from_settings(settings, resolved_state_mode)
    provider = settings.auth_provider

    def _principal_is_active(identity_id: str) -> bool:
        record = identity_authority.read_identity(identity_id=identity_id)
        # An absent row is never an implicit grant.
        return record is not None and record.is_active

    def _record_admission(identity_id: str, username: str, quota_written: bool) -> None:
        # Runs INSIDE ensure_identity's transaction: a failed audit rolls
        # the activation back. Only reachable when an admission activates,
        # which for SSO is the bootstrap seed -- a first login lands pending
        # (D12) and is activated by an administrator through the admin path.
        audit_recorder.record_identity_admitted(
            provider=provider,
            identity_id=identity_id,
            username=username,
            tokens_per_day=settings.quota_default_tokens_per_day if quota_written else None,
            storage_bytes=settings.quota_default_storage_bytes if quota_written else None,
        )

    def _upsert_identity(claims: IdentityClaims) -> AdmittedIdentity:
        # D12: every SSO first login is PENDING until an administrator
        # activates it. There is no open-registration reading for an IdP
        # login -- the IdP verified who the person is, not whether this
        # container admits them.
        return identity_authority.ensure_identity(
            claims=claims,
            activate=False,
            quota_tokens_per_day=settings.quota_default_tokens_per_day,
            quota_storage_bytes=settings.quota_default_storage_bytes,
            record_admission=_record_admission,
        ).record

    def _read_identity(identity_id: str) -> AdmittedIdentity | None:
        return identity_authority.read_identity(identity_id=identity_id)

    def _claim_checks(claims: Mapping[str, Any]) -> None:
        profile.claim_checks(claims, settings)

    token_issuer = SessionTokenIssuer(
        signing_key=derive_session_token_key(settings.secret_key),
        provider=provider,
        audience=session_token_audience(settings.public_base_url),
        token_expiry_hours=DEFAULT_TOKEN_EXPIRY_HOURS,
        max_refresh_chain_hours=DEFAULT_MAX_REFRESH_CHAIN_HOURS,
        principal_is_active=_principal_is_active,
    )
    return SsoWiring(
        profile=profile,
        provider=provider,
        issuer_url=issuer_url,
        expected_origins=profile.expected_origins(settings, issuer_url),
        client_id=settings.sso_client_id,
        client_secret=settings.sso_client_secret.get_secret_value(),
        transaction_secret=settings.sso_transaction_secret.get_secret_value(),
        public_base_url=settings.public_base_url,
        token_issuer=token_issuer,
        handoffs=SsoHandoffRepository(session_engine),
        upsert_identity=_upsert_identity,
        read_identity=_read_identity,
        claim_checks=_claim_checks,
        map_identity=profile.map_identity,
    )


async def resolve_sso_runtime(
    wiring: SsoWiring,
    settings: WebSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SsoRuntime:
    """Resolve the IdP endpoints and bind the runtime the routes read.

    The break-glass override wins when configured -- it was validated
    against the profile's origin policy when the settings were built.
    Otherwise discovery runs under that same policy. Raises
    ``SsoDiscoveryFailed`` (an ``SsoLoginError``) when the IdP cannot be
    reached or its document fails the policy; the caller decides that is a
    boot failure.

    ``transport`` exists for tests to stand a fake IdP in front of both the
    discovery fetch and every later JWKS refresh; production passes nothing.
    """
    endpoints = configured_endpoint_override(settings)
    if endpoints is None:
        endpoints = await fetch_discovery_endpoints(
            issuer=wiring.issuer_url,
            expected_origins=wiring.expected_origins,
            transport=transport,
        )
    validator = JWKSTokenValidator(
        wiring.issuer_url,
        wiring.client_id,
        settings.jwks_cache_ttl_seconds,
        settings.jwks_failure_retry_seconds,
        settings.jwks_max_stale_seconds,
        algorithms=wiring.profile.id_token_algorithms,
        jwks_uri=endpoints.jwks_uri,
        transport=transport,
    )
    client = SsoClient(
        provider=wiring.provider,
        client_id=wiring.client_id,
        client_secret=wiring.client_secret,
        redirect_uri=f"{wiring.public_base_url.rstrip('/')}{SSO_CALLBACK_PATH}",
        transaction_secret=wiring.transaction_secret,
        public_base_url=wiring.public_base_url,
        endpoints=endpoints,
        userinfo=wiring.profile.userinfo,
        scopes=wiring.profile.scopes,
    )
    return SsoRuntime(
        client=client,
        validator=validator,
        claim_checks=wiring.claim_checks,
        map_identity=wiring.map_identity,
        handoffs=wiring.handoffs,
        upsert_identity=wiring.upsert_identity,
        read_identity=wiring.read_identity,
        issuer=wiring.token_issuer,
        transport=transport,
    )
