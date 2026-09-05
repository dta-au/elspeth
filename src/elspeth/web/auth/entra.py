"""Azure Entra ID authentication provider.

Uses JWKSTokenValidator via composition, adding Entra-specific tenant
validation and group/role claim extraction. The OIDC issuer is derived
from the tenant_id.
"""

from __future__ import annotations

from elspeth.web.auth.claims import IdTokenClaims
from elspeth.web.auth.models import AuthenticationError, UserIdentity, UserProfile
from elspeth.web.auth.oidc import JWKSTokenValidator
from elspeth.web.auth.providers import get_profile


class EntraAuthProvider:
    """Validates Azure Entra ID tokens with tenant and group claim handling.

    Composes JWKSTokenValidator for JWKS discovery and JWT decode, adding:
    - Tenant ID verification (``tid`` claim must match expected tenant)
    - Group claim extraction (``groups`` + ``role:``-prefixed ``roles``)
    """

    def __init__(
        self,
        tenant_id: str,
        audience: str,
        jwks_cache_ttl_seconds: int = 3600,
        jwks_failure_retry_seconds: int = 300,
        jwks_max_stale_seconds: int = 86_400,
    ) -> None:
        self._tenant_id = tenant_id
        issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
        self._validator = JWKSTokenValidator(
            issuer=issuer,
            audience=audience,
            jwks_cache_ttl_seconds=jwks_cache_ttl_seconds,
            jwks_failure_retry_seconds=jwks_failure_retry_seconds,
            jwks_max_stale_seconds=jwks_max_stale_seconds,
            algorithms=get_profile("entra").id_token_algorithms,
        )

    def _validate_tenant(self, claims: IdTokenClaims) -> None:
        """Verify the tid claim matches the expected tenant.

        Raises AuthenticationError if ``tid`` is missing or mismatched.
        The ``tid`` claim is required in Entra ID tokens -- absence (which
        the token boundary also reads a non-string ``tid`` as) indicates a
        non-Entra token or a configuration error.
        """
        if claims.tenant_id is None:
            raise AuthenticationError("Missing tenant claim (tid) — token may not be from Entra ID")
        if claims.tenant_id != self._tenant_id:
            raise AuthenticationError(f"Invalid tenant: received tid={claims.tenant_id!r}")

    @staticmethod
    def _extract_groups(claims: IdTokenClaims) -> tuple[str, ...]:
        """Group IDs plus role-prefixed entries from the owned Entra claims.

        The token boundary already applied the shape rule (absent is none, a
        list is coerced to strings, anything else refused the token) and
        recorded whether Entra emitted a group-overage marker instead of the
        groups themselves. Overage must not become empty membership.
        """
        if claims.groups_overage:
            raise AuthenticationError("Entra token contains a group overage marker; group membership must be resolved via Microsoft Graph")
        return claims.groups + tuple(f"role:{role}" for role in claims.roles)

    async def authenticate(self, token: str) -> UserIdentity:
        """Validate an Entra ID token with tenant verification.

        Performs standard OIDC validation (signature, expiry, issuer,
        audience) via JWKSTokenValidator, then checks the tenant claim.
        """
        claims = await self._validator.decode_token_with_refresh(token)

        self._validate_tenant(claims)

        return UserIdentity(
            user_id=claims.subject,
            # preferred_username is an optional cosmetic claim, read as None by
            # the token boundary when absent, null, non-string or blank.
            username=claims.preferred_username or claims.subject,
        )

    async def get_user_info(self, token: str) -> UserProfile:
        """Decode an Entra ID token and extract profile with group claims."""
        claims = await self._validator.decode_token_with_refresh(token)

        self._validate_tenant(claims)

        display_name = claims.name if claims.name is not None else claims.preferred_username

        return UserProfile(
            user_id=claims.subject,
            username=claims.preferred_username or claims.subject,
            display_name=display_name,
            email=claims.email,
            groups=self._extract_groups(claims),
        )
