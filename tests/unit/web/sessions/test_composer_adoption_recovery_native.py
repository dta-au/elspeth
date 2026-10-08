"""Real SQLite and physical-worker proofs for failed adoption recovery.

The wrappers below gate named physical call sites. They never manufacture a
terminal row, a required ticket, or a completed executor Future.
"""

from __future__ import annotations

import asyncio
import errno
import threading
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.coordination import lifecycle
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.process_watchdog_codec import RecoveryReason
from elspeth.web.required_work import RequiredWorkIncomplete, RequiredWorkSource, make_required_work_key
from elspeth.web.sessions import composer_async_worker as worker_module
from elspeth.web.sessions.composer_operation_errors import request_cancelled_error
from elspeth.web.sessions.composer_operations import COMPOSER_SHUTDOWN, ComposerOperationError, ComposerOperationFenceLost
from elspeth.web.sessions.service import ComposerTerminalSQLCompletionUnknown
from tests.unit.web.sessions.test_composer_async_worker import _admit, _file_app, _worker


def _leaves(error: BaseException) -> tuple[BaseException, ...]:
    if isinstance(error, BaseExceptionGroup):
        return tuple(leaf for member in error.exceptions for leaf in _leaves(member))
    return (error,)


def _explicit_originals(error: BaseException) -> tuple[BaseException, ...]:
    pending = [error]
    seen: set[int] = set()
    found: list[BaseException] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        found.append(current)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
    return tuple(found)


async def _wait_for(entered: threading.Event) -> None:
    for _ in range(500):
        if entered.is_set():
            return
        await asyncio.sleep(0.01)
    pytest.fail("named physical SQL call did not enter")


def _operation_error(authority: ComposerAsyncOperationAuthority, record) -> ComposerOperationError:
    terminal = authority.get(session_id=record.session_id, operation_id=record.operation_id)
    assert terminal is not None and terminal.status == "failed"
    return ComposerOperationError.model_validate_json(terminal.result_json)


def _assert_handoff(owner, authority: ComposerAsyncOperationAuthority, record, observations):
    handoff = owner.result()
    assert type(handoff) is worker_module._AdoptionSettlementHandoff
    terminal = authority.get(session_id=record.session_id, operation_id=record.operation_id)
    assert terminal is not None and handoff.terminal == terminal
    assert terminal.session_id == record.session_id and terminal.operation_id == record.operation_id
    assert len(handoff.cancellations) == len(observations)
    assert all(actual is expected for actual, expected in zip(handoff.cancellations, observations, strict=True))
    return handoff


def _observe_owner_shield_deliveries(monkeypatch, owner, entered: threading.Event, release: threading.Event, child_coro: str):
    """Capture originals delivered to the actual owner before its physical join ends."""
    actual_shield = asyncio.shield
    delivered: list[asyncio.CancelledError] = []

    def observe_shield(awaitable):
        shielded = actual_shield(awaitable)
        if (
            asyncio.current_task() is not owner
            or not isinstance(awaitable, asyncio.Task)
            or awaitable.get_coro().__qualname__ != child_coro
        ):
            # Every other caller retains asyncio.shield's actual Future.
            return shielded

        # This exact _join_owned call only directly awaits the returned value.
        async def observe():
            try:
                return await shielded
            except asyncio.CancelledError as original:
                if entered.is_set() and not release.is_set():
                    delivered.append(original)
                raise

        return observe()

    monkeypatch.setattr(worker_module.asyncio, "shield", observe_shield)
    return delivered


async def _begin_failed_adoption(tmp_path, monkeypatch, *, sql_error: BaseException, deadline_seconds: float = 85.0):
    app, service, engine, composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    entered = threading.Event()
    release = threading.Event()

    def failed_cas(context):
        entered.set()
        assert release.wait(timeout=10)
        raise sql_error

    monkeypatch.setattr(service.session_operation_authority, "compare_and_swap", failed_cas)
    worker = _worker(app, authority)
    record = await _admit(app, service, authority, operation_id=str(uuid4()), deadline_seconds=deadline_seconds)
    task = asyncio.create_task(worker.run_until_idle())
    try:
        await _wait_for(entered)
        current = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert current is not None and current.status == "running"
    except BaseException:
        release.set()
        try:
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=10)
        finally:
            engine.dispose()
        raise
    return app, service, engine, composer, authority, worker, current, task, release


async def _finish(task: asyncio.Task[None], release: threading.Event, engine) -> None:
    release.set()
    try:
        await asyncio.wait_for(task, timeout=10)
    finally:
        engine.dispose()


def _capture_original_coordinator(monkeypatch, worker):
    captured = []
    original = worker._reserve_failed_terminal_projection

    def capture(coordinator):
        if not captured:
            captured.append(coordinator)
        return original(coordinator)

    monkeypatch.setattr(worker, "_reserve_failed_terminal_projection", capture)
    return captured


