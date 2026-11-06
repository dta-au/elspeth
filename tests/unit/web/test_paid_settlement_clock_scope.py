"""Paid SQL selects the original reserve only in its physical ticket scope."""

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import cast

import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkAuthority, RequiredWorkCoordinator, RequiredWorkSource
from elspeth.web.sessions import continuation_sql_deadline as deadline_module
from elspeth.web.sessions import locking
from elspeth.web.sessions.continuation_sql_deadline import ContinuationSQLDeadlineOwner
from elspeth.web.sessions.models import sessions_table
from tests.unit.web.coordination.test_composer_operation_authority import _wait_for_thread_event
from tests.unit.web.sessions.test_continuation_sql_deadline import _owners
from tests.unit.web.sessions.test_continuation_sql_deadline import operation_store as _operation_store

operation_store = _operation_store


def _coordinator(running, owner):
    authority = RequiredWorkAuthority(
        RequiredAuthorityKind.DURABLE_COMPOSE,
        running.session_operation_context,
        running.claim.operation_id,
        running.claim.attempt,
    )
    return RequiredWorkCoordinator(authority, sql_deadline_owner=owner)


def _expired_business(owner):
    owner.begin_action(
        owner.policy.actions.read,
        started_at=time.monotonic() - owner.policy.actions.read.total - 1.0,
    )


@pytest.mark.asyncio
async def test_paid_ticket_uses_original_reserve_without_moving_shared_settlement_state(operation_store):
    running, owner, _signal = _owners(operation_store)
    coordinator = _coordinator(running, owner)
    _expired_business(owner)
    sid = str(running.claim.session_id)
    with pytest.raises(TimeoutError):
        owner.remaining_statement(session_id=sid)

    def paid():
        selected = locking._CONTINUATION_SQL_DEADLINE.get()
        assert selected == (owner, locking._ContinuationSQLDeadlineMode.PAID_SETTLEMENT)
        with operation_store[0].connect() as conn, locking.continuation_metadata_connection_deadline_scope(conn):
            assert conn.execute(select(sessions_table.c.id).where(sessions_table.c.id == sid)).scalar_one() == sid
        return owner.remaining_statement(session_id=sid, paid_settlement=True)

    outcome = await run_required_sql_finish_once(coordinator.reserve(RequiredWorkSource.PROVIDER_SETTLEMENT_SQL), paid)
    assert type(outcome) is RequiredSQLReturned and 0 < outcome.value <= owner.statement_cap
    assert owner._settling is False
    with pytest.raises(TimeoutError):
        owner.remaining_statement(session_id=sid)
    assert locking._CONTINUATION_SQL_DEADLINE.get() is None


@pytest.mark.asyncio
async def test_ordinary_sql_concurrent_with_and_after_paid_cannot_borrow_reserve(operation_store):
    running, owner, _signal = _owners(operation_store)
    coordinator = _coordinator(running, owner)
    _expired_business(owner)
    sid = str(running.claim.session_id)
    entered, release = threading.Event(), threading.Event()

    def paid():
        assert locking._CONTINUATION_SQL_DEADLINE.get() == (owner, locking._ContinuationSQLDeadlineMode.PAID_SETTLEMENT)
        entered.set()
        assert release.wait(timeout=5)
        return owner.remaining_statement(session_id=sid, paid_settlement=True)

    def ordinary():
        assert locking._CONTINUATION_SQL_DEADLINE.get() == (owner, False)
        return owner.remaining_statement(session_id=sid)

    task = asyncio.create_task(run_required_sql_finish_once(coordinator.reserve(RequiredWorkSource.PROVIDER_SETTLEMENT_SQL), paid))
    try:
        await _wait_for_thread_event(entered)
        during = await run_required_sql_finish_once(coordinator.reserve(RequiredWorkSource.CONTINUATION_READ_SQL), ordinary)
        assert type(during) is RequiredSQLRaised and type(during.error) is TimeoutError
    finally:
        release.set()
    paid_outcome = await task
    assert type(paid_outcome) is RequiredSQLReturned and paid_outcome.value > 0
    after = await run_required_sql_finish_once(
        coordinator.reserve(RequiredWorkSource.CONTINUATION_READ_SQL, recurrence_ordinal=1), ordinary
    )
    assert type(after) is RequiredSQLRaised and type(after.error) is TimeoutError
    assert owner._settling is False


@dataclass(frozen=True)
class _ObservedClock:
    now: float

    def monotonic(self) -> float:
        return self.now


