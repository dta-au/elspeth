"""``guard_external_effect`` refuses through its compare-and-swap, not its attribute checks.

The guard first reads the lease's own attributes (``_closed``, ``_close_task``
and the renewal error) and then calls the authority's ``compare_and_swap``.
These tests change the fence after the attribute reads and before the
compare-and-swap, through a real SQLite authority, and assert that the guard
raises ``SessionOperationFenceLost`` with the reason the authority records.
The attribute reads cannot see any of these changes: the fence released as a
COMPOSE lease's close releases it (``release_at_close``), taken over by a
successor, or expired.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine, update
from tests.fixtures.identities import ensure_test_identity

from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import session_operation_fences_table
from elspeth.web.sessions.schema import initialize_session_schema

_PAST = datetime(2000, 1, 1, tzinfo=UTC)
_OWNER = "guard-owner"


class _InterleavingAuthority(SQLiteLocalSessionOperationAuthority):
    """SQLite authority that runs one step just before delegating ``compare_and_swap``.

    The lease calls ``compare_and_swap`` after its attribute checks, so the
    step runs between those checks and the real compare-and-swap.
    """

    def __init__(self, engine: Engine) -> None:
        super().__init__(engine)
        self.engine = engine
        self.interleave: Callable[[_InterleavingAuthority, SessionOperationContext], None] | None = None
        self.interleaved: list[SessionOperationContext] = []
        self.successors: list[SessionOperationContext] = []

    def compare_and_swap(self, context: SessionOperationContext) -> None:
        if self.interleave is not None:
            self.interleave(self, context)
            self.interleaved.append(context)
        super().compare_and_swap(context)


def _release_as_close_does(authority: _InterleavingAuthority, context: SessionOperationContext) -> None:
    authority.release_at_close(context)


def _take_over(authority: _InterleavingAuthority, context: SessionOperationContext) -> None:
    authority.release(context)
    authority.successors.append(
        authority.acquire(
            session_id=UUID(context.fence.session_id),
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id="guard-successor",
            lease_seconds=30,
        )
    )


def _expire(authority: _InterleavingAuthority, context: SessionOperationContext) -> None:
    with authority.engine.begin() as conn:
        conn.execute(
            update(session_operation_fences_table)
            .where(session_operation_fences_table.c.session_id == context.fence.session_id)
            .values(lease_expires_at=_PAST)
        )


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("interleave", "reason"),
    [
        pytest.param(_release_as_close_does, FenceLossReason.RELEASED, id="released-as-close-releases"),
        pytest.param(_take_over, FenceLossReason.STALE_EPOCH, id="taken-over"),
        pytest.param(_expire, FenceLossReason.LEASE_EXPIRED, id="expired"),
    ],
)
async def test_guard_raises_when_the_fence_changes_between_its_attribute_checks_and_its_compare_and_swap(
    engine: Engine,
    interleave: Callable[[_InterleavingAuthority, SessionOperationContext], None],
    reason: FenceLossReason,
) -> None:
    authority = _InterleavingAuthority(engine)
    created = authority.create_session_with_initial_fence(
        user_id="alice", title="Guard", auth_provider_type="local", owner_instance_id=_OWNER, lease_seconds=30
    )
    lease = await SessionOperationLease.acquire(
        authority,
        session_id=UUID(str(created.id)),
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=_OWNER,
        lease_seconds=30,
    )
    try:
        # Control: with the fence unchanged the same guard call passes.
        await run_sync_in_worker(lease.guard_external_effect)
        assert authority.interleaved == []

        authority.interleave = interleave
        with pytest.raises(SessionOperationFenceLost) as raised:
            await run_sync_in_worker(lease.guard_external_effect)
        assert raised.value.reason is reason

        # The step ran, after the attribute checks had passed, for this lease's context.
        assert authority.interleaved == [lease.context]
    finally:
        authority.interleave = None
        # A released or taken-over fence is not this lease's to release, so close reports that too.
        with contextlib.suppress(SessionOperationFenceLost):
            await lease.close()
        for successor in authority.successors:
            authority.release(successor)
    assert lease.closed
