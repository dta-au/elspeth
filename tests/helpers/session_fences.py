"""Seed the retained session-operation fence for a directly inserted session row.

Production sessions are created through
``create_session_with_initial_fence`` which writes the ``sessions`` row and a
released epoch-1 ``create`` fence in one transaction. Tests that insert
``sessions_table`` rows by hand to shape a scenario leave that fence absent, so
every fenced writer reports ``SessionOperationFenceLost: missing``. This helper
writes the same released epoch-1 fence the lifecycle method writes; it does not
open a lease and does not bypass any production check.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Connection, insert

from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.sessions.models import session_operation_fences_table


def seed_session_operation_fence(
    conn: Connection,
    session_id: UUID | str,
    *,
    owner_instance_id: str,
    created_at: datetime | None = None,
) -> None:
    """Insert the released epoch-1 ``create`` fence for ``session_id``."""
    if type(owner_instance_id) is not str or not owner_instance_id:
        raise ValueError("owner_instance_id must be a nonblank string")
    stamped = created_at if created_at is not None else datetime.now(UTC)
    conn.execute(
        insert(session_operation_fences_table).values(
            session_id=str(session_id),
            operation_id=uuid4().hex,
            lease_token=uuid4().hex,
            operation_kind=SessionOperationKind.CREATE.value,
            owner_instance_id=owner_instance_id,
            operation_epoch=1,
            lease_expires_at=stamped,
            released_at=stamped,
        )
    )


def ensure_session_fence(engine, session_id: UUID | str, *, owner_instance_id: str) -> bool:
    """Seed the released epoch-1 fence iff the session row exists without one.

    Returns True when a fence was seeded (the caller may retry its acquire).
    Returns False when there is nothing this helper can honestly repair: the
    session row itself is absent, or a fence row already exists (the original
    failure then had a different cause and must propagate).
    """
    from sqlalchemy import select

    from elspeth.web.sessions.models import sessions_table

    sid = str(session_id)
    with engine.begin() as conn:
        session_row = conn.execute(select(sessions_table.c.id, sessions_table.c.created_at).where(sessions_table.c.id == sid)).one_or_none()
        if session_row is None:
            return False
        fence_row = conn.execute(
            select(session_operation_fences_table.c.session_id).where(session_operation_fences_table.c.session_id == sid)
        ).one_or_none()
        if fence_row is not None:
            return False
        seed_session_operation_fence(conn, sid, owner_instance_id=owner_instance_id)
        return True
