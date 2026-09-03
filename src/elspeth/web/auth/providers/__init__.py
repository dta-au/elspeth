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

Phase 1 registers the profiles and their identity; the OIDC mechanics
(``resolve_issuer``, ``expected_origins``, ``claim_checks``, ``map_identity``,
``userinfo``) and ``required_settings`` arrive with the login path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, get_args

from elspeth.contracts.auth import AuthProviderType

__all__ = ["PROFILE_REGISTRY", "IdPProfile", "get_profile", "registered_provider_names"]


@dataclass(frozen=True, slots=True)
class IdPProfile:
    """A single identity provider's declaration."""

    name: AuthProviderType
    """The ``AuthProviderType`` value that selects this profile."""

    description: str
    """Operator-facing one-line summary, used in configuration errors."""


_ENTRA = IdPProfile(
    name="entra",
    description="Microsoft Entra ID; issuer derived from entra_tenant_id",
)
_GOOGLE = IdPProfile(
    name="google",
    description="Google Workspace; requires a hosted domain",
)
_OIDC = IdPProfile(
    name="oidc",
    description="Generic OIDC provider, including AWS Cognito",
)
_VANGUARD = IdPProfile(
    name="vanguard",
    description="VANguard; issuer supplied by the operator",
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