@pytest.mark.asyncio
async def test_owner_cancel_and_required_sql_existing_cause_retain_exact_originals(tmp_path, monkeypatch) -> None:
    physical = OperationalError("adoption CAS", {}, RuntimeError("physical private SQL failure"))
    driver_cause = RuntimeError("preexisting physical cause")
    physical.__cause__ = driver_cause
    adoption_lists: list[list[asyncio.CancelledError]] = []
    seen_cancellations: list[asyncio.CancelledError] = []
    handoff_lists: list[list[asyncio.CancelledError]] = []
    original_adopt = lifecycle.SessionOperationLease.adopt
    original_retain = lifecycle._retain_cancellation

    async def observe_adopt(
        cls, authority, context, *, lease_seconds, renew_interval_seconds=None, required_work=None, cancellation_observations=None
    ):
        assert cancellation_observations is not None and required_work is not None
        adoption_lists.append(cancellation_observations)
        return await original_adopt(
            authority,
            context,
            lease_seconds=lease_seconds,
            renew_interval_seconds=renew_interval_seconds,
            required_work=required_work,
            cancellation_observations=cancellation_observations,
        )

    def observe_retain(target, cancellation):
        if adoption_lists and target is adoption_lists[0]:
            seen_cancellations.append(cancellation)
        return original_retain(target, cancellation)

    monkeypatch.setattr(lifecycle.SessionOperationLease, "adopt", classmethod(observe_adopt))
    monkeypatch.setattr(lifecycle, "_retain_cancellation", observe_retain)
    _app, _service, engine, composer, authority, worker, record, task, release = await _begin_failed_adoption(
        tmp_path, monkeypatch, sql_error=physical
    )
    key = (record.session_id, record.operation_id)
    original_coordinator = _capture_original_coordinator(monkeypatch, worker)
    original_begin = worker._begin_adoption_failure_custody

    def observe_handoff(running, original):
        retained = original_begin(running, original)
        handoff_lists.append(retained)
        return retained

    monkeypatch.setattr(worker, "_begin_adoption_failure_custody", observe_handoff)
    try:
        authority.request_cancel(session_id=record.session_id, operation_id=record.operation_id, cancelled_failure=request_cancelled_error)
        owners = tuple(worker._jobs.values())
        assert len(owners) == 1
        owners[0].cancel(COMPOSER_SHUTDOWN)
        for _ in range(100):
            if seen_cancellations:
                break
            await asyncio.sleep(0.01)
        assert len(seen_cancellations) == 1 and len(adoption_lists) == 1
        assert adoption_lists[0][0] is seen_cancellations[0]
        owners[0].cancel("second adoption cleanup waiter")
        for _ in range(100):
            if len(seen_cancellations) >= 2:
                break
            await asyncio.sleep(0.01)
        assert len(seen_cancellations) >= 2
        owners[0].cancel("third adoption cleanup waiter")
        for _ in range(100):
            if len(seen_cancellations) >= 3:
                break
            await asyncio.sleep(0.01)
        assert len(seen_cancellations) >= 3
        release.set()
        await asyncio.wait_for(task, timeout=10)
        assert len(handoff_lists) == 1 and handoff_lists[0] is adoption_lists[0]
        assert all(any(item is observed for item in handoff_lists[0]) for observed in seen_cancellations[:3])
        _assert_handoff(owners[0], authority, record, handoff_lists[0])
        assert len(original_coordinator) == 1
        adoption = [ticket for ticket in original_coordinator[0].tickets if ticket.key.source is RequiredWorkSource.LEASE_ADOPTION]
        assert len(adoption) == 1
        assert adoption[0].key == make_required_work_key(original_coordinator[0].authority, RequiredWorkSource.LEASE_ADOPTION)
        assert any(receipt.original_root is physical for receipt in adoption[0].receipts())
        assert physical.__cause__ is driver_cause
        assert _operation_error(authority, record).http_status == 503
        assert composer.calls == 0
        assert key not in worker._adoption_recovery_cancellations
    finally:
        await _finish(task, release, engine)


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ("initial_read", "service"))
async def test_initial_settlement_waiters_retain_cancellation_and_real_fence(tmp_path, monkeypatch, gate: str) -> None:
    physical = OperationalError("adoption CAS", {}, RuntimeError("private SQL failure"))
    _app, service, engine, composer, authority, worker, record, task, release = await _begin_failed_adoption(
        tmp_path, monkeypatch, sql_error=physical
    )
    key = (record.session_id, record.operation_id)
    read_entered = threading.Event()
    read_release = threading.Event()
    service_entered = asyncio.Event()
    service_release = asyncio.Event()
    grouped: list[BaseExceptionGroup] = []
    original_read = authority.get_with_database_now
    original_service = service.fail_composer_async_operation
    original_classifier = worker._grouped_prewrite_fence_refusal
    reads = 0

    def gated_read(*, session_id, operation_id):
        nonlocal reads
        reads += 1
        if gate == "initial_read" and reads == 1:
            read_entered.set()
            assert read_release.wait(timeout=10)
        return original_read(session_id=session_id, operation_id=operation_id)

    async def gated_service(*args, **kwargs):
        service_entered.set()
        await service_release.wait()
        return await original_service(*args, **kwargs)

    def classify(original):
        grouped.append(original)
        return original_classifier(original)

    monkeypatch.setattr(authority, "get_with_database_now", gated_read)
    if gate == "service":
        monkeypatch.setattr(service, "fail_composer_async_operation", gated_service)
        monkeypatch.setattr(worker, "_grouped_prewrite_fence_refusal", classify)
    retained = worker._adoption_recovery_cancellations[key]
    try:
        release.set()
        if gate == "initial_read":
            await _wait_for(read_entered)
        else:
            await asyncio.wait_for(service_entered.wait(), timeout=5)
        owners = tuple(worker._jobs.values())
        assert len(owners) == 1
        owners[0].cancel(COMPOSER_SHUTDOWN)
        await asyncio.sleep(0.02)
        read_release.set()
        service_release.set()
        await asyncio.wait_for(task, timeout=10)
        assert any(isinstance(item, asyncio.CancelledError) for item in retained)
        _assert_handoff(owners[0], authority, record, retained)
        if gate == "service":
            assert len(grouped) == 1
            leaves = _leaves(grouped[0])
            assert len([item for item in leaves if isinstance(item, SessionOperationFenceLost)]) == 1
            assert all(isinstance(item, (SessionOperationFenceLost, ComposerOperationFenceLost, asyncio.CancelledError)) for item in leaves)
            assert any(item is observed for item in leaves for observed in retained if isinstance(item, asyncio.CancelledError))
        assert _operation_error(authority, record).http_status == 503
        assert composer.calls == 0
        assert key not in worker._adoption_recovery_cancellations
    finally:
        read_release.set()
        service_release.set()
        await _finish(task, release, engine)


