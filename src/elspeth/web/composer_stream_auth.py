"""Factory-owned live authorization for Composer progress connections."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from elspeth.contracts.auth import AuthProviderType
from elspeth.web.async_workers import run_stream_read_in_worker
from elspeth.web.auth.local import LocalAuthProvider
from elspeth.web.auth.models import AuthenticationError, UserIdentity
from elspeth.web.auth.session_token import SessionTokenClaims
from elspeth.web.auth.sso import SsoAuthProvider
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority, StreamSessionScope
from elspeth.web.sessions.protocol import SessionServiceProtocol


@dataclass(frozen=True, slots=True)
class ComposerStreamAuthServices:
    """Reuse the configured provider and issuer without copying key material."""

    provider: LocalAuthProvider | SsoAuthProvider
    decode: Callable[[str], SessionTokenClaims]
    identity_authority: RepositoryIdentityAuthority
    sessions: SessionServiceProtocol
    provider_type: AuthProviderType

    async def authorize(self, *, token: str, principal: UserIdentity, session_id: UUID) -> bool:
        """Decide one send from the token and one database instant.

        The checks are the provider's ``authenticate`` (binding expiry, active
        identity, live session family, and for a local provider the current
        credential), the live ``user`` role, and the session's owner, provider
        and archive marker. The database half runs on one connection with one
        clock sample (``read_stream_session_scope``) instead of four
        connections and three samples; the identity row is read once.
        """
        try:
            claims = self.decode(token)
            if claims.identity_id != principal.user_id or claims.provider != self.provider_type or claims.expires_at <= time.time():
                return False
            session = await run_stream_read_in_worker(self._read_session_scope, claims, session_id)
            if session is None:
                return False
            return session.user_id == principal.user_id and session.auth_provider_type == self.provider_type and session.archived_at is None
        except (AuthenticationError, ValueError):
            return False

    def _read_session_scope(self, claims: SessionTokenClaims, session_id: UUID) -> StreamSessionScope | None:
        # The issuer's ``authorize_binding`` refuses an expired binding before
        # it reads anything; the same comparison, at the same point.
        if claims.binding.expires_at <= time.time():
            return None
        scope = self.identity_authority.read_stream_session_scope(
            identity_id=claims.identity_id,
            provider=self.provider_type,
            family_id=claims.binding.family_id,
            role="user",
            session_id=session_id,
        )
        if scope is None:
            return None
        # ``auth.db`` is a separate database: the local provider's credential
        # check cannot share the sessions connection.
        if isinstance(self.provider, LocalAuthProvider) and not self.provider._credential_is_current(
            claims.username, claims.binding.credential_generation
        ):
            return None
        return scope
