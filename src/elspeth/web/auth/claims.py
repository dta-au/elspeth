"""The owned shapes of what an IdP says: ID-token claims and a userinfo body.

Both documents come from OUTSIDE the trust domain, so per ADR-032 each is
parsed ONCE, at one ``@trust_boundary``, into a type this module owns with a
closed field set and value assertions. The parsers live at the boundaries
(``auth/id_token.py`` for the token, ``auth/sso.py`` for userinfo); this
module holds the types and the three value readers they share, so that "a
claim that is not a non-blank string is absent" is decided in one place.

The field set is CLOSED on purpose: it is exactly the claims some profile or
provider reads. A profile that needs a claim that is not here adds the field
and the assertion that makes it typed -- never a mapping escape hatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import final

from elspeth.web.auth.models import AuthenticationError
from elspeth.web.validation import has_visible_content

__all__ = [
    "IdTokenClaims",
    "UserinfoClaims",
    "claim_is_exactly_true",
    "optional_string_claim",
    "required_string_claim",
    "string_list_claim",
]


def optional_string_claim(value: object) -> str | None:
    """Read one optional string claim; anything that is not one is absent.

    Deliberately lenient: these feed display fields, and an IdP sending a
    number where a name belongs should not deny anyone access. "Visible
    content" rather than ``strip()``: a zero-width space is not a name.
    """
    if type(value) is not str or not has_visible_content(value):
        return None
    return value


def required_string_claim(value: object, *, name: str, document: str) -> str:
    """Read one required string claim, or refuse the document."""
    if value is None:
        raise AuthenticationError(f"{document} is missing the required claim {name!r}")
    if type(value) is not str or not has_visible_content(value):
        raise AuthenticationError(f"{document} claim {name!r} must be a non-blank string")
    return value


def claim_is_exactly_true(value: object) -> bool:
    """True only for the JSON boolean ``true``, never for a truthy value.

    ``"false"`` is a non-empty string and so is ``"0"``; an IdP that sends
    either -- or that omits the claim entirely -- must not be read as
    asserting anything.
    """
    return value is True


def string_list_claim(value: object, *, name: str) -> tuple[str, ...]:
    """Read a list-valued claim as a tuple of strings, or refuse the document.

    Absent is the empty tuple. Elements are rendered with ``str`` -- an IdP
    may send integers where identifiers belong (Entra group object ids), and
    that is the one deliberate Tier-3 coercion the legacy bearer path made.
    Any other shape is an IdP misconfiguration and is refused rather than
    read as "no entries".
    """
    if value is None:
        return ()
    if type(value) is not list:
        raise AuthenticationError(
            f"Unexpected type for {name!r} claim: {type(value).__name__} (expected list) — check IdP token configuration"
        )
    return tuple(str(entry) for entry in value)


@final
@dataclass(frozen=True, slots=True)
class IdTokenClaims:
    """The claims of ONE verified ID token, as the closed set ELSPETH reads.

    Constructed only by ``parse_id_token_claims`` in ``auth/id_token.py``,
    after PyJWT has verified the signature, issuer, audience and clock. The
    envelope fields are asserted again there because a verified signature
    says who minted the token, not that every claim has the type its name
    implies.

    ``audience`` keeps the wire distinction: a string when the token named
    one audience, a tuple when it carried a list. The authorized-party check
    turns on exactly that distinction and must not lose it.

    Optional profile claims are ``None`` when absent OR when present with the
    wrong type or no visible content; ``email_verified`` is true only for the
    JSON boolean.

    LEGACY BEARER PATH ONLY: ``groups``, ``roles`` and ``groups_overage``
    exist for ``OIDCAuthProvider`` and ``EntraAuthProvider`` and are deleted
    in the same commit that deletes them (identity sprint step E). The SSO
    walk never reads them (D17: IdP groups are organisation facts, not
    compartment facts).
    """

    issuer: str
    subject: str
    audience: str | tuple[str, ...]
    issued_at: int
    expires_at: int
    nonce: str | None = None
    authorized_party: str | None = None
    preferred_username: str | None = None
    name: str | None = None
    email: str | None = None
    email_verified: bool = False
    tenant_id: str | None = None
    """Entra ``tid``."""
    hosted_domain: str | None = None
    """Google Workspace ``hd``; absent for every personal account."""
    cognito_username: str | None = None
    """Cognito ``cognito:username``."""
    given_name: str | None = None
    family_name: str | None = None
    abn: str | None = None
    """VANguard's Australian Business Number claim."""
    groups: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    groups_overage: bool = False
    """Entra emitted a group-overage marker instead of the groups themselves."""


@final
@dataclass(frozen=True, slots=True)
class UserinfoClaims:
    """A userinfo body, bound to the ID token's subject, as the closed set read.

    Constructed only by ``parse_userinfo`` in ``auth/sso.py``, whose one
    check of its own is that ``sub`` equals the verified token's. VANguard is
    the only profile that calls userinfo, and these are the fields it reads.
    """

    subject: str
    email: str | None = None
    given_name: str | None = None
    family_name: str | None = None
    abn: str | None = None
