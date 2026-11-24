"""A lease releases its own fence at close, including after a failed renewal (file SQLite)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select, update
from tests.fixtures.identities import ensure_test_identity

from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.coordination.contracts import (
    CloseReleaseOutcome,
    FenceLossReason,
    SessionOperationFenceLost,
)
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import session_operation_fences_table
from elspeth.web.sessions.schema import initialize_session_schema

_PAST = datetime(2000, 1, 1, tzinfo=UTC)


@pytest.fixture()
def engine(tmp_path: Path) -> Iterator[Engine]:
    session_engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(session_engine)
    with session_engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    try:
        yield session_engine
    finally:
        session_engine.dispose()


def _session(authority: SQLiteLocalSessionOperationAuthority) -> UUID:
    created = authority.create_session_with_initial_fence(
        user_id="alice", title="Close release", auth_provider_type="local", owner_instance_id="owner-a", lease_seconds=30
    )
    return UUID(str(created.id))


def _acquire(authority: SQLiteLocalSessionOperationAuthority, session_id: UUID, owner: str) -> SessionOperationContext:
    return authority.acquire(session_id=session_id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id=owner, lease_seconds=30)


def _row(engine: Engine, session_id: UUID) -> Any:
    with engine.connect() as conn:
        return conn.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(session_id))
        ).one()


def _expire(engine: Engine, session_id: UUID) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(session_operation_fences_table)
            .where(session_operation_fences_table.c.session_id == str(session_id))
            .values(lease_expires_at=_PAST)
        )


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# --- the authority verb ---------------------------------------------------


def test_close_release_of_a_live_fence_releases_it_and_reports_live(engine: Engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session_id = _session(authority)
    context = _acquire(authority, session_id, "owner-a")
    before = _as_utc(_row(engine, session_id).lease_expires_at)

    assert authority.release_at_close(context) is CloseReleaseOutcome.RELEASED_LIVE

    row = _row(engine, session_id)
    assert row.released_at is not None
    assert _as_utc(row.lease_expires_at) <= before


def test_close_release_after_the_deadline_releases_without_moving_the_deadline(engine: Engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session_id = _session(authority)
    context = _acquire(authority, session_id, "owner-a")
    _expire(engine, session_id)

    assert authority.release_at_close(context) is CloseReleaseOutcome.RELEASED_AFTER_EXPIRY

    row = _row(engine, session_id)
    assert row.released_at is not None
    assert _as_utc(row.lease_expires_at) == _PAST


def test_plain_release_still_refuses_a_fence_past_its_deadline(engine: Engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session_id = _session(authority)
    context = _acquire(authority, session_id, "owner-a")
    _expire(engine, session_id)

    with pytest.raises(SessionOperationFenceLost) as lost:
        authority.release(context)

    assert lost.value.reason is FenceLossReason.LEASE_EXPIRED
    assert _row(engine, session_id).released_at is None


def test_close_release_never_releases_a_successor_that_took_over(engine: Engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session_id = _session(authority)
    stale = _acquire(authority, session_id, "owner-a")
    _expire(engine, session_id)
    successor = _acquire(authority, session_id, "owner-b")

    with pytest.raises(SessionOperationFenceLost) as lost:
        authority.release_at_close(stale)

    assert lost.value.reason is FenceLossReason.STALE_EPOCH
    row = _row(engine, session_id)
    assert (row.operation_id, row.lease_token, row.operation_epoch, row.released_at) == (
        successor.fence.operation_id,
        successor.fence.lease_token,
        successor.fence.operation_epoch,
        None,
    )
    # The successor still renews and releases as the owner.
    authority.renew(successor, lease_seconds=30)
    assert authority.release_at_close(successor) is CloseReleaseOutcome.RELEASED_LIVE


def test_close_release_never_releases_a_successor_with_the_same_epoch_but_another_identity(engine: Engine) -> None:
    """Epoch alone does not identify the fence: operation id and token are also bound."""
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session_id = _session(authority)
    context = _acquire(authority, session_id, "owner-a")
    other_identity = SessionOperationContext(
        SessionOperationFence(context.fence.session_id, str(uuid4()), "foreign token", context.fence.operation_epoch),
        context.operation_kind,
    )

    with pytest.raises(SessionOperationFenceLost) as lost:
        authority.release_at_close(other_identity)

    assert lost.value.reason is FenceLossReason.TOKEN_MISMATCH
    assert _row(engine, session_id).released_at is None
