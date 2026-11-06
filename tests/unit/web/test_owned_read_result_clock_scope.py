"""Actual read returns borrow only their original finite disposition reserve."""

import time
from concurrent.futures import Future
from dataclasses import dataclass, replace

import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_required_read_finish_once, run_required_sql_finish_once
from elspeth.web.composer.tools._dispatch import execute_discovery_tool_with_context
from elspeth.web.continuation_retention_memo import current_retention_stage_memo
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredReadProduced, RequiredWorkSource
from elspeth.web.sessions import continuation_sql_deadline as deadline_module
from elspeth.web.sessions import locking
from elspeth.web.sessions.continuation_owner_results import ContinuationReadObservations, build_read_owner_result
from elspeth.web.sessions.continuation_sql_deadline import ContinuationReadResultDeadline, ContinuationSQLDeadlineOwner
from elspeth.web.sessions.models import sessions_table
from tests.unit.web.composer.test_tools import _empty_state, _trained_tool_context
from tests.unit.web.sessions.test_continuation_sql_deadline import _owners
from tests.unit.web.sessions.test_continuation_sql_deadline import operation_store as _operation_store
from tests.unit.web.test_paid_settlement_clock_scope import _coordinator

operation_store = _operation_store


def _actual_read():
    state = _empty_state()
    result = execute_discovery_tool_with_context("get_pipeline_state", {}, state, _trained_tool_context())
    retained = build_read_owner_result(
        tool_name="get_pipeline_state", arguments={}, result=result, pre_state=state, observations=ContinuationReadObservations()
    )
    return RequiredReadProduced(result, retained)


@dataclass(frozen=True)
class _Clock:
    now: float

    def monotonic(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_actual_return_after_action_expiry_and_drain_uses_only_scoped_original_reserve(operation_store, monkeypatch):
    running, owner, signal = _owners(operation_store)
    coordinator = _coordinator(running, owner)
    owner.begin_action(owner.policy.actions.read, started_at=time.monotonic() - owner.policy.actions.read.total + 0.5)
    original_clock = owner.capture_read_result_deadline()
    sid = str(running.claim.session_id)
    produced_values = []
    memos = []

    def produce():
        assert locking._CONTINUATION_SQL_DEADLINE.get() == (owner, False)
        assert owner.remaining_statement(session_id=sid) > 0
        memos.append(current_retention_stage_memo())
        produced = _actual_read()
        produced_values.append(produced)
        signal.request()
        monkeypatch.setattr(deadline_module, "time", _Clock(original_clock.action_expires_at + 0.1))
        return produced

    def finish(produced):
        assert produced is produced_values[0]
        assert memos[0] is not None and current_retention_stage_memo() is memos[0]
        selected = locking._CONTINUATION_SQL_DEADLINE.get()
        assert selected is not None and selected[0] is owner and type(selected[1]) is ContinuationReadResultDeadline
        assert selected[1] == original_clock
        with operation_store[0].connect() as conn, locking.continuation_metadata_connection_deadline_scope(conn):
            assert conn.execute(select(sessions_table.c.id).where(sessions_table.c.id == sid)).scalar_one() == sid
        return produced

    ticket = coordinator.reserve(RequiredWorkSource.CONTINUATION_READ_RESULT_SQL)
    assert ticket.permits_process_drain is False
    outcome = await run_required_read_finish_once(ticket, produce, finish)
    assert type(outcome) is RequiredSQLReturned and outcome.value is produced_values[0]
    assert owner.capture_read_result_deadline() == original_clock and owner._settling is False
    assert memos[0] is not None and not memos[0].active
    with pytest.raises(TimeoutError):
        owner.remaining_statement(session_id=sid)
    assert locking._CONTINUATION_SQL_DEADLINE.get() is None
    ordinary = await run_required_sql_finish_once(
        coordinator.reserve(RequiredWorkSource.CONTINUATION_STATE_SQL), lambda: owner.remaining_statement(session_id=sid)
    )
    assert type(ordinary) is RequiredSQLRaised and type(ordinary.error) is TimeoutError


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ("expired", "draining"))
async def test_expired_or_draining_before_dispatch_never_calls_read_or_finish(operation_store, refusal):
    running, owner, signal = _owners(operation_store)
    owner.begin_action(
        owner.policy.actions.read,
        started_at=time.monotonic() - (owner.policy.actions.read.total + 1 if refusal == "expired" else 0),
    )
    if refusal == "draining":
        signal.request()
    calls = []

    def produce():
        calls.append("read")
        return _actual_read()

    def finish(produced):
        calls.append("finish")
        return produced

    outcome = await run_required_read_finish_once(
        _coordinator(running, owner).reserve(RequiredWorkSource.CONTINUATION_READ_RESULT_SQL), produce, finish
    )
    assert type(outcome) is RequiredSQLRaised and type(outcome.error) is TimeoutError
    assert calls == []


