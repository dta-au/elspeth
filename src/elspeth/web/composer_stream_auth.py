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
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.sessions.protocol import SessionNotFoundError, SessionServiceProtocol


@dataclass(frozen=True, slots=True)
class ComposerStreamAuthServices:
    """Reuse the configured provider and issuer without copying key material."""

    provider: LocalAuthProvider | SsoAuthProvider
    decode: Callable[[str], SessionTokenClaims]
    identity_authority: RepositoryIdentityAuthority
    sessions: SessionServiceProtocol
    provider_type: AuthProviderType

    async def authorize(self, *, token: str, principal: UserIdentity, session_id: UUID) -> bool:
        try:
            claims = self.decode(token)
            if claims.identity_id != principal.user_id or claims.provider != self.provider_type or claims.expires_at <= time.time():
                return False
            identity = await run_stream_read_in_worker(self.provider._authenticate_sync, token)
            if identity.user_id != principal.user_id:
                return False
            if not await run_stream_read_in_worker(
                self.identity_authority.holds_active_human_role,
                identity_id=principal.user_id,
                provider=self.provider_type,
                role="user",
            ):
                return False
            session = await run_stream_read_in_worker(self.sessions.get_session_for_stream, session_id)
            return session.user_id == principal.user_id and session.auth_provider_type == self.provider_type and session.archived_at is None
        except (AuthenticationError, ValueError, SessionNotFoundError):
            return False
