"""Per-IdP behaviour: issuer resolution, origin policy, claim checks, mapping.

One module of plain functions rather than four subclasses, so each behaviour
is named, documented with the provider quirk that motivates it, and testable
without constructing a profile. The registry wires them onto profiles.

Everything here reads data ELSPETH did not produce. ``map_identity`` and
``claim_checks`` are the Tier-3 boundary: they take whatever shape the IdP
sent, assert what they need, and construct an owned ``IdentityClaims`` — no
caller downstream ever sees a raw claim dict.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from elspeth.web.auth.models import AuthenticationError, IdentityClaims
from elspeth.web.auth.urls import https_url_origin, validate_oidc_issuer

if TYPE_CHECKING:  # pragma: no cover - annotations only, and this import
    # would be circular at runtime: web.config imports the registry to drive
    # its validation.
    from elspeth.web.config import WebSettings

# Measured 2026-09-02 from Google's discovery document. Google is the one
# provider whose endpoints are genuinely CROSS-ORIGIN — authorization on
# accounts.google.com, token on oauth2.googleapis.com, jwks on
# www.googleapis.com, userinfo on openidconnect.googleapis.com — which is why
# a global same-origin rule had to become a per-profile one.
GOOGLE_ISSUER = "https://accounts.google.com"
_GOOGLE_ORIGINS = frozenset(
    {
        "https://accounts.google.com",
        "https://oauth2.googleapis.com",
        "https://openidconnect.googleapis.com",
        "https://www.googleapis.com",
    }
)

_ENTRA_LOGIN_ORIGIN = "https://login.microsoftonline.com"


def _require_claim(payload: Mapping[str, Any], name: str, *, provider: str) -> str:
    """Read one required string claim, or fail the login.

    A single named accessor for foreign data, per ADR-032: the alternative is
    ``.get()`` scattered through four mapping functions, each deciding for
    itself what a missing claim means.
    """
    if name not in payload:
        raise AuthenticationError(f"{provider} ID token is missing the required claim {name!r}")
    value = payload[name]
    if type(value) is not str or not value.strip():
        raise AuthenticationError(f"{provider} claim {name!r} must be a non-blank string")
    return value


def _optional_claim(payload: Mapping[str, Any], name: str) -> str | None:
    """Read one optional string claim, discarding anything that is not one.

    Deliberately lenient: these feed display fields, and an IdP sending a
    number where a name belongs should not deny anyone access.
    """
    if name not in payload:
        return None
    value = payload[name]
    if type(value) is not str or not value.strip():
        return None
    return value


def _claim_is_exactly_true(payload: Mapping[str, Any], name: str) -> bool:
    """True only for the JSON boolean ``true``, never for a truthy value.

    ``"false"`` is a non-empty string and so is ``"0"``; an IdP that sends
    either — or that omits the claim entirely — must not be read as asserting
    anything. Read through this rather than ``.get()`` so the absent case is
    stated once instead of at every call site.
    """
    if name not in payload:
        return False
    return payload[name] is True


def _first_present(payload: Mapping[str, Any], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = _optional_claim(payload, name)
        if value is not None:
            return value
    return None


# --- issuer resolution -------------------------------------------------


def issuer_from_settings(settings: WebSettings) -> str:
    """Generic OIDC and VANguard: the operator states the issuer."""
    assert settings.sso_issuer is not None, "required_settings guarantees sso_issuer for this profile"
    return validate_oidc_issuer(settings.sso_issuer)


def issuer_from_entra_tenant(settings: WebSettings) -> str:
    """Entra derives its issuer from the tenant, so there is one source.

    Accepting ``sso_issuer`` as well would let an operator point the tenant
    check and the issuer check at different directories.
    """
    assert settings.entra_tenant_id is not None, "required_settings guarantees entra_tenant_id"
    return validate_oidc_issuer(f"{_ENTRA_LOGIN_ORIGIN}/{settings.entra_tenant_id}/v2.0")


def google_issuer(settings: WebSettings) -> str:
    """Fixed. The bare ``accounts.google.com`` form is rejected upstream."""
    del settings
    return validate_oidc_issuer(GOOGLE_ISSUER)


# --- endpoint origin policy --------------------------------------------


def same_origin_as_issuer(settings: WebSettings, issuer: str) -> frozenset[str]:
    """VANguard: every endpoint sits on the issuer's own origin (measured)."""
    del settings
    return frozenset({https_url_origin(issuer)})


def issuer_origin_plus_operator_origins(settings: WebSettings, issuer: str) -> frozenset[str]:
    """Generic OIDC: the issuer's origin plus whatever the operator declares.

    Cognito needs this and is the reason it exists: its hosted login domain
    is a different origin from the user-pool issuer, so a same-origin rule
    would refuse a correctly configured deployment.
    """
    return frozenset({https_url_origin(issuer), *settings.sso_endpoint_origins})


