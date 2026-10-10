"""Actual EXECUTE authorities/threads, with a nominal no-signal watchdog only.

No provider, server, or observer double is used. Pipeline dispatch controls run
one physical executor invocation; they do not claim engine/provider acceptance.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import CodeType, FunctionType
from uuid import UUID

import pytest
import pytest_asyncio
import structlog
from fastapi import FastAPI
from starlette.requests import Request

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web import async_workers
from elspeth.web.application_finalizers import ApplicationFinalizerOwner
from elspeth.web.auth.models import UserIdentity
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.composer import yaml_generator
from elspeth.web.coordination.contracts import RecoveryRequiredReason
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.execution.progress import ProgressBroadcaster
from elspeth.web.execution.recovery import RunRecoveryCoordinator
from elspeth.web.execution.routes import create_execution_router
from elspeth.web.execution.service import ExecutionServiceImpl, close_execute_lease_before_transfer
from elspeth.web.execution_lease_cleanup import ExecutionAcquisitionObligation, ExecutionLeaseReleaseRegistry
from elspeth.web.process_recovery import ProcessRecovery
from elspeth.web.required_executor import RequiredGenerationUnavailable
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import wire_test_pipeline_user_authority
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog
from tests.helpers.session_fences import seed_live_compose_context
from tests.unit.web.blobs.test_service import _seed_active_run
from tests.unit.web.blobs.test_service_fencing import _insert_session
from tests.unit.web.test_app import _settings


async def _until(predicate) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.001)


def _leaves(original: BaseException) -> list[BaseException]:
    if isinstance(original, BaseExceptionGroup):
        return [leaf for child in original.exceptions for leaf in _leaves(child)]
    return [original]


@dataclass
class _Harness:
    registry: ExecutionLeaseReleaseRegistry
    service: ExecutionServiceImpl
    sessions: SessionServiceImpl
    owner: ApplicationFinalizerOwner
    recovery: ProcessRecovery
    gates: list[threading.Event] = field(default_factory=list)
    tasks: list[asyncio.Task] = field(default_factory=list)
    leases: list[SessionOperationLease] = field(default_factory=list)
    runs: dict[UUID, UUID] = field(default_factory=dict)
    witnessed_cleanup_errors: list[BaseException] = field(default_factory=list)

    async def seed_run(self, session_id: UUID) -> UUID:
        context = seed_live_compose_context(self.sessions._engine, session_id)
        run_id = UUID(
            await _seed_active_run(
                self.sessions._engine,
                session_id,
                session_operation_context=context,
                source={
                    "plugin": "csv",
                    "on_success": "output",
                    "on_validation_failure": "discard",
                    "options": {"path": "unused-owner-control.csv"},
                },
            )
        )
        self.sessions.session_operation_authority.release(context)
        self.runs[session_id] = run_id
        return run_id

    async def lease(self, session_id: UUID) -> tuple[ExecutionAcquisitionObligation, SessionOperationLease]:
        await self.seed_run(session_id)
        obligation = self.registry.admit(
            self.sessions.session_operation_authority,
            session_id=session_id,
            owner_instance_id=self.sessions.session_operation_owner_instance_id,
            lease_seconds=30,
        )
        lease = await SessionOperationLease.acquire(
            self.sessions.session_operation_authority,
            session_id=session_id,
            operation_kind=SessionOperationKind.EXECUTE,
            owner_instance_id=self.sessions.session_operation_owner_instance_id,
            lease_seconds=30,
            execution_obligation=obligation,
        )
        self.leases.append(lease)
        return obligation, lease

    def dispatch(self, obligation: ExecutionAcquisitionObligation, lease: SessionOperationLease) -> None:
        watcher = self.service._create_loss_watcher(
            lease,
            threading.Event(),
            run_id=self.runs[obligation.session_id],
        )
        self.tasks.append(watcher)
        self.service._submit_owned_pipeline(obligation, lease, watcher, lambda: None)


@pytest_asyncio.fixture
async def actual(tmp_path: Path) -> AsyncIterator[_Harness]:
    # Fresh actual process/generation selection, not an observer-only event.
    await async_workers.shutdown_async_workers()
    owner = ApplicationFinalizerOwner()
    draining = threading.Event()
    recovery = ProcessRecovery(watchdog=OwnedTestProcessWatchdog(draining), instance_draining=draining)
    async_workers.configure_required_executor_recovery(
        drain_seconds=2,
        instance_draining=draining,
        generation_unavailable=threading.Event(),
        recovery_callback=recovery.required_generation_expired,
        application_finalizer_owner=owner,
    )
    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(engine)
    sessions = SessionServiceImpl(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("execute-controls"))
    loop = asyncio.get_running_loop()
    registry = ExecutionLeaseReleaseRegistry(owner=owner, recovery=recovery, loop=loop)
    service = ExecutionServiceImpl.for_trained_operator(
        loop=loop,
        broadcaster=ProgressBroadcaster(loop),
        settings=_settings(tmp_path, composer_boot_probe_enabled=False),
        session_service=sessions,
        yaml_generator=yaml_generator,
        telemetry=build_sessions_telemetry(),
        execution_lease_release_registry=registry,
    )
    harness = _Harness(registry, service, sessions, owner, recovery)
    try:
        yield harness
    finally:
        # Every physical gate opens even when the intended assertion is red.
        for gate in harness.gates:
            gate.set()
        for watcher_owner in service._loss_watcher_owners:
            watcher_owner.close_requested.set()
        failures: list[BaseException] = []

        async def settle(awaitable) -> None:
            try:
                await asyncio.wait_for(awaitable, 5)
            except BaseException as original:
                unexpected = [leaf for leaf in _leaves(original) if not any(leaf is known for known in harness.witnessed_cleanup_errors)]
                failures.extend(unexpected)

        owner.seal()
        draining.set()
        if service.executor_join_task is None:
            await settle(async_workers.run_application_finalizer_in_worker(registry.executor_finalizer))
        else:
            await settle(asyncio.gather(service.executor_join_task, return_exceptions=True))
        # A source mutant may intentionally omit an authorized literal-submit
        # handoff. After the oracle, physically clean that exact owner with the
        # actual issued capability and unchanged producer coroutine. This is
        # neither a finalizer retry nor synthetic retirement. Callback Unknown
        # and already-declared/no-return allocations are excluded by the API.
        if registry.executor_join_succeeded:
            for obligation in registry.joined_pipeline_submission_failures():
                try:
                    obligation.declare_unknown_pipeline_cleanup()
                    lease = obligation.lease
                    assert lease is not None
                    watcher = service._pipeline_watchers[obligation]
                    cleanup = asyncio.create_task(service._finish_execution_authority(obligation, lease, watcher, None))
                    obligation.bind_completion_task(cleanup)
                    harness.tasks.append(cleanup)
                except BaseException as original:
                    failures.append(original)
        for lease in harness.leases:
            await settle(lease.close())
            obligation = lease.execution_obligation
            if obligation.completion_task is not None:
                await settle(asyncio.gather(obligation.completion_task, return_exceptions=True))
        # Test-owned tasks have their outcomes asserted by each case. Consume
        # their actual settled results; do not propagate cancellation into
        # a loss watcher or treat wrapper completion as physical release.
        if harness.tasks:
            await settle(asyncio.gather(*harness.tasks, return_exceptions=True))
        if service._registry_join_task is not None:
            await settle(asyncio.gather(service._registry_join_task, return_exceptions=True))
        await settle(recovery.join_escalation())
        await settle(async_workers.shutdown_async_workers())
        try:
            engine.dispose()
        except BaseException as original:
            failures.append(original)
        if failures:
            raise BaseExceptionGroup("EXECUTE control cleanup retained independent faults", failures)


@contextmanager
def _hold_transaction_return(monkeypatch, harness: _Harness, session_ids: list[UUID]):
    """Hold canonical SQL only after real commit/lock exit, before return."""
    authority = harness.sessions.session_operation_authority
    original = SQLiteLocalSessionOperationAuthority._locked_transaction
    entered = {str(sid): threading.Event() for sid in session_ids}
    allowed = {str(sid): threading.Event() for sid in session_ids}
    calls = {str(sid): 0 for sid in session_ids}
    lock = threading.Lock()
    harness.gates.extend(allowed.values())

    @contextmanager
    def held(self, session_id: str) -> Iterator:
        with original(self, session_id) as connection:
            yield connection
        with lock:
            hold = self is authority and session_id in calls and calls[session_id] == 0
            if self is authority and session_id in calls:
                calls[session_id] += 1
        if hold:
            entered[session_id].set()
            assert allowed[session_id].wait(5), "control SQL gate was not released"

    monkeypatch.setattr(SQLiteLocalSessionOperationAuthority, "_locked_transaction", held)
    try:
        yield entered, allowed, calls
    finally:
        for gate in allowed.values():
            gate.set()
        monkeypatch.setattr(SQLiteLocalSessionOperationAuthority, "_locked_transaction", original)


@pytest.mark.asyncio
async def test_actual_route_late_acquire_after_seal_never_dispatches_and_releases_once(actual, monkeypatch):
    sid = _insert_session(actual.sessions._engine)
    await actual.seed_run(sid)
    app = FastAPI()
    app.state.session_service = actual.sessions
    app.state.settings = actual.service._settings
    app.state.execution_lease_release_registry = actual.registry
    wire_test_pipeline_user_authority(app, identity_id="test-user")
    request = Request({"type": "http", "method": "POST", "path": f"/api/sessions/{sid}/execute", "headers": [], "app": app})
    endpoint = next(route.endpoint for route in create_execution_router().routes if route.name == "execute_pipeline")
    dispatches = []
    original_execute = ExecutionServiceImpl.execute

    async def observe_execute(service, *args, **kwargs):
        dispatches.append((args, kwargs))
        return await original_execute(service, *args, **kwargs)

    monkeypatch.setattr(ExecutionServiceImpl, "execute", observe_execute)
    with _hold_transaction_return(monkeypatch, actual, [sid]) as (entered, allowed, calls):
        route = asyncio.create_task(
            endpoint(
                session_id=sid,
                request=request,
                user=UserIdentity(user_id="test-user", username="test-user"),
                state_id=None,
                execute_request=None,
                service=actual.service,
                session_service=actual.sessions,
            )
        )
        actual.tasks.append(route)
        await _until(entered[str(sid)].is_set)
        # Actual canonical acquire has committed, but physical SQL Future is
        # still held. Registry owns it before any returned lease exists.
        obligations = tuple(actual.registry._pending.values())
        assert len(obligations) == 1
        obligation = obligations[0]
        assert obligation.acquire_submission.future is not None
        assert not obligation.acquire_submission.future.done()
        actual.registry.seal()
        actual.recovery.instance_draining.set()
        with pytest.raises(AuditIntegrityError):
            actual.registry.assert_completed()
        allowed[str(sid)].set()
        with pytest.raises(RequiredGenerationUnavailable):
            await asyncio.wait_for(route, 5)
        await _until(lambda: (actual.registry.observe_ready(), obligation.retired)[1])
        assert dispatches == [], "late acquired authority reached business dispatch"
        assert obligation.release_succeeded and obligation.context is not None
        assert obligation.release_submission.source_value is None
        assert calls[str(sid)] == 2, "expected one canonical acquire and one canonical release"
        with pytest.raises(AuditIntegrityError):
            actual.registry.assert_completed()
        actual.owner.seal()
        await asyncio.wait_for(actual.service.shutdown(), 5)
        assert actual.registry.executor_join_succeeded
        actual.registry.assert_completed()
        # New acquisition after seal must not enter canonical SQL at all.
        before = calls[str(sid)]
        with pytest.raises(RequiredGenerationUnavailable):
            actual.registry.admit(
                actual.sessions.session_operation_authority,
                session_id=sid,
                owner_instance_id=actual.sessions.session_operation_owner_instance_id,
                lease_seconds=30,
            )
        assert calls[str(sid)] == before


@pytest.mark.asyncio
async def test_actual_two_release_owners_join_despite_executor_fault_and_three_caller_cancels(actual, monkeypatch):
    ids = [_insert_session(actual.sessions._engine) for _ in range(2)]
    pairs = [await actual.lease(sid) for sid in ids]
    original_shutdown = ThreadPoolExecutor.shutdown
    join_error = OSError("private-executor-join-control")
    joins = []

    def fault_after_physical_join(executor, *args, **kwargs):
        original_shutdown(executor, *args, **kwargs)
        if executor is actual.service._executor:
            joins.append(executor)
            raise join_error

    monkeypatch.setattr(ThreadPoolExecutor, "shutdown", fault_after_physical_join)
    with _hold_transaction_return(monkeypatch, actual, ids) as (entered, allowed, calls):
        for obligation, lease in pairs:
            actual.dispatch(obligation, lease)
        await _until(lambda: all(event.is_set() for event in entered.values()))
        actual.owner.seal()
        actual.recovery.instance_draining.set()
        shutdown = asyncio.create_task(actual.service.shutdown())
        actual.tasks.append(shutdown)
        await _until(lambda: actual.service.executor_join_task is not None and actual.service.executor_join_task.done())
        await _until(lambda: any(original is join_error for original in actual.registry._failures))
        assert joins == [actual.service._executor]
        assert not shutdown.done(), "executor fault abandoned independent releases"
        delivered = []
        original_retain = async_workers._retain_cancellation

        def capture(cancellations, original):
            original_retain(cancellations, original)
            if asyncio.current_task() is shutdown:
                delivered.append(original)

        monkeypatch.setattr("elspeth.web.execution.service._retain_cancellation", capture)
        for ordinal in range(3):
            shutdown.cancel(f"private-shutdown-cancel-{ordinal}")
            await _until(lambda count=ordinal + 1: len(delivered) == count)
        allowed[str(ids[0])].set()
        await _until(lambda: (actual.registry.observe_ready(), pairs[0][0].retired)[1])
        assert not shutdown.done(), "first release completion abandoned held sibling"
        assert not pairs[1][0].retired
        allowed[str(ids[1])].set()
        with pytest.raises(BaseExceptionGroup) as outcome:
            await asyncio.wait_for(shutdown, 5)
        leaves = _leaves(outcome.value)
        assert join_error in leaves and all(any(leaf is cancel for leaf in leaves) for cancel in delivered)
        assert len(delivered) == 3 and len({id(cancel) for cancel in delivered}) == 3
        assert all(obligation.release_succeeded and obligation.retired for obligation, _ in pairs)
        assert all(calls[str(sid)] == 1 for sid in ids)
        with pytest.raises(BaseExceptionGroup):
            actual.registry.assert_completed()


@pytest.mark.asyncio
async def test_actual_inline_pipeline_callback_runs_outside_generation_and_registry_locks(actual, monkeypatch):
    obligation, lease = await actual.lease(_insert_session(actual.sessions._engine))
    original = Future.add_done_callback
    probes = []

    def register(future, callback):
        if future is obligation.pipeline:
            # Hold registration until the real physical invocation finishes,
            # forcing concurrent.Future's actual inline terminal callback path.
            future.result(timeout=5)
            observed = []

            def probe():
                for lock in (obligation.generation.submission_lock, obligation.registry._lock):
                    acquired = lock.acquire(blocking=False)
                    observed.append(acquired)
                    if acquired:
                        lock.release()

            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(5)
            assert not thread.is_alive()
            probes.append(observed)
            assert observed == [True, True], "inline callback was allocated under admission locks"
        return original(future, callback)

    monkeypatch.setattr(Future, "add_done_callback", register)
    actual.dispatch(obligation, lease)
    await _until(lambda: obligation.completion_task is not None and obligation.completion_task.done())
    actual.registry.observe_ready()
    assert probes == [[True, True]]
    assert obligation.release_succeeded and obligation.retired
    actual.owner.seal()
    actual.recovery.instance_draining.set()
    await asyncio.wait_for(actual.service.shutdown(), 5)
    actual.registry.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("allocation_after_side_effect", [False, True])
async def test_actual_callback_scheduling_fault_never_becomes_absent_completion_success(actual, monkeypatch, allocation_after_side_effect):
    obligation, lease = await actual.lease(_insert_session(actual.sessions._engine))
    original = asyncio.run_coroutine_threadsafe
    failure = OSError("callback-scheduling-private-control")
    scheduled = []

    def fault(coroutine, loop):
        if coroutine.cr_code.co_name == "_finish_execution_authority":
            if allocation_after_side_effect:
                scheduled.append(original(coroutine, loop))
            else:
                coroutine.close()  # This control knows it has not submitted.
            raise failure
        return original(coroutine, loop)

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", fault)
    actual.dispatch(obligation, lease)
    await _until(lambda: any(error is failure for error in actual.registry._failures))
    assert obligation.completion_required and obligation.pipeline is not None
    assert obligation.pipeline.done()
    assert obligation.completion is None
    if allocation_after_side_effect:
        await _until(lambda: obligation.completion_task is not None and obligation.completion_task.done())
        actual.registry.observe_ready()
        assert obligation.release_succeeded and obligation.retired
        assert scheduled[0].done()
    else:
        assert obligation.completion_task is None and not obligation.release_succeeded
        assert actual.registry.has_pending_physical_owners()
    with pytest.raises((AuditIntegrityError, BaseExceptionGroup)):
        actual.registry.assert_completed()
    assert actual.service._pipeline_completion_owners[obligation.pipeline] is obligation
    assert obligation in actual.service._pipeline_watchers
    # Cleanup uses actual known lease after the missing-owner oracle; it does
    # not manufacture completion_task/retirement/COMPLETE for this Unknown.


@pytest.mark.asyncio
async def test_actual_duplicate_terminal_callback_is_idempotent_but_foreign_future_is_integrity(actual):
    obligation, lease = await actual.lease(_insert_session(actual.sessions._engine))
    actual.dispatch(obligation, lease)
    await _until(lambda: obligation.completion_task is not None and obligation.completion_task.done())
    completion = obligation.completion_task
    actual.service._on_pipeline_done(obligation.pipeline, session_operation_lease=lease)
    assert obligation.completion_task is completion
    assert obligation.release_succeeded
    foreign = Future()
    foreign.set_result(None)
    with pytest.raises(AuditIntegrityError):
        actual.service._on_pipeline_done(foreign, session_operation_lease=lease)
    assert obligation.completion_task is completion
    assert any(isinstance(error, AuditIntegrityError) for error in actual.registry._failures)


@pytest.mark.asyncio
async def test_actual_queue_then_raise_pipeline_submit_retains_unknown_and_never_replays(actual, monkeypatch):
    obligation, lease = await actual.lease(_insert_session(actual.sessions._engine))
    private = actual.service._executor
    assert private is not None
    entered, allowed = threading.Event(), threading.Event()
    actual.gates.append(allowed)
    invoked = []
    original = OSError("accepted-pipeline-no-return")

    production_submit = ThreadPoolExecutor.submit

    def queue_then_raise(executor, function, /, *args, **kwargs):
        future = production_submit(executor, function, *args, **kwargs)
        if executor is private:
            raise original
        return future

    monkeypatch.setattr(ThreadPoolExecutor, "submit", queue_then_raise)
    watcher = actual.service._create_loss_watcher(
        lease,
        threading.Event(),
        run_id=actual.runs[obligation.session_id],
    )
    actual.tasks.append(watcher)

    def work():
        invoked.append("actual-once")
        entered.set()
        assert allowed.wait(5)
        return None

    try:
        with pytest.raises(OSError) as fault:
            actual.service._submit_owned_pipeline(obligation, lease, watcher, work)
        assert fault.value is original
        await _until(entered.is_set)
        assert obligation.pipeline_submission_unknown is original
        assert obligation.pipeline is None and obligation.completion_required
        assert not obligation.release_succeeded and actual.registry.has_pending_physical_owners()
        with pytest.raises((AuditIntegrityError, BaseExceptionGroup)):
            actual.registry.assert_completed()
        actual.owner.seal()
        actual.recovery.instance_draining.set()
        shutdown = asyncio.create_task(actual.service.shutdown())
        actual.tasks.append(shutdown)
        await _until(lambda: actual.service.executor_join_task is not None)
        assert not actual.registry.executor_join_succeeded
        assert obligation.completion_task is None and not obligation.release_succeeded
        allowed.set()
        with pytest.raises((OSError, BaseExceptionGroup)) as joined:
            await asyncio.wait_for(shutdown, 5)
        assert any(error is original for error in _leaves(joined.value))
        assert actual.registry.executor_join_succeeded
        assert invoked == ["actual-once"], "unknown accepted pipeline was replayed"
        assert obligation.pipeline is None and obligation.completion_task is not None
        assert obligation.release_succeeded and obligation.retired
        assert obligation in actual.service._pipeline_watchers
        assert obligation in actual.service._failed_submission_cleanup_started
        with pytest.raises(BaseExceptionGroup):
            actual.registry.assert_completed()
    finally:
        allowed.set()


@pytest.mark.asyncio
async def test_actual_missing_release_rejects_complete_then_real_join_allows_it(actual):
    obligation, lease = await actual.lease(_insert_session(actual.sessions._engine))
    actual.registry.seal()
    actual.recovery.instance_draining.set()
    with pytest.raises(AuditIntegrityError):
        actual.registry.assert_completed()
    await lease.close()
    await actual.registry.join_all()
    assert obligation.release_succeeded and obligation.retired
    with pytest.raises(AuditIntegrityError):
        actual.registry.assert_completed()
    actual.owner.seal()
    await asyncio.wait_for(actual.service.shutdown(), 5)
    assert actual.registry.executor_join_succeeded
    actual.registry.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("seal_before_acquire_return", [False, True])
async def test_actual_recovery_preadmits_and_closes_missing_baseline_or_late_authority(
    actual, monkeypatch, tmp_path, seal_before_acquire_return
):
    sid = _insert_session(actual.sessions._engine)
    run_id = await actual.seed_run(sid)
    candidate = await actual.sessions.get_run(run_id)
    coordinator = RunRecoveryCoordinator(
        actual.sessions,
        actual.service,
        BlobServiceImpl(actual.sessions._engine, tmp_path / "blobs"),
        landscape_url=f"sqlite:///{tmp_path / 'missing-landscape.db'}",
        create_tables=True,
    )
    with _hold_transaction_return(monkeypatch, actual, [sid]) as (entered, allowed, _calls):
        recovery = asyncio.create_task(coordinator._recover_candidate(candidate))
        actual.tasks.append(recovery)
        await _until(entered[str(sid)].is_set)
        obligations = tuple(actual.registry._pending.values())
        assert len(obligations) == 1 and obligations[0].acquire_submission.future is not None
        obligation = obligations[0]
        if seal_before_acquire_return:
            actual.registry.seal()
            actual.recovery.instance_draining.set()
        allowed[str(sid)].set()
        if seal_before_acquire_return:
            with pytest.raises(RequiredGenerationUnavailable):
                await asyncio.wait_for(recovery, 5)
        else:
            await asyncio.wait_for(recovery, 5)
            current = await actual.sessions.get_run(run_id)
            assert current.recovery_required_reason is RecoveryRequiredReason.MISSING_BASELINE
        await _until(lambda: (actual.registry.observe_ready(), obligation.retired)[1])
        assert obligation.release_succeeded and not obligation.completion_required
        assert obligation.pipeline is None, "recovery early return submitted a pipeline"


@pytest.mark.asyncio
async def test_actual_pipeline_decision_refuses_copied_or_wrong_kind_context_without_submission(actual):
    obligation, lease = await actual.lease(_insert_session(actual.sessions._engine))
    original = lease.context
    copies = [replace(original), replace(original, operation_kind=SessionOperationKind.COMPOSE)]
    for context in copies:
        assert context is not original
        with pytest.raises(AuditIntegrityError), obligation.pipeline_submission_decision(context):
            raise AssertionError("foreign context reached actual submission decision")
    assert obligation.context is original and obligation.pipeline is None
    assert not obligation.completion_required


@pytest.mark.asyncio
async def test_actual_pretransfer_body_fault_retains_three_close_cancellations(actual, monkeypatch):
    sid = _insert_session(actual.sessions._engine)
    obligation, lease = await actual.lease(sid)
    primary = ValueError("private-body-original")
    delivered = []
    original_retain = async_workers._retain_cancellation

    async def body():
        try:
            raise primary
        finally:
            await close_execute_lease_before_transfer(lease, primary=primary)

    with _hold_transaction_return(monkeypatch, actual, [sid]) as (entered, allowed, calls):
        task = asyncio.create_task(body())
        actual.tasks.append(task)

        def capture(cancellations, original):
            original_retain(cancellations, original)
            if asyncio.current_task() is task:
                delivered.append(original)

        monkeypatch.setattr("elspeth.web.execution.service._retain_cancellation", capture)
        await _until(entered[str(sid)].is_set)
        for ordinal in range(3):
            task.cancel(f"private-close-caller-{ordinal}")
            await _until(lambda count=ordinal + 1: len(delivered) == count)
        assert not task.done() and not obligation.release_succeeded
        allowed[str(sid)].set()
        with pytest.raises(BaseExceptionGroup) as joined:
            await asyncio.wait_for(task, 5)
        roots = _leaves(joined.value)
        assert any(error is primary for error in roots), "successful close lost active body original"
        assert len(delivered) == 3 and len({id(cancel) for cancel in delivered}) == 3
        assert all(any(error is cancel for error in roots) for cancel in delivered)
        assert obligation.release_succeeded and calls[str(sid)] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("release_fault", [False, True])
async def test_actual_pretransfer_clean_close_resumes_body_and_fault_retains_both(actual, monkeypatch, release_fault):
    sid = _insert_session(actual.sessions._engine)
    obligation, lease = await actual.lease(sid)
    primary, sql_fault = ValueError("actual-body-original"), OSError("actual-release-original")
    authority = actual.sessions.session_operation_authority
    original = SQLiteLocalSessionOperationAuthority._locked_transaction

    @contextmanager
    def transaction(self, session_id):
        with original(self, session_id) as connection:
            yield connection
        if release_fault and self is authority and session_id == str(sid):
            raise sql_fault

    monkeypatch.setattr(SQLiteLocalSessionOperationAuthority, "_locked_transaction", transaction)

    async def body():
        try:
            raise primary
        finally:
            await close_execute_lease_before_transfer(lease, primary=primary)

    task = asyncio.create_task(body())
    actual.tasks.append(task)
    with pytest.raises((ValueError, BaseExceptionGroup)) as joined:
        await asyncio.wait_for(task, 5)
    roots = _leaves(joined.value)
    assert any(error is primary for error in roots)
    if release_fault:
        assert any(error is sql_fault for error in roots)
        actual.witnessed_cleanup_errors.append(sql_fault)
        assert not obligation.release_succeeded
    else:
        assert joined.value is primary and obligation.release_succeeded
    monkeypatch.setattr(SQLiteLocalSessionOperationAuthority, "_locked_transaction", original)


@contextmanager
def _hold_actual_run_read(monkeypatch, actual, fault=None):
    """Actual get_run SQL context exit, below canonical authority and API."""
    engine = actual.sessions._engine
    original = engine.begin
    original_run_sync = SessionServiceImpl._run_sync
    read_codes = [code for code in SessionServiceImpl.get_run.__code__.co_consts if isinstance(code, CodeType) and code.co_name == "_sync"]
    assert len(read_codes) == 1
    read_code = read_codes[0]
    in_run_read = ContextVar("execute_control_actual_run_read", default=False)
    entered, allowed, exited = threading.Event(), threading.Event(), threading.Event()
    actual.gates.append(allowed)
    reads = []

    @contextmanager
    def begin():
        with original() as connection:
            yield connection
        if not in_run_read.get():
            return
        reads.append(threading.get_ident())
        entered.set()
        try:
            assert allowed.wait(5), "actual SQL read gate was not released"
            if fault is not None:
                raise fault
        finally:
            exited.set()

    async def run_sync(service, function, *args, **kwargs):
        if service is not actual.sessions or not isinstance(function, FunctionType) or function.__code__ is not read_code:
            return await original_run_sync(service, function, *args, **kwargs)

        def actual_read(*call_args, **call_kwargs):
            token = in_run_read.set(True)
            try:
                return function(*call_args, **call_kwargs)
            finally:
                in_run_read.reset(token)

        return await original_run_sync(service, actual_read, *args, **kwargs)

    monkeypatch.setattr(SessionServiceImpl, "_run_sync", run_sync)
    monkeypatch.setattr(engine, "begin", begin)
    try:
        yield entered, allowed, exited, reads
    finally:
        allowed.set()
        monkeypatch.setattr(engine, "begin", original)
        monkeypatch.setattr(SessionServiceImpl, "_run_sync", original_run_sync)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["loss-wait", "sql-read"])
async def test_actual_watcher_cooperative_close_joins_initiated_read_without_next_poll(actual, monkeypatch, phase):
    obligation, lease = await actual.lease(_insert_session(actual.sessions._engine))
    with _hold_actual_run_read(monkeypatch, actual) as (entered, allowed, exited, reads):
        watcher = actual.service._create_loss_watcher(lease, threading.Event(), run_id=actual.runs[obligation.session_id])
        actual.tasks.append(watcher)
        await _until(lambda: actual.service._loss_watcher_owner(watcher, lease).task is watcher)
        if phase == "sql-read":
            await _until(entered.is_set)
        closing = asyncio.create_task(actual.service._close_execution_authority(lease, watcher, None))
        actual.tasks.append(closing)
        await _until(lambda: actual.service._loss_watcher_owner(watcher, lease).close_requested.is_set())
        if phase == "sql-read":
            assert not watcher.done() and not closing.done() and not obligation.release_succeeded
            allowed.set()
        await asyncio.wait_for(closing, 5)
        owner = actual.service._loss_watcher_owner(watcher, lease)
        assert owner.outcome == (watcher, None) and watcher.done()
        assert obligation.release_succeeded
        assert len(reads) == (1 if phase == "sql-read" else 0), "closed watcher initiated another authoritative read"
        if phase == "sql-read":
            assert exited.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault_kind", ["cancel-shaped", "sql-fault"])
async def test_actual_watcher_late_physical_original_survives_close_and_three_caller_cancels(actual, monkeypatch, fault_kind):
    obligation, lease = await actual.lease(_insert_session(actual.sessions._engine))
    # Same text a cancellation marker might use is not provenance. This is an
    # actual physical SQL context-exit outcome, not a watcher Task.cancel call.
    fault = asyncio.CancelledError("execution-private-close") if fault_kind == "cancel-shaped" else ValueError("actual-read-fault")
    delivered = []
    original_retain = async_workers._retain_cancellation
    with _hold_actual_run_read(monkeypatch, actual, fault) as (entered, allowed, exited, reads):
        watcher = actual.service._create_loss_watcher(lease, threading.Event(), run_id=actual.runs[obligation.session_id])
        actual.tasks.append(watcher)
        await _until(entered.is_set)
        closing = asyncio.create_task(actual.service._close_execution_authority(lease, watcher, None))
        actual.tasks.append(closing)

        def capture(cancellations, original):
            original_retain(cancellations, original)
            if asyncio.current_task() is closing:
                delivered.append(original)

        monkeypatch.setattr("elspeth.web.execution.service._retain_cancellation", capture)
        await _until(lambda: actual.service._loss_watcher_owner(watcher, lease).close_requested.is_set())
        for ordinal in range(3):
            closing.cancel(f"watcher-join-caller-{ordinal}")
            await _until(lambda count=ordinal + 1: len(delivered) == count)
        assert not closing.done() and not watcher.done() and not obligation.release_succeeded
        allowed.set()
        with pytest.raises(BaseExceptionGroup) as joined:
            await asyncio.wait_for(closing, 5)
        roots = _leaves(joined.value)
        assert any(error is fault for error in roots), "cooperative close discarded actual SQL original"
        assert actual.service._loss_watcher_owner(watcher, lease).outcome[1] is fault
        assert len(delivered) == 3 and len({id(cancel) for cancel in delivered}) == 3
        assert all(any(error is cancel for error in roots) for cancel in delivered)
        assert obligation.release_succeeded and exited.is_set() and len(reads) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_during_close", [False, True])
async def test_actual_external_watcher_cancel_retains_exact_producer_outcome(actual, cancel_during_close):
    obligation, lease = await actual.lease(_insert_session(actual.sessions._engine))
    watcher = actual.service._create_loss_watcher(lease, threading.Event(), run_id=actual.runs[obligation.session_id])
    actual.tasks.append(watcher)
    await asyncio.sleep(0)
    if cancel_during_close:
        closing = asyncio.create_task(actual.service._close_execution_authority(lease, watcher, None))
        actual.tasks.append(closing)
        await _until(lambda: actual.service._loss_watcher_owner(watcher, lease).close_requested.is_set())
        assert not watcher.done()
        watcher.cancel("actual-external-watcher-cancel")
    else:
        watcher.cancel("actual-external-watcher-cancel")
        await _until(watcher.done)
        closing = asyncio.create_task(actual.service._close_execution_authority(lease, watcher, None))
        actual.tasks.append(closing)
    with pytest.raises(asyncio.CancelledError) as joined:
        await asyncio.wait_for(closing, 5)
    owner = actual.service._loss_watcher_owner(watcher, lease)
    assert owner.outcome[0] is watcher and joined.value is owner.outcome[1]
    assert joined.value.args == ("actual-external-watcher-cancel",)
    assert obligation.release_succeeded


@pytest.mark.asyncio
async def test_actual_unknown_submit_handoff_survives_three_executor_observer_cancels_and_joins_siblings(actual, monkeypatch):
    ids = [_insert_session(actual.sessions._engine) for _ in range(2)]
    pairs = [await actual.lease(sid) for sid in ids]
    private = actual.service._executor
    entered, allowed = threading.Event(), threading.Event()
    actual.gates.append(allowed)
    invoked, submitted = [], []
    fault = OSError("actual-submit-no-return")
    production_submit = ThreadPoolExecutor.submit

    def queue_then_raise(executor, function, /, *args, **kwargs):
        future = production_submit(executor, function, *args, **kwargs)
        if executor is private:
            index = len(submitted)
            submitted.append(future)
            if index == 1:
                raise fault
        return future

    monkeypatch.setattr(ThreadPoolExecutor, "submit", queue_then_raise)
    for index, (obligation, lease) in enumerate(pairs):
        watcher = actual.service._create_loss_watcher(lease, threading.Event(), run_id=actual.runs[obligation.session_id])
        actual.tasks.append(watcher)

        def work(ordinal=index):
            invoked.append(ordinal)
            entered.set()
            assert allowed.wait(5), "actual private invocation was not released"
            return None

        if index == 0:
            actual.service._submit_owned_pipeline(obligation, lease, watcher, work)
        else:
            with pytest.raises(OSError) as raised:
                actual.service._submit_owned_pipeline(obligation, lease, watcher, work)
            assert raised.value is fault
    await _until(entered.is_set)
    actual.owner.seal()
    actual.recovery.instance_draining.set()
    delivered = []
    original_retain = async_workers._retain_cancellation

    def capture(cancellations, original):
        original_retain(cancellations, original)
        if asyncio.current_task() is actual.service.executor_join_task:
            delivered.append(original)

    monkeypatch.setattr(async_workers, "_retain_cancellation", capture)
    with _hold_transaction_return(monkeypatch, actual, ids) as (sql_entered, sql_allowed, calls):
        shutdown = asyncio.create_task(actual.service.shutdown())
        actual.tasks.append(shutdown)
        await _until(lambda: actual.service.executor_join_task is not None)
        executor_owner = actual.service.executor_join_task
        for ordinal in range(3):
            executor_owner.cancel(f"private-executor-observer-{ordinal}")
            await _until(lambda count=ordinal + 1: len(delivered) == count)
        assert not actual.registry.executor_join_succeeded
        assert all(obligation.completion_task is None and not obligation.release_succeeded for obligation, _ in pairs)
        allowed.set()
        await _until(executor_owner.done)
        assert actual.registry.executor_join_succeeded
        assert pairs[1][0].completion_task is not None, "observer cancellation skipped authorized submit cleanup"
        await _until(lambda: all(gate.is_set() for gate in sql_entered.values()))
        assert pairs[0][0].completion_task is not None
        assert all(not obligation.release_succeeded for obligation, _ in pairs)
        assert not shutdown.done()
        sql_allowed[str(ids[0])].set()
        await _until(lambda: (actual.registry.observe_ready(), pairs[0][0].retired)[1])
        assert not pairs[1][0].retired and not shutdown.done()
        sql_allowed[str(ids[1])].set()
        with pytest.raises(BaseExceptionGroup) as joined:
            await asyncio.wait_for(shutdown, 5)
        roots = _leaves(joined.value)
        assert any(error is fault for error in roots)
        assert len(delivered) == 3 and len({id(cancel) for cancel in delivered}) == 3
        assert all(any(error is cancel for error in roots) for cancel in delivered)
        assert invoked == [0, 1] and len(submitted) == 2, "unknown private invocation replayed"
        assert pairs[0][0].pipeline is submitted[0] and pairs[1][0].pipeline is None
        assert all(obligation.release_succeeded and obligation.retired for obligation, _ in pairs)
        assert all(calls[str(sid)] == 1 for sid in ids)
        with pytest.raises(BaseExceptionGroup):
            actual.registry.assert_completed()


@pytest.mark.asyncio
async def test_actual_unknown_submit_with_failed_executor_receipt_cannot_handoff_release(actual, monkeypatch):
    obligation, lease = await actual.lease(_insert_session(actual.sessions._engine))
    private = actual.service._executor
    submit_fault, join_fault = OSError("actual-submit-no-return"), OSError("actual-private-join-fault")
    production_submit, production_shutdown = ThreadPoolExecutor.submit, ThreadPoolExecutor.shutdown
    invoked = []

    def submit(executor, function, /, *args, **kwargs):
        future = production_submit(executor, function, *args, **kwargs)
        if executor is private:
            raise submit_fault
        return future

    def join(executor, *args, **kwargs):
        production_shutdown(executor, *args, **kwargs)
        if executor is private:
            raise join_fault

    monkeypatch.setattr(ThreadPoolExecutor, "submit", submit)
    monkeypatch.setattr(ThreadPoolExecutor, "shutdown", join)
    watcher = actual.service._create_loss_watcher(lease, threading.Event(), run_id=actual.runs[obligation.session_id])
    actual.tasks.append(watcher)
    with pytest.raises(OSError) as submitted:
        actual.service._submit_owned_pipeline(obligation, lease, watcher, lambda: invoked.append("actual-once"))
    assert submitted.value is submit_fault
    actual.owner.seal()
    actual.recovery.instance_draining.set()
    # Observe the actual executor owner directly: a registry join is correctly
    # unresolved here. Do not turn a Task timeout into completion evidence.
    joining = asyncio.create_task(actual.service._join_executor_owner())
    actual.tasks.append(joining)
    with pytest.raises(OSError) as joined:
        await asyncio.wait_for(joining, 5)
    assert joined.value is join_fault and invoked == ["actual-once"]
    assert not actual.registry.executor_join_succeeded
    assert obligation.pipeline is None and obligation.completion_task is None
    assert not obligation.release_succeeded and actual.registry.has_pending_physical_owners()
    with pytest.raises((AuditIntegrityError, BaseExceptionGroup)):
        actual.registry.assert_completed()


def _active_service_custody(service):
    return {
        "pipeline_owners": len(service._pipeline_completion_owners),
        "pipeline_started": len(service._pipeline_completion_started),
        "pipeline_watchers": len(service._pipeline_watchers),
        "failed_submission_cleanup": len(service._failed_submission_cleanup_started),
        "watcher_owners": len(service._loss_watcher_owners),
        "completion_wrappers": len(service._lease_completion_futures),
    }


@pytest.mark.asyncio
async def test_actual_successive_execution_retirement_releases_service_roots_preserves_delayed_duplicates(actual, monkeypatch):
    baseline = _active_service_custody(actual.service)
    assert baseline == dict.fromkeys(baseline, 0)
    observed_after_join, invocations, retained = [], [], []
    original = ExecutionServiceImpl._observe_execution_completion

    def observe(service, obligation, task=None):
        original(service, obligation, task)
        # Observe only after the actual shipped bookkeeper has returned and
        # the actual source receipts are consumed. No collection predicate
        # gates this signal: an omitted retirement must reach the assertion.
        if (
            obligation.retired
            and obligation.completion_task is not None
            and obligation.completion_task.done()
            and obligation.completion is not None
            and obligation.completion.done()
        ):
            observed_after_join.append(obligation)

    monkeypatch.setattr(ExecutionServiceImpl, "_observe_execution_completion", observe)
    for ordinal in range(3):
        obligation, lease = await actual.lease(_insert_session(actual.sessions._engine))
        watcher = actual.service._create_loss_watcher(lease, threading.Event(), run_id=actual.runs[obligation.session_id])
        owner = actual.service._loss_watcher_owner(watcher, lease)
        actual.tasks.append(watcher)

        def work(index=ordinal):
            invocations.append(index)
            return None

        actual.service._submit_owned_pipeline(obligation, lease, watcher, work)
        await _until(lambda cap=obligation: any(item is cap for item in observed_after_join))
        assert _active_service_custody(actual.service) == baseline, "successful execution retained service-owned history"
        assert lease.closed and obligation.release_succeeded and obligation.retired
        assert owner.outcome == (watcher, None) and watcher.done()
        assert obligation.completion_outcome_recorded and obligation.completion_original_error is None
        assert obligation.lifecycle_outcome_recorded and obligation.lifecycle_original_error is None
        retained.append((obligation, lease, watcher, owner, obligation.completion_task, obligation.completion))
    # Strong external references above keep all original objects alive. The
    # empty service census therefore proves retirement rather than GC timing.
    assert invocations == [0, 1, 2]
    for obligation, lease, watcher, owner, completion_task, wrapper in retained:
        actual.service._on_pipeline_done(obligation.pipeline, session_operation_lease=lease, loss_watcher=watcher)
        assert obligation.completion_task is completion_task and obligation.completion is wrapper
        assert _active_service_custody(actual.service) == baseline
        assert owner.outcome == (watcher, None)
    assert invocations == [0, 1, 2], "retired duplicate callback replayed a physical invocation"
    foreign = Future()
    foreign.set_result(None)
    obligation, lease, _watcher, _owner, completion_task, wrapper = retained[-1]
    with pytest.raises(AuditIntegrityError):
        actual.service._on_pipeline_done(foreign, session_operation_lease=lease)
    assert obligation.completion_task is completion_task and obligation.completion is wrapper
    assert _active_service_custody(actual.service) == baseline
    assert invocations == [0, 1, 2]


@pytest.mark.asyncio
async def test_actual_held_release_stays_rooted_until_all_receipts_are_consumed(actual, monkeypatch):
    sid = _insert_session(actual.sessions._engine)
    obligation, lease = await actual.lease(sid)
    with _hold_transaction_return(monkeypatch, actual, [sid]) as (entered, allowed, calls):
        actual.dispatch(obligation, lease)
        watcher = actual.service._pipeline_watchers[obligation]
        owner = actual.service._loss_watcher_owner(watcher, lease)
        await _until(entered[str(sid)].is_set)
        assert obligation.pipeline.done() and watcher.done()
        assert obligation.release_submission.future is not None and not obligation.release_submission.future.done()
        assert obligation.completion_task is not None and not obligation.completion_task.done()
        # Challenge the actual readiness bookkeeper at this held phase. This
        # does not claim that a terminal completion callback arrived early.
        actual.service._observe_execution_completion(obligation)
        assert obligation in actual.service._pipeline_watchers, "held physical release lost service-owned custody"
        assert obligation.pipeline in actual.service._pipeline_completion_owners
        assert owner in actual.service._loss_watcher_owners
        assert not obligation.retired and actual.registry.has_pending_physical_owners()
        actual.registry.seal()
        with pytest.raises(AuditIntegrityError):
            actual.registry.assert_completed()
        allowed[str(sid)].set()
        await _until(lambda: (actual.registry.observe_ready(), obligation.retired)[1])
        await _until(lambda: _active_service_custody(actual.service) == dict.fromkeys(_active_service_custody(actual.service), 0))
        assert obligation.release_succeeded and lease.closed and calls[str(sid)] == 1
