"""Actual required-SQL scope custody and finite V2 timing controls.

PostgreSQL held-lock/process proofs remain separate; the SQLite store here
checks physical bridge/owner propagation without changing its busy semantics.
"""

import asyncio
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass

import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.continuation_retention_memo import current_retention_stage_memo, retention_stage_memo_scope
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkAuthority, RequiredWorkCoordinator, RequiredWorkSource
from elspeth.web.sessions import continuation_sql_deadline as deadline_module
from elspeth.web.sessions import locking
from elspeth.web.sessions.composer_drain import ComposerDrainDeadline, ComposerDrainSignal
from elspeth.web.sessions.composer_finalization import ContinuationFinalizationBudget
from elspeth.web.sessions.continuation_sql_deadline import ContinuationSQLDeadlineOwner
from elspeth.web.sessions.models import sessions_table
from tests.unit.web.coordination.test_composer_continuation_handover import _running
from tests.unit.web.coordination.test_composer_operation_authority import _wait_for_thread_event
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as _operation_store

operation_store = _operation_store


def _owners(store):
    authority, _record, running = _running(store)
    root = decode_continuation_snapshot(authority.load_continuation_snapshot(running).payload_json)
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    signal = ComposerDrainSignal()
    owner = ContinuationSQLDeadlineOwner(
        running,
        policy=policy,
        drain_signal=signal,
        drain_seconds=10.0,
        hard_expires_at=time.monotonic() + 100,
    )
    return running, owner, signal


def test_deadline_keeps_finite_inflight_cap_and_original_action_drain_clocks(operation_store):
    running, owner, signal = _owners(operation_store)
    sid = str(running.claim.session_id)
    assert 0 < owner.remaining_statement(session_id=sid) <= owner.statement_cap <= owner.drain_seconds - owner.drain_finish_reserve_seconds
    assert owner.policy.segment.reserve == 25.0
    assert owner.drain_finish_reserve_seconds == 3.0
    signal.request()
    original = signal.started_at
    signal.request()
    assert signal.started_at == original
    explicit = ComposerDrainDeadline.from_start(started_at=time.monotonic() - 1, drain_seconds=0.1)
    with pytest.raises(TimeoutError):
        owner.remaining_statement(session_id=sid, deadline=explicit)
    owner.begin_action(owner.policy.actions.read, started_at=time.monotonic() - owner.policy.actions.read.total - 1)
    budget = ContinuationFinalizationBudget(owner)
    assert budget.remaining_seconds(monotonic_now=time.monotonic()) < 0
    with pytest.raises(TimeoutError):
        owner.remaining_statement(session_id=sid)
    # Lifecycle renewal/finish uses the original remaining hard/drain reserve;
    # an expired business action cannot manufacture physical lease loss.
    assert owner.remaining_statement(session_id=sid, lifecycle=True) > 0
    owner.begin_settlement()
    assert owner.remaining_statement(session_id=sid) > 0
    assert budget.remaining_seconds(monotonic_now=time.monotonic()) < 0
    owner.begin_settlement()
    assert budget.remaining_seconds(monotonic_now=time.monotonic()) < 0
    with pytest.raises(AuditIntegrityError, match="another session"):
        owner.remaining_statement(session_id="foreign-session")


@dataclass(frozen=True)
class _ObservedClock:
    now: float

    def monotonic(self) -> float:
        return self.now


def test_drain_cancels_business_at_d_minus_three_and_keeps_original_disposition_deadline(operation_store, monkeypatch):
    running, owner, signal = _owners(operation_store)
    signal.request()
    started = signal.started_at
    assert started is not None
    sid = str(running.claim.session_id)
    monkeypatch.setattr(deadline_module, "time", _ObservedClock(started + 8.0))
    with pytest.raises(TimeoutError):
        owner.remaining_statement(session_id=sid)
    owner.begin_settlement()
    assert owner.remaining_statement(session_id=sid) == 1.0
    signal.request()
    assert signal.started_at == started
    monkeypatch.setattr(deadline_module, "time", _ObservedClock(started + 10.0))
    with pytest.raises(TimeoutError):
        owner.remaining_statement(session_id=sid)
    with pytest.raises(TimeoutError):
        owner.remaining_statement(session_id=sid, lifecycle=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", (False, True))
@pytest.mark.parametrize("with_memo", (False, True))
@pytest.mark.parametrize("cancel", (False, True))
async def test_actual_required_sql_installs_and_resets_scope_on_its_same_physical_thread(operation_store, fail, with_memo, cancel):
    running, owner, _signal = _owners(operation_store)
    authority = RequiredWorkAuthority(
        RequiredAuthorityKind.DURABLE_COMPOSE, running.session_operation_context, running.claim.operation_id, running.claim.attempt
    )
    coordinator = RequiredWorkCoordinator(authority, sql_deadline_owner=owner)
    ticket = coordinator.reserve(RequiredWorkSource.CONTINUATION_READ_SQL)
    entered, release = threading.Event(), threading.Event()
    physical_thread = []
    scope_observation = []
    callback_observation = []
    callback_done = threading.Event()
    original = RuntimeError("controlled physical SQL failure")

    def query():
        physical_thread.append(threading.get_ident())
        entered.set()
        assert release.wait(timeout=5)
        scope_observation.append((locking._CONTINUATION_SQL_DEADLINE.get(), current_retention_stage_memo()))
        assert locking._CONTINUATION_SQL_DEADLINE.get() == (owner, False)
        assert current_retention_stage_memo() is memo
        if fail:
            raise original
        with locking.locked_session_transaction(operation_store[0], str(running.claim.session_id)) as conn:
            return conn.execute(select(sessions_table.c.id)).scalar_one()

    with retention_stage_memo_scope() if with_memo else nullcontext() as memo:
        task = asyncio.create_task(run_required_sql_finish_once(ticket, query))
        try:
            await _wait_for_thread_event(entered)
            assert ticket._future is not None and not ticket._future.done()

            def after_physical(_future):
                callback_observation.append(
                    (threading.get_ident(), locking._CONTINUATION_SQL_DEADLINE.get(), current_retention_stage_memo())
                )
                callback_done.set()

            ticket._future.add_done_callback(after_physical)
            if cancel:
                task.cancel("joined SQL cancellation")
                await asyncio.sleep(0)
        finally:
            release.set()
            result = await task
    await _wait_for_thread_event(callback_done)
    assert scope_observation == [((owner, False), memo)]
    assert callback_observation == [(physical_thread[0], None, None)]
    assert current_retention_stage_memo() is None and locking._CONTINUATION_SQL_DEADLINE.get() is None
    assert len(result.deferred_cancellations) == int(cancel)
    coordinator.validate_continuation_sql_outcome(
        ticket=ticket, expected_source=RequiredWorkSource.CONTINUATION_READ_SQL, actual_outcome=result
    )
    if fail:
        assert type(result) is RequiredSQLRaised and result.error is original
    else:
        assert type(result) is RequiredSQLReturned and result.value == str(running.claim.session_id)

    # The same existing bridge with no V2 owner keeps its ordinary behavior.
    ordinary = RequiredWorkCoordinator(authority)
    control = ordinary.reserve(RequiredWorkSource.CONTINUATION_READ_SQL)
    outcome = await run_required_sql_finish_once(control, locking._CONTINUATION_SQL_DEADLINE.get)
    assert type(outcome) is RequiredSQLReturned and outcome.value is None