def test_result_clock_cannot_renew_or_cross_reserve_hard_drain_or_action(operation_store, monkeypatch):
    running, owner, signal = _owners(operation_store)
    sid = str(running.claim.session_id)
    with pytest.raises(AuditIntegrityError, match="begun read"):
        owner.capture_read_result_deadline()
    owner.begin_action(owner.policy.actions.read, started_at=time.monotonic())
    clock = owner.capture_read_result_deadline()
    assert clock.expires_at <= owner.hard_expires_at
    monkeypatch.setattr(deadline_module, "time", _Clock(clock.expires_at + 0.1))
    with pytest.raises(TimeoutError):
        owner.remaining_statement(session_id=sid, read_result=clock)
    monkeypatch.undo()
    signal.request()
    assert signal.started_at is not None
    monkeypatch.setattr(deadline_module, "time", _Clock(signal.started_at + owner.drain_seconds + 0.1))
    with pytest.raises(TimeoutError):
        owner.remaining_statement(session_id=sid, read_result=clock)
    monkeypatch.undo()
    with pytest.raises(AuditIntegrityError, match="original action"):
        owner.remaining_statement(session_id=sid, read_result=replace(clock, expires_at=clock.expires_at + 1))
    owner.begin_action(owner.policy.actions.primary, started_at=time.monotonic())
    with pytest.raises(AuditIntegrityError, match="original action"):
        owner.remaining_statement(session_id=sid, read_result=clock)


def test_read_carrier_refuses_substituted_complete_result():
    actual = _actual_read()
    changed = replace(actual.result, post_call_hints=("foreign result",))
    with pytest.raises(AuditIntegrityError, match="actual returned ToolResult"):
        RequiredReadProduced(changed, actual.owned_result)


def test_original_hard_expiry_clips_read_result_reserve(operation_store, monkeypatch):
    running, original, signal = _owners(operation_store)
    hard = time.monotonic() + 1.0
    owner = ContinuationSQLDeadlineOwner(running, policy=original.policy, drain_signal=signal, drain_seconds=10.0, hard_expires_at=hard)
    owner.begin_action(owner.policy.actions.read, started_at=time.monotonic())
    clock = owner.capture_read_result_deadline()
    assert clock.expires_at == hard
    monkeypatch.setattr(deadline_module, "time", _Clock(hard + 0.1))
    with pytest.raises(TimeoutError):
        owner.remaining_statement(session_id=str(running.claim.session_id), read_result=clock)


@pytest.mark.asyncio
async def test_finish_failure_keeps_original_error_and_resets_scopes(operation_store):
    running, owner, _signal = _owners(operation_store)
    owner.begin_action(owner.policy.actions.read, started_at=time.monotonic())
    original = RuntimeError("actual read retention failure")
    memos = []

    def finish(produced):
        produced.validate()
        memos.append(current_retention_stage_memo())
        raise original

    outcome = await run_required_read_finish_once(
        _coordinator(running, owner).reserve(RequiredWorkSource.CONTINUATION_READ_RESULT_SQL), _actual_read, finish
    )
    assert type(outcome) is RequiredSQLRaised and outcome.error is original
    assert memos[0] is not None and not memos[0].active
    assert locking._CONTINUATION_SQL_DEADLINE.get() is None and owner._settling is False


@pytest.mark.asyncio
async def test_ordinary_source_cannot_enter_read_disposition(operation_store):
    running, owner, _signal = _owners(operation_store)
    owner.begin_action(owner.policy.actions.read, started_at=time.monotonic())
    coordinator = _coordinator(running, owner)
    ticket = coordinator.reserve(RequiredWorkSource.CONTINUATION_STATE_SQL)
    with pytest.raises(AuditIntegrityError, match="exact registered") as raised:
        await run_required_read_finish_once(ticket, _actual_read, lambda produced: produced)
    assert ticket.complete and ticket.errors == (raised.value,)
    coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize(("setup", "message"), (("missing_owner", "exact registered"), ("wrong_action", "original begun read")))
async def test_read_setup_refusal_closes_only_unsubmitted_sql_ticket(operation_store, setup, message):
    running, owner, _signal = _owners(operation_store)
    if setup == "missing_owner":
        coordinator = _coordinator(running, None)
    else:
        owner.begin_action(owner.policy.actions.primary, started_at=time.monotonic())
        coordinator = _coordinator(running, owner)
    ticket = coordinator.reserve(RequiredWorkSource.CONTINUATION_READ_RESULT_SQL)
    calls = []

    def produce():
        calls.append("produce")
        return _actual_read()

    def finish(produced):
        calls.append("finish")
        return produced

    with pytest.raises(AuditIntegrityError, match=message) as raised:
        await run_required_read_finish_once(ticket, produce, finish)
    assert ticket.complete and ticket.errors == (raised.value,)
    assert calls == []
    coordinator.assert_completed()
    with pytest.raises(AuditIntegrityError, match="cannot be reused"):
        await run_required_read_finish_once(ticket, produce, finish)
    assert ticket.errors == (raised.value,) and calls == []


@pytest.mark.asyncio
async def test_read_setup_refusal_preserves_an_already_bound_future(operation_store):
    running, owner, _signal = _owners(operation_store)
    owner.begin_action(owner.policy.actions.primary, started_at=time.monotonic())
    coordinator = _coordinator(running, owner)
    ticket = coordinator.reserve(RequiredWorkSource.CONTINUATION_READ_RESULT_SQL)
    future: Future[object] = Future()
    ticket.bind_future(future)
    calls = []

    def produce():
        calls.append("produce")
        return _actual_read()

    with pytest.raises(AuditIntegrityError, match="begun read"):
        await run_required_read_finish_once(ticket, produce, lambda produced: produced)
    assert not ticket.complete and ticket.errors == () and not future.done()
    assert calls == []
    future.set_result(None)
    ticket.observe_actual_outcome()
    coordinator.assert_completed()