@pytest.mark.asyncio
async def test_failed_fresh_terminal_sql_keeps_projection_pending_and_escalates(tmp_path, monkeypatch) -> None:
    physical = OperationalError("adoption CAS", {}, RuntimeError("private SQL failure"))
    _app, service, engine, composer, authority, worker, record, task, release = await _begin_failed_adoption(
        tmp_path, monkeypatch, sql_error=physical
    )
    fresh_error = OperationalError("fresh terminal", {}, RuntimeError("before commit"))
    original_release = service.session_operation_authority.release
    released_epochs: list[int] = []
    attempts = 0

    def observed_release(context):
        released_epochs.append(context.fence.operation_epoch)
        return original_release(context)

    def failed_fresh_terminal(**kwargs):
        nonlocal attempts
        attempts += 1
        raise fresh_error

    monkeypatch.setattr(service.session_operation_authority, "release", observed_release)
    monkeypatch.setattr(authority, "settle_lost", failed_fresh_terminal)
    key = (record.session_id, record.operation_id)
    try:
        release.set()
        await asyncio.wait_for(task, timeout=10)
        assert attempts == 1
        assert released_epochs == [record.session_operation_epoch]
        current = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert current is not None and current.status == "running"
        assert key in worker._required_coordinators and key in worker._recovery_coordinators
        recovered = worker._recovery_coordinators[key]
        assert len(recovered) == 1
        projection = [ticket for ticket in recovered[0].tickets if ticket.key.source is RequiredWorkSource.TERMINAL_FAILURE_PROJECTION]
        assert len(projection) == 1 and not projection[0].complete
        assert projection[0].key == make_required_work_key(recovered[0].authority, RequiredWorkSource.TERMINAL_FAILURE_PROJECTION)
        writer = [ticket for ticket in recovered[0].tickets if ticket.key.source is RequiredWorkSource.TERMINAL_FAILURE_SQL]
        assert len(writer) == 1 and any(receipt.original_root is fresh_error for receipt in writer[0].receipts())
        assert composer.calls == 0
        for _ in range(100):
            if worker._process_recovery.watchdog.reasons:
                break
            await asyncio.sleep(0.01)
        assert RecoveryReason.REQUIRED_WORKER_LOST in worker._process_recovery.watchdog.reasons
    finally:
        await _finish(task, release, engine)


@pytest.mark.asyncio
async def test_failed_physical_readback_cannot_complete_fresh_projection_or_release(tmp_path, monkeypatch) -> None:
    physical = OperationalError("adoption CAS", {}, RuntimeError("private SQL failure"))
    _app, service, engine, composer, authority, worker, record, task, release = await _begin_failed_adoption(
        tmp_path, monkeypatch, sql_error=physical
    )
    readback_error = OperationalError("fresh readback", {}, RuntimeError("physical read failed"))
    original_get = authority.get
    original_release = service.session_operation_authority.release
    released_epochs: list[int] = []

    def failed_readback(*, session_id, operation_id):
        raise readback_error

    def observed_release(context):
        released_epochs.append(context.fence.operation_epoch)
        return original_release(context)

    monkeypatch.setattr(authority, "get", failed_readback)
    monkeypatch.setattr(service.session_operation_authority, "release", observed_release)
    key = (record.session_id, record.operation_id)
    owners = tuple(worker._jobs.values())
    assert len(owners) == 1
    try:
        release.set()
        await asyncio.wait_for(task, timeout=10)
        actual = original_get(session_id=record.session_id, operation_id=record.operation_id)
        assert actual is not None and actual.status == "failed"
        assert released_epochs == [record.session_operation_epoch]
        recovered = worker._recovery_coordinators[key]
        assert len(recovered) == 1
        projection = [ticket for ticket in recovered[0].tickets if ticket.key.source is RequiredWorkSource.TERMINAL_FAILURE_PROJECTION]
        assert len(projection) == 1 and not projection[0].complete
        readback = [ticket for ticket in recovered[0].tickets if ticket.key.source is RequiredWorkSource.TERMINAL_WRITER_READ_SQL]
        assert len(readback) == 1
        assert any(receipt.original_root is readback_error for receipt in readback[0].receipts())
        retained = owners[0].exception()
        assert retained is not None
        leaves = _leaves(retained)
        assert any(isinstance(item, ComposerTerminalSQLCompletionUnknown) and item.__cause__ is readback_error for item in leaves)
        assert any(isinstance(item, RequiredWorkIncomplete) for item in leaves)
        for _ in range(100):
            if worker._process_recovery.watchdog.reasons:
                break
            await asyncio.sleep(0.01)
        assert RecoveryReason.REQUIRED_WORKER_LOST in worker._process_recovery.watchdog.reasons
        assert composer.calls == 0
    finally:
        await _finish(task, release, engine)


@pytest.mark.asyncio
async def test_foreign_real_database_readback_refuses_fresh_projection(tmp_path, monkeypatch) -> None:
    physical = OperationalError("adoption CAS", {}, RuntimeError("private SQL failure"))
    app, service, engine, composer, authority, worker, record, task, release = await _begin_failed_adoption(
        tmp_path, monkeypatch, sql_error=physical
    )
    other = await _admit(app, service, authority, operation_id=str(uuid4()))
    other_terminal = authority.request_cancel(
        session_id=other.session_id,
        operation_id=other.operation_id,
        cancelled_failure=request_cancelled_error,
    )
    assert other_terminal is not None and other_terminal.status == "failed"
    original_get = authority.get
    original_release = service.session_operation_authority.release
    original_settle = authority.settle_lost
    released_epochs: list[int] = []
    writes = 0

    def foreign_physical_readback(*, session_id, operation_id):
        if session_id == record.session_id and operation_id == record.operation_id:
            # This is a real independently committed DB row. The injected fault
            # is routing the wrong physical read to this caller, not fabricating
            # a ComposerOperationRecord or marking a ticket complete.
            return original_get(session_id=other.session_id, operation_id=other.operation_id)
        return original_get(session_id=session_id, operation_id=operation_id)

    def observed_release(context):
        released_epochs.append(context.fence.operation_epoch)
        return original_release(context)

    def observed_settle(**kwargs):
        nonlocal writes
        writes += 1
        return original_settle(**kwargs)

    monkeypatch.setattr(authority, "get", foreign_physical_readback)
    monkeypatch.setattr(service.session_operation_authority, "release", observed_release)
    monkeypatch.setattr(authority, "settle_lost", observed_settle)
    key = (record.session_id, record.operation_id)
    try:
        release.set()
        await asyncio.wait_for(task, timeout=10)
        assert writes == 1 and released_epochs == [record.session_operation_epoch]
        own_terminal = original_get(session_id=record.session_id, operation_id=record.operation_id)
        assert own_terminal is not None and own_terminal.status == "failed"
        assert own_terminal.session_id != other_terminal.session_id
        recovered = worker._recovery_coordinators[key]
        assert len(recovered) == 1
        projection = [ticket for ticket in recovered[0].tickets if ticket.key.source is RequiredWorkSource.TERMINAL_FAILURE_PROJECTION]
        assert len(projection) == 1 and not projection[0].complete
        for _ in range(100):
            if worker._process_recovery.watchdog.reasons:
                break
            await asyncio.sleep(0.01)
        assert RecoveryReason.REQUIRED_WORKER_LOST in worker._process_recovery.watchdog.reasons
        assert composer.calls == 0
    finally:
        await _finish(task, release, engine)


