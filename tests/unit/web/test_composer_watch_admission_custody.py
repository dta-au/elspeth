"""Draft actual-pool observation controls; no providers or serving sockets."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web import async_workers
from elspeth.web.composer_watch_reads import ComposerOperationWatchReader
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkCoordinator,
    RequiredWorkSource,
    required_failure_leaves,
)
from elspeth.web.sessions.composer_async_worker import _finish_operation_watcher, _OperationWatchObservation
from elspeth.web.sessions.composer_operations import ComposerOperationError, ComposerOperationRunning, ComposerTurnDeadlineExpired
from elspeth.web.sessions.composer_turn import ComposerBudgetAnchor
from tests.unit.web.sessions.test_composer_async_worker import _admit, _file_app, _worker


async def _reader(tmp_path, *, budget: float = 30.0):
    app, service, engine, _composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    record = await _admit(app, service, authority, operation_id=str(uuid4()))
    claim = authority.claim_next(limit=1)[0]
    context = service.session_operation_authority.start_composer_async_operation(
        claim,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
        auth_provider_type=app.state.settings.auth_provider,
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.DURABLE_COMPOSE, context, claim.operation_id, claim.attempt)
    )
    lease = await SessionOperationLease.adopt(service.session_operation_authority, context, lease_seconds=30, required_work=coordinator)
    worker = _worker(app, authority)
    running = ComposerOperationRunning(claim, context)
    reader = ComposerOperationWatchReader.for_operation(worker, running, lease, ComposerBudgetAnchor(budget, time.monotonic()))
    return reader, lease, engine, record


async def _fill_pool():
    held = threading.Event()
    count_lock = threading.Lock()
    entered = 0

    def work():
        nonlocal entered
        with count_lock:
            entered += 1
        held.wait()

    tasks = [asyncio.create_task(async_workers.run_sync_in_worker(work)) for _ in range(async_workers.ADMISSION_CAPACITY)]
    while async_workers.outstanding_admissions() != async_workers.ADMISSION_CAPACITY or entered != async_workers.MAX_WORKERS:
        await asyncio.sleep(0.001)
    return held, tasks


@pytest.mark.asyncio
async def test_real_full_pool_refusal_is_issued_without_submission_then_recovers(tmp_path, monkeypatch):
    reader, lease, engine, record = await _reader(tmp_path)
    held, tasks = await _fill_pool()
    receipts = []
    production_issue = ComposerOperationWatchReader.issue_refusal
    issued = asyncio.Event()

    def capture(self, *args):
        production_issue(self, *args)
        receipts.append(self._issued)
        issued.set()

    monkeypatch.setattr(ComposerOperationWatchReader, "issue_refusal", capture)
    read = asyncio.create_task(reader.read())
    try:
        await issued.wait()
        receipt = receipts[0]
        assert receipt is not None and receipt.owner is reader
        assert receipt.generation is reader.generation and receipt.executor is reader.executor
        assert not receipt.reservation.held and receipt.reservation.future is None
        assert receipt.reservation._registering_generation is None
        assert not receipt.reservation.witness.snapshot().entered
        assert async_workers.outstanding_admissions() == 32
        held.set()
        await asyncio.gather(*tasks)
        current, _now = await read
        assert current is not None and current.operation_id == record.operation_id
        assert reader._refusals[0] is receipt.original
        assert async_workers.outstanding_admissions() == 0
        with pytest.raises(AuditIntegrityError):
            reader.consume_refusal(receipt.original)
    finally:
        held.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        if not read.done():
            read.cancel()
            await asyncio.gather(read, return_exceptions=True)
        await lease.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_slot_after_original_cutoff_never_submits_observation(tmp_path):
    reader, lease, engine, _record = await _reader(tmp_path, budget=0.15)
    held, tasks = await _fill_pool()
    read = asyncio.create_task(reader.read())
    try:
        await asyncio.sleep(0.2)
        held.set()
        await asyncio.gather(*tasks)
        with pytest.raises(ComposerTurnDeadlineExpired):
            await read
        assert reader._issued is None and reader._registered_issuance is None
        assert async_workers.outstanding_admissions() == 0
    finally:
        held.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await lease.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_final_submission_check_refuses_cutoff_after_actual_slot_admission(tmp_path, monkeypatch):
    reader, lease, engine, _record = await _reader(tmp_path)
    calls = []
    production_try = async_workers._try_admit

    def authority_read(**kwargs):
        calls.append(kwargs)
        raise AssertionError("cutoff must prevent this authority invocation")

    def admit_then_expire():
        admitted = production_try()
        if admitted:
            reader.deadline = time.monotonic() - 1.0
        return admitted

    reader._invocation = authority_read
    monkeypatch.setattr(async_workers, "_try_admit", admit_then_expire)
    try:
        with pytest.raises(ComposerTurnDeadlineExpired):
            await reader.read()
        assert calls == []
        assert async_workers.outstanding_admissions() == 0
        assert reader._issued is None and reader._registered_issuance is None
    finally:
        monkeypatch.setattr(async_workers, "_try_admit", production_try)
        await lease.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_copied_refusal_cannot_replace_actual_issuance_slot(tmp_path, monkeypatch):
    reader, lease, engine, _record = await _reader(tmp_path)
    held, tasks = await _fill_pool()
    production_issue = ComposerOperationWatchReader.issue_refusal

    def copy_issued(self, *args):
        production_issue(self, *args)
        assert self._issued is not None
        self._issued = replace(self._issued)

    monkeypatch.setattr(ComposerOperationWatchReader, "issue_refusal", copy_issued)
    try:
        with pytest.raises(BaseExceptionGroup) as failure:
            await reader.read()
        assert any(isinstance(error, AuditIntegrityError) for error in failure.value.exceptions)
        assert async_workers.outstanding_admissions() == 32
    finally:
        held.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await lease.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_same_timeout_class_from_actual_sql_never_issues_retry(tmp_path):
    reader, lease, engine, _record = await _reader(tmp_path)
    original = async_workers.AsyncWorkerAdmissionTimeoutError("inside submitted authority")
    invocations = []

    def authority_read(**kwargs):
        invocations.append(kwargs)
        raise original

    reader._invocation = authority_read
    try:
        with pytest.raises(async_workers.AsyncWorkerAdmissionTimeoutError) as failure:
            await reader.read()
        assert failure.value is original
        assert len(invocations) == 1
        assert reader._issued is None and reader._registered_issuance is None
        assert reader._refusals == []
        assert async_workers.outstanding_admissions() == 0
    finally:
        await lease.close()
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("sql_fails", [False, True])
async def test_three_delivered_original_cancellations_join_actual_sql_once(tmp_path, monkeypatch, sql_fails):
    reader, lease, engine, record = await _reader(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    original_sql = RuntimeError("actual observer SQL failed")
    captured = []
    invocations = []
    production_retain = async_workers._retain_cancellation
    read = None

    def authority_read(**kwargs):
        invocations.append(kwargs)
        entered.set()
        release.wait()
        if sql_fails:
            raise original_sql
        return record, record.deadline_at

    def retain(cancellations, original):
        production_retain(cancellations, original)
        if asyncio.current_task() is read and all(original is not prior for prior in captured):
            captured.append(original)

    reader._invocation = authority_read
    monkeypatch.setattr(async_workers, "_retain_cancellation", retain)
    read = asyncio.create_task(reader.read())
    try:
        while not entered.is_set():
            await asyncio.sleep(0.001)
        for index in range(3):
            read.cancel(f"delivered original {index}")
            while len(captured) <= index:
                await asyncio.sleep(0.001)
        assert not read.done()
        assert async_workers.outstanding_admissions() == 1
        release.set()
        with pytest.raises(BaseExceptionGroup) as failure:
            await read
        expected = (*captured, original_sql) if sql_fails else tuple(captured)
        assert len(failure.value.exceptions) == len(expected)
        assert all(actual is original for actual, original in zip(failure.value.exceptions, expected, strict=True))
        assert [cancel.args for cancel in captured] == [(f"delivered original {index}",) for index in range(3)]
        assert len(invocations) == 1
        assert async_workers.outstanding_admissions() == 0
    finally:
        release.set()
        await asyncio.gather(read, return_exceptions=True)
        await lease.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_drain_after_initiated_read_joins_then_refuses_another_read(tmp_path):
    reader, lease, engine, record = await _reader(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def authority_read(**kwargs):
        calls.append(kwargs)
        entered.set()
        release.wait()
        return record, record.deadline_at

    reader._invocation = authority_read
    read = asyncio.create_task(reader.read())
    try:
        while not entered.is_set():
            await asyncio.sleep(0.001)
        reader.worker._instance_draining.set()
        assert not read.done() and async_workers.outstanding_admissions() == 1
        release.set()
        current, _now = await read
        assert current is record
        with pytest.raises(asyncio.CancelledError):
            await reader.read()
        assert len(calls) == 1
        assert async_workers.outstanding_admissions() == 0
    finally:
        release.set()
        await asyncio.gather(read, return_exceptions=True)
        await lease.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_local_stop_wakes_once_and_still_allows_actual_authority_read(tmp_path, monkeypatch):
    reader, lease, engine, record = await _reader(tmp_path)
    key = (reader.running.claim.session_id, reader.running.claim.operation_id)
    stop = asyncio.Event()
    stop.set()
    reader.worker._local_cancels[key] = stop
    sleeps = []
    production_sleep = asyncio.sleep

    async def capture_sleep(delay):
        sleeps.append(delay)
        await production_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", capture_sleep)
    try:
        await reader.wait_retry_cadence()
        await reader.wait_retry_cadence()
        assert sleeps == [0, 0.25]
        monkeypatch.setattr(asyncio, "sleep", production_sleep)
        current, _now = await reader.read()
        assert current is not None and current.operation_id == record.operation_id
        assert current.cancel_requested_at is None
    finally:
        monkeypatch.setattr(asyncio, "sleep", production_sleep)
        await lease.close()
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault_kind", ["success", "sql", "unclassified", "integrity", "sql_cancel_same_marker"])
@pytest.mark.parametrize("external_cancels", [False, True])
async def test_actual_reader_close_is_bookkeeping_not_r1_failure(tmp_path, monkeypatch, fault_kind, external_cancels):
    reader, lease, engine, record = await _reader(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    if fault_kind == "sql":
        original_fault = OperationalError("owned test read", {}, RuntimeError("offline SQL fault"))
    elif fault_kind == "unclassified":
        original_fault = RuntimeError("offline original read fault")
    elif fault_kind == "integrity":
        original_fault = AuditIntegrityError("offline original invariant fault")
    elif fault_kind == "sql_cancel_same_marker":
        original_fault = asyncio.CancelledError()
    else:
        original_fault = None
    captured = []
    production_retain = async_workers._retain_cancellation
    read = None

    def authority_read(**kwargs):
        entered.set()
        release.wait()
        if original_fault is not None:
            if fault_kind == "sql_cancel_same_marker":
                original_fault.args = (reader._internal_cleanup_marker,)
            raise original_fault
        return record, datetime.now(UTC)

    def retain(cancellations, original):
        production_retain(cancellations, original)
        # read() also re-retains the physical SQL CancelledError here. Capture
        # loop deliveries only; expected adds that exact SQL original once.
        if asyncio.current_task() is read and original is not original_fault and all(original is not prior for prior in captured):
            captured.append(original)

    reader._invocation = authority_read
    monkeypatch.setattr(async_workers, "_retain_cancellation", retain)
    read = asyncio.create_task(reader.read())
    close = None
    try:
        while not entered.is_set():
            await asyncio.sleep(0.001)
        close = asyncio.create_task(_finish_operation_watcher(read, _OperationWatchObservation(reader=reader)))
        while not captured:
            await asyncio.sleep(0.001)
        private_close = captured[0]
        assert private_close.args == (reader._internal_cleanup_marker,)
        if external_cancels:
            for index in range(3):
                read.cancel(f"external delivered original {index}")
                while len(captured) < index + 2:
                    await asyncio.sleep(0.001)
        assert not read.done() and not close.done()
        assert async_workers.outstanding_admissions() == 1
        release.set()
        errors = await close
        assert async_workers.outstanding_admissions() == 0
        assert len(reader._cleanup_cancellations) == 1
        assert reader._cleanup_cancellations[0] is private_close
        expected = tuple(captured[1:]) + ((original_fault,) if original_fault is not None else ())
        if not expected:
            assert errors == ()
        else:
            assert len(errors) == 1
            coordinator = lease.required_work
            assert coordinator is not None
            setup = coordinator.reserve(RequiredWorkSource.OWNED_TURN_SETUP_PRODUCER)
            setup.complete_owned(errors[0])
            reader.worker._required_coordinators[(record.session_id, record.operation_id)] = coordinator
            selected = reader.worker._failure_for(record, datetime.now(UTC), errors[0])
            if fault_kind == "sql":
                assert selected.http_status == 503 and selected.error_type == "database_unavailable"
            elif fault_kind == "integrity":
                assert selected.http_status == 500 and selected.error_type == "audit_integrity_error"
            elif external_cancels or fault_kind == "sql_cancel_same_marker":
                assert selected.failure_code == "worker_lost"
            else:
                # Restoring private-close promotion makes rank80 outrank the
                # real unclassified rank100 fault and changes this selection.
                assert selected.http_status == 500 and selected.failure_code == "operation_failed"
            leaves = required_failure_leaves(errors[0])
            assert len(leaves) == len(expected)
            assert all(actual is original for actual, original in zip(leaves, expected, strict=True))
            assert all(leaf is not private_close for leaf in leaves)
    finally:
        release.set()
        await asyncio.gather(read, return_exceptions=True)
        if close is not None:
            await asyncio.gather(close, return_exceptions=True)
        await lease.close()
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("external", [False, True])
async def test_actual_whole_watcher_cadence_close_preserves_owner_and_r1_selection(tmp_path, monkeypatch, external):
    initial_reader, lease, engine, record = await _reader(tmp_path)
    worker = initial_reader.worker
    cadence = asyncio.Event()
    owner_gate = asyncio.Event()
    production_cadence = ComposerOperationWatchReader.wait_watch_cadence

    async def observe_cadence(self):
        cadence.set()
        await production_cadence(self)

    monkeypatch.setattr(ComposerOperationWatchReader, "wait_watch_cadence", observe_cadence)
    owner = asyncio.create_task(owner_gate.wait())
    observation = _OperationWatchObservation()
    watcher = asyncio.create_task(worker._watch(initial_reader.running, lease, owner, initial_reader.anchor, observation))
    try:
        await cadence.wait()
        assert observation.reader is not None
        assert async_workers.outstanding_admissions() == 0
        if external:
            watcher.cancel("actual external cadence cancellation")
            while not watcher.done():
                await asyncio.sleep(0.001)
        errors = await _finish_operation_watcher(watcher, observation)
        if external:
            original = observation.failure
            assert isinstance(original, asyncio.CancelledError)
            assert original.args == ("actual external cadence cancellation",)
            assert any(original is leaf for error in errors for leaf in required_failure_leaves(error))
            assert owner.cancelling() == 1
            selected = worker._failure_for(record, datetime.now(UTC), original)
            assert selected.failure_code == "worker_lost"
        else:
            assert errors == ()
            assert observation.failure is None
            assert owner.cancelling() == 0 and not owner.done()
            assert observation.reader._cleanup_cancellations
            # A real body fault must keep its existing selection after an
            # ordinary watcher close; no private rank80 witness may join it.
            body_original = RuntimeError("offline actual body fault")
            coordinator = lease.required_work
            assert coordinator is not None
            setup = coordinator.reserve(RequiredWorkSource.OWNED_TURN_SETUP_PRODUCER)
            setup.complete_owned(body_original)
            worker._required_coordinators[(record.session_id, record.operation_id)] = coordinator
            selected = worker._failure_for(record, datetime.now(UTC), body_original)
            assert selected.http_status == 500 and selected.failure_code == "operation_failed"
    finally:
        if not watcher.done():
            await _finish_operation_watcher(watcher, observation)
        owner_gate.set()
        await asyncio.gather(owner, return_exceptions=True)
        await lease.close()
        engine.dispose()


async def _factory_installed_reader(tmp_path):
    from elspeth.web.sessions.composer_operations import composer_operation_request_hash
    from elspeth.web.sessions.schemas import SendMessageRequest
    from tests.helpers.composer_operations import build_composer_operation_app

    fixture = await build_composer_operation_app(tmp_path, timeout_seconds=30.0)
    app = fixture.app
    service = app.state.session_service
    authority = app.state.composer_async_operation_authority
    worker = app.state.composer_async_worker
    assert worker._app is app
    assert worker._instance_draining is app.state.instance_draining
    assert worker._process_recovery is app.state.process_recovery
    assert worker._process_recovery.instance_draining is async_workers._INSTANCE_DRAINING
    operation_id = str(uuid4())
    request = SendMessageRequest(operation_id=operation_id, content="Observe without provider", state_id=None)
    record, fresh = authority.admit(
        session_id=fixture.session_id,
        operation_id=operation_id,
        kind="compose_message",
        request_hash=composer_operation_request_hash(session_id=fixture.session_id, kind="compose_message", request=request),
        actor_user_id="transport-user",
        request_id="factory-observation",
        base_state_id=None,
        request_json=request.model_dump_json(),
        deadline_seconds=30.0,
        max_nonterminal=64,
        auth_provider_type=app.state.settings.auth_provider,
    )
    assert fresh
    claim = authority.claim_next(limit=1)[0]
    context = service.session_operation_authority.start_composer_async_operation(
        claim,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
        auth_provider_type=app.state.settings.auth_provider,
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.DURABLE_COMPOSE, context, claim.operation_id, claim.attempt)
    )
    lease = await SessionOperationLease.adopt(service.session_operation_authority, context, lease_seconds=30, required_work=coordinator)
    reader = ComposerOperationWatchReader.for_operation(
        worker, ComposerOperationRunning(claim, context), lease, ComposerBudgetAnchor(30.0, time.monotonic())
    )
    generation = async_workers._GENERATION_CUSTODIAN
    assert generation is not None
    assert generation.instance_draining is app.state.instance_draining
    assert generation.executor is async_workers._SHARED_EXECUTOR
    return reader, lease, app, record


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ("initiated", "waiting", "after_slot"))
async def test_actual_factory_process_latch_blocks_read_at_each_submission_boundary(tmp_path, monkeypatch, boundary):
    from elspeth.web.required_executor import RequiredGenerationUnavailable

    reader, lease, app, _record = await _factory_installed_reader(tmp_path)
    calls = []
    entered = threading.Event()
    release = threading.Event()
    held = None
    tasks = []
    production_read = reader._invocation
    production_try = async_workers._try_admit

    def actual_read(**kwargs):
        calls.append(kwargs)
        entered.set()
        if boundary == "initiated":
            release.wait()
        return production_read(**kwargs)

    def take_slot_then_drain():
        admitted = production_try()
        if admitted:
            app.state.instance_draining.set()
        return admitted

    reader._invocation = actual_read
    if boundary == "waiting":
        held, tasks = await _fill_pool()
    elif boundary == "after_slot":
        monkeypatch.setattr(async_workers, "_try_admit", take_slot_then_drain)
    read = asyncio.create_task(reader.read())
    try:
        if boundary == "initiated":
            while not entered.is_set():
                await asyncio.sleep(0.001)
            app.state.instance_draining.set()
            assert not read.done()
            assert async_workers.outstanding_admissions() == 1
            release.set()
            await read
            assert len(calls) == 1
            with pytest.raises(RequiredGenerationUnavailable):
                await reader.read()
        elif boundary == "waiting":
            await asyncio.sleep(0.05)
            app.state.instance_draining.set()
            with pytest.raises(RequiredGenerationUnavailable):
                await read
            assert calls == []
            assert async_workers.outstanding_admissions() == 32
        else:
            with pytest.raises(RequiredGenerationUnavailable):
                await read
            assert calls == []
        assert reader._issued is None and reader._registered_issuance is None
        assert app.state.process_recovery.instance_draining.is_set()
        assert app.state.composer_async_worker._instance_draining is async_workers._INSTANCE_DRAINING
    finally:
        release.set()
        if held is not None:
            held.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(read, return_exceptions=True)
        # The permanent latch refuses ordinary observation. This owned
        # COMPOSE release retains its existing required drain authority.
        await lease.close()
        assert app.state.process_recovery.instance_draining.is_set()
        assert async_workers.outstanding_admissions() == 0
        assert not app.state.process_recovery.watchdog.completed
        app.state.session_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("child_fault", ["none", "sql", "same_marker_cancel"])
@pytest.mark.parametrize("external_cancels", [0, 3])
async def test_actual_whole_watch_private_close_during_loss_join_preserves_child_original(
    tmp_path, monkeypatch, child_fault, external_cancels
):
    initial_reader, lease, engine, record = await _reader(tmp_path)
    worker = initial_reader.worker
    terminal_failure = ComposerOperationError(
        http_status=503,
        failure_code="worker_lost",
        error_type="composer_operation_worker_lost",
        body={"error_type": "composer_operation_worker_lost", "detail": "Owned terminal fixture"},
        diagnostic_id=None,
    )
    terminal = await worker._app.state.session_service.fail_composer_async_operation(initial_reader.running, failure=terminal_failure)
    assert terminal.status == "failed" and terminal.operation_id == initial_reader.running.claim.operation_id
    child_join = asyncio.Event()
    release_child = asyncio.Event()
    owner_gate = asyncio.Event()
    child_originals = []
    external_originals = []
    private_delivery = asyncio.Event()
    production_delivery = ComposerOperationWatchReader.retain_join_loop_delivery

    def retain_delivery(self, original):
        production_delivery(self, original)
        if self.owns_internal_cleanup_observation(original):
            private_delivery.set()
        else:
            external_originals.append(original)

    monkeypatch.setattr(ComposerOperationWatchReader, "retain_join_loop_delivery", retain_delivery)
    production_read = ComposerOperationWatchReader.read

    async def read_terminal(self):
        current, now = await production_read(self)
        assert current is not None
        assert current == terminal
        return current, now

    async def held_loss(self, *, cancellation_observations=None):
        deferred_child_failure = None
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as internal_child_close:
            assert cancellation_observations is not None
            cancellation_observations.append(internal_child_close)
            child_join.set()
            await release_child.wait()
            if child_fault == "sql":
                original = OperationalError("offline child failure", {}, RuntimeError("offline"))
                child_originals.append(original)
                return original
            if child_fault == "same_marker_cancel":
                # The actual loss API returns its original error. Sharing the
                # private marker does not make this child-result object private.
                original = asyncio.CancelledError(*internal_child_close.args)
                child_originals.append(original)
                deferred_child_failure = original
            else:
                raise
        if deferred_child_failure is not None:
            raise deferred_child_failure

    monkeypatch.setattr(ComposerOperationWatchReader, "read", read_terminal)
    monkeypatch.setattr(SessionOperationLease, "wait_until_lost", held_loss)
    owner = asyncio.create_task(owner_gate.wait())
    observation = _OperationWatchObservation()
    watcher = asyncio.create_task(worker._watch(initial_reader.running, lease, owner, initial_reader.anchor, observation))
    finishing = None
    try:
        await child_join.wait()
        assert not watcher.done()
        finishing = asyncio.create_task(_finish_operation_watcher(watcher, observation))
        assert observation.reader is not None
        await asyncio.wait_for(private_delivery.wait(), timeout=1.0)
        assert observation.reader._cleanup_cancellations
        assert not watcher.done() and not finishing.done()
        for index in range(external_cancels):
            watcher.cancel(f"actual external join cancellation {index}")
            while len(external_originals) != index + 1:
                await asyncio.sleep(0.001)
        release_child.set()
        errors = await finishing
        assert watcher.done() and observation.failure is None
        assert owner.cancelling() == 0
        leaves = tuple(leaf for error in errors for leaf in required_failure_leaves(error))
        expected = (*external_originals, *child_originals)
        assert len(leaves) == len(expected)
        assert all(actual is original for actual, original in zip(leaves, expected, strict=True))
        assert all(leaf is not private for leaf in leaves for private in observation.reader._cleanup_cancellations)
        for error in child_originals:
            selected = worker._failure_for(record, datetime.now(UTC), error)
            if child_fault == "sql":
                assert selected.http_status == 503
            else:
                assert selected.failure_code == "worker_lost"
        assert async_workers.outstanding_admissions() == 0
    finally:
        release_child.set()
        if finishing is not None:
            await asyncio.gather(finishing, return_exceptions=True)
        elif not watcher.done():
            await _finish_operation_watcher(watcher, observation)
        owner_gate.set()
        await asyncio.gather(owner, return_exceptions=True)
        await lease.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_factory_drain_refuses_fresh_compose_creation_but_joins_owned_release(tmp_path):
    from elspeth.web.required_executor import RequiredGenerationUnavailable

    _reader, lease, app, _record = await _factory_installed_reader(tmp_path)
    coordinator = lease.required_work
    assert coordinator is not None
    calls = []
    ticket = coordinator.reserve(RequiredWorkSource.PROPOSAL_CREATION_SQL)
    app.state.process_recovery.begin_shutdown()
    try:
        with pytest.raises(RequiredGenerationUnavailable):
            await async_workers.run_required_sql_in_worker(ticket, lambda: calls.append("new business SQL"))
        assert calls == []
        assert ticket.complete
        assert async_workers.outstanding_admissions() == 0
        await lease.close()
        assert app.state.instance_draining.is_set()
        assert async_workers.outstanding_admissions() == 0
        assert not app.state.process_recovery.watchdog.completed
    finally:
        await lease.close()
        app.state.session_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("loss_outcome", ["private_close", "external_preentry", "same_marker_return", "same_marker_throw"])
async def test_actual_done_physical_terminal_read_preserves_loss_entry_and_foreign_cancellations(tmp_path, monkeypatch, loss_outcome):
    import ast
    import inspect
    import sys
    import textwrap

    from elspeth.web.sessions import composer_async_worker as worker_module

    initial_reader, lease, engine, record = await _reader(tmp_path)
    worker = initial_reader.worker
    owner_gate = asyncio.Event()
    owner = asyncio.create_task(owner_gate.wait())
    observation = _OperationWatchObservation()
    real_create = asyncio.create_task
    real_submit = async_workers._submit_shared
    real_loss = SessionOperationLease.wait_until_lost
    real_factory = ComposerOperationWatchReader.for_operation
    physical_returns = []
    factory_entries = []
    factory_originals = []
    submit_entries = []
    submit_originals = []
    physical_submissions = []
    physical_originals = []
    physical_original_sources = []
    child_entries = []
    child_originals = []
    loss_tasks = []
    first_preentry_results = []
    previous_trace = sys.gettrace()
    foreign_marker = object()
    watcher: asyncio.Task[object] | None = None
    primary: BaseException | None = None
    cleanup_failures: list[BaseException] = []
    try:
        terminal_failure = ComposerOperationError(
            http_status=503,
            failure_code="worker_lost",
            error_type="composer_operation_worker_lost",
            body={"error_type": "composer_operation_worker_lost", "detail": "Owned terminal fixture"},
            diagnostic_id=None,
        )
        terminal = await worker._app.state.session_service.fail_composer_async_operation(initial_reader.running, failure=terminal_failure)
        assert terminal.status == "failed" and terminal.operation_id == initial_reader.running.claim.operation_id
        if loss_outcome == "external_preentry":
            # Deterministically control the actual helper's private object
            # allocation, so the foreign pre-entry cancellation has the SAME
            # marker identity, not just similar text. No close was issued to this
            # already-canceled Task and no producer catch receipt exists.
            monkeypatch.setattr(worker_module, "object", lambda: foreign_marker, raising=False)
            source_lines, source_start = inspect.getsourcelines(_finish_operation_watcher)
            source_tree = ast.parse(textwrap.dedent("".join(source_lines)))
            calls = [
                node
                for node in ast.walk(source_tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "watcher"
                and node.func.attr == "result"
            ]
            assert len(calls) == 1
            actual_result_line = source_start + calls[0].lineno - 1

            def observe_first_result(frame, event, argument):
                if (
                    event == "exception"
                    and frame.f_code is _finish_operation_watcher.__code__
                    and frame.f_lineno == actual_result_line
                    and loss_tasks
                    and frame.f_locals["watcher"] is loss_tasks[0]
                ):
                    original = argument[1]
                    if isinstance(original, asyncio.CancelledError):
                        first_preentry_results.append(original)
                return observe_first_result

            # Test-only CPython exception-delivery observation captures the actual
            # FIRST native Task.result() throw, without replacing Task methods or
            # consuming that canceled Task's outcome before production sees it.

        def original_source_frames(original):
            chain = []
            seen = set()
            current = original
            while current is not None and id(current) not in seen:
                seen.add(id(current))
                frames = []
                trace = current.__traceback__
                while trace is not None:
                    frames.append((trace.tb_frame.f_code.co_filename.rsplit("/", 1)[-1], trace.tb_frame.f_code.co_name, trace.tb_lineno))
                    trace = trace.tb_next
                chain.append((type(current).__name__, frames))
                current = current.__cause__ if current.__cause__ is not None else current.__context__
            return chain

        def factory(cls, *args):
            factory_entries.append(args)
            try:
                reader = real_factory(*args)
            except BaseException as original:
                factory_originals.append(original)
                raise
            actual_read = reader._invocation

            def actual_terminal_read(**kwargs):
                current, now = actual_read(**kwargs)
                assert current == terminal
                return current, now

            reader._invocation = actual_terminal_read
            return reader

        async def returned_done(*args, **kwargs):
            submit_entries.append((args, kwargs))
            try:
                future = await real_submit(*args, **kwargs)
            except BaseException as original:
                submit_originals.append(original)
                raise
            if kwargs.get("watch_reader") is not None:
                # A real shared ThreadPool Future, completed before its async
                # return. No synthetic Future or event-loop scheduling is used.
                physical_submissions.append(future)
                try:
                    future.result(timeout=1.0)
                except BaseException as original:
                    physical_originals.append(original)
                    physical_original_sources.append(original_source_frames(original))
                    raise
                assert future.done()
                physical_returns.append(future)
            return future

        def create_selected(coroutine, *, name=None, **kwargs):
            task = real_create(coroutine, name=name, **kwargs)
            if name == "composer-operation-loss-watch":
                loss_tasks.append(task)
                if loss_outcome == "external_preentry":
                    assert not task.done()
                    task.cancel(foreign_marker)
            return task

        async def selected_loss(self, *, cancellation_observations=None):
            child_entries.append(asyncio.current_task())
            deferred_child_failure = None
            try:
                return await real_loss(self, cancellation_observations=cancellation_observations)
            except asyncio.CancelledError as actual_close:
                assert cancellation_observations is not None
                assert any(original is actual_close for original in cancellation_observations)
                if loss_outcome in ("same_marker_return", "same_marker_throw"):
                    distinct = asyncio.CancelledError(*actual_close.args)
                    child_originals.append(distinct)
                    if loss_outcome == "same_marker_throw":
                        deferred_child_failure = distinct
                    else:
                        return distinct
                else:
                    raise
            if deferred_child_failure is not None:
                raise deferred_child_failure

        monkeypatch.setattr(ComposerOperationWatchReader, "for_operation", classmethod(factory))
        monkeypatch.setattr(async_workers, "_submit_shared", returned_done)
        monkeypatch.setattr(asyncio, "create_task", create_selected)
        monkeypatch.setattr(SessionOperationLease, "wait_until_lost", selected_loss)
        watcher = real_create(worker._watch(initial_reader.running, lease, owner, initial_reader.anchor, observation))
        if loss_outcome == "external_preentry":
            sys.settrace(observe_first_result)
        while not watcher.done():
            await asyncio.sleep(0.001)
        errors = await _finish_operation_watcher(watcher, observation)
        assert len(loss_tasks) == 1 and loss_tasks[0].done()
        assert len(physical_returns) == 1 and physical_returns[0].done(), {
            "factory_entries": len(factory_entries),
            "factory_originals": [type(original).__name__ for original in factory_originals],
            "submit_entries": len(submit_entries),
            "submit_originals": [type(original).__name__ for original in submit_originals],
            "physical_submissions": len(physical_submissions),
            "physical_originals": [type(original).__name__ for original in physical_originals],
            "physical_original_sources": physical_original_sources,
            "watch_observation_failure": None if observation.failure is None else type(observation.failure).__name__,
        }
        leaves = tuple(leaf for error in errors for leaf in required_failure_leaves(error))
        if loss_outcome == "private_close":
            assert child_entries == loss_tasks
            assert errors == () and observation.failure is None
            assert owner.cancelling() == 0
        elif loss_outcome == "external_preentry":
            assert child_entries == []
            assert len(leaves) == 1 and isinstance(leaves[0], asyncio.CancelledError)
            assert leaves[0].args == (foreign_marker,)
            assert len(first_preentry_results) == 1
            assert leaves[0] is first_preentry_results[0]
            assert observation.reader is not None
            assert not observation.reader.owns_internal_cleanup_observation(leaves[0])
        else:
            assert child_entries == loss_tasks
            assert len(leaves) == 1 and leaves[0] is child_originals[0]
            selected = worker._failure_for(record, datetime.now(UTC), leaves[0])
            assert selected.failure_code == "worker_lost"
        assert async_workers.outstanding_admissions() == 0
    except BaseException as original:
        primary = original
    finally:
        try:
            sys.settrace(previous_trace)
        except BaseException as original:
            cleanup_failures.append(original)
        if watcher is not None:
            try:
                if not watcher.done():
                    await _finish_operation_watcher(watcher, observation)
            except BaseException as original:
                cleanup_failures.append(original)
        try:
            owner_gate.set()
        except BaseException as original:
            cleanup_failures.append(original)
        try:
            await asyncio.gather(owner, return_exceptions=True)
        except BaseException as original:
            cleanup_failures.append(original)
        try:
            await lease.close()
        except BaseException as original:
            cleanup_failures.append(original)
        try:
            engine.dispose()
        except BaseException as original:
            cleanup_failures.append(original)
    if primary is not None:
        if cleanup_failures:
            raise BaseExceptionGroup("Actual terminal fixture and cleanup retained originals", [primary, *cleanup_failures]) from None
        raise primary
    if cleanup_failures:
        raise BaseExceptionGroup("Actual terminal fixture cleanup retained originals", cleanup_failures)


@pytest.mark.asyncio
@pytest.mark.parametrize("allocation", ["before", "after"])
@pytest.mark.parametrize("cause_trap", [False, True], ids=["runtime-error", "cause-trap"])
async def test_loss_task_allocation_failure_keeps_actual_owner_unknown_without_replay(tmp_path, monkeypatch, allocation, cause_trap):
    initial_reader, lease, engine, _record = await _reader(tmp_path)
    worker = initial_reader.worker
    owner_gate = asyncio.Event()
    owner = asyncio.create_task(owner_gate.wait())
    observation = _OperationWatchObservation()
    real_create = asyncio.create_task
    allocation_original = RuntimeError("selected actual allocation boundary failed")
    cause_reads = []
    true_cause = None
    if cause_trap:
        wrong_cause = RuntimeError("foreign getter supplied the wrong cause")
        true_cause = RuntimeError("actual stored allocation cause")

        class AllocationCauseTrap(RuntimeError):
            @property
            def __cause__(self):
                cause_reads.append(self)
                return wrong_cause

        allocation_original = AllocationCauseTrap("selected actual allocation boundary failed")
        BaseException.__cause__.__set__(allocation_original, true_cause)
    declared_coroutines = []
    actual_children = []
    delivered = []
    real_delivery = ComposerOperationWatchReader.retain_join_loop_delivery

    def fail_selected(coroutine, *, name=None, **kwargs):
        if name != "composer-operation-loss-watch":
            return real_create(coroutine, name=name, **kwargs)
        declared_coroutines.append(coroutine)
        if allocation == "after":
            actual_children.append(real_create(coroutine, name=name, **kwargs))
        raise allocation_original

    def capture_delivery(self, original):
        real_delivery(self, original)
        if not self.owns_internal_cleanup_observation(original):
            delivered.append(original)

    monkeypatch.setattr(asyncio, "create_task", fail_selected)
    monkeypatch.setattr(ComposerOperationWatchReader, "retain_join_loop_delivery", capture_delivery)
    watcher = real_create(worker._watch(initial_reader.running, lease, owner, initial_reader.anchor, observation))
    try:
        while not declared_coroutines:
            await asyncio.sleep(0.001)
        if allocation == "before":
            for index in range(3):
                watcher.cancel(f"allocation unknown original {index}")
                while len(delivered) != index + 1:
                    await asyncio.sleep(0.001)
            assert not watcher.done()
            assert worker._instance_draining.is_set()
            assert len(declared_coroutines) == 1 and actual_children == []
            assert not worker._process_recovery.watchdog.completed
            # The controlled supplier proves no allocation occurred and now
            # schedules its exact retained coroutine once. This is an actual
            # owner handoff, not a forged task/result or production retry.
            actual_children.append(real_create(declared_coroutines[0], name="controlled-original-loss-entry"))
        while not watcher.done():
            await asyncio.sleep(0.001)
        errors = await _finish_operation_watcher(watcher, observation)
        assert cause_reads == [], "Watch must not read the allocation exception cause getter"
        assert BaseException.__cause__.__get__(allocation_original, type(allocation_original)) is true_cause
        leaves = tuple(leaf for error in errors for leaf in required_failure_leaves(error))
        assert len(declared_coroutines) == 1 and len(actual_children) == 1
        assert actual_children[0].done()
        expected = (allocation_original, *delivered)
        assert len(leaves) == len(expected)
        assert all(actual is original for actual, original in zip(leaves, expected, strict=True))
        assert not worker._process_recovery.watchdog.completed
    finally:
        if not actual_children and declared_coroutines:
            actual_children.append(real_create(declared_coroutines[0], name="controlled-finally-loss-entry"))
        await asyncio.gather(watcher, *actual_children, return_exceptions=True)
        owner_gate.set()
        await asyncio.gather(owner, return_exceptions=True)
        await lease.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_loss_entry_owner_refuses_foreign_returned_task(tmp_path):
    from elspeth.web.sessions.composer_async_worker import _OperationLossWatchOwner

    gate = asyncio.Event()
    first = asyncio.create_task(gate.wait())
    second = asyncio.create_task(gate.wait())
    receipt = _OperationLossWatchOwner(asyncio.Event())
    try:
        receipt.bind_returned_task(first)
        with pytest.raises(AuditIntegrityError):
            receipt.bind_returned_task(second)
        assert receipt.task is first and not receipt.entered.is_set()
        assert receipt.conflicting_tasks == [second]
    finally:
        gate.set()
        await asyncio.gather(first, second)
