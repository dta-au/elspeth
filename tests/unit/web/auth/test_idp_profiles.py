"""What each IdP profile refuses, and what it makes of what it accepts.

Spec: docs/specs/2026-09-02-pluggable-sso-design.md §1, §Profiles.

These are the fail-closed checks between "the token's signature verified" and
"this person is admitted". A signature only proves the IdP issued the token —
not that it issued it for THIS deployment — so every test here is about the
gap between those two statements.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest

from elspeth.web.auth.models import AuthenticationError, IdentityClaims
from elspeth.web.auth.providers import PROFILE_REGISTRY, get_profile
from elspeth.web.config import WebSettings


def _settings(**overrides: object) -> WebSettings:
    return WebSettings(
        secret_key=secrets.token_hex(40),
        shareable_link_signing_key=secrets.token_hex(40),
        composer_max_composition_turns=10,
        composer_max_discovery_turns=5,
        composer_timeout_seconds=120.0,
        composer_rate_limit_per_minute=10,
        **overrides,
    )


# TRANSITIONAL. The old browser path's oidc/entra validator arms still
# require these, so a settings object for those two providers cannot be built
# without them yet. They are deleted together with that path
# (elspeth-e385ed06e1), and this dict goes with them; vanguard and google
# never needed it because they never had a hand-written arm.
_LEGACY_OLD_PATH_FIELDS: dict[str, dict[str, object]] = {
    "oidc": {"oidc_issuer": "https://issuer.example.gov.au", "oidc_audience": "aud", "oidc_client_id": "client"},
    "entra": {"oidc_audience": "aud", "oidc_client_id": "client"},
}


def _idp_settings(provider: str, **overrides: object) -> WebSettings:
    return _settings(
        auth_provider=provider,
        sso_client_id="client",
        sso_client_secret="secret",
        sso_transaction_secret="transaction-secret",
        public_base_url="https://elspeth.example.gov.au",
        compartment_id="compartment-a",
        quota_default_tokens_per_day=100_000,
        quota_default_storage_bytes=1_000_000,
        **_LEGACY_OLD_PATH_FIELDS.get(provider, {}),
        **overrides,
    )


class TestEntraTenantCheck:
    """Without ``tid``, a multi-tenant app registration admits any tenant."""

    def test_a_token_from_another_tenant_is_refused(self) -> None:
        settings = _idp_settings("entra", entra_tenant_id="ours")
        with pytest.raises(AuthenticationError, match="different tenant"):
            get_profile("entra").claim_checks({"tid": "theirs"}, settings)

    def test_a_token_with_no_tenant_claim_is_refused(self) -> None:
        """Absence must not read as "unrestricted"."""
        settings = _idp_settings("entra", entra_tenant_id="ours")
        with pytest.raises(AuthenticationError, match="missing the required claim 'tid'"):
            get_profile("entra").claim_checks({}, settings)

    def test_the_configured_tenant_passes(self) -> None:
        settings = _idp_settings("entra", entra_tenant_id="ours")
        get_profile("entra").claim_checks({"tid": "ours"}, settings)

    def test_the_issuer_is_derived_from_the_tenant_not_configured(self) -> None:
        settings = _idp_settings("entra", entra_tenant_id="ours")
        assert get_profile("entra").resolve_issuer(settings) == "https://login.microsoftonline.com/ours/v2.0"


class TestGoogleHostedDomainCheck:
    """``hd`` is emitted for Workspace accounts ONLY.

    It is not in Google's published ``claims_supported``, so a personal
    gmail.com account produces a token with no ``hd`` at all. If absence read
    as "no restriction", every Google account on earth would be a valid login
    for a deployment that asked for one Workspace domain.
    """

    def test_a_personal_account_with_no_hosted_domain_is_refused(self) -> None:
        settings = _idp_settings("google", google_hosted_domain="example.gov.au")
        with pytest.raises(AuthenticationError, match="missing the required claim 'hd'"):
            get_profile("google").claim_checks({"email_verified": True}, settings)

    def test_another_workspace_domain_is_refused(self) -> None:
        settings = _idp_settings("google", google_hosted_domain="example.gov.au")
        with pytest.raises(AuthenticationError, match="different hosted domain"):
            get_profile("google").claim_checks({"email_verified": True, "hd": "elsewhere.com"}, settings)

    def test_an_unverified_email_is_refused_even_in_the_right_domain(self) -> None:
        settings = _idp_settings("google", google_hosted_domain="example.gov.au")
        with pytest.raises(AuthenticationError, match="verified email"):
            get_profile("google").claim_checks({"email_verified": False, "hd": "example.gov.au"}, settings)

    def test_a_missing_email_verified_claim_is_refused(self) -> None:
        settings = _idp_settings("google", google_hosted_domain="example.gov.au")
        with pytest.raises(AuthenticationError, match="verified email"):
            get_profile("google").claim_checks({"hd": "example.gov.au"}, settings)

    def test_a_truthy_non_true_email_verified_is_refused(self) -> None:
        """``"false"`` is a non-empty string, and a loose check would admit it."""
        settings = _idp_settings("google", google_hosted_domain="example.gov.au")
        with pytest.raises(AuthenticationError, match="verified email"):
            get_profile("google").claim_checks({"email_verified": "false", "hd": "example.gov.au"}, settings)

    def test_a_verified_workspace_account_passes(self) -> None:
        settings = _idp_settings("google", google_hosted_domain="example.gov.au")
        get_profile("google").claim_checks({"email_verified": True, "hd": "example.gov.au"}, settings)


class TestEndpointOriginPolicy:
    def test_google_endpoints_are_cross_origin_and_all_four_are_allowed(self) -> None:
        """The measured reason the global same-origin rule had to go."""
        settings = _idp_settings("google", google_hosted_domain="example.gov.au")
        profile = get_profile("google")
        origins = profile.expected_origins(settings, profile.resolve_issuer(settings))
        assert origins == frozenset(
            {
                "https://accounts.google.com",
                "https://oauth2.googleapis.com",
                "https://openidconnect.googleapis.com",
                "https://www.googleapis.com",
            }
        )

    def test_vanguard_is_same_origin_as_its_issuer(self) -> None:
        settings = _idp_settings("vanguard", sso_issuer="https://vanguard.example.gov.au/oidc")
        profile = get_profile("vanguard")
        assert profile.expected_origins(settings, profile.resolve_issuer(settings)) == frozenset({"https://vanguard.example.gov.au"})

    def test_generic_oidc_widens_to_the_operators_declared_origins(self) -> None:
        """Cognito's hosted login domain is not the pool issuer's origin."""
        settings = _idp_settings(
            "oidc",
            sso_issuer="https://cognito-idp.ap-southeast-2.amazonaws.com/pool",
            sso_endpoint_origins=("https://login.example.gov.au",),
        )
        profile = get_profile("oidc")
        origins = profile.expected_origins(settings, profile.resolve_issuer(settings))
        assert origins == frozenset({"https://cognito-idp.ap-southeast-2.amazonaws.com", "https://login.example.gov.au"})

    def test_an_oidc_deployment_that_declares_nothing_stays_same_origin(self) -> None:
        settings = _idp_settings("oidc", sso_issuer="https://issuer.example.gov.au")
        profile = get_profile("oidc")
        assert profile.expected_origins(settings, profile.resolve_issuer(settings)) == frozenset({"https://issuer.example.gov.au"})


class TestIdentityMapping:
    def test_a_token_with_no_subject_is_refused_by_every_profile(self) -> None:
        """``(provider, subject)`` is the identity key; blank collapses it."""
        for name, profile in PROFILE_REGISTRY.items():
            with pytest.raises(AuthenticationError, match="sub"):
                profile.map_identity({"email": "a@b.gov.au"}, None)
            with pytest.raises(AuthenticationError, match="non-blank"):
                profile.map_identity({"sub": "   "}, None)
            assert name  # every registered profile was exercised

    def test_cognito_username_fallback_order(self) -> None:
        profile = get_profile("oidc")
        preferred = profile.map_identity({"sub": "s", "preferred_username": "ada", "cognito:username": "cog"}, None)
        assert preferred.username == "ada"
        cognito = profile.map_identity({"sub": "s", "cognito:username": "cog"}, None)
        assert cognito.username == "cog"
        bare = profile.map_identity({"sub": "s"}, None)
        assert bare.username == "s"

    def test_entra_collects_no_groups(self) -> None:
        """D17: IdP groups are organisation facts, never compartment facts."""
        claims = profile_claims = {"sub": "s", "preferred_username": "ada", "groups": ["admins", "everyone"]}
        mapped = get_profile("entra").map_identity(claims, None)
        assert not hasattr(mapped, "groups")
        assert profile_claims["groups"] == ["admins", "everyone"]  # untouched, just unread

    def test_vanguard_assembles_a_display_name_and_carries_the_abn(self) -> None:
        mapped = get_profile("vanguard").map_identity(
            {"sub": "person@example.gov.au"},
            {"given_name": "Ada", "family_name": "Lovelace", "abn": "51824753556"},
        )
        assert mapped.display_name == "Ada Lovelace"
        assert mapped.organisation_id == "51824753556"
        assert mapped.subject == "person@example.gov.au"

    def test_vanguard_survives_a_partial_name(self) -> None:
        mapped = get_profile("vanguard").map_identity({"sub": "p@x.gov.au"}, {"given_name": "Ada"})
        assert mapped.display_name == "Ada"
        onlyfamily = get_profile("vanguard").map_identity({"sub": "p@x.gov.au"}, {"family_name": "Lovelace"})
        assert onlyfamily.display_name == "Lovelace"
        neither = get_profile("vanguard").map_identity({"sub": "p@x.gov.au"}, {})
        assert neither.display_name is None

    def test_a_non_string_claim_never_reaches_an_owned_field(self) -> None:
        """An IdP sending the wrong type must not deny access over a display name."""
        mapped = get_profile("google").map_identity({"sub": "s", "name": 42, "email": ["a@b"]}, None)
        assert mapped.display_name is None
        assert mapped.email is None
        assert mapped.username == "s"

    def test_every_profile_maps_to_its_own_provider_value(self) -> None:
        for name, profile in PROFILE_REGISTRY.items():
            mapped = profile.map_identity({"sub": "s"}, {} if profile.userinfo else None)
            assert mapped.provider == name


class TestIdentityClaimsIsAnOwnedType:
    def test_a_blank_subject_is_refused(self) -> None:
        with pytest.raises(AuthenticationError, match="subject"):
            IdentityClaims(provider="oidc", subject=" ", username="ada")

    def test_blank_cosmetic_fields_are_dropped_not_fatal(self) -> None:
        claims = IdentityClaims(
            provider="oidc",
            subject="s",
            username="ada",
            display_name="   ",
            email="\t",
            organisation_id=" ",
        )
        assert claims.display_name is None
        assert claims.email is None
        assert claims.organisation_id is None


def test_userinfo_is_declared_only_where_it_is_needed() -> None:
    """VANguard alone: the others carry everything in the ID token.

    A userinfo call the profile does not need is an extra network dependency
    on the login path and one more response to parse at a trust boundary.
    """
    assert {name for name, profile in PROFILE_REGISTRY.items() if profile.userinfo} == {"vanguard"}


def test_no_profile_accepts_an_algorithm_from_the_token() -> None:
    """Pinned per profile, never read from the header (elspeth-e8a9973c37)."""
    for profile in PROFILE_REGISTRY.values():
        assert profile.id_token_algorithms == ("RS256",)


def _unused(value: Any) -> None:  # pragma: no cover - keeps the import honest
    del value