@pytest.mark.asyncio
async def test_same_job_returned_value_disagreement_refuses_projection_with_bounded_transport_fault(tmp_path, monkeypatch) -> None:
    """Corrupt only the returned DTO after the actual terminal SQL committed."""
    physical = OperationalError("adoption CAS", {}, RuntimeError("private SQL failure"))
    _app, service, engine, composer, authority, worker, record, task, release = await _begin_failed_adoption(
        tmp_path, monkeypatch, sql_error=physical
    )
    original_settle = authority.settle_lost
    original_release = service.session_operation_authority.release
    committed = []
    altered_return = []
    released_epochs: list[int] = []

    def commit_then_corrupt_return(**kwargs):
        actual = original_settle(**kwargs)
        committed.append(actual)
        # Explicit bounded transport fault: the real SQL ran once and its
        # immutable DB row is untouched. Only this returned nominal DTO has
        # one altered value, so the real independent readback must disagree.
        altered = replace(actual, updated_at=actual.updated_at + timedelta(microseconds=1))
        altered_return.append(altered)
        return altered

    def observed_release(context):
        released_epochs.append(context.fence.operation_epoch)
        return original_release(context)

    monkeypatch.setattr(authority, "settle_lost", commit_then_corrupt_return)
    monkeypatch.setattr(service.session_operation_authority, "release", observed_release)
    key = (record.session_id, record.operation_id)
    owners = tuple(worker._jobs.values())
    assert len(owners) == 1
    try:
        release.set()
        await asyncio.wait_for(task, timeout=10)
        assert len(committed) == len(altered_return) == 1
        actual = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert actual == committed[0] and actual != altered_return[0]
        assert actual is not None and actual.status == "failed"
        assert altered_return[0].session_id == actual.session_id and altered_return[0].operation_id == actual.operation_id
        assert released_epochs == [record.session_operation_epoch]
        escaped = owners[0].exception()
        assert escaped is not None and any(isinstance(item, AuditIntegrityError) for item in _explicit_originals(escaped))
        recovered = worker._recovery_coordinators[key]
        assert len(recovered) == 1
        projection = [ticket for ticket in recovered[0].tickets if ticket.key.source is RequiredWorkSource.TERMINAL_FAILURE_PROJECTION]
        assert len(projection) == 1 and not projection[0].complete
        assert key in worker._adoption_recovery_pending
        for _ in range(100):
            if worker._process_recovery.watchdog.reasons:
                break
            await asyncio.sleep(0.01)
        assert RecoveryReason.REQUIRED_WORKER_LOST in worker._process_recovery.watchdog.reasons
        assert composer.calls == 0
    finally:
        await _finish(task, release, engine)


@pytest.mark.asyncio
async def test_grouped_fence_plus_unrelated_failure_refuses_fresh_writer(tmp_path, monkeypatch) -> None:
    physical = OperationalError("adoption CAS", {}, RuntimeError("private SQL failure"))
    _app, service, engine, composer, authority, worker, record, task, release = await _begin_failed_adoption(
        tmp_path, monkeypatch, sql_error=physical
    )
    original_service = service.fail_composer_async_operation
    unrelated = ValueError("unrelated joined failure")
    actual_fences: list[SessionOperationFenceLost] = []
    classified = []
    original_classifier = worker._grouped_prewrite_fence_refusal

    async def add_unrelated_after_real_fence(*args, **kwargs):
        try:
            return await original_service(*args, **kwargs)
        except SessionOperationFenceLost as fence:
            actual_fences.append(fence)
            raise BaseExceptionGroup("real fence plus unrelated failure", [fence, unrelated]) from None

    def observe_classifier(grouped):
        classified.append((grouped, original_classifier(grouped)))
        return classified[-1][1]

    monkeypatch.setattr(service, "fail_composer_async_operation", add_unrelated_after_real_fence)
    monkeypatch.setattr(worker, "_grouped_prewrite_fence_refusal", observe_classifier)
    key = (record.session_id, record.operation_id)
    try:
        release.set()
        await asyncio.wait_for(task, timeout=10)
        assert len(actual_fences) == 1 and len(classified) == 1
        assert classified[0][1] is None
        assert any(item is actual_fences[0] for item in _leaves(classified[0][0]))
        assert any(item is unrelated for item in _leaves(classified[0][0]))
        assert key in worker._required_coordinators and key not in worker._recovery_coordinators
        assert authority.get(session_id=record.session_id, operation_id=record.operation_id).status == "running"
        for _ in range(100):
            if worker._process_recovery.watchdog.reasons:
                break
            await asyncio.sleep(0.01)
        assert RecoveryReason.REQUIRED_WORKER_LOST in worker._process_recovery.watchdog.reasons
        assert composer.calls == 0
    finally:
        await _finish(task, release, engine)


