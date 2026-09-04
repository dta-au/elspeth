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

from typing import Any, Literal, cast

import structlog

from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.web.auth.id_token import JWKSTokenValidator
from elspeth.web.auth.models import AuthenticationError, UserIdentity, UserProfile
from elspeth.web.validation import has_visible_content

__all__ = ["JWKSTokenValidator", "OIDCAuthProvider", "optional_profile_claim"]

slog = structlog.get_logger()


def optional_profile_claim(payload: dict[str, Any], claim_name: str) -> str | None:
    """Return optional cosmetic IdP claims as visible strings or None."""
    # Tier-3 token claims: an optional claim may simply be absent. Read it
    # explicitly (membership-then-subscript) so the "absent -> None" step is
    # visible decision-making rather than a `.get()` that hides it.
    value = payload[claim_name] if claim_name in payload else None
    if value is None or not isinstance(value, str):
        return None
    claim_value = cast(str, value)
    if not has_visible_content(claim_value):
        return None
    return claim_value


class OIDCAuthProvider:
    """Validates OIDC tokens via JWKS discovery."""

    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_cache_ttl_seconds: int = 3600,
        jwks_failure_retry_seconds: int = 300,
        jwks_max_stale_seconds: int = 86_400,
        *,
        audience_claim: Literal["aud", "client_id"] = "aud",
    ) -> None:
        self._validator = JWKSTokenValidator(
            issuer,
            audience,
            jwks_cache_ttl_seconds,
            jwks_failure_retry_seconds,
            jwks_max_stale_seconds,
            audience_claim=audience_claim,
        )

    async def authenticate(self, token: str) -> UserIdentity:
        """Validate an OIDC token and return the authenticated identity."""
        payload = dict(await self._validator.decode_token_with_refresh(token))

        try:
            sub = payload["sub"]
        except KeyError as exc:
            raise AuthenticationError("Missing required 'sub' claim in token") from exc

        # preferred_username is an optional cosmetic claim. Decide the username
        # explicitly: use the IdP-supplied visible value when present, otherwise
        # fall back to the canonical `sub` identifier (always a valid principal).
        preferred_username = self._optional_profile_claim(payload, "preferred_username")
        username = preferred_username if preferred_username is not None else sub

        return UserIdentity(
            user_id=sub,
            username=username,
        )

    @staticmethod
    def _optional_profile_claim(payload: dict[str, Any], claim_name: str) -> str | None:
        """Return optional cosmetic claims as visible strings or None."""
        return optional_profile_claim(payload, claim_name)

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
        """Decode the OIDC token and extract profile claims."""
        payload = dict(await self._validator.decode_token_with_refresh(token))

        try:
            sub = payload["sub"]
        except KeyError as exc:
            raise AuthenticationError("Missing required 'sub' claim in token") from exc

        raw_groups = payload.get("groups")
        if raw_groups is None:
            groups: list[str] = []
        elif isinstance(raw_groups, list):
            # Coerce group IDs to str — IdPs may send integers (e.g. Entra
            # group object IDs). This is intentional Tier 3 coercion.
            groups = [str(g) for g in raw_groups]
        else:
            raise AuthenticationError(
                f"Unexpected type for 'groups' claim: {type(raw_groups).__name__} (expected list) — check IdP token configuration"
            )

        display_name = self._optional_profile_claim(payload, "name")
        if display_name is None:
            display_name = self._optional_profile_claim(payload, "preferred_username")

        # preferred_username is an optional cosmetic claim. Decide the username
        # explicitly: use the IdP-supplied visible value when present, otherwise
        # fall back to the canonical `sub` identifier (always a valid principal).
        preferred_username = self._optional_profile_claim(payload, "preferred_username")
        username = preferred_username if preferred_username is not None else sub

        return UserProfile(
            user_id=sub,
            username=username,
            display_name=display_name,
            email=self._optional_profile_claim(payload, "email"),
            groups=tuple(groups),
        )
