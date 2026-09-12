"""Explicit identity setup for tests which create identity-owned rows."""

from datetime import UTC, datetime

from sqlalchemy import Connection, insert, select

from elspeth.web.sessions.models import identities_table


def ensure_test_identity(conn: Connection, *, identity_id: str, provider: str = "local") -> None:
    """Create a test owner if absent; preserve any existing identity's state.

    This is fixture setup, never production admission or automatic role grant.
    Existing rows may deliberately be pending, disabled or bound differently.
    """
    if conn.execute(select(identities_table.c.identity_id).where(identities_table.c.identity_id == identity_id)).first() is not None:
        return
    conn.execute(
        insert(identities_table).values(
            identity_id=identity_id,
            provider=provider,
            subject=identity_id,
            username=identity_id,
            first_seen_at=datetime.now(UTC),
            access_state="active",
            activated_at=datetime.now(UTC),
        )
    )
