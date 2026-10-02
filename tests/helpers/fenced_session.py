"""A registered mutation-connection token over one owned session, for authority-level tests.

The identity-workflow authorities (quota Task I1, approvals Task I3) take a
``connection_token`` exactly as production facets hand one over. These fixtures
build the smallest real version of that: an active identity, a session it owns
created through the session-operation authority, and one open transaction whose
connection is registered in the mutation-connection registry for the test's
lifetime and rolled back afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import insert
from sqlalchemy.engine import Connection, Engine

from elspeth.web.sessions.models import quota_policies_table

IDENTITY_TOKENS_PER_DAY = 1000
CONTAINER_TOKENS_PER_DAY = 5000


@dataclass(frozen=True, slots=True)
class FencedSession:
    engine: Engine
    connection_token: str
    identity_id: str
    session_id: str


@dataclass(frozen=True, slots=True)
class FencedSessionWithPolicy:
    engine: Engine
    connection_token: str
    identity_id: str
    session_id: str
    identity_policy_id: str
    container_policy_id: str


def seed_token_policies(conn: Connection, *, identity_id: str) -> tuple[str, str]:
    """Insert one active identity policy and one active container ceiling; return their ids."""
    set_at = datetime(2026, 9, 1, tzinfo=UTC)
    conn.execute(
        insert(quota_policies_table).values(
            policy_id="quota-identity",
            identity_id=identity_id,
            tokens_per_day=IDENTITY_TOKENS_PER_DAY,
            storage_bytes=1_000_000,
            set_by_actor="operator",
            set_by_identity_id=None,
            set_at=set_at,
        )
    )
    conn.execute(
        insert(quota_policies_table).values(
            policy_id="quota-container",
            identity_id=None,
            tokens_per_day=CONTAINER_TOKENS_PER_DAY,
            storage_bytes=10_000_000,
            set_by_actor="config",
            set_by_identity_id=None,
            set_at=set_at,
        )
    )
    return "quota-identity", "quota-container"
