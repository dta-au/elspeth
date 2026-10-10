"""Real SQL/Future ownership controls for lifecycle receipt validation."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web import async_workers
from elspeth.web.coordination import lifecycle
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkCoordinator,
    RequiredWorkIncomplete,
    RequiredWorkSource,
)
from elspeth.web.sessions.models import session_operation_fences_table
from elspeth.web.sessions.service import ComposerTerminalSQLCompletionUnknown
from tests.unit.web.sessions.test_composer_async_worker import _file_app


async def _actual_scope(tmp_path):
    app, service, engine, _composer = _file_app(tmp_path)
    session = await service.create_session("alice", "Physical lifecycle receipt", app.state.settings.auth_provider)
    authority = service.session_operation_authority
    context = authority.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=30,
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    return service, engine, authority, context, coordinator


def _read_actual_fence(engine, context):
    with engine.connect() as connection:
        return connection.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == context.fence.session_id)
        ).one()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", (RequiredWorkSource.LEASE_RENEWAL, RequiredWorkSource.TERMINAL_WRITER_READ_SQL))
async def test_exact_registered_actual_returned_carrier_and_foreign_result_refusal(tmp_path, source) -> None:
    _service, engine, authority, context, coordinator = await _actual_scope(tmp_path)
    ticket = coordinator.reserve(source)
    try:
        outcome = await async_workers.run_required_sql_finish_once(ticket, _read_actual_fence, engine, context)
        assert isinstance(outcome, RequiredSQLReturned)
        assert ticket._future is not None and ticket._future.done()
        coordinator.validate_joined_lifecycle_sql_outcome(ticket=ticket, expected_source=source, actual_outcome=outcome)
        with pytest.raises(AuditIntegrityError):
            coordinator.validate_joined_lifecycle_sql_outcome(
                ticket=ticket,
                expected_source=source,
                actual_outcome=RequiredSQLReturned(tuple(outcome.value), outcome.deferred_cancellations),
            )
        foreign_owner = RequiredWorkCoordinator(coordinator.authority)
        with pytest.raises(AuditIntegrityError):
            foreign_owner.validate_joined_lifecycle_sql_outcome(ticket=ticket, expected_source=source, actual_outcome=outcome)
        wrong_source = (
            RequiredWorkSource.LEASE_RENEWAL
            if source is RequiredWorkSource.TERMINAL_WRITER_READ_SQL
            else RequiredWorkSource.TERMINAL_WRITER_READ_SQL
        )
        with pytest.raises(AuditIntegrityError):
            coordinator.validate_joined_lifecycle_sql_outcome(ticket=ticket, expected_source=wrong_source, actual_outcome=outcome)
    finally:
        authority.release(context)
        engine.dispose()


@pytest.mark.asyncio
async def test_actual_raised_carrier_preserves_exact_sql_fault_and_refuses_counterfeit(tmp_path) -> None:
    _service, engine, authority, context, coordinator = await _actual_scope(tmp_path)
    ticket = coordinator.reserve(RequiredWorkSource.LEASE_RENEWAL)
    original = OperationalError("controlled renewal", {}, RuntimeError("owned SQL driver"))

    def owned_sql_fault():
        _read_actual_fence(engine, context)
        raise original

    try:
        outcome = await async_workers.run_required_sql_finish_once(ticket, owned_sql_fault)
        assert isinstance(outcome, RequiredSQLRaised) and outcome.error is original
        coordinator.validate_joined_lifecycle_sql_outcome(
            ticket=ticket,
            expected_source=RequiredWorkSource.LEASE_RENEWAL,
            actual_outcome=outcome,
        )
        with pytest.raises(AuditIntegrityError):
            coordinator.validate_joined_lifecycle_sql_outcome(
                ticket=ticket,
                expected_source=RequiredWorkSource.LEASE_RENEWAL,
                actual_outcome=RequiredSQLRaised(OperationalError("controlled renewal", {}, RuntimeError("owned SQL driver")), ()),
            )
        assert ticket.errors == (original,)
    finally:
        authority.release(context)
        engine.dispose()


@pytest.mark.asyncio
async def test_actual_known_no_submission_carrier_is_distinct_from_unobserved_ticket(tmp_path, monkeypatch) -> None:
    _service, engine, authority, context, coordinator = await _actual_scope(tmp_path)
    ticket = coordinator.reserve(RequiredWorkSource.LEASE_RENEWAL)
    invoked = []
    monkeypatch.setattr(async_workers, "_RECOVERY_CALLBACK", None)

    def forbidden_sql():
        invoked.append(True)
        return _read_actual_fence(engine, context)

    try:
        outcome = await async_workers.run_required_sql_finish_once(ticket, forbidden_sql)
        assert isinstance(outcome, RequiredSQLRaised) and isinstance(outcome.error, AuditIntegrityError)
        assert ticket.complete and ticket._future is None and invoked == []
        coordinator.validate_joined_lifecycle_sql_outcome(
            ticket=ticket,
            expected_source=RequiredWorkSource.LEASE_RENEWAL,
            actual_outcome=outcome,
        )
        unobserved = coordinator.reserve(RequiredWorkSource.LEASE_RENEWAL, recurrence_ordinal=1)
        with pytest.raises(AuditIntegrityError):
            coordinator.validate_joined_lifecycle_sql_outcome(
                ticket=unobserved,
                expected_source=RequiredWorkSource.LEASE_RENEWAL,
                actual_outcome=outcome,
            )
        assert not unobserved.complete
    finally:
        authority.release(context)
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("physical_state", ("queued", "entered"))
@pytest.mark.parametrize("sql_fails", (False, True))
async def test_actual_renewal_snapshot_retains_three_original_cancels_and_physical_sql(
    tmp_path, monkeypatch, physical_state: str, sql_fails: bool
) -> None:
    _service, engine, authority, context, coordinator = await _actual_scope(tmp_path)
    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", executor)
    blocker_entered, unblock = threading.Event(), threading.Event()
    sql_entered, sql_release = threading.Event(), threading.Event()
    blocker = None
    if physical_state == "queued":
        blocker = executor.submit(lambda: (blocker_entered.set(), unblock.wait(10)))
        async with asyncio.timeout(5):
            while not blocker_entered.is_set():
                await asyncio.sleep(0.001)
    original = OperationalError("controlled real renewal", {}, RuntimeError("owned driver fault"))
    production_renew = authority.renew
    actual_sql_calls = []

    def held_actual_renew(*args, **kwargs):
        actual_sql_calls.append(True)
        sql_entered.set()
        if not sql_release.wait(10):
            raise TimeoutError("Required renewal SQL was not released")
        if sql_fails:
            _read_actual_fence(engine, context)
            raise original
        return production_renew(*args, **kwargs)

    monkeypatch.setattr(authority, "renew", held_actual_renew)
    lease = lifecycle.SessionOperationLease(authority, context, lease_seconds=30, renew_interval_seconds=0.05, required_work=coordinator)
    production_sleep = asyncio.sleep
    observer_ready = asyncio.Event()
    delivered = []

    async def observe_delivered_sleep(seconds):
        current = asyncio.current_task()
        if current is not None and current.get_name() == "physical-renewal-observer":
            observer_ready.set()
        try:
            await production_sleep(seconds)
        except asyncio.CancelledError as original_cancel:
            if current is not None and current.get_name() == "physical-renewal-observer":
                delivered.append(original_cancel)
            raise

    observer = None
    try:
        async with asyncio.timeout(5):
            while lease._renewal_attempt is None or lease._renewal_attempt.ticket._future is None:
                await production_sleep(0.001)
        attempt = lease._renewal_attempt
        assert attempt.task is not None and not attempt.task.done()
        actual_future = attempt.ticket._future
        assert actual_future is not None and not actual_future.done()
        if physical_state == "queued":
            assert not actual_future.running() and not sql_entered.is_set() and actual_sql_calls == []
        else:
            async with asyncio.timeout(5):
                while not sql_entered.is_set():
                    await production_sleep(0.001)
            assert actual_future.running() and actual_sql_calls == [True]
        monkeypatch.setattr(lifecycle.asyncio, "sleep", observe_delivered_sleep)
        observer = asyncio.create_task(lease.observe_current_renewal_attempt(), name="physical-renewal-observer")
        await asyncio.wait_for(observer_ready.wait(), 5)
        for index in range(3):
            observer.cancel(f"physical renewal cancellation {index}")
            async with asyncio.timeout(5):
                while len(delivered) <= index:
                    await production_sleep(0.001)
            assert delivered[index].args == (f"physical renewal cancellation {index}",)
            assert not observer.done() and not actual_future.done() and not attempt.ticket.complete
            assert not lease._stop_renewal.is_set()
        unblock.set()
        async with asyncio.timeout(5):
            while not sql_entered.is_set():
                await production_sleep(0.001)
        assert not actual_future.done()
        sql_release.set()
        outcomes = await observer
        expected = (*delivered, *((original,) if sql_fails else ()))
        assert len(outcomes) == len(expected)
        assert all(actual is wanted for actual, wanted in zip(outcomes, expected, strict=True))
        assert attempt.outcome_observed and attempt.ticket.complete and actual_future.done()
        assert actual_sql_calls == [True]
        assert not lease._stop_renewal.is_set()
        if sql_fails:
            assert attempt.error is original and attempt.ticket.errors == (original,)
        else:
            assert attempt.returned is context
            assert not lease._renewal_task.done()
        if blocker is not None:
            assert blocker.done()
    finally:
        unblock.set()
        sql_release.set()
        if observer is not None and not observer.done():
            await asyncio.gather(observer, return_exceptions=True)
        try:
            if sql_fails:
                with pytest.raises(OperationalError) as closed:
                    await lease.close()
                assert closed.value is original
            else:
                await lease.close()
        finally:
            await async_workers.shutdown_async_workers()
            if sql_fails:
                authority.release(context)  # Explicit test-owner cleanup of the deliberately lost renewal fence.
            engine.dispose()


@pytest.mark.asyncio
async def test_actual_unentered_renewal_task_preserves_original_cancel_and_refuses_release(tmp_path, monkeypatch) -> None:
    _service, engine, authority, context, coordinator = await _actual_scope(tmp_path)
    production_create_task = asyncio.create_task
    allocated = []
    invoked = []
    real_renew = authority.renew

    def observed_renew(*args, **kwargs):
        invoked.append(True)
        return real_renew(*args, **kwargs)

    def cancel_actual_attempt_before_entry(coroutine, *args, **kwargs):
        task = production_create_task(coroutine, *args, **kwargs)
        if kwargs.get("name") == "session-operation-renewal-attempt":
            allocated.append(task)
            task.cancel("actual renewal task cancelled before entry")
        return task

    monkeypatch.setattr(authority, "renew", observed_renew)
    monkeypatch.setattr(lifecycle.asyncio, "create_task", cancel_actual_attempt_before_entry)
    lease = lifecycle.SessionOperationLease(authority, context, lease_seconds=30, renew_interval_seconds=0.01, required_work=coordinator)
    try:
        async with asyncio.timeout(5):
            while lease._renewal_attempt is None or not allocated or not allocated[0].done():
                await asyncio.sleep(0.001)
        attempt = lease._renewal_attempt
        errors = await lease.observe_current_renewal_attempt()
        assert len(errors) == 1 and isinstance(errors[0], ComposerTerminalSQLCompletionUnknown)
        retained_unknown = errors[0]
        retained_cancellation = retained_unknown.__cause__
        assert isinstance(retained_cancellation, asyncio.CancelledError)
        assert errors[0].__cause__.args == ("actual renewal task cancelled before entry",)
        assert attempt.task is allocated[0] and not attempt.outcome_observed
        assert not attempt.ticket.complete and attempt.ticket._future is None
        assert invoked == []
        before = _read_actual_fence(engine, context)
        with pytest.raises(BaseExceptionGroup) as closing:
            await lease.close()
        assert len(closing.value.exceptions) == 2
        assert any(error is retained_unknown for error in closing.value.exceptions)
        assert any(isinstance(error, RequiredWorkIncomplete) for error in closing.value.exceptions)
        assert retained_unknown.__cause__ is retained_cancellation
        repeated = await lease.observe_current_renewal_attempt()
        assert repeated == (retained_unknown,)
        assert _read_actual_fence(engine, context) == before
        assert all(ticket.key.source is not RequiredWorkSource.LEASE_RELEASE for ticket in coordinator.tickets)
    finally:
        if not lease.closed:
            await asyncio.gather(lease.close(), return_exceptions=True)
        # The test observed cancellation before bridge entry; release exact fixture
        # authority without manufacturing a production completion receipt.
        authority.release(context)
        engine.dispose()


@pytest.mark.asyncio
async def test_genuine_sql_return_of_foreign_context_is_nominal_integrity_not_ordinary_weather(tmp_path, monkeypatch) -> None:
    _service, engine, authority, context, coordinator = await _actual_scope(tmp_path)
    foreign_path = tmp_path / "foreign-scope"
    foreign_path.mkdir()
    _foreign_service, foreign_engine, foreign_authority, foreign_context, _foreign_coordinator = await _actual_scope(foreign_path)
    real_renew = authority.renew
    returned = []

    def wrong_context_after_real_sql(*args, **kwargs):
        actual = real_renew(*args, **kwargs)
        assert actual is context
        returned.append(foreign_context)
        return foreign_context

    monkeypatch.setattr(authority, "renew", wrong_context_after_real_sql)
    lease = lifecycle.SessionOperationLease(authority, context, lease_seconds=30, renew_interval_seconds=0.01, required_work=coordinator)
    try:
        async with asyncio.timeout(5):
            while lease._renewal_attempt is None or not lease._renewal_attempt.outcome_observed:
                await asyncio.sleep(0.001)
        attempt = lease._renewal_attempt
        errors = await lease.observe_current_renewal_attempt()
        assert returned == [foreign_context]
        assert attempt.ticket.complete and attempt.actual_outcome is not None
        coordinator.validate_joined_lifecycle_sql_outcome(
            ticket=attempt.ticket,
            expected_source=RequiredWorkSource.LEASE_RENEWAL,
            actual_outcome=attempt.actual_outcome,
        )
        assert isinstance(attempt.actual_outcome, RequiredSQLReturned)
        assert attempt.actual_outcome.value is foreign_context
        assert len(errors) == 1 and isinstance(errors[0], AuditIntegrityError)
        assert not isinstance(errors[0], ComposerTerminalSQLCompletionUnknown)
        assert errors[0] is attempt.error
        with pytest.raises(AuditIntegrityError) as closed:
            await lease.close()
        assert closed.value is errors[0]
    finally:
        if not lease.closed:
            await asyncio.gather(lease.close(), return_exceptions=True)
        authority.release(context)
        foreign_authority.release(foreign_context)
        engine.dispose()
        foreign_engine.dispose()


@pytest.mark.asyncio
async def test_actual_renewal_task_cancellations_and_sql_fault_signal_loss_to_owner(tmp_path, monkeypatch):
    _service, engine, authority, context, coordinator = await _actual_scope(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    original_renew = authority.renew
    original_sleep = asyncio.sleep
    sql_error = OperationalError("actual required renewal fault", {}, RuntimeError("retained physical driver fault"))
    caught = []
    owner_cancellations = []
    lease = None

    def actual_renew_then_fault(actual_context, *, lease_seconds):
        returned = original_renew(actual_context, lease_seconds=lease_seconds)
        assert returned == context
        entered.set()
        assert release.wait(10)
        raise sql_error

    async def observe_actual_sleep(seconds):
        try:
            return await original_sleep(seconds)
        except asyncio.CancelledError as original:
            if lease is not None and lease._renewal_attempt is not None and asyncio.current_task() is lease._renewal_attempt.task:
                caught.append(original)
            raise

    async def owned_body():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as original:
            owner_cancellations.append(original)
            raise

    monkeypatch.setattr(authority, "renew", actual_renew_then_fault)
    monkeypatch.setattr(asyncio, "sleep", observe_actual_sleep)
    lease = lifecycle.SessionOperationLease(authority, context, lease_seconds=30, renew_interval_seconds=0.01, required_work=coordinator)
    owner = lease.create_task(owned_body(), name="actual-loss-owner")
    waiter = asyncio.create_task(lease.wait_until_lost())
    try:
        async with asyncio.timeout(10):
            while not entered.is_set():
                await original_sleep(0.001)
            attempt = lease._renewal_attempt
            assert attempt is not None and attempt.task is not None
            for index in range(1, 4):
                marker = object()
                attempt.task.cancel(marker)
                while len(caught) < index:
                    await original_sleep(0.001)
                assert caught[index - 1].args == (marker,)
                assert not attempt.task.done() and not waiter.done()
            release.set()
            loss = await waiter
            await asyncio.gather(owner, return_exceptions=True)
        assert isinstance(loss, BaseExceptionGroup) and lease.renewal_error is loss
        assert attempt.error is loss and attempt.outcome_observed
        assert isinstance(attempt.actual_outcome, RequiredSQLRaised)
        assert attempt.actual_outcome.error is sql_error
        assert len(caught) == 3 and attempt.actual_outcome.deferred_cancellations == tuple(caught)
        assert len(loss.exceptions) == 4
        assert all(actual is original for actual, original in zip(loss.exceptions[:3], caught, strict=True))
        assert loss.exceptions[3] is sql_error
        assert attempt.ticket.complete and attempt.ticket._future is not None and attempt.ticket._future.done()
        assert lease._lost_event.is_set() and owner.cancelled() and len(owner_cancellations) == 1
        before = _read_actual_fence(engine, context)
        with pytest.raises(BaseExceptionGroup) as closing:
            await lease.close()
        assert closing.value is loss
        assert _read_actual_fence(engine, context) == before
        assert all(ticket.key.source is not RequiredWorkSource.LEASE_RELEASE for ticket in coordinator.tickets)
    finally:
        release.set()
        if not waiter.done():
            waiter.cancel()
        if not owner.done():
            owner.cancel("test-owned teardown after assertion")
        await asyncio.gather(waiter, owner, return_exceptions=True)
        if not lease.closed:
            with suppress(BaseExceptionGroup):
                await lease.close()
        authority.release(context)
        engine.dispose()


@pytest.mark.asyncio
async def test_actual_receipt_rejection_retrieves_task_error_without_replacing_unknown(tmp_path, monkeypatch):
    _service, engine, authority, context, coordinator = await _actual_scope(tmp_path)
    actual_create_task = asyncio.create_task
    actual_bridge = lifecycle.run_required_sql_finish_once
    allocated = []
    retrieved = []
    sql_outcomes = []

    class RetrievalObservedTask(asyncio.Task):
        def result(self):
            retrieved.append(self)
            return super().result()

    def observed_create_task(coroutine, *args, **kwargs):
        if kwargs.get("name") == "session-operation-renewal-attempt":
            task = RetrievalObservedTask(coroutine, name=kwargs["name"])
            allocated.append(task)
            return task
        return actual_create_task(coroutine, *args, **kwargs)

    async def actual_sql_then_wrong_carrier(ticket, func, *args, **kwargs):
        outcome = await actual_bridge(ticket, func, *args, **kwargs)
        assert isinstance(outcome, RequiredSQLReturned)
        sql_outcomes.append(outcome)
        return RequiredSQLReturned(replace(outcome.value), outcome.deferred_cancellations)

    monkeypatch.setattr(lifecycle.asyncio, "create_task", observed_create_task)
    monkeypatch.setattr(lifecycle, "run_required_sql_finish_once", actual_sql_then_wrong_carrier)
    lease = lifecycle.SessionOperationLease(authority, context, lease_seconds=30, renew_interval_seconds=0.01, required_work=coordinator)
    try:
        async with asyncio.timeout(5):
            unknown = await lease.wait_until_lost()
        assert isinstance(unknown, ComposerTerminalSQLCompletionUnknown)
        assert isinstance(unknown.__cause__, AuditIntegrityError)
        attempt = lease._renewal_attempt
        assert attempt is not None and attempt.task is allocated[0] and attempt.task.done()
        assert attempt.error is unknown and not attempt.outcome_observed
        assert len(sql_outcomes) == 1 and sql_outcomes[0].value == context
        assert attempt.ticket._future is not None and attempt.ticket._future.done()
        assert any(task is attempt.task for task in retrieved)
        original_cause = unknown.__cause__
        assert await lease.observe_current_renewal_attempt() == (unknown,)
        assert unknown.__cause__ is original_cause
        with pytest.raises(ComposerTerminalSQLCompletionUnknown) as closing:
            await lease.close()
        assert closing.value is unknown and unknown.__cause__ is original_cause
        assert all(ticket.key.source is not RequiredWorkSource.LEASE_RELEASE for ticket in coordinator.tickets)
    finally:
        if not lease.closed:
            with suppress(ComposerTerminalSQLCompletionUnknown):
                await lease.close()
        authority.release(context)
        engine.dispose()
