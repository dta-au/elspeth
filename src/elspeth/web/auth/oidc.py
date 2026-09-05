"""The legacy OIDC authentication provider.

SCHEDULED FOR DELETION with the pluggable-SSO cutover: the profile registry in
``auth/providers`` plus the login walk in ``auth/sso.py`` replace it. What is
NOT legacy is ID-token validation, which moved to ``auth/id_token.py`` ahead of
that deletion so the SSO routes never depended on a module marked to be
removed.

The re-export below keeps ``auth/entra.py`` — also scheduled for deletion —
importing from here until both go together.
"""

from __future__ import annotations

import httpx
import structlog

from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.web.auth.id_token import JWKSTokenValidator
from elspeth.web.auth.models import UserIdentity, UserProfile
from elspeth.web.auth.providers import get_profile

__all__ = ["JWKSTokenValidator", "OIDCAuthProvider"]

slog = structlog.get_logger()


class OIDCAuthProvider:
    """Validates OIDC bearer tokens via JWKS discovery.

    The accepted signature algorithms are the ``oidc`` profile's pinned
    list, fixed at construction. The Cognito access-token mode that used to
    live here (``audience_claim="client_id"``) is gone with the branch that
    served it: Cognito re-registers as a confidential client through the SSO
    profile (spec D2), and its access tokens are not bearer credentials for
    this API.
    """

    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_cache_ttl_seconds: int = 3600,
        jwks_failure_retry_seconds: int = 300,
        jwks_max_stale_seconds: int = 86_400,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._validator = JWKSTokenValidator(
            issuer,
            audience,
            jwks_cache_ttl_seconds,
            jwks_failure_retry_seconds,
            jwks_max_stale_seconds,
            algorithms=get_profile("oidc").id_token_algorithms,
            transport=transport,
        )

    async def authenticate(self, token: str) -> UserIdentity:
        """Validate an OIDC token and return the authenticated identity."""
        claims = await self._validator.decode_token_with_refresh(token)

        # preferred_username is an optional cosmetic claim, read as None by
        # the token boundary when absent or not a visible string; fall back
        # to the canonical `sub` identifier (always a valid principal).
        return UserIdentity(
            user_id=claims.subject,
            username=claims.preferred_username if claims.preferred_username is not None else claims.subject,
        )

    @trust_boundary(
        tier=3,
        source="OIDC bearer access token from a remote IdP; decoded payload carries optional profile claims including 'groups'",
        source_param="token",
        suppresses=("R1",),
        invariant="raises AuthenticationError on malformed non-list 'groups'; treats absent 'groups' as no groups; never coerces scalar groups silently",
        test_ref="tests/unit/web/auth/test_oidc_provider.py::TestOIDCGetUserInfo::test_non_list_groups_claim_raises",
        test_fingerprint="cefa7844868a4e9b7662d3966a910dc1698332f477a06c5b9050ddb699657898",
    )
    async def get_user_info(self, token: str) -> UserProfile:
        """Decode the OIDC token and extract profile claims.

        The ``groups`` shape rule (absent is none, a list is coerced to
        strings, anything else is refused) is applied by the token boundary
        in ``auth/id_token.py``; this reads the owned result.
        """
        claims = await self._validator.decode_token_with_refresh(token)

        display_name = claims.name if claims.name is not None else claims.preferred_username
        username = claims.preferred_username if claims.preferred_username is not None else claims.subject

        return UserProfile(
            user_id=claims.subject,
            username=username,
            display_name=display_name,
            email=claims.email,
            groups=claims.groups,
        )
