"""Actual SQL values survive cancellation without unknown-result handoffs."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web import async_workers
from elspeth.web.required_executor import InvocationReservation, RequiredInvocationWitness
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkAuthority, RequiredWorkCoordinator, RequiredWorkSource
from tests.unit.web.test_required_executor_custody import QueueThenRaiseExecutor


def publication_ticket():
    context = SessionOperationContext(
        SessionOperationFence(str(uuid4()), str(uuid4()), "owned-test-fence", 1), SessionOperationKind.COMPOSE
    )
    coordinator = RequiredWorkCoordinator(RequiredWorkAuthority(RequiredAuthorityKind.DURABLE_COMPOSE, context, str(uuid4()), 1))
    return coordinator.reserve(RequiredWorkSource.PIPELINE_PUBLICATION_SQL)


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("queued", [False, True])
async def test_actual_sql_outcome_and_original_cancellations_are_handed_off_once(monkeypatch, fails, queued):
    ticket = publication_ticket()
    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", executor)
    entered = threading.Event()
    release = threading.Event()
    value = object()
    original = RuntimeError("controlled physical failure")
    calls = []
    observed = []
    retain = async_workers._retain_cancellation
    blocker_entered = threading.Event()
    if queued:
        executor.submit(lambda: (blocker_entered.set(), release.wait()))
        while not blocker_entered.is_set():
            await asyncio.sleep(0.001)

    def observe(cancellations, cancellation):
        observed.append(cancellation)
        retain(cancellations, cancellation)

    monkeypatch.setattr(async_workers, "_retain_cancellation", observe)

    def sql():
        calls.append("sql")
        entered.set()
        release.wait()
        if fails:
            raise original
        return value

    task = asyncio.create_task(async_workers.run_required_sql_finish_once(ticket, sql))
    try:
        while not (async_workers.outstanding_admissions() == 1 if queued else entered.is_set()):
            await asyncio.sleep(0.001)
        for index in range(3):
            task.cancel(f"owned cancellation {index}")
            while len(observed) != index + 1:
                await asyncio.sleep(0.001)
            assert not task.done() and not ticket.complete
            assert async_workers.outstanding_admissions() == 1
            assert calls == ([] if queued else ["sql"])
        release.set()
        outcome = await task
        assert calls == ["sql"] and ticket.complete
        assert len(outcome.deferred_cancellations) == 3
        assert all(actual is expected for actual, expected in zip(outcome.deferred_cancellations, observed, strict=True))
        if fails:
            assert isinstance(outcome, RequiredSQLRaised) and outcome.error is original
            assert ticket.errors == (original,)
        else:
            assert isinstance(outcome, RequiredSQLReturned) and outcome.value is value
        assert async_workers.outstanding_admissions() == 0
        with pytest.raises(AuditIntegrityError):
            await async_workers.run_required_sql_finish_once(ticket, lambda: calls.append("replay"))
        assert calls == ["sql"]
    finally:
        release.set()
        await async_workers.shutdown_async_workers()


@pytest.mark.asyncio
async def test_known_no_submission_refusal_has_original_error_without_callable(monkeypatch):
    ticket = publication_ticket()
    async_workers._INSTANCE_DRAINING.set()
    calls = []
    outcome = await async_workers.run_required_sql_finish_once(ticket, lambda: calls.append("forbidden"))
    assert isinstance(outcome, RequiredSQLRaised)
    assert ticket.complete and ticket.errors == (outcome.error,)
    assert calls == [] and outcome.deferred_cancellations == ()
    assert async_workers.outstanding_admissions() == 0


@pytest.mark.asyncio
async def test_unproven_physical_failure_never_issues_known_arm(monkeypatch):
    ticket = publication_ticket()
    original = RuntimeError("controlled unknown custody")

    async def unknown(*args, **kwargs):
        raise original

    monkeypatch.setattr(async_workers, "_submit_shared", unknown)
    with pytest.raises(RuntimeError) as raised:
        await async_workers.run_required_sql_finish_once(ticket, lambda: pytest.fail("unknown callable executed"))
    assert raised.value is original and not ticket.complete


@pytest.mark.asyncio
async def test_cancelled_pre_submission_retains_exact_original_without_sql(monkeypatch):
    ticket = publication_ticket()
    original = asyncio.CancelledError("owned admission cancellation")

    async def cancelled_admission():
        raise original

    monkeypatch.setattr(async_workers, "_acquire_admission", cancelled_admission)
    outcome = await async_workers.run_required_sql_finish_once(ticket, lambda: pytest.fail("unadmitted SQL"))
    assert isinstance(outcome, RequiredSQLRaised) and outcome.error is original
    assert outcome.deferred_cancellations == (original,) and outcome.deferred_cancellations[0] is original
    assert ticket.complete and ticket.errors == (original,)
    await async_workers.shutdown_async_workers()


@pytest.mark.asyncio
async def test_no_return_submission_only_hands_off_after_real_aborted_witness(monkeypatch):
    ticket = publication_ticket()
    executor = QueueThenRaiseExecutor(max_workers=1)
    monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", executor)
    calls = []
    outcome = await async_workers.run_required_sql_finish_once(ticket, lambda: calls.append("forbidden"))
    assert isinstance(outcome, RequiredSQLRaised) and ticket.complete
    assert calls == [] and async_workers.outstanding_admissions() == 0
    generation = async_workers._GENERATION_CUSTODIAN
    assert generation is not None
    while not generation.recovery_finished.is_set():
        await asyncio.sleep(0.001)
    assert generation.joined.is_set()
    await async_workers.shutdown_async_workers()


def test_closed_carriers_refuse_malformed_or_duplicate_cancellations():
    cancellation = asyncio.CancelledError("owned")
    with pytest.raises(ValueError):
        RequiredSQLReturned(1, (cancellation, cancellation))
    with pytest.raises(TypeError):
        RequiredSQLRaised(RuntimeError(), (RuntimeError(),))


def test_aborted_exit_and_generation_join_do_not_complete_ticket_or_release_twice():
    ticket = publication_ticket()
    original = RuntimeError("owned submit failure")
    ticket.observe_submission_unknown(original)
    witness = RequiredInvocationWitness(1)
    witness.abort()
    with pytest.raises(RuntimeError):
        witness.invoke(lambda: pytest.fail("aborted business callable executed"))
    releases = []
    reservation = InvocationReservation(witness, lambda: releases.append("released"), ticket)
    reservation.held = True
    reservation.submission_error = original
    reservation.release_aborted()
    reservation.release_joined()
    reservation.release_aborted()
    assert releases == ["released"] and ticket.complete


def test_dual_witness_control_rejects_semantic_completion_before_release_guard(monkeypatch):
    test_aborted_exit_and_generation_join_do_not_complete_ticket_or_release_twice()

    def broken_release_joined(self):
        self.ticket.complete_generation_joined(self.submission_error)
        self._release_once()

    monkeypatch.setattr(InvocationReservation, "release_joined", broken_release_joined)
    with pytest.raises(AuditIntegrityError):
        test_aborted_exit_and_generation_join_do_not_complete_ticket_or_release_twice()