def test_paid_scope_respects_reserve_hard_and_full_drain_expiry(operation_store, monkeypatch):
    running, owner, signal = _owners(operation_store)
    sid = str(running.claim.session_id)
    _expired_business(owner)
    assert owner.remaining_statement(session_id=sid, paid_settlement=True) > 0
    reserve = owner._settlement_expires_at
    monkeypatch.setattr(deadline_module, "time", _ObservedClock(reserve + 0.01))
    with pytest.raises(TimeoutError):
        owner.remaining_statement(session_id=sid, paid_settlement=True)
    monkeypatch.undo()

    hard_owner = ContinuationSQLDeadlineOwner(
        running,
        policy=owner.policy,
        drain_signal=signal,
        drain_seconds=10.0,
        hard_expires_at=time.monotonic() + 1.0,
    )
    hard_owner.begin_action(hard_owner.policy.actions.read, started_at=time.monotonic())
    monkeypatch.setattr(deadline_module, "time", _ObservedClock(hard_owner.hard_expires_at + 0.01))
    with pytest.raises(TimeoutError):
        hard_owner.remaining_statement(session_id=sid, paid_settlement=True)
    monkeypatch.undo()

    signal.request()
    started = signal.started_at
    assert started is not None
    monkeypatch.setattr(deadline_module, "time", _ObservedClock(started + owner.drain_seconds + 0.01))
    with pytest.raises(TimeoutError):
        owner.remaining_statement(session_id=sid, paid_settlement=True)
    with pytest.raises(AuditIntegrityError, match="combine"):
        owner.remaining_statement(session_id=sid, lifecycle=True, paid_settlement=True)


def test_wrong_owner_and_ordinary_ticket_cannot_select_paid_scope(operation_store):
    running, owner, _signal = _owners(operation_store)
    coordinator = _coordinator(running, owner)
    ordinary = coordinator.reserve(RequiredWorkSource.CONTINUATION_READ_SQL)
    with pytest.raises(AuditIntegrityError, match="actual provider settlement ticket"):
        locking.ContinuationPaidSettlementScope(ordinary, owner)
    with (
        pytest.raises(AuditIntegrityError, match="exact timing mode"),
        locking.continuation_sql_deadline_scope(owner, paid_settlement=cast(locking.ContinuationPaidSettlementScope, "paid_settlement")),
    ):
        raise AssertionError("foreign mode entered")
    wrong = RequiredWorkAuthority(
        RequiredAuthorityKind.DURABLE_COMPOSE,
        running.session_operation_context,
        running.claim.operation_id,
        running.claim.attempt + 1,
    )
    with pytest.raises(AuditIntegrityError, match="physical/logical owner"):
        RequiredWorkCoordinator(wrong, sql_deadline_owner=owner)
    assert owner._settling is False and locking._CONTINUATION_SQL_DEADLINE.get() is None


@pytest.mark.asyncio
async def test_paid_scope_retains_original_cancellations_and_resets_after_physical_failure(operation_store):
    running, owner, _signal = _owners(operation_store)
    coordinator = _coordinator(running, owner)
    _expired_business(owner)
    entered, release = threading.Event(), threading.Event()
    original = RuntimeError("controlled paid SQL failure")

    def failing_paid():
        assert locking._CONTINUATION_SQL_DEADLINE.get() == (owner, locking._ContinuationSQLDeadlineMode.PAID_SETTLEMENT)
        entered.set()
        assert release.wait(timeout=5)
        raise original

    ticket = coordinator.reserve(RequiredWorkSource.PROVIDER_SETTLEMENT_SQL)
    task = asyncio.create_task(run_required_sql_finish_once(ticket, failing_paid))
    try:
        await _wait_for_thread_event(entered)
        assert task.cancel("first original")
        await asyncio.sleep(0)
        assert task.cancel("second original")
        await asyncio.sleep(0)
    finally:
        release.set()
    outcome = await task
    assert type(outcome) is RequiredSQLRaised and outcome.error is original
    assert len(outcome.deferred_cancellations) == 2
    assert outcome.deferred_cancellations[0] is not outcome.deferred_cancellations[1]
    assert ticket.complete and owner._settling is False and locking._CONTINUATION_SQL_DEADLINE.get() is None
    with pytest.raises(AuditIntegrityError, match="Completed required SQL ticket"):
        await run_required_sql_finish_once(ticket, failing_paid)


@pytest.mark.asyncio
async def test_paid_ticket_without_continuation_owner_keeps_ordinary_scope(operation_store):
    running, owner, _signal = _owners(operation_store)
    ordinary = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.DURABLE_COMPOSE,
            running.session_operation_context,
            running.claim.operation_id,
            running.claim.attempt,
        )
    )
    outcome = await run_required_sql_finish_once(
        ordinary.reserve(RequiredWorkSource.PROVIDER_SETTLEMENT_SQL), locking._CONTINUATION_SQL_DEADLINE.get
    )
    assert type(outcome) is RequiredSQLReturned and outcome.value is None
    assert owner._settling is False
