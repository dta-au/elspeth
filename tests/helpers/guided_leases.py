"""Model an abandoned guided worker: both of its authorities lapse.

A takeover owns both authorities -- the guided-operation row's lease and the
session-operation fence. Expiring only the guided row while the original
COMPOSE fence remains live must fail: the platform's session fence refuses a
second live lease. A test that models a lost worker therefore lapses BOTH
rows -- the guided row's lease expires and the session-operation generation
is released -- exactly as an abandoned process's leases would lapse before
another replica takes the operation over. No production check is bypassed:
the takeover still runs the real acquire and reserve paths against these rows.

This is the shape ``test_guided_full.py`` established for the plan takeover;
promoted here so every takeover and crash test lapses the same two rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text

from elspeth.web.sessions.models import session_operation_fences_table


def abandon_guided_worker_leases(engine, *, session_id: UUID | str, operation_id: str) -> None:
    """Expire ``operation_id``'s guided lease and release the session's fence generation.

    Both writes must hit exactly one row: a zero-row expiry would model
    nothing and silently turn the test into a different scenario.
    """
    sid = str(session_id)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        expired = connection.execute(
            text(
                "UPDATE guided_operations SET lease_expires_at = :expired WHERE session_id = :session_id AND operation_id = :operation_id"
            ),
            {"expired": now - timedelta(seconds=1), "session_id": sid, "operation_id": operation_id},
        )
        if expired.rowcount != 1:
            raise AssertionError(f"expected exactly one guided operation row for {operation_id!r}, matched {expired.rowcount}")
        released = connection.execute(
            session_operation_fences_table.update().where(session_operation_fences_table.c.session_id == sid).values(released_at=now)
        )
        if released.rowcount != 1:
            raise AssertionError(f"expected exactly one session-operation fence row for {sid!r}, matched {released.rowcount}")
