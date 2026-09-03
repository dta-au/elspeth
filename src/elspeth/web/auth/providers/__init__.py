"""IdP profile registry.

One frozen profile per identity provider ELSPETH can authenticate a browser
against.  ``WebSettings.auth_provider`` names exactly one of them, and every
other ``sso_*`` field is that profile's configuration for this container: the
build carries no credentials and the profile carries no deployment facts, so
switching a container from one IdP to another is a config change and a
restart, never a build.

The registry asserts parity with :data:`AuthProviderType` at **import**, which
makes an unregistered provider a boot failure rather than a test failure.
``AuthProviderType`` itself stays a hand-written L0 Literal -- contracts import
nothing above them, and a Literal cannot be computed -- so adding an IdP is a
deliberate edit in two places that this module refuses to let drift apart.

Phase 1 registers each profile's identity and its settings matrix; the OIDC
mechanics (``resolve_issuer``, ``expected_origins``, ``claim_checks``,
``map_identity``, ``userinfo``) arrive with the login path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, get_args

from elspeth.contracts.auth import AuthProviderType

__all__ = ["PROFILE_REGISTRY", "IdPProfile", "get_profile", "registered_provider_names"]

# Required of EVERY IdP deployment, whichever profile is selected. The backend
# is a confidential client, so it always needs a client id, a secret and a
# transaction secret; it always needs its own public origin to build a
# redirect URI; and an activated identity always needs a quota row and a
# compartment marking. None of this varies by IdP, so it is stated once rather
# than repeated in four profiles.
_COMMON_IDP_REQUIRED: Final[tuple[str, ...]] = (
    "sso_client_id",
    "sso_client_secret",
    "sso_transaction_secret",
    "public_base_url",
    "compartment_id",
    "quota_default_tokens_per_day",
    "quota_default_storage_bytes",
)

# Settings that mean something to SOME profile and nothing to the others.
# ``forbidden_settings`` is DERIVED by subtracting what a profile uses from
# this set, so a new provider-specific setting cannot become silently
# permitted everywhere because an author forgot three "forbidden" lists.
_PROVIDER_SPECIFIC_SETTINGS: Final[frozenset[str]] = frozenset(
    {
        "sso_issuer",
        "entra_tenant_id",
        "google_hosted_domain",
        "sso_endpoint_origins",
    }
)


@dataclass(frozen=True, slots=True)
class IdPProfile:
    """A single identity provider's declaration."""

    name: AuthProviderType
    """The ``AuthProviderType`` value that selects this profile."""

    description: str
    """Operator-facing one-line summary, used in configuration errors."""

    specific_required: tuple[str, ...] = ()
    """Provider-specific settings this profile cannot start without."""

    specific_optional: tuple[str, ...] = ()
    """Provider-specific settings this profile accepts but does not demand."""

    @property
    def required_settings(self) -> tuple[str, ...]:
        """Every setting readiness must find non-empty for this profile."""
        return _COMMON_IDP_REQUIRED + self.specific_required

    @property
    def forbidden_settings(self) -> frozenset[str]:
        """Provider-specific settings that mean nothing to this profile.

        Derived, never listed: a setting configured for the wrong IdP is an
        operator error worth naming, and deriving the answer means adding a
        provider-specific setting cannot quietly become permitted everywhere.
        """
        return _PROVIDER_SPECIFIC_SETTINGS - {*self.specific_required, *self.specific_optional}


_ENTRA = IdPProfile(
    name="entra",
    description="Microsoft Entra ID; issuer derived from entra_tenant_id",
    # The issuer is DERIVED from the tenant, so accepting sso_issuer as well
    # would let two sources of truth disagree.
    specific_required=("entra_tenant_id",),
)
_GOOGLE = IdPProfile(
    name="google",
    description="Google Workspace; requires a hosted domain",
    # The issuer is fixed at https://accounts.google.com. Without a hosted
    # domain any Google account in the world is a valid login, so this is
    # required rather than defaulted.
    specific_required=("google_hosted_domain",),
)
_OIDC = IdPProfile(
    name="oidc",
    description="Generic OIDC provider, including AWS Cognito",
    specific_required=("sso_issuer",),
    # Cognito's hosted domain differs from the pool issuer, so the generic
    # profile is the one that may widen beyond same-origin. Optional: a
    # deployment whose endpoints are all issuer-origin needs nothing here.
    specific_optional=("sso_endpoint_origins",),
)
_VANGUARD = IdPProfile(
    name="vanguard",
    description="VANguard; issuer supplied by the operator",
    specific_required=("sso_issuer",),
)

PROFILE_REGISTRY: Final[dict[AuthProviderType, IdPProfile]] = {profile.name: profile for profile in (_ENTRA, _GOOGLE, _OIDC, _VANGUARD)}


def registered_provider_names() -> tuple[str, ...]:
    """Every provider an operator may select, sorted, including ``local``."""
    return tuple(sorted({*PROFILE_REGISTRY, "local"}))


def get_profile(provider: AuthProviderType) -> IdPProfile:
    """Return the profile for ``provider``.

    ``local`` is not an IdP: it has no profile, and asking for one is a
    programming error rather than a configuration error.
    """
    if provider not in PROFILE_REGISTRY:
        raise KeyError(f"no IdP profile is registered for provider {provider!r}; registered: {sorted(PROFILE_REGISTRY)}")
    return PROFILE_REGISTRY[provider]


def _assert_registry_matches_contract() -> None:
    """Fail the import when the registry and the L0 Literal disagree.

    ``local`` is authentication without an IdP, so it is a member of the
    Literal with no profile behind it; every other value must be registered.
    """
    declared = frozenset(get_args(AuthProviderType))
    registered = frozenset(PROFILE_REGISTRY) | {"local"}
    if registered != declared:
        raise RuntimeError(
            "IdP profile registry does not match AuthProviderType: "
            f"declared but unregistered {sorted(declared - registered)!r}, "
            f"registered but undeclared {sorted(registered - declared)!r}"
        )


_assert_registry_matches_contract()