@pytest.mark.asyncio
async def test_required_recovery_read_storage_failure_outranks_durable_stop(tmp_path, monkeypatch) -> None:
    physical = OSError("ordinary failed adoption")
    _app, _service, engine, composer, authority, worker, record, task, release = await _begin_failed_adoption(
        tmp_path, monkeypatch, sql_error=physical
    )
    read_error = OperationalError("recovery read", {}, RuntimeError("private storage failure"))
    original_read = authority.get_with_database_now
    original_settle = authority.settle_lost
    reads = 0
    writes: list[tuple[ComposerOperationError, bool]] = []

    def fail_one_read(*, session_id, operation_id):
        nonlocal reads
        reads += 1
        if reads == 2:
            raise read_error
        return original_read(session_id=session_id, operation_id=operation_id)

    def observe_settle(**kwargs):
        writes.append((kwargs["failure"], kwargs["authoritative_failure"]))
        return original_settle(**kwargs)

    monkeypatch.setattr(authority, "get_with_database_now", fail_one_read)
    monkeypatch.setattr(authority, "settle_lost", observe_settle)
    original_coordinator = _capture_original_coordinator(monkeypatch, worker)
    try:
        authority.request_cancel(session_id=record.session_id, operation_id=record.operation_id, cancelled_failure=request_cancelled_error)
        release.set()
        await asyncio.wait_for(task, timeout=10)
        assert reads >= 3 and len(writes) == 1
        assert writes[0][0].http_status == 503 and writes[0][1] is True
        reads_tickets = [
            ticket for ticket in original_coordinator[0].tickets if ticket.key.source is RequiredWorkSource.TERMINAL_WRITER_READ_SQL
        ]
        assert len(reads_tickets) >= 2
        assert {ticket.key for ticket in reads_tickets} >= {
            make_required_work_key(original_coordinator[0].authority, RequiredWorkSource.TERMINAL_WRITER_READ_SQL, recurrence_ordinal=2),
            make_required_work_key(original_coordinator[0].authority, RequiredWorkSource.TERMINAL_WRITER_READ_SQL, recurrence_ordinal=3),
        }
        assert any(receipt.original_root is read_error for ticket in reads_tickets for receipt in ticket.receipts())
        assert _operation_error(authority, record).http_status == 503
        assert composer.calls == 0
    finally:
        await _finish(task, release, engine)


@pytest.mark.asyncio
async def test_repeated_close_waiter_cancellation_joins_physical_release_error(tmp_path, monkeypatch) -> None:
    physical = OperationalError("adoption CAS", {}, RuntimeError("private SQL failure"))
    _app, service, engine, composer, authority, worker, record, task, release = await _begin_failed_adoption(
        tmp_path, monkeypatch, sql_error=physical
    )
    original_release = service.session_operation_authority.release
    fresh_entered = threading.Event()
    fresh_release = threading.Event()
    close_error = OperationalError("fresh release", {}, RuntimeError("physical close failed"))
    released_epochs: list[int] = []

    def failed_fresh_release(context):
        released_epochs.append(context.fence.operation_epoch)
        if context.fence.operation_epoch == record.session_operation_epoch:
            return original_release(context)
        fresh_entered.set()
        assert fresh_release.wait(timeout=10)
        raise close_error

    monkeypatch.setattr(service.session_operation_authority, "release", failed_fresh_release)
    key = (record.session_id, record.operation_id)
    owners = tuple(worker._jobs.values())
    assert len(owners) == 1
    delivered = _observe_owner_shield_deliveries(monkeypatch, owners[0], fresh_entered, fresh_release, "SessionOperationLease.close")
    try:
        release.set()
        await _wait_for(fresh_entered)
        assert key in worker._recovery_coordinators
        recovered = worker._recovery_coordinators[key][0]
        owners[0].cancel("first fresh close waiter")
        for _ in range(100):
            if len(delivered) >= 1:
                break
            await asyncio.sleep(0.01)
        assert len(delivered) == 1
        first = delivered[0]
        owners[0].cancel("second fresh close waiter")
        for _ in range(100):
            if len(delivered) >= 2:
                break
            await asyncio.sleep(0.01)
        retained = worker._adoption_recovery_cancellations[key]
        assert len(delivered) == 2 and delivered[1] is not first
        assert not any(item is first or item is delivered[1] for item in retained)
        second = delivered[1]
        fresh_release.set()
        await asyncio.wait_for(task, timeout=10)
        assert any(item is first for item in retained)
        assert any(item is second for item in retained)
        escaped = owners[0].exception()
        assert escaped is not None
        originals = _explicit_originals(escaped)
        assert any(item is close_error for item in originals)
        assert any(item is first for item in originals)
        assert any(item is second for item in originals)
        assert key in worker._adoption_recovery_pending
        assert worker._adoption_recovery_cancellations[key] is retained
        assert key in worker._required_coordinators
        assert worker._recovery_coordinators[key][0] is recovered
        release_ticket = [ticket for ticket in recovered.tickets if ticket.key.source is RequiredWorkSource.LEASE_RELEASE]
        assert len(release_ticket) == 1
        assert any(receipt.original_root is close_error for receipt in release_ticket[0].receipts())
        assert released_epochs == [record.session_operation_epoch, record.session_operation_epoch + 1]
        assert _operation_error(authority, record).http_status == 503
        assert composer.calls == 0 and not worker._jobs
        assert all(not item.get_name().startswith("session-operation-close") for item in asyncio.all_tasks())
        for _ in range(100):
            if worker._process_recovery.watchdog.reasons:
                break
            await asyncio.sleep(0.01)
        assert RecoveryReason.REQUIRED_WORKER_LOST in worker._process_recovery.watchdog.reasons
    finally:
        fresh_release.set()
        await _finish(task, release, engine)