def entra_origins(settings: WebSettings, issuer: str) -> frozenset[str]:
    del settings, issuer
    return frozenset({_ENTRA_LOGIN_ORIGIN})


def google_origins(settings: WebSettings, issuer: str) -> frozenset[str]:
    del settings, issuer
    return _GOOGLE_ORIGINS


# --- claim checks beyond standard validation ---------------------------


def no_extra_claim_checks(payload: Mapping[str, Any], settings: WebSettings) -> None:
    del payload, settings


def check_entra_tenant(payload: Mapping[str, Any], settings: WebSettings) -> None:
    """The ``tid`` must be the tenant this container is configured for.

    Without it, any Entra tenant that trusts the same multi-tenant app
    registration authenticates against this deployment.
    """
    assert settings.entra_tenant_id is not None, "required_settings guarantees entra_tenant_id"
    tenant = _require_claim(payload, "tid", provider="entra")
    if tenant != settings.entra_tenant_id:
        raise AuthenticationError("Entra ID token was issued by a different tenant")


def check_google_hosted_domain(payload: Mapping[str, Any], settings: WebSettings) -> None:
    """Verified email plus the configured Workspace domain, both required.

    ``hd`` is emitted for Workspace accounts ONLY and is absent from Google's
    published ``claims_supported``, so a personal account produces a token
    with no ``hd`` at all. Treating absence as "no restriction" would make
    every Google account on earth a valid login, which is why this fails
    closed on a missing claim rather than skipping the check.
    """
    assert settings.google_hosted_domain is not None, "required_settings guarantees google_hosted_domain"
    if not _claim_is_exactly_true(payload, "email_verified"):
        raise AuthenticationError("Google ID token does not assert a verified email")
    hosted_domain = _require_claim(payload, "hd", provider="google")
    if hosted_domain != settings.google_hosted_domain:
        raise AuthenticationError("Google ID token is from a different hosted domain")


# --- identity mapping (Tier 3 -> owned type) ---------------------------


def map_generic_oidc(id_claims: Mapping[str, Any], userinfo: Mapping[str, Any] | None) -> IdentityClaims:
    """Cognito and other generic providers.

    ``preferred_username`` then ``cognito:username`` then ``sub``: the first
    is the standard claim, the second is what Cognito actually sends, and the
    subject is the guaranteed fallback because it is the identity key anyway.
    """
    del userinfo
    subject = _require_claim(id_claims, "sub", provider="oidc")
    return IdentityClaims(
        provider="oidc",
        subject=subject,
        username=_first_present(id_claims, ("preferred_username", "cognito:username")) or subject,
        display_name=_optional_claim(id_claims, "name"),
        email=_optional_claim(id_claims, "email"),
    )


def map_entra(id_claims: Mapping[str, Any], userinfo: Mapping[str, Any] | None) -> IdentityClaims:
    """Entra. Groups and roles are NOT collected (D17).

    IdP groups are organisation facts, not compartment facts: a group name
    from a directory this container does not administer says nothing about
    what someone may do here. Authority comes from ``identity_roles``.
    """
    del userinfo
    subject = _require_claim(id_claims, "sub", provider="entra")
    return IdentityClaims(
        provider="entra",
        subject=subject,
        username=_optional_claim(id_claims, "preferred_username") or subject,
        display_name=_optional_claim(id_claims, "name"),
        email=_optional_claim(id_claims, "email"),
    )


def map_google(id_claims: Mapping[str, Any], userinfo: Mapping[str, Any] | None) -> IdentityClaims:
    del userinfo
    subject = _require_claim(id_claims, "sub", provider="google")
    return IdentityClaims(
        provider="google",
        subject=subject,
        username=_optional_claim(id_claims, "email") or subject,
        display_name=_optional_claim(id_claims, "name"),
        email=_optional_claim(id_claims, "email"),
    )


def map_vanguard(id_claims: Mapping[str, Any], userinfo: Mapping[str, Any] | None) -> IdentityClaims:
    """VANguard, the only profile that calls userinfo.

    The subject is an email TODAY (measured 2026-09-02); whether it is stable
    and non-email is the open question D10 turns on, and the rebound
    detection on ``identities`` exists precisely because it may not be. The
    mapping does not care: it keys on ``sub`` either way, and R3 notices if
    the email behind a subject changes.

    ``abn`` becomes ``organisation_id``. The display name is assembled from
    name parts because VANguard does not send a composed ``name``.
    """
    subject = _require_claim(id_claims, "sub", provider="vanguard")
    claims: Mapping[str, Any] = userinfo if userinfo is not None else id_claims
    given = _optional_claim(claims, "given_name")
    family = _optional_claim(claims, "family_name")
    display_name = " ".join(part for part in (given, family) if part) or None
    return IdentityClaims(
        provider="vanguard",
        subject=subject,
        username=subject,
        display_name=display_name,
        email=_optional_claim(claims, "email") or subject,
        organisation_id=_optional_claim(claims, "abn"),
    )
