"""Authentication provider protocols.

AuthProvider: the two-method interface that all auth implementations satisfy.
CredentialAuthProvider: extends AuthProvider with login() and refresh() for
providers that support username/password authentication (e.g., LocalAuthProvider).

No exception definitions here -- AuthenticationError lives in models.py.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from elspeth.web.auth.models import UserIdentity, UserProfile


@runtime_checkable
class AuthProvider(Protocol):
    """Protocol for pluggable authentication providers."""

    async def authenticate(self, token: str) -> UserIdentity:
        """Validate a token and return the authenticated identity.

        Raises AuthenticationError if the token is invalid, expired,
        or otherwise unacceptable. Raises AuthProviderUnavailable when
        upstream provider availability prevents validation.
        """
        ...

    async def get_user_info(self, token: str) -> UserProfile:
        """Get full user profile from a valid token.

        Raises AuthenticationError if the token is invalid. Raises
        AuthProviderUnavailable when upstream provider availability prevents
        profile lookup.
        """
        ...


class CredentialAuthProvider(AuthProvider, Protocol):
    """AuthProvider that also supports username/password login and token refresh.

    Used by local (and future LDAP) auth providers. Routes check
    settings.auth_provider to determine if these methods are available,
    then narrow the type to CredentialAuthProvider for method access.
    """

    async def login(self, username: str, password: str) -> str:
        """Authenticate with credentials and return a JWT.

        Raises AuthenticationError on invalid credentials.
        """
        ...

    async def refresh(self, token: str) -> str:
        """Issue a successor token from a valid existing one.

        Takes the token itself so the provider enforces the refresh-chain
        bound against its OWN verified decode. Passing a caller-extracted
        ``iat`` would mean a security bound reading a value the caller
        obtained without verifying a signature.

        Raises AuthenticationError if the token is invalid, the identity may
        no longer act, the account no longer exists, or the chain has expired.
        """
        ...
