"""Per-IdP behaviour: issuer resolution, origin policy, claim checks, mapping.

One module of plain functions rather than four subclasses, so each behaviour
is named, documented with the provider quirk that motivates it, and testable
without constructing a profile. The registry wires them onto profiles.

``map_identity`` and ``claim_checks`` read the OWNED claims -- the ID
token's :class:`IdTokenClaims` and, for the one profile that calls it,
userinfo's :class:`UserinfoClaims`. Both were parsed at their document's
trust boundary (``auth/id_token.py``, ``auth/sso.py``); nothing here sees a
raw claim dict, and a profile that needs a claim the closed set lacks adds
the field there, typed, rather than reaching around the boundary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from elspeth.web.auth.claims import IdTokenClaims, UserinfoClaims
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


def no_extra_claim_checks(claims: IdTokenClaims, settings: WebSettings) -> None:
    del claims, settings


def check_entra_tenant(claims: IdTokenClaims, settings: WebSettings) -> None:
    """The ``tid`` must be the tenant this container is configured for.

    Without it, any Entra tenant that trusts the same multi-tenant app
    registration authenticates against this deployment. Absent -- which the
    boundary also reads a non-string ``tid`` as -- is refused, not skipped.
    """
    assert settings.entra_tenant_id is not None, "required_settings guarantees entra_tenant_id"
    if claims.tenant_id is None:
        raise AuthenticationError("Entra ID token is missing the required claim 'tid'")
    if claims.tenant_id != settings.entra_tenant_id:
        raise AuthenticationError("Entra ID token was issued by a different tenant")


def check_google_hosted_domain(claims: IdTokenClaims, settings: WebSettings) -> None:
    """Verified email plus the configured Workspace domain, both required.

    ``hd`` is emitted for Workspace accounts ONLY and is absent from Google's
    published ``claims_supported``, so a personal account produces a token
    with no ``hd`` at all. Treating absence as "no restriction" would make
    every Google account on earth a valid login, which is why this fails
    closed on a missing claim rather than skipping the check.
    """
    assert settings.google_hosted_domain is not None, "required_settings guarantees google_hosted_domain"
    if not claims.email_verified:
        raise AuthenticationError("Google ID token does not assert a verified email")
    if claims.hosted_domain is None:
        raise AuthenticationError("Google ID token is missing the required claim 'hd'")
    if claims.hosted_domain != settings.google_hosted_domain:
        raise AuthenticationError("Google ID token is from a different hosted domain")


# --- identity mapping (owned claims -> IdentityClaims) -----------------


def map_generic_oidc(id_claims: IdTokenClaims, userinfo: UserinfoClaims | None) -> IdentityClaims:
    """Cognito and other generic providers.

    ``preferred_username`` then ``cognito:username`` then ``sub``: the first
    is the standard claim, the second is what Cognito actually sends, and the
    subject is the guaranteed fallback because it is the identity key anyway.
    """
    del userinfo
    return IdentityClaims(
        provider="oidc",
        subject=id_claims.subject,
        username=id_claims.preferred_username or id_claims.cognito_username or id_claims.subject,
        display_name=id_claims.name,
        email=id_claims.email,
    )


def map_entra(id_claims: IdTokenClaims, userinfo: UserinfoClaims | None) -> IdentityClaims:
    """Entra. Groups and roles are NOT collected (D17).

    IdP groups are organisation facts, not compartment facts: a group name
    from a directory this container does not administer says nothing about
    what someone may do here. Authority comes from ``identity_roles``.
    """
    del userinfo
    return IdentityClaims(
        provider="entra",
        subject=id_claims.subject,
        username=id_claims.preferred_username or id_claims.subject,
        display_name=id_claims.name,
        email=id_claims.email,
    )


def map_google(id_claims: IdTokenClaims, userinfo: UserinfoClaims | None) -> IdentityClaims:
    del userinfo
    return IdentityClaims(
        provider="google",
        subject=id_claims.subject,
        username=id_claims.email or id_claims.subject,
        display_name=id_claims.name,
        email=id_claims.email,
    )


def map_vanguard(id_claims: IdTokenClaims, userinfo: UserinfoClaims | None) -> IdentityClaims:
    """VANguard, the only profile that calls userinfo.

    The subject is an email TODAY (measured 2026-09-02); whether it is stable
    and non-email is the open question D10 turns on, and the rebound
    detection columns on ``identities`` exist precisely because it may not
    be. The mapping does not care: it keys on ``sub`` either way.

    Those columns are RECORDING ONLY. ``subject_email_at_first_seen`` is
    written at first sight and compared nowhere, ``rebound_at`` is only ever
    written NULL, and no ``disable_reason='rebound'`` exists in the tree: the
    R3 refusal is specified (spec §Refusals R3, D32) and NOT implemented
    (elspeth-9c25083a03). An earlier version of this docstring said "R3
    notices if the email behind a subject changes", which asserted behaviour
    the tree does not have. Until R3 is built, a provider that recycles a
    subject binds the new holder to the prior one's identity row.

    ``abn`` becomes ``organisation_id``. The display name is assembled from
    name parts because VANguard does not send a composed ``name``.
    """
    source: IdTokenClaims | UserinfoClaims = userinfo if userinfo is not None else id_claims
    display_name = " ".join(part for part in (source.given_name, source.family_name) if part) or None
    return IdentityClaims(
        provider="vanguard",
        subject=id_claims.subject,
        username=id_claims.subject,
        display_name=display_name,
        email=source.email or id_claims.subject,
        organisation_id=source.abn,
    )
