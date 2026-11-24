"""A lease gives up its own fence at close, including after a failed renewal, for every kind it can hold (file SQLite).

This module imports nothing the change adds, so it runs unchanged against the
source before the change.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.exc import OperationalError
from tests.fixtures.identities import ensure_test_identity

from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web import async_workers
from elspeth.web.coordination.contracts import (
    FenceLossReason,
    SessionOperationFenceLost,
    SessionOperationLeaseDisposition,
)
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkAuthority, RequiredWorkCoordinator
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import session_operation_fences_table, session_read_admissions_table
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


# --- the lease lifecycle -------------------------------------------------


class _FailingRenewal:
    """Lease authority stand-in whose renewals raise a non-fence database error."""

    def __init__(self, real: SQLiteLocalSessionOperationAuthority) -> None:
        self._real = real
        self.renewals = 0

    def renew(self, context: SessionOperationContext, *, lease_seconds: int) -> SessionOperationContext:
        self.renewals += 1
        raise OperationalError("renew", {}, ConnectionError("database unavailable"))

    def release_at_close(self, context: SessionOperationContext) -> object:
        return self._real.release_at_close(context)


async def _lease_with_failing_renewal(
    authority: SQLiteLocalSessionOperationAuthority, session_id: UUID, monkeypatch: pytest.MonkeyPatch, *, required: bool
) -> SessionOperationLease:
    lease = await SessionOperationLease.acquire(
        authority,
        session_id=session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="owner-a",
        lease_seconds=30,
        renew_interval_seconds=0.05,
    )
    failing = _FailingRenewal(authority)
    if required:
        lease.bind_required_work(
            RequiredWorkCoordinator(
                RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, lease.context, invocation_id=str(uuid4()))
            )
        )
        monkeypatch.setattr(lease, "_authority", failing)
    else:
        monkeypatch.setattr(lease, "_upkeep_authority", failing)
    return lease


async def _until(predicate: Callable[[], bool], *, seconds: float = 5) -> None:
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.timeout(30, method="thread")
@pytest.mark.parametrize("required", (False, True), ids=("upkeep-renewal", "required-work-renewal"))
async def test_close_after_a_failed_renewal_releases_the_live_fence(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, *, required: bool
) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session_id = _session(authority)
    lease = await _lease_with_failing_renewal(authority, session_id, monkeypatch, required=required)
    try:
        await _until(lambda: lease.renewal_error is not None)
        with pytest.raises(OperationalError):
            await asyncio.wait_for(lease.close(), timeout=10)

        assert lease.disposition is SessionOperationLeaseDisposition.LOST
        row = _row(engine, session_id)
        assert row.released_at is not None
        # The fence was still within its 30 s lease: another owner gets the
        # session at once instead of waiting for the deadline.
        assert _acquire(authority, session_id, "owner-b").fence.operation_epoch == row.operation_epoch + 1
    finally:
        await asyncio.wait_for(async_workers.shutdown_async_workers(), timeout=10)


@pytest.mark.asyncio
@pytest.mark.timeout(30, method="thread")
@pytest.mark.parametrize("required", (False, True), ids=("upkeep-renewal", "required-work-renewal"))
async def test_close_after_a_failed_renewal_and_a_passed_deadline_releases_without_moving_it(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, *, required: bool
) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session_id = _session(authority)
    lease = await _lease_with_failing_renewal(authority, session_id, monkeypatch, required=required)
    try:
        await _until(lambda: lease.renewal_error is not None)
        _expire(engine, session_id)
        with pytest.raises(OperationalError):
            await asyncio.wait_for(lease.close(), timeout=10)

        assert lease.disposition is SessionOperationLeaseDisposition.LOST
        row = _row(engine, session_id)
        assert row.released_at is not None
        assert _as_utc(row.lease_expires_at) == _PAST
    finally:
        await asyncio.wait_for(async_workers.shutdown_async_workers(), timeout=10)


@pytest.mark.asyncio
@pytest.mark.timeout(30, method="thread")
async def test_close_of_a_healthy_lease_after_its_deadline_reports_the_loss_and_releases(engine: Engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session_id = _session(authority)
    lease = await SessionOperationLease.acquire(
        authority,
        session_id=session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="owner-a",
        lease_seconds=30,
        renew_interval_seconds=29,
    )
    try:
        _expire(engine, session_id)
        with pytest.raises(SessionOperationFenceLost) as lost:
            await asyncio.wait_for(lease.close(), timeout=10)

        assert lost.value.reason is FenceLossReason.LEASE_EXPIRED
        assert lease.disposition is SessionOperationLeaseDisposition.LOST
        assert _row(engine, session_id).released_at is not None
    finally:
        await asyncio.wait_for(async_workers.shutdown_async_workers(), timeout=10)


# --- every lease kind the authority creates --------------------------------

# EXECUTE leases release through their execution obligation, not this close.
_CLOSE_RELEASED_KINDS = tuple(kind for kind in SessionOperationKind if kind is not SessionOperationKind.EXECUTE)


def _still_held(engine: Engine, context: SessionOperationContext) -> bool:
    """Whether this lease's own authority row is still unreleased (fence row, or read admission)."""
    with engine.connect() as conn:
        if context.operation_kind is SessionOperationKind.BLOB_READ:
            admission = conn.execute(
                select(session_read_admissions_table.c.operation_id).where(
                    session_read_admissions_table.c.session_id == context.fence.session_id,
                    session_read_admissions_table.c.operation_id == context.fence.operation_id,
                )
            ).one_or_none()
            return admission is not None
        row = conn.execute(
            select(session_operation_fences_table.c.released_at).where(
                session_operation_fences_table.c.session_id == context.fence.session_id,
                session_operation_fences_table.c.operation_id == context.fence.operation_id,
            )
        ).one_or_none()
        return row is not None and row.released_at is None


