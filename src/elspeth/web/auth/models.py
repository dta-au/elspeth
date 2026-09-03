"""Authentication data models.

UserIdentity and UserProfile are frozen dataclasses. All fields are scalars,
None, or tuple of scalars -- no freeze guard needed.

AuthenticationError is the domain exception raised by all auth providers
when token validation fails. AuthProviderUnavailable is the domain exception
for upstream provider availability failures.
"""

from __future__ import annotations

from dataclasses import dataclass

from elspeth.contracts.auth import AuthProviderType
from elspeth.web.validation import has_visible_content


@dataclass(frozen=True, slots=True)
class UserIdentity:
    """Minimal authenticated identity -- returned from every auth check."""

    user_id: str
    username: str

    def __post_init__(self) -> None:
        if not isinstance(self.user_id, str) or not has_visible_content(self.user_id):
            raise AuthenticationError("user_id must be a non-blank string with visible content")
        if not isinstance(self.username, str) or not has_visible_content(self.username):
            raise AuthenticationError("username must be a non-blank string with visible content")


@dataclass(frozen=True, slots=True)
class UserProfile:
    """Extended user profile information."""

    user_id: str
    username: str
    display_name: str | None = None
    email: str | None = None
    groups: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.user_id, str) or not has_visible_content(self.user_id):
            raise AuthenticationError("user_id must be a non-blank string with visible content")
        if not isinstance(self.username, str) or not has_visible_content(self.username):
            raise AuthenticationError("username must be a non-blank string with visible content")
        # Coerce invisible-only display_name to None rather than raising —
        # display_name is cosmetic IdP metadata, not a security-critical
        # identity field.  Denying auth for a bad display name would be
        # disproportionate.
        if self.display_name is not None and not has_visible_content(self.display_name):
            object.__setattr__(self, "display_name", None)
        if self.email is not None and not has_visible_content(self.email):
            object.__setattr__(self, "email", None)


class AuthenticationError(Exception):
    """Raised when authentication fails.

    Caught by the auth middleware and converted to HTTP 401.
    """

    def __init__(self, detail: str = "Authentication failed") -> None:
        self.detail = detail
        super().__init__(detail)


class AuthProviderUnavailable(AuthenticationError):
    """Raised when an upstream auth provider cannot validate availability.

    This is separate from invalid credentials: the client may hold a valid
    token, but the provider's discovery/JWKS service is unavailable. HTTP
    routes map this to 503 rather than telling clients to re-authenticate.
    """

    def __init__(self, detail: str = "Authentication provider unavailable") -> None:
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class IdentityClaims:
    """What one IdP login says about who the person is.

    The OWNED type at the end of the Tier-3 boundary: a profile's
    ``map_identity`` reads whatever shape the provider sent and constructs
    this, so nothing downstream ever touches raw IdP data. Every field is a
    scalar or None, and every one is validated here rather than trusted.

    This is not a session and not an authorisation. It says who logged in;
    whether they may do anything is ``identities.access_state`` plus their
    roles, read on every request.
    """

    provider: AuthProviderType
    subject: str
    username: str
    display_name: str | None = None
    email: str | None = None
    organisation_id: str | None = None

    def __post_init__(self) -> None:
        # ``(provider, subject)`` is the identity key. A blank subject would
        # collapse every identity from one provider onto a single row, so it
        # is refused here as well as by the database.
        if not has_visible_content(self.subject):
            raise AuthenticationError("IdP subject must be a non-blank string with visible content")
        if not has_visible_content(self.username):
            raise AuthenticationError("username must be a non-blank string with visible content")
        # Cosmetic metadata is coerced rather than fatal, matching
        # UserProfile: refusing a login over a blank display name would be
        # disproportionate. An email that is present but blank is dropped for
        # the same reason -- it is never an authorisation input.
        #
        # Written out rather than looped over field names: resolving an
        # attribute from a string on a type we own is dynamic access, which
        # this repository inventories and forbids outside a declared Tier-3
        # parse. Three lines are cheaper than an adjudicated exemption.
        if self.display_name is not None and not has_visible_content(self.display_name):
            object.__setattr__(self, "display_name", None)
        if self.email is not None and not has_visible_content(self.email):
            object.__setattr__(self, "email", None)
        if self.organisation_id is not None and not has_visible_content(self.organisation_id):
            object.__setattr__(self, "organisation_id", None)