@pytest.mark.asyncio
async def test_successful_fresh_close_hands_off_late_waiter_originals_before_cleanup(tmp_path, monkeypatch) -> None:
    physical = OperationalError("adoption CAS", {}, RuntimeError("private SQL failure"))
    _app, service, engine, composer, authority, worker, record, task, release = await _begin_failed_adoption(
        tmp_path, monkeypatch, sql_error=physical
    )
    original_release = service.session_operation_authority.release
    fresh_entered = threading.Event()
    fresh_release = threading.Event()
    key = (record.session_id, record.operation_id)
    observations = worker._adoption_recovery_cancellations[key]

    def hold_fresh_release(context):
        if context.fence.operation_epoch > record.session_operation_epoch:
            fresh_entered.set()
            assert fresh_release.wait(timeout=10)
        return original_release(context)

    monkeypatch.setattr(service.session_operation_authority, "release", hold_fresh_release)
    owners = tuple(worker._jobs.values())
    assert len(owners) == 1
    delivered = _observe_owner_shield_deliveries(monkeypatch, owners[0], fresh_entered, fresh_release, "SessionOperationLease.close")
    try:
        release.set()
        await _wait_for(fresh_entered)
        original = worker._required_coordinators[key]
        recovered = worker._recovery_coordinators[key][0]
        assert original.all_completed and all(
            ticket.complete for ticket in recovered.tickets if ticket.key.source is RequiredWorkSource.TERMINAL_FAILURE_PROJECTION
        )
        owners[0].cancel("late close waiter one")
        for _ in range(100):
            if delivered:
                break
            await asyncio.sleep(0.01)
        assert len(delivered) == 1
        first = delivered[0]
        owners[0].cancel("late close waiter two")
        for _ in range(100):
            if len(delivered) >= 2:
                break
            await asyncio.sleep(0.01)
        assert len(delivered) == 2 and delivered[1] is not first
        assert not observations
        fresh_release.set()
        await asyncio.wait_for(task, timeout=10)
        handoff = _assert_handoff(owners[0], authority, record, observations)
        assert any(item is first for item in handoff.cancellations)
        assert any(item is delivered[1] for item in handoff.cancellations)
        assert _operation_error(authority, record).http_status == 503
        assert key not in worker._adoption_recovery_cancellations
        assert composer.calls == 0
    finally:
        fresh_release.set()
        await _finish(task, release, engine)


@pytest.mark.asyncio
@pytest.mark.parametrize("caller_cancel", (False, True))
async def test_failed_adoption_release_retains_physical_child_cancel_separately_from_waiter(
    tmp_path, monkeypatch, caller_cancel: bool
) -> None:
    physical_sql = OSError(errno.EIO, "physical adoption storage failure")
    _app, service, engine, composer, authority, worker, record, task, release = await _begin_failed_adoption(
        tmp_path, monkeypatch, sql_error=physical_sql
    )
    original_release = service.session_operation_authority.release
    physical_cancel = asyncio.CancelledError("physical failed-adoption release")
    old_release_entered = threading.Event()
    finish_release = threading.Event()
    if not caller_cancel:
        finish_release.set()
    actual_release_calls = []
    key = (record.session_id, record.operation_id)
    observations = worker._adoption_recovery_cancellations[key]
    original_retain = lifecycle._retain_cancellation
    actual_retains: list[asyncio.CancelledError] = []

    def observed_retain(target, cancellation):
        if target is observations:
            actual_retains.append(cancellation)
        return original_retain(target, cancellation)

    def release_then_physical_cancel(context):
        actual_release_calls.append(context)
        outcome = original_release(context)
        if context.fence.operation_epoch == record.session_operation_epoch:
            old_release_entered.set()
            assert finish_release.wait(timeout=10)
            raise physical_cancel
        return outcome

    monkeypatch.setattr(lifecycle, "_retain_cancellation", observed_retain)
    monkeypatch.setattr(service.session_operation_authority, "release", release_then_physical_cancel)
    owners = tuple(worker._jobs.values())
    assert len(owners) == 1
    try:
        release.set()
        await _wait_for(old_release_entered)
        if caller_cancel:
            owners[0].cancel("caller waiting for physical failed-adoption release")
            for _ in range(100):
                if actual_retains:
                    break
                await asyncio.sleep(0.01)
            assert len(actual_retains) >= 1
            assert actual_retains[0] is not physical_cancel
        finish_release.set()
        await asyncio.wait_for(task, timeout=10)
        assert any(item is physical_cancel for item in actual_retains)
        assert any(item is physical_cancel for item in observations)
        if caller_cancel:
            assert any(item is not physical_cancel for item in observations)
        else:
            assert len(observations) == 1 and observations[0] is physical_cancel
        handoff = _assert_handoff(owners[0], authority, record, observations)
        assert any(item is physical_cancel for item in handoff.cancellations)
        assert len([context for context in actual_release_calls if context.fence.operation_epoch == record.session_operation_epoch]) == 1
        assert _operation_error(authority, record).http_status == 503
        assert composer.calls == 0 and key not in worker._adoption_recovery_cancellations
    finally:
        finish_release.set()
        await _finish(task, release, engine)


@pytest.mark.asyncio
async def test_peer_terminal_before_recovery_read_prevents_second_writer(tmp_path, monkeypatch) -> None:
    physical = OSError("ordinary failed adoption")
    _app, service, engine, composer, authority, worker, record, task, release = await _begin_failed_adoption(
        tmp_path, monkeypatch, sql_error=physical
    )
    original_read = authority.get_with_database_now
    original_settle = authority.settle_lost
    peer_terminal = []
    writes = 0
    reads = 0

    def count_writer(**kwargs):
        nonlocal writes
        writes += 1
        return original_settle(**kwargs)

    def peer_before_recovery_read(*, session_id, operation_id):
        nonlocal reads
        reads += 1
        if reads == 2:
            # The previous admission lease was physically released by adoption
            # cleanup. The peer now acquires its own newer COMPOSE fence and
            # publishes with the actual authority transaction.
            peer = service.session_operation_authority.acquire(
                session_id=session_id,
                operation_kind=SessionOperationKind.COMPOSE,
                owner_instance_id=service.session_operation_owner_instance_id,
                lease_seconds=service.session_operation_lease_seconds,
            )
            try:
                terminal = original_settle(
                    session_operation_context=peer,
                    session_id=session_id,
                    operation_id=operation_id,
                    failure=request_cancelled_error(request_id=record.request_id),
                    authoritative_failure=False,
                )
                peer_terminal.append(terminal)
            finally:
                service.session_operation_authority.release(peer)
        return original_read(session_id=session_id, operation_id=operation_id)

    monkeypatch.setattr(authority, "get_with_database_now", peer_before_recovery_read)
    monkeypatch.setattr(authority, "settle_lost", count_writer)
    key = (record.session_id, record.operation_id)
    try:
        stopped = authority.request_cancel(
            session_id=record.session_id, operation_id=record.operation_id, cancelled_failure=request_cancelled_error
        )
        assert stopped is not None and stopped.cancel_requested_at is not None
        release.set()
        await asyncio.wait_for(task, timeout=10)
        assert reads >= 2 and len(peer_terminal) == 1 and writes == 0
        terminal = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert terminal == peer_terminal[0] and terminal.status == "failed"
        assert _operation_error(authority, record).http_status == 499
        assert composer.calls == 0 and key not in worker._recovery_coordinators
    finally:
        await _finish(task, release, engine)