def _expire_own_authority_row(engine: Engine, context: SessionOperationContext) -> None:
    """Move this lease's own authority row (fence row, or read admission) past its deadline."""
    with engine.begin() as conn:
        if context.operation_kind is SessionOperationKind.BLOB_READ:
            conn.execute(
                update(session_read_admissions_table)
                .where(
                    session_read_admissions_table.c.session_id == context.fence.session_id,
                    session_read_admissions_table.c.operation_id == context.fence.operation_id,
                )
                # The row's CHECK keeps expiry after admission, so both move back.
                .values(admitted_at=_PAST, expires_at=_PAST + timedelta(seconds=1))
            )
            return
        conn.execute(
            update(session_operation_fences_table)
            .where(
                session_operation_fences_table.c.session_id == context.fence.session_id,
                session_operation_fences_table.c.operation_id == context.fence.operation_id,
            )
            .values(lease_expires_at=_PAST)
        )


@pytest.mark.asyncio
@pytest.mark.timeout(30, method="thread")
@pytest.mark.parametrize("kind", _CLOSE_RELEASED_KINDS, ids=lambda kind: kind.value)
async def test_every_lease_kind_past_its_deadline_after_a_renewal_error_leaves_the_session_acquirable(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, kind: SessionOperationKind
) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session_id = _session(authority)
    lease = await SessionOperationLease.acquire(
        authority,
        session_id=session_id,
        operation_kind=kind,
        owner_instance_id="owner-a",
        lease_seconds=30,
        renew_interval_seconds=0.05,
    )
    monkeypatch.setattr(lease, "_upkeep_authority", _FailingRenewal(authority))
    try:
        await _until(lambda: lease.renewal_error is not None)
        _expire_own_authority_row(engine, lease.context)
        with pytest.raises(OperationalError):
            await asyncio.wait_for(lease.close(), timeout=10)

        assert lease.disposition is SessionOperationLeaseDisposition.LOST
        # The same replica starts the next operation of this kind, and a
        # writing operation, at once.
        following = authority.acquire(session_id=session_id, operation_kind=kind, owner_instance_id="owner-a", lease_seconds=30)
        authority.release(following)
        writer = authority.acquire(
            session_id=session_id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner-a", lease_seconds=30
        )
        assert writer.operation_kind is SessionOperationKind.COMPOSE
    finally:
        await asyncio.wait_for(async_workers.shutdown_async_workers(), timeout=10)


@pytest.mark.asyncio
@pytest.mark.timeout(30, method="thread")
async def test_close_after_a_failed_renewal_never_releases_a_successor_that_took_over(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session_id = _session(authority)
    lease = await _lease_with_failing_renewal(authority, session_id, monkeypatch, required=False)
    try:
        await _until(lambda: lease.renewal_error is not None)
        _expire(engine, session_id)
        successor = _acquire(authority, session_id, "owner-b")

        with pytest.raises(OperationalError):
            await asyncio.wait_for(lease.close(), timeout=10)

        assert lease.disposition is SessionOperationLeaseDisposition.LOST
        row = _row(engine, session_id)
        assert (row.operation_id, row.lease_token, row.operation_epoch, row.released_at) == (
            successor.fence.operation_id,
            successor.fence.lease_token,
            successor.fence.operation_epoch,
            None,
        )
        # The successor still renews and releases as the owner.
        authority.renew(successor, lease_seconds=30)
        authority.release(successor)
        assert _row(engine, session_id).released_at is not None
    finally:
        await asyncio.wait_for(async_workers.shutdown_async_workers(), timeout=10)


@pytest.mark.asyncio
@pytest.mark.timeout(30, method="thread")
@pytest.mark.parametrize("kind", _CLOSE_RELEASED_KINDS, ids=lambda kind: kind.value)
async def test_every_lease_kind_releases_at_close_after_a_renewal_error(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, kind: SessionOperationKind
) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session_id = _session(authority)
    lease = await SessionOperationLease.acquire(
        authority,
        session_id=session_id,
        operation_kind=kind,
        owner_instance_id="owner-a",
        lease_seconds=30,
        renew_interval_seconds=0.05,
    )
    monkeypatch.setattr(lease, "_upkeep_authority", _FailingRenewal(authority))
    try:
        await _until(lambda: lease.renewal_error is not None)
        with pytest.raises(OperationalError):
            await asyncio.wait_for(lease.close(), timeout=10)

        assert lease.disposition is SessionOperationLeaseDisposition.LOST
        assert _still_held(engine, lease.context) is False
        # Still well inside the 30 s lease: the same replica starts the next
        # operation of this kind at once.
        following = authority.acquire(session_id=session_id, operation_kind=kind, owner_instance_id="owner-a", lease_seconds=30)
        assert following.operation_kind is kind
    finally:
        await asyncio.wait_for(async_workers.shutdown_async_workers(), timeout=10)
