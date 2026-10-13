"""Small session-authority double for token cryptography unit tests.

Persistence and concurrency use RepositoryAuthSessionAuthority in their own
tests. This double is explicitly supplied, never a production fallback.
"""

import time
from dataclasses import dataclass

from elspeth.contracts.auth import AuthProviderType
from elspeth.web.auth.session_token import SessionBinding, SessionTokenClaims


def make_session_binding() -> SessionBinding:
    return SessionBinding("test-session-family", 0, int(time.time()) + 86400)


def make_session_claims(token: str) -> SessionTokenClaims:
    assert token == "authenticated-test-token"
    binding = make_session_binding()
    return SessionTokenClaims("test-user", "testuser", "local", int(time.time()), binding.expires_at, "test-jti", binding)


@dataclass
class _Family:
    identity_id: str
    provider: AuthProviderType
    expires_at: int
    revoked: bool = False


class TestSessionFamilies:
    __test__ = False

    def __init__(self) -> None:
        self.rows: dict[str, _Family] = {}

    def create(self, *, family_id: str, identity_id: str, provider: AuthProviderType, expires_at: int) -> None:
        assert family_id not in self.rows
        self.rows[family_id] = _Family(identity_id, provider, expires_at)

    def is_active(self, *, family_id: str, identity_id: str, provider: AuthProviderType) -> bool:
        row = self.rows.get(family_id)
        return (
            row is not None
            and row.identity_id == identity_id
            and row.provider == provider
            and not row.revoked
            and row.expires_at > time.time()
        )

    def revoke(self, *, family_id: str, identity_id: str, provider: AuthProviderType) -> None:
        row = self.rows.get(family_id)
        if row is not None and row.identity_id == identity_id and row.provider == provider:
            row.revoked = True