@pytest.mark.asyncio
async def test_already_terminal_initial_read_hands_off_seed_and_read_waiter(tmp_path, monkeypatch) -> None:
    physical = OperationalError("adoption CAS", {}, RuntimeError("private SQL failure"))
    _app, service, engine, composer, authority, worker, record, task, release = await _begin_failed_adoption(
        tmp_path, monkeypatch, sql_error=physical
    )
    original_read = authority.get_with_database_now
    original_settle = authority.settle_lost
    read_entered = threading.Event()
    read_release = threading.Event()
    peer_terminal = []
    reads = 0
    key = (record.session_id, record.operation_id)
    observations = worker._adoption_recovery_cancellations[key]

    def peer_then_held_initial_read(*, session_id, operation_id):
        nonlocal reads
        reads += 1
        if reads == 1:
            peer = service.session_operation_authority.acquire(
                session_id=session_id,
                operation_kind=SessionOperationKind.COMPOSE,
                owner_instance_id=service.session_operation_owner_instance_id,
                lease_seconds=service.session_operation_lease_seconds,
            )
            try:
                peer_terminal.append(
                    original_settle(
                        session_operation_context=peer,
                        session_id=session_id,
                        operation_id=operation_id,
                        failure=request_cancelled_error(request_id=record.request_id),
                        authoritative_failure=False,
                    )
                )
            finally:
                service.session_operation_authority.release(peer)
            read_entered.set()
            assert read_release.wait(timeout=10)
        return original_read(session_id=session_id, operation_id=operation_id)

    monkeypatch.setattr(authority, "get_with_database_now", peer_then_held_initial_read)
    owners = tuple(worker._jobs.values())
    assert len(owners) == 1
    try:
        stopped = authority.request_cancel(
            session_id=record.session_id, operation_id=record.operation_id, cancelled_failure=request_cancelled_error
        )
        assert stopped is not None and stopped.cancel_requested_at is not None
        owners[0].cancel("seeded adoption owner")
        for _ in range(100):
            if observations:
                break
            await asyncio.sleep(0.01)
        assert len(observations) >= 1
        seed = observations[0]
        delivered = _observe_owner_shield_deliveries(monkeypatch, owners[0], read_entered, read_release, "run_sync_in_worker")
        release.set()
        await _wait_for(read_entered)
        owners[0].cancel("initial committed read waiter")
        for _ in range(100):
            if delivered:
                break
            await asyncio.sleep(0.01)
        assert len(delivered) == 1 and delivered[0] is not seed
        assert len(observations) == 1 and observations[0] is seed
        read_release.set()
        await asyncio.wait_for(task, timeout=10)
        handoff = _assert_handoff(owners[0], authority, record, observations)
        assert any(item is delivered[0] for item in handoff.cancellations)
        assert len(peer_terminal) == 1 and handoff.terminal == peer_terminal[0]
        assert key not in worker._adoption_recovery_cancellations
        assert key not in worker._recovery_coordinators
        assert _operation_error(authority, record).http_status == 499
        assert composer.calls == 0
    finally:
        read_release.set()
        await _finish(task, release, engine)


@pytest.mark.asyncio
async def test_durable_stop_selected_by_real_fresh_terminal_and_readback(tmp_path, monkeypatch) -> None:
    physical = OSError("ordinary failed adoption")
    _app, _service, engine, composer, authority, _worker_owner, record, task, release = await _begin_failed_adoption(
        tmp_path, monkeypatch, sql_error=physical
    )
    original_settle = authority.settle_lost
    writes = []

    def observe_settle(**kwargs):
        result = original_settle(**kwargs)
        writes.append((kwargs, result))
        return result

    monkeypatch.setattr(authority, "settle_lost", observe_settle)
    try:
        stopped = authority.request_cancel(
            session_id=record.session_id, operation_id=record.operation_id, cancelled_failure=request_cancelled_error
        )
        assert stopped is not None and stopped.cancel_requested_at is not None
        release.set()
        await asyncio.wait_for(task, timeout=10)
        assert len(writes) == 1
        assert writes[0][0]["authoritative_failure"] is False
        terminal = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert terminal == writes[0][1] and terminal.status == "failed"
        assert _operation_error(authority, record).http_status == 499
        assert composer.calls == 0
    finally:
        await _finish(task, release, engine)


@pytest.mark.asyncio
async def test_database_deadline_selected_by_real_fresh_terminal_and_readback(tmp_path, monkeypatch) -> None:
    physical = OSError("ordinary failed adoption")
    _app, _service, engine, composer, authority, _worker_owner, record, task, release = await _begin_failed_adoption(
        tmp_path, monkeypatch, sql_error=physical, deadline_seconds=2.0
    )
    original_settle = authority.settle_lost
    writes = []

    def observe_settle(**kwargs):
        result = original_settle(**kwargs)
        writes.append((kwargs, result))
        return result

    monkeypatch.setattr(authority, "settle_lost", observe_settle)
    try:
        # Hold the actual CAS Future beyond the database deadline; no test
        # mutates the job row or substitutes a clock result.
        await asyncio.sleep(2.1)
        release.set()
        await asyncio.wait_for(task, timeout=10)
        assert len(writes) == 1 and writes[0][0]["authoritative_failure"] is False
        terminal = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert terminal == writes[0][1] and terminal.status == "failed"
        assert _operation_error(authority, record).http_status == 504
        assert composer.calls == 0
    finally:
        await _finish(task, release, engine)


