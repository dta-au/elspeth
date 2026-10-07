"""Actual queued/no-return/generation join controls for the shared gate."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from elspeth.web import async_workers
from elspeth.web.required_executor import InvocationGate, RequiredInvocationWitness
from tests.fixtures.required_executor import RecordingRequiredGenerationRecovery


class QueueThenRaiseExecutor(ThreadPoolExecutor):
    def submit(self, fn, /, *args, **kwargs):
        super().submit(fn, *args, **kwargs)
        raise RuntimeError("submit failed after queueing")


class BeforeQueueRaiseExecutor(ThreadPoolExecutor):
    def submit(self, fn, /, *args, **kwargs):
        raise RuntimeError("submit failed before queueing")


@pytest.mark.asyncio
@pytest.mark.parametrize("executor_type", [QueueThenRaiseExecutor, BeforeQueueRaiseExecutor])
async def test_no_return_quarantines_and_joins_before_sequential_replacement(monkeypatch, executor_type) -> None:
    executor = executor_type(max_workers=1)
    monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", executor)
    invoked = []
    before = async_workers.outstanding_admissions()
    with pytest.raises(RuntimeError, match="submit failed"):
        await async_workers.run_sync_in_worker(lambda: invoked.append("forbidden"))
    generation = async_workers._GENERATION_CUSTODIAN
    assert generation is not None
    while not generation.joined.is_set():
        await asyncio.sleep(0.01)
    assert invoked == []
    assert async_workers.outstanding_admissions() == before
    assert async_workers._SHARED_EXECUTOR is not executor
    assert all(reservation.released for reservation in generation.reservations.values())
    assert await async_workers.run_sync_in_worker(lambda: 7) == 7
    await async_workers.shutdown_async_workers()


@pytest.mark.asyncio
async def test_hung_generation_uses_captured_observer_and_never_reopens_after_escalation(monkeypatch) -> None:
    executor = QueueThenRaiseExecutor(max_workers=1)
    held = threading.Event()
    entered = threading.Event()
    # Real preexisting worker physically prevents the aborted wrapper joining.
    ThreadPoolExecutor.submit(executor, lambda: (entered.set(), held.wait()))
    while not entered.is_set():
        await asyncio.sleep(0.01)
    monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", executor)
    recorder = RecordingRequiredGenerationRecovery()
    draining = threading.Event()
    unavailable = threading.Event()
    async_workers.configure_required_executor_recovery(
        drain_seconds=0.05, instance_draining=draining, generation_unavailable=unavailable, recovery_callback=recorder
    )
    task = asyncio.create_task(async_workers.run_sync_in_worker(lambda: pytest.fail("aborted callable executed")))
    try:
        while not recorder.observations:
            await asyncio.sleep(0.01)
        generation = async_workers._GENERATION_CUSTODIAN
        assert generation is not None
        assert draining.is_set() and unavailable.is_set()
        assert len(recorder.observations) == 1
        assert not generation.joined.is_set()
        assert async_workers._SHARED_EXECUTOR is executor
        held.set()
        with pytest.raises(RuntimeError, match="submit failed"):
            await task
        assert generation.joined.is_set()
        assert async_workers._SHARED_EXECUTOR is executor
        assert unavailable.is_set()
    finally:
        held.set()
        executor.shutdown(wait=True)


def test_aborted_witness_never_invokes_callable() -> None:
    witness = RequiredInvocationWitness(1)
    witness.abort()
    with pytest.raises(RuntimeError, match="never armed"):
        witness.invoke(lambda: pytest.fail("aborted callable executed"))
    snapshot = witness.snapshot()
    assert snapshot.gate is InvocationGate.ABORTED
    assert snapshot.valid_aborted_exit


@pytest.mark.asyncio
async def test_callback_install_failure_retains_real_future_until_generation_join(monkeypatch) -> None:
    from concurrent.futures import Future

    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", executor)
    original = Future.add_done_callback
    refused = []

    def refuse_registration(future, callback):
        refused.append(future)
        raise RuntimeError("callback setup failed")

    monkeypatch.setattr(Future, "add_done_callback", refuse_registration)
    invoked = []
    before = async_workers.outstanding_admissions()
    with pytest.raises(RuntimeError, match="callback setup failed"):
        await async_workers.run_stream_read_in_worker(lambda: invoked.append("forbidden"))
    assert invoked == []
    assert len(refused) == 1 and refused[0].done()
    generation = async_workers._GENERATION_CUSTODIAN
    assert generation is not None and generation.joined.is_set()
    assert async_workers.outstanding_admissions() == before
    monkeypatch.setattr(Future, "add_done_callback", original)
    await async_workers.shutdown_async_workers()


@pytest.mark.asyncio
async def test_repeated_anomalies_do_not_accumulate_admission_charges(monkeypatch) -> None:
    before = async_workers.outstanding_admissions()
    for _ in range(4):
        executor = QueueThenRaiseExecutor(max_workers=1)
        monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", executor)
        monkeypatch.setattr(async_workers, "_GENERATION_CUSTODIAN", None)
        with pytest.raises(RuntimeError, match="submit failed"):
            await async_workers.run_sync_in_worker(lambda: pytest.fail("replayed work"))
        generation = async_workers._GENERATION_CUSTODIAN
        assert generation is not None
        while not generation.joined.is_set():
            await asyncio.sleep(0.01)
        assert async_workers.outstanding_admissions() == before
        assert len(generation.reservations) == 1
        await async_workers.shutdown_async_workers()


def test_impossible_aborted_witness_cannot_release_reservation() -> None:
    from elspeth.web.required_executor import InvocationReservation, RequiredInvocationIntegrityError

    releases = []
    witness = RequiredInvocationWitness(1)
    witness.abort()
    witness._exited = True
    reservation = InvocationReservation(witness, lambda: releases.append("released"))
    reservation.held = True
    reservation.submission_error = RuntimeError("original")
    with pytest.raises(RequiredInvocationIntegrityError):
        reservation.release_aborted()
    assert releases == [] and not reservation.released


@pytest.mark.asyncio
async def test_missing_recovery_registration_refuses_first_submission(monkeypatch) -> None:
    from elspeth.contracts.errors import AuditIntegrityError

    monkeypatch.setattr(async_workers, "_RECOVERY_CALLBACK", None)
    monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", None)
    before = async_workers.outstanding_admissions()
    invoked = []
    with pytest.raises(AuditIntegrityError):
        async_workers.required_generation_unavailable()
    with pytest.raises(AuditIntegrityError):
        await async_workers.run_sync_in_worker(lambda: invoked.append("forbidden"))
    assert async_workers._SHARED_EXECUTOR is None
    assert invoked == []
    assert async_workers.outstanding_admissions() == before


@pytest.mark.asyncio
async def test_active_completed_invocations_do_not_accumulate_generation_registry() -> None:
    for value in range(40):
        assert await async_workers.run_sync_in_worker(lambda captured=value: captured) == value
    generation = async_workers._GENERATION_CUSTODIAN
    assert generation is not None
    assert generation.reservations == {}
    assert async_workers.outstanding_admissions() == 0
    await async_workers.shutdown_async_workers()


@pytest.mark.asyncio
async def test_submit_failure_after_wrapper_entry_aborts_before_callable_start(monkeypatch) -> None:
    import time

    witnesses = []

    class RecordingWitness(RequiredInvocationWitness):
        def __init__(self, generation_identity):
            super().__init__(generation_identity)
            witnesses.append(self)

    class EnteredThenRaiseExecutor(ThreadPoolExecutor):
        def submit(self, fn, /, *args, **kwargs):
            super().submit(fn, *args, **kwargs)
            deadline = time.monotonic() + 2
            while not witnesses[0].snapshot().entered:
                if time.monotonic() >= deadline:
                    raise AssertionError("controlled wrapper failed to enter")
                time.sleep(0.001)
            assert not witnesses[0].snapshot().callable_started
            raise RuntimeError("submit failed after actual wrapper entry")

    monkeypatch.setattr(async_workers, "RequiredInvocationWitness", RecordingWitness)
    executor = EnteredThenRaiseExecutor(max_workers=1)
    monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", executor)
    invoked = []
    with pytest.raises(RuntimeError, match="after actual wrapper entry"):
        await async_workers.run_sync_in_worker(lambda: invoked.append("forbidden"))
    assert witnesses[0].snapshot().valid_aborted_exit
    assert invoked == []
    assert async_workers.outstanding_admissions() == 0
    generation = async_workers._GENERATION_CUSTODIAN
    assert generation is not None
    while not generation.recovery_finished.is_set():
        await asyncio.sleep(0.01)
    assert generation.joined.is_set()
    assert async_workers._SHARED_EXECUTOR is not executor
    await async_workers.shutdown_async_workers()


@pytest.mark.asyncio
@pytest.mark.parametrize("sql_fails", [False, True])
async def test_queued_required_sql_survives_repeated_cancellation_and_retains_actual_outcome(monkeypatch, sql_fails) -> None:
    from uuid import uuid4

    from sqlalchemy.exc import OperationalError

    from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
    from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkAuthority, RequiredWorkCoordinator, RequiredWorkSource

    context = SessionOperationContext(
        SessionOperationFence(str(uuid4()), str(uuid4()), "owned-test-fence", 1), SessionOperationKind.COMPOSE
    )
    coordinator = RequiredWorkCoordinator(RequiredWorkAuthority(RequiredAuthorityKind.DURABLE_COMPOSE, context, str(uuid4()), 1))
    ticket = coordinator.reserve(RequiredWorkSource.PIPELINE_PUBLICATION_SQL)
    executor = ThreadPoolExecutor(max_workers=1)
    held = threading.Event()
    entered = threading.Event()
    blocker = executor.submit(lambda: (entered.set(), held.wait()))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", executor)
    calls = []
    original = OperationalError("controlled SQL", {}, RuntimeError("owned driver failure"))

    def required_sql():
        calls.append("sql")
        if sql_fails:
            raise original
        return 19

    delivered_cancellations: list[asyncio.CancelledError] = []
    production_retain = async_workers._retain_cancellation

    def retain_original(cancellations: list[asyncio.CancelledError], cancellation: asyncio.CancelledError) -> None:
        delivered_cancellations.append(cancellation)
        production_retain(cancellations, cancellation)

    monkeypatch.setattr(async_workers, "_retain_cancellation", retain_original)
    task = asyncio.create_task(async_workers.run_required_sql_in_worker(ticket, required_sql))
    try:
        while async_workers.outstanding_admissions() != 1:
            await asyncio.sleep(0.001)
        for index in range(3):
            task.cancel(f"owned queued cancellation {index}")
            await asyncio.sleep(0.01)
            assert len(delivered_cancellations) == index + 1
            assert delivered_cancellations[index].args == (f"owned queued cancellation {index}",)
            assert all(delivered_cancellations[index] is not earlier for earlier in delivered_cancellations[:index])
            assert not task.done()
            assert not ticket.complete
            assert async_workers.outstanding_admissions() == 1
            assert calls == []
        held.set()
        with pytest.raises(BaseExceptionGroup) as cancelled:
            await task
        assert calls == ["sql"]
        assert ticket.complete and coordinator.all_completed
        assert ticket.errors == ((original,) if sql_fails else ())
        assert len(delivered_cancellations) == 3
        expected_originals: tuple[BaseException, ...] = (*delivered_cancellations, *((original,) if sql_fails else ()))

        def assert_retained_originals(outcome: BaseExceptionGroup) -> None:
            assert len(outcome.exceptions) == len(expected_originals)
            assert all(actual is expected for actual, expected in zip(outcome.exceptions, expected_originals, strict=True))

        assert_retained_originals(cancelled.value)
        if sql_fails:
            assert ticket.errors[0] is original
            assert cancelled.value.exceptions[-1] is original
        dropped_original = BaseExceptionGroup("Controlled dropped cancellation", list(expected_originals[1:]))
        with pytest.raises(AssertionError):
            assert_retained_originals(dropped_original)
        assert async_workers.outstanding_admissions() == 0
        assert blocker.done()
    finally:
        held.set()
        await async_workers.shutdown_async_workers()


@pytest.mark.asyncio
async def test_valid_aborted_exit_releases_before_hung_old_generation_join_without_replacement() -> None:
    from elspeth.web.required_executor import InvocationReservation, RequiredExecutorGenerationCustodian

    executor = ThreadPoolExecutor(max_workers=1)
    held = threading.Event()
    entered = threading.Event()
    blocker = executor.submit(lambda: (entered.set(), held.wait()))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    unavailable = threading.Event()
    draining = threading.Event()
    releases = []
    replacements = []
    generation = RequiredExecutorGenerationCustodian(
        executor,
        generation_identity=701,
        drain_seconds=10,
        generation_unavailable=unavailable,
        instance_draining=draining,
        recovery_callback=RecordingRequiredGenerationRecovery(),
        replacement_factory=lambda: ThreadPoolExecutor(max_workers=1),
        install_replacement=replacements.append,
    )
    witness = RequiredInvocationWitness(701)
    witness.abort()
    with pytest.raises(RuntimeError, match="never armed"):
        witness.invoke(lambda: pytest.fail("unarmed callable ran"))
    reservation = InvocationReservation(witness, lambda: releases.append("released"))
    reservation.held = True
    reservation.submission_error = RuntimeError("owned no-return submission")
    generation.register(reservation)
    try:
        generation.quarantine()
        while not reservation.released:
            await asyncio.sleep(0.001)
        assert releases == ["released"]
        assert not blocker.done() and not generation.joined.is_set()
        assert unavailable.is_set() and replacements == []
        held.set()
        while not generation.recovery_finished.is_set():
            await asyncio.sleep(0.001)
        assert generation.joined.is_set() and len(replacements) == 1
        assert releases == ["released"]
        assert generation.observer is not None
        await generation.observer
    finally:
        held.set()
        executor.shutdown(wait=True)
        for replacement in replacements:
            replacement.shutdown(wait=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("configured,captured", [(0.5, 0.5), (60.0, 30.0)])
async def test_generation_deadline_is_fixed_captured_minimum_and_callback_fault_retained(monkeypatch, configured, captured) -> None:
    from types import SimpleNamespace

    import elspeth.web.required_executor as custody

    executor = ThreadPoolExecutor(max_workers=1)
    held = threading.Event()
    entered = threading.Event()
    executor.submit(lambda: (entered.set(), held.wait()))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    clock = [100.0]
    monkeypatch.setattr(custody, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    callback_error = RuntimeError("controlled recovery callback failure")
    observations = []
    replacements = []

    def fail_callback(observation):
        observations.append(observation)
        raise callback_error

    unavailable = threading.Event()
    draining = threading.Event()
    generation = custody.RequiredExecutorGenerationCustodian(
        executor,
        generation_identity=702,
        drain_seconds=configured,
        generation_unavailable=unavailable,
        instance_draining=draining,
        recovery_callback=fail_callback,
        replacement_factory=lambda: ThreadPoolExecutor(max_workers=1),
        install_replacement=replacements.append,
    )
    try:
        generation.quarantine()
        assert generation.deadline == 100.0 + captured
        first_observer = generation.observer
        generation.quarantine()
        assert generation.observer is first_observer and generation.deadline == 100.0 + captured
        clock[0] = 100.0 + captured
        assert first_observer is not None
        with pytest.raises(RuntimeError) as raised:
            await first_observer
        assert raised.value is callback_error and generation.observer_error is callback_error
        assert len(observations) == 1
        assert observations[0].captured_deadline == 100.0 + captured
        assert draining.is_set() and unavailable.is_set() and generation.escalated
        assert not generation.joined.is_set() and replacements == []
        held.set()
        while not generation.recovery_finished.is_set():
            await asyncio.sleep(0.001)
        assert generation.joined.is_set() and replacements == []
        assert draining.is_set() and unavailable.is_set()
    finally:
        held.set()
        executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_fixture_witness_refuses_actual_pending_and_unreleased_future_then_accepts_completion() -> None:
    from elspeth.web.required_executor import InvocationReservation, RequiredExecutorGenerationCustodian
    from tests.fixtures.required_executor import assert_owned_executor_fixture_settled

    assert_owned_executor_fixture_settled(None)
    executor = ThreadPoolExecutor(max_workers=1)
    generation = RequiredExecutorGenerationCustodian(
        executor,
        generation_identity=703,
        drain_seconds=10,
        generation_unavailable=threading.Event(),
        instance_draining=threading.Event(),
        recovery_callback=RecordingRequiredGenerationRecovery(),
        replacement_factory=lambda: ThreadPoolExecutor(max_workers=1),
        install_replacement=lambda replacement: replacement.shutdown(wait=True),
    )
    assert_owned_executor_fixture_settled(generation)
    held = threading.Event()
    entered = threading.Event()
    actual = executor.submit(lambda: (entered.set(), held.wait()))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    reservation = InvocationReservation(RequiredInvocationWitness(703), lambda: None)
    reservation.held = True
    reservation.future = actual
    generation.register(reservation)
    try:
        with pytest.raises(BaseExceptionGroup) as pending:
            assert_owned_executor_fixture_settled(generation)
        assert len(pending.value.exceptions) == 2
        held.set()
        while not actual.done():
            await asyncio.sleep(0.001)
        with pytest.raises(BaseExceptionGroup) as unreleased:
            assert_owned_executor_fixture_settled(generation)
        assert len(unreleased.value.exceptions) == 1
        reservation.release_future(actual)
        assert_owned_executor_fixture_settled(generation)
    finally:
        held.set()
        executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_fixture_witness_refuses_unjoined_quarantine_and_preserves_original_fault() -> None:
    from elspeth.web.required_executor import RequiredExecutorGenerationCustodian
    from tests.fixtures.required_executor import assert_owned_executor_fixture_settled

    executor = ThreadPoolExecutor(max_workers=1)
    held = threading.Event()
    entered = threading.Event()
    executor.submit(lambda: (entered.set(), held.wait()))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    original = RuntimeError("owned retained observer failure")
    generation = RequiredExecutorGenerationCustodian(
        executor,
        generation_identity=704,
        drain_seconds=10,
        generation_unavailable=threading.Event(),
        instance_draining=threading.Event(),
        recovery_callback=RecordingRequiredGenerationRecovery(),
        replacement_factory=lambda: ThreadPoolExecutor(max_workers=1),
        install_replacement=lambda replacement: replacement.shutdown(wait=True),
    )
    try:
        generation.quarantine()
        generation.observer_error = original
        with pytest.raises(BaseExceptionGroup) as unresolved:
            assert_owned_executor_fixture_settled(generation)
        assert original in unresolved.value.exceptions
        held.set()
        while not generation.recovery_finished.is_set():
            await asyncio.sleep(0.001)
        assert generation.observer is not None
        await generation.observer
        assert_owned_executor_fixture_settled(generation)
    finally:
        held.set()
        executor.shutdown(wait=True)