@pytest.mark.asyncio
async def test_real_newer_peer_fence_refuses_fresh_writer_and_keeps_projection(tmp_path, monkeypatch) -> None:
    physical = OSError("ordinary failed adoption")
    _app, service, engine, composer, authority, worker, record, task, release = await _begin_failed_adoption(
        tmp_path, monkeypatch, sql_error=physical
    )
    original_read = authority.get_with_database_now
    original_settle = authority.settle_lost
    peer_contexts = []
    reads = 0
    writes = 0

    def acquire_peer_before_recovery_read(*, session_id, operation_id):
        nonlocal reads
        reads += 1
        if reads == 2:
            peer_contexts.append(
                service.session_operation_authority.acquire(
                    session_id=session_id,
                    operation_kind=SessionOperationKind.COMPOSE,
                    owner_instance_id=service.session_operation_owner_instance_id,
                    lease_seconds=service.session_operation_lease_seconds,
                )
            )
        return original_read(session_id=session_id, operation_id=operation_id)

    def observed_settle(**kwargs):
        nonlocal writes
        writes += 1
        return original_settle(**kwargs)

    monkeypatch.setattr(authority, "get_with_database_now", acquire_peer_before_recovery_read)
    monkeypatch.setattr(authority, "settle_lost", observed_settle)
    key = (record.session_id, record.operation_id)
    try:
        release.set()
        await asyncio.wait_for(task, timeout=10)
        assert reads >= 2 and len(peer_contexts) == 1
        assert peer_contexts[0].fence.operation_epoch > record.session_operation_epoch
        assert writes == 0 and key not in worker._recovery_coordinators
        current = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert current is not None and current.status == "running"
        assert key in worker._required_coordinators
        projection = [
            ticket
            for ticket in worker._required_coordinators[key].tickets
            if ticket.key.source is RequiredWorkSource.TERMINAL_FAILURE_PROJECTION
        ]
        assert len(projection) == 1 and not projection[0].complete
        for _ in range(100):
            if worker._process_recovery.watchdog.reasons:
                break
            await asyncio.sleep(0.01)
        assert RecoveryReason.REQUIRED_WORKER_LOST in worker._process_recovery.watchdog.reasons
        assert composer.calls == 0
    finally:
        release.set()
        for context in peer_contexts:
            service.session_operation_authority.release(context)
        await _finish(task, release, engine)


@pytest.mark.asyncio
async def test_adoption_task_allocated_then_factory_raises_keeps_unknown_custody(tmp_path, monkeypatch) -> None:
    app, service, engine, composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    worker = _worker(app, authority)
    record = await _admit(app, service, authority, operation_id=str(uuid4()))
    entered = threading.Event()
    release = threading.Event()
    original_cas = service.session_operation_authority.compare_and_swap
    original_release = service.session_operation_authority.release
    original_settle = authority.settle_lost
    actual_create_task = asyncio.create_task
    allocation_error = RuntimeError("adoption task factory allocated then raised")
    allocated: list[asyncio.Task] = []
    release_calls = 0
    fresh_writes = 0
    driver = None

    def held_real_cas(context):
        entered.set()
        assert release.wait(timeout=10)
        return original_cas(context)

    def allocate_then_raise(coroutine, *args, **kwargs):
        created = actual_create_task(coroutine, *args, **kwargs)
        if kwargs.get("name") == "session-operation-adopt-compare-and-swap":
            allocated.append(created)
            raise allocation_error
        return created

    def observe_release(context):
        nonlocal release_calls
        release_calls += 1
        return original_release(context)

    def observe_fresh_writer(**kwargs):
        nonlocal fresh_writes
        fresh_writes += 1
        return original_settle(**kwargs)

    monkeypatch.setattr(service.session_operation_authority, "compare_and_swap", held_real_cas)
    monkeypatch.setattr(service.session_operation_authority, "release", observe_release)
    monkeypatch.setattr(authority, "settle_lost", observe_fresh_writer)
    monkeypatch.setattr(lifecycle.asyncio, "create_task", allocate_then_raise)
    key = (record.session_id, record.operation_id)
    try:
        driver = actual_create_task(worker.run_until_idle())
        await _wait_for(entered)
        assert len(allocated) == 1 and not allocated[0].done()
        assert key in worker._required_coordinators
        original = worker._required_coordinators[key]
        adoption = [ticket for ticket in original.tickets if ticket.key.source is RequiredWorkSource.LEASE_ADOPTION]
        assert len(adoption) == 1 and not adoption[0].complete
        assert adoption[0].key == make_required_work_key(original.authority, RequiredWorkSource.LEASE_ADOPTION)
        assert release_calls == 0 and fresh_writes == 0
        current = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert current is not None and current.status == "running"
        release.set()
        await asyncio.wait_for(asyncio.gather(*allocated, return_exceptions=True), timeout=10)
        assert allocated[0].done()
        await asyncio.wait_for(driver, timeout=10)
        assert key in worker._required_coordinators and key in worker._adoption_recovery_pending
        projection = [ticket for ticket in original.tickets if ticket.key.source is RequiredWorkSource.TERMINAL_FAILURE_PROJECTION]
        assert len(projection) == 1 and not projection[0].complete
        assert key not in worker._recovery_coordinators
        assert release_calls == 0 and fresh_writes == 0
        current = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert current is not None and current.status == "running"
        for _ in range(100):
            if worker._process_recovery.watchdog.reasons:
                break
            await asyncio.sleep(0.01)
        assert RecoveryReason.REQUIRED_WORKER_LOST in worker._process_recovery.watchdog.reasons
        assert composer.calls == 0
    finally:
        release.set()
        if allocated:
            await asyncio.wait_for(asyncio.gather(*allocated, return_exceptions=True), timeout=10)
            assert all(item.done() for item in allocated)
        if driver is not None and not driver.done():
            await asyncio.wait_for(asyncio.gather(driver, return_exceptions=True), timeout=10)
        engine.dispose()
