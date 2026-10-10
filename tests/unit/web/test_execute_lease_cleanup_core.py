"""Unexecuted actual-SQL EXECUTE registry custody controls."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import MethodType

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web import async_workers
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.execution_lease_cleanup import ExecutionLeaseReleaseRegistry
from elspeth.web.sessions.models import session_operation_fences_table
from tests.helpers import composer_operations
from tests.helpers.composer_operations import build_composer_operation_app


class _CoreLifecycleOwner:
    """One exact app and its physical shared worker owners for one test."""

    def __init__(self) -> None:
        self.factory_attempts = 0
        self.app = None
        self.recovery = None
        self.generation = None
        self.shared_before = None
        self.body_original: BaseException | None = None
        self.shutdown_issuance_started = False
        self.shutdown_task: asyncio.Task[None] | None = None
        self.unresolved = False


class _CoreBodyReport:
    def __init__(self, item, owner: _CoreLifecycleOwner) -> None:
        self.item = item
        self.owner = owner

    def pytest_runtest_makereport(self, item, call) -> None:
        if item is self.item and call.when == "call" and call.excinfo is not None:
            self.owner.body_original = call.excinfo.value


async def _observe_core_task(task: asyncio.Task[None], originals: list[BaseException]) -> bool:
    # wait() observes completion without propagating a cancelled producer's
    # exception through the wait. Only caller cancellation reaches this catch.
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError as original:
            originals.append(original)
        except BaseException as original:
            originals.append(original)
            break
    if not task.done():
        return False
    try:
        task.result()
    except BaseException as original:
        if not any(original is prior for prior in originals):
            originals.append(original)
    return True


async def _join_known_core_task(task: asyncio.Task[None], originals: list[BaseException]) -> None:
    """Finish custody of one published Task after its first observation failed."""
    while not task.done():
        try:
            # shield keeps the exact strongly referenced producer alive when
            # the fixture caller is cancelled; it does not issue a new Task.
            await asyncio.shield(task)
        except BaseException as original:
            if not any(original is prior for prior in originals):
                originals.append(original)
            if not task.done():
                # A broken wait seam may fail without yielding. Give the
                # already-issued producer a separate scheduling opportunity.
                try:
                    await asyncio.sleep(0)
                except BaseException as yield_original:
                    if not any(yield_original is prior for prior in originals):
                        originals.append(yield_original)
    try:
        task.result()
    except BaseException as original:
        if not any(original is prior for prior in originals):
            originals.append(original)


async def _join_core_drain_thread(thread: threading.Thread, originals: list[BaseException]) -> None:
    if thread.ident is None:
        raise AssertionError("Core generation drain thread has no start receipt")
    while thread.is_alive():
        try:
            await asyncio.sleep(0.01)
        except asyncio.CancelledError as original:
            originals.append(original)
    thread.join(timeout=0)
    if thread.is_alive():
        raise AssertionError("Core generation drain thread remained live after join")


async def _cleanup_core_lifecycle(owner: _CoreLifecycleOwner) -> list[BaseException]:
    originals: list[BaseException] = []
    app = owner.app
    generation = async_workers._GENERATION_CUSTODIAN
    shared = async_workers._SHARED_EXECUTOR
    owner.generation = generation
    owner.shared_before = shared
    recovery = owner.recovery
    if owner.factory_attempts == 0 and app is None:
        if generation is None and shared is None:
            return originals
        originals.append(AssertionError("Core test without an app inherited an unowned shared generation"))
        owner.unresolved = True
    elif owner.factory_attempts != 1 or app is None or recovery is None:
        originals.append(AssertionError("Core app allocation outcome Unknown before session seed"))
        owner.unresolved = True
    else:
        try:
            callback = async_workers._RECOVERY_CALLBACK
            if (
                async_workers._APPLICATION_FINALIZER_OWNER is not app.state.application_finalizer_owner
                or async_workers._INSTANCE_DRAINING is not recovery.instance_draining
                or not isinstance(callback, MethodType)
                or callback.__self__ is not recovery
            ):
                raise AssertionError("Core app lost its exact shared recovery owner")
            if generation is not None and (
                generation.instance_draining is not recovery.instance_draining
                or generation.generation_unavailable is not async_workers._GENERATION_UNAVAILABLE
            ):
                raise AssertionError("Core generation belongs to another recovery owner")
            if generation is not None and shared is not generation.executor and generation.state != "closed":
                raise AssertionError("Core shared executor replacement lacks completed old generation")
        except BaseException as original:
            originals.append(original)
            owner.unresolved = True
    if generation is not None:
        observer = generation.observer
        thread = generation.drain_thread
        if observer is not None:
            try:
                observed = await _observe_core_task(observer, originals)
            except BaseException as original:
                originals.append(original)
                observed = False
            if not observed:
                await _join_known_core_task(observer, originals)
                originals.append(AssertionError("Core generation observer outcome remains unobserved"))
                owner.unresolved = True
        if thread is not None:
            try:
                await _join_core_drain_thread(thread, originals)
            except BaseException as original:
                originals.append(original)
                owner.unresolved = True
        if generation.state == "quarantined" or thread is not None:
            if observer is None or thread is None or not generation.joined.is_set() or not generation.recovery_finished.is_set():
                originals.append(AssertionError("Core quarantined generation lacks actual observer, thread or join receipt"))
                owner.unresolved = True
            if generation.drain_error is not None:
                originals.append(generation.drain_error)
                owner.unresolved = True
            if generation.observer_error is not None:
                originals.append(generation.observer_error)
    if recovery is not None:
        try:
            # Flush already-scheduled record_failure callbacks before the one
            # capture of the exact app escalation Task.
            await asyncio.sleep(0)
        except asyncio.CancelledError as original:
            originals.append(original)
        escalation = recovery._escalation_task
        if recovery._shutdown_started and escalation is None:
            originals.append(AssertionError("Core recovery escalation issuance returned no Task"))
            owner.unresolved = True
        elif escalation is not None:
            try:
                observed = await _observe_core_task(escalation, originals)
            except BaseException as original:
                originals.append(original)
                observed = False
            if not observed:
                await _join_known_core_task(escalation, originals)
                originals.append(AssertionError("Core recovery escalation outcome remains unobserved"))
                owner.unresolved = True
        if recovery._monitor_task is not None:
            originals.append(AssertionError("Core app unexpectedly started a lifespan monitor"))
            owner.unresolved = True
    # A failed drain may leave joined unset forever. The real shared shutdown
    # waits for that event, so do not claim a detached executor by hanging it.
    can_shutdown = generation is None or generation.state != "quarantined" or generation.joined.is_set()
    if can_shutdown:
        owner.shutdown_issuance_started = True
        try:
            coroutine = async_workers.shutdown_async_workers()
        except BaseException as original:
            originals.append(original)
            owner.unresolved = True
        else:
            try:
                owner.shutdown_task = asyncio.create_task(coroutine, name="core-shared-worker-shutdown")
            except BaseException as original:
                originals.append(original)
                owner.unresolved = True
                try:
                    coroutine.close()
                except BaseException as close_original:
                    originals.append(close_original)
            else:
                try:
                    observed = await _observe_core_task(owner.shutdown_task, originals)
                except BaseException as original:
                    originals.append(original)
                    observed = False
                if not observed:
                    await _join_known_core_task(owner.shutdown_task, originals)
                    originals.append(AssertionError("Core shared shutdown Task outcome remains unobserved"))
                    owner.unresolved = True
                if async_workers._SHARED_EXECUTOR is not None:
                    originals.append(AssertionError("Core shared executor remained installed after shutdown"))
                    owner.unresolved = True
    else:
        originals.append(AssertionError("Core shared shutdown cannot await a missing generation join receipt"))
        owner.unresolved = True
    if app is not None and not owner.unresolved:
        try:
            app.state.session_engine.dispose()
        except BaseException as original:
            originals.append(original)
    return originals


@pytest_asyncio.fixture(autouse=True)
async def _owned_core_lifecycle(explicit_required_executor_recovery, monkeypatch, request):
    """Join the module's actual app/shared physical owners before root undo."""
    owner = _CoreLifecycleOwner()
    actual_create_app = composer_operations.create_app

    def capture_created_app(*args, **kwargs):
        owner.factory_attempts += 1  # Before a no-return factory allocation.
        if owner.factory_attempts != 1:
            raise AssertionError("Core control constructed more than one app")
        app = actual_create_app(*args, **kwargs)
        owner.app = app  # Before build_composer_operation_app seeds a session.
        owner.recovery = app.state.process_recovery
        return app

    report = _CoreBodyReport(request.node, owner)
    plugin_name = f"core-physical-lifecycle-{id(owner)}"
    request.config.pluginmanager.register(report, name=plugin_name)
    try:
        monkeypatch.setattr(composer_operations, "create_app", capture_created_app)
        yield
    finally:
        originals: list[BaseException] = []
        try:
            originals.extend(await _cleanup_core_lifecycle(owner))
        except BaseException as original:
            originals.append(original)
            owner.unresolved = True
        try:
            request.config.pluginmanager.unregister(report)
        except BaseException as original:
            originals.append(original)
        if owner.unresolved:
            request.session.shouldstop = "Core physical lifecycle custody is unresolved"
        if originals:
            if owner.body_original is not None:
                originals.insert(0, owner.body_original)
            raise BaseExceptionGroup("Core body and physical lifecycle originals", originals)


async def _core(tmp_path):
    fixture = await build_composer_operation_app(tmp_path, timeout_seconds=30.0)
    app = fixture.app
    registry = ExecutionLeaseReleaseRegistry(
        owner=app.state.application_finalizer_owner,
        recovery=app.state.process_recovery,
        loop=asyncio.get_running_loop(),
    )
    assert registry.owner is async_workers._APPLICATION_FINALIZER_OWNER
    assert registry.recovery.instance_draining is async_workers._INSTANCE_DRAINING
    return fixture, registry


async def _lease(fixture, registry, session_id):
    service = fixture.app.state.session_service
    obligation = registry.admit(
        service.session_operation_authority,
        session_id=session_id,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=30,
    )
    lease = await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=session_id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=30,
        execution_obligation=obligation,
    )
    return obligation, lease


@pytest.mark.asyncio
async def test_actual_commit_then_acquire_handoff_failure_retains_unknown_authority(tmp_path, monkeypatch):
    fixture, registry = await _core(tmp_path)
    service = fixture.app.state.session_service
    original = OperationalError("local controlled acknowledgment failure", {}, RuntimeError("offline acknowledgment"))
    original_transaction = SQLiteLocalSessionOperationAuthority._locked_transaction
    commits = []

    @contextmanager
    def fail_after_real_commit(self, session_id):
        with original_transaction(self, session_id) as connection:
            yield connection
        commits.append(session_id)
        raise original

    monkeypatch.setattr(SQLiteLocalSessionOperationAuthority, "_locked_transaction", fail_after_real_commit)
    obligation = registry.admit(
        service.session_operation_authority,
        session_id=fixture.session_id,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=30,
    )
    try:
        with pytest.raises(OperationalError) as failure:
            await SessionOperationLease.acquire(
                service.session_operation_authority,
                session_id=fixture.session_id,
                operation_kind=SessionOperationKind.EXECUTE,
                owner_instance_id=service.session_operation_owner_instance_id,
                lease_seconds=30,
                execution_obligation=obligation,
            )
        assert failure.value is original
        assert commits == [str(fixture.session_id)]
        submission = obligation.acquire_submission
        assert submission.future is not None and submission.future.done()
        assert submission.reservation is not None and submission.reservation.released
        assert submission.original_error is original
        assert submission.domain_refusal is None
        assert submission.source_value is None and obligation.context is None
        with fixture.app.state.session_engine.connect() as connection:
            fence = (
                connection.execute(
                    select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(fixture.session_id))
                )
                .mappings()
                .one()
            )
        assert fence["operation_kind"] == SessionOperationKind.EXECUTE.value
        assert fence["released_at"] is None
        registry.seal()
        for _ in range(3):
            assert any(error is original for error in registry.observe_ready())
        assert registry.has_pending_physical_owners()
        assert not obligation.retired
        assert async_workers.outstanding_admissions() == 0
        with pytest.raises(BaseExceptionGroup) as retained:
            registry.assert_completed()
        assert any(error is original for error in retained.value.exceptions)
        await fixture.app.state.process_recovery.join_escalation()
        assert not fixture.app.state.process_recovery.watchdog.completed
    finally:
        # Temporary DB still contains the intentionally unknown authority.
        # No reconstructed context, fabricated release or COMPLETE is issued.
        fixture.app.state.session_engine.dispose()


@pytest.mark.asyncio
async def test_bad_first_actual_completion_proof_does_not_skip_held_second_release(tmp_path, monkeypatch):
    fixture, registry = await _core(tmp_path)
    service = fixture.app.state.session_service
    second_session = await service.create_session("transport-user", "Second actual owner", "local")
    first, first_lease = await _lease(fixture, registry, fixture.session_id)
    second, second_lease = await _lease(fixture, registry, second_session.id)
    execution_executor = ThreadPoolExecutor(max_workers=1)
    producer_original = RuntimeError("actual producer result deliberately disagrees")
    with first.pipeline_submission_decision(first_lease.context):
        pipeline = execution_executor.submit(lambda: None)
        first.bind_pipeline_future(pipeline)

    async def bad_completion():
        actual_task = asyncio.current_task()
        assert actual_task is not None
        first.bind_completion_task(actual_task)
        first.record_completion_outcome(producer_original)
        # A genuine corruption control: saved failure but actual Task returns.

    bad = asyncio.create_task(bad_completion())
    await bad
    entered = threading.Event()
    release = threading.Event()
    original_transaction = SQLiteLocalSessionOperationAuthority._locked_transaction

    @contextmanager
    def hold_second_actual_release(self, session_id):
        with original_transaction(self, session_id) as connection:
            if session_id == str(second_session.id):
                entered.set()
                release.wait()
            yield connection

    monkeypatch.setattr(SQLiteLocalSessionOperationAuthority, "_locked_transaction", hold_second_actual_release)
    closing = asyncio.create_task(second_lease.close())
    body_error = None
    try:
        while not entered.is_set():
            await asyncio.sleep(0.001)
        assert async_workers.outstanding_admissions() == 1
        registry.seal()
        faults = registry.observe_ready()
        assert any(isinstance(error, AuditIntegrityError) for error in faults)
        assert not first.completion_observed
        assert not closing.done()
        assert second.release_submission is not None and not second.release_submission.observed
        first_faults = tuple(faults)
        for _ in range(3):
            current = registry.observe_ready()
            assert len(current) == len(first_faults)
            assert all(left is right for left, right in zip(current, first_faults, strict=True))
        release.set()
        await closing
        registry.observe_ready()
        assert second.lifecycle_observed and second.release_succeeded and second.retired
        assert not first.retired
        assert registry.has_pending_physical_owners()
        assert async_workers.outstanding_admissions() == 0
        with pytest.raises(BaseExceptionGroup):
            registry.assert_completed()
        await fixture.app.state.process_recovery.join_escalation()
        assert not fixture.app.state.process_recovery.watchdog.completed
    except BaseException as original:
        body_error = original
        raise
    finally:
        release.set()
        cleanup_errors = []
        await asyncio.gather(closing, return_exceptions=True)
        # Every actual sibling cleanup is attempted even if the first fails.
        # Its bad receipt remains pending; cleanup cannot issue COMPLETE.
        try:
            await first_lease.close()
        except BaseException as original:
            cleanup_errors.append(original)
        try:
            execution_executor.shutdown(wait=True, cancel_futures=False)
        except BaseException as original:
            cleanup_errors.append(original)
        try:
            fixture.app.state.session_engine.dispose()
        except BaseException as original:
            cleanup_errors.append(original)
        if cleanup_errors:
            originals = ([body_error] if body_error is not None else []) + cleanup_errors
            if len(originals) == 1:
                raise originals[0]
            raise BaseExceptionGroup("Core control retained body and independent cleanup originals", originals)


@pytest.mark.asyncio
@pytest.mark.parametrize("hold_stage", ["before_release", "inside_counter_release"])
async def test_actual_domain_refusal_census_waits_for_held_release_callback(tmp_path, monkeypatch, hold_stage):
    from elspeth.web.required_executor import InvocationReservation
    from elspeth.web.required_sql_outcomes import RequiredSQLRaised

    fixture, registry = await _core(tmp_path)
    service = fixture.app.state.session_service
    authority = service.session_operation_authority
    existing = authority.acquire(
        session_id=fixture.session_id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=30,
    )
    obligation = registry.admit(
        authority,
        session_id=fixture.session_id,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=30,
    )
    entered = threading.Event()
    release = threading.Event()
    original_callback = InvocationReservation.release_future

    def held_actual_callback(self, future):
        if self is obligation.acquire_submission.reservation:
            entered.set()
            release.wait()
        return original_callback(self, future)

    if hold_stage == "before_release":
        monkeypatch.setattr(InvocationReservation, "release_future", held_actual_callback)
    else:
        original_counter_release = async_workers._release_admission

        def held_counter_release():
            entered.set()
            release.wait()
            original_counter_release()

        monkeypatch.setattr(async_workers, "_release_admission", held_counter_release)
    bridge = asyncio.create_task(async_workers.run_execution_lease_sql_finish_once(obligation.acquire_submission))
    try:
        while not entered.is_set():
            await asyncio.sleep(0.001)
        submission = obligation.acquire_submission
        assert submission.future is not None and submission.future.done()
        assert submission.domain_refusal is not None
        assert submission.reservation is not None
        assert submission.reservation.released is (hold_stage == "inside_counter_release")
        assert not submission.callback_return_observed
        assert async_workers.outstanding_admissions() == 1
        for _ in range(3):
            registry.observe_ready()
            assert not submission.observed and not obligation.retired
            assert registry.has_pending_physical_owners()
        release.set()
        outcome = await bridge
        assert type(outcome) is RequiredSQLRaised
        assert outcome.error is submission.domain_refusal
        assert submission.callback_return_observed
        assert submission.reservation is not None and submission.reservation.released
        registry.observe_ready()
        assert submission.observed and obligation.retired
        assert async_workers.outstanding_admissions() == 0
    finally:
        release.set()
        await asyncio.gather(bridge, return_exceptions=True)
        authority.release(existing)
        fixture.app.state.session_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["lambda", "foreign_bound"])
async def test_issued_release_invoke_rebinding_refused_before_sql_or_admission(tmp_path, monkeypatch, variant):
    fixture, registry = await _core(tmp_path)
    obligation, lease = await _lease(fixture, registry, fixture.session_id)
    submission = obligation.issue_release(lease.context)
    calls = []

    def false_release():
        calls.append("impostor")
        return None

    original = submission.invoke
    replacement = false_release if variant == "lambda" else obligation.acquire_submission.invoke
    monkeypatch.setattr(submission, "invoke", replacement)
    try:
        with pytest.raises(AuditIntegrityError):
            await async_workers.run_execution_lease_sql_finish_once(submission)
        assert calls == []
        assert submission.future is None and submission.reservation is None
        assert not submission.observed and not obligation.release_succeeded
        assert async_workers.outstanding_admissions() == 0
    finally:
        monkeypatch.setattr(submission, "invoke", original)
        lease._stop_renewal.set()
        if lease._renewal_task is not None:
            await lease._renewal_task
        outcome = await async_workers.run_execution_lease_sql_finish_once(submission)
        from elspeth.web.required_sql_outcomes import RequiredSQLReturned

        assert type(outcome) is RequiredSQLReturned
        # The actual already-issued release was joined directly; no second
        # release or fabricated lifecycle-completion receipt is issued.
        fixture.app.state.session_engine.dispose()


@pytest.mark.asyncio
async def test_cancelled_actual_acquire_late_release_waits_for_callback_before_retirement(tmp_path, monkeypatch):
    from elspeth.web.required_executor import InvocationReservation

    fixture, registry = await _core(tmp_path)
    service = fixture.app.state.session_service
    obligation = registry.admit(
        service.session_operation_authority,
        session_id=fixture.session_id,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=30,
    )
    acquire_entered, release_acquire = threading.Event(), threading.Event()
    callback_entered, release_callback = threading.Event(), threading.Event()
    delivered = []
    original_transaction = SQLiteLocalSessionOperationAuthority._locked_transaction
    original_callback = InvocationReservation.release_future
    original_retain = async_workers._retain_cancellation

    @contextmanager
    def hold_acquisition(self, session_id):
        with original_transaction(self, session_id) as connection:
            if obligation.acquire_submission.source_value is None:
                acquire_entered.set()
                release_acquire.wait()
            yield connection

    def hold_late_release_callback(self, future):
        submission = obligation.release_submission
        if submission is not None and self is submission.reservation:
            callback_entered.set()
            release_callback.wait()
        return original_callback(self, future)

    def retain_original(target, original):
        original_retain(target, original)
        if all(original is not old for old in delivered):
            delivered.append(original)

    monkeypatch.setattr(SQLiteLocalSessionOperationAuthority, "_locked_transaction", hold_acquisition)
    monkeypatch.setattr(InvocationReservation, "release_future", hold_late_release_callback)
    monkeypatch.setattr(async_workers, "_retain_cancellation", retain_original)
    acquiring = asyncio.create_task(
        SessionOperationLease.acquire(
            service.session_operation_authority,
            session_id=fixture.session_id,
            operation_kind=SessionOperationKind.EXECUTE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=30,
            execution_obligation=obligation,
        )
    )
    try:
        while not acquire_entered.is_set():
            await asyncio.sleep(0.001)
        for index in range(3):
            acquiring.cancel(f"actual late-acquire original {index}")
            while len(delivered) != index + 1:
                await asyncio.sleep(0.001)
        release_acquire.set()
        while not callback_entered.is_set():
            await asyncio.sleep(0.001)
        submission = obligation.release_submission
        assert submission is not None and submission.future is not None and submission.future.done()
        assert obligation.lease is None
        assert not obligation.lifecycle_required and not obligation.completion_required
        for _ in range(3):
            registry.observe_ready()
            assert not obligation.retired and not submission.observed
            assert registry.has_pending_physical_owners()
            assert async_workers.outstanding_admissions() == 1
        release_callback.set()
        with pytest.raises(BaseExceptionGroup) as failure:
            await acquiring
        assert len(failure.value.exceptions) == 3
        assert all(left is right for left, right in zip(failure.value.exceptions, delivered, strict=True))
        registry.observe_ready()
        assert obligation.retired and obligation.release_succeeded
        assert async_workers.outstanding_admissions() == 0
    finally:
        release_acquire.set()
        release_callback.set()
        await asyncio.gather(acquiring, return_exceptions=True)
        fixture.app.state.session_engine.dispose()


@pytest.mark.asyncio
async def test_actual_arm_then_setup_raise_retains_setup_and_sql_outcome(tmp_path, monkeypatch):
    from elspeth.web.required_executor import RequiredInvocationWitness
    from elspeth.web.required_sql_outcomes import RequiredSQLRaised

    fixture, registry = await _core(tmp_path)
    service = fixture.app.state.session_service
    obligation = registry.admit(
        service.session_operation_authority,
        session_id=fixture.session_id,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=30,
    )
    original_arm = RequiredInvocationWitness.arm
    setup_original = RuntimeError("actual arm published then setup failed")
    calls = []

    def publish_then_raise(self):
        original_arm(self)
        if obligation.acquire_submission.reservation is not None and self is obligation.acquire_submission.reservation.witness:
            calls.append(self)
            raise setup_original

    monkeypatch.setattr(RequiredInvocationWitness, "arm", publish_then_raise)
    try:
        outcome = await async_workers.run_execution_lease_sql_finish_once(obligation.acquire_submission)
        assert type(outcome) is RequiredSQLRaised and outcome.error is setup_original
        submission = obligation.acquire_submission
        assert len(calls) == 1
        assert submission.future is not None and submission.future.done()
        assert submission.callback_return_observed and submission.observed
        assert submission.reservation is not None and submission.reservation.released
        assert submission.reservation.witness.snapshot().callable_started
        assert submission.source_value is obligation.context and obligation.context is not None
        assert registry.has_pending_physical_owners() and not obligation.retired
        assert any(error is setup_original for error in registry.observe_ready())
        assert async_workers.outstanding_admissions() == 0
        while not obligation.generation.joined.is_set():
            await asyncio.sleep(0.001)
        assert not fixture.app.state.process_recovery.watchdog.completed
    finally:
        # The test received the exact canonical context, so it can release its
        # temporary DB resource. This is not a registry receipt or COMPLETE.
        if obligation.context is not None:
            service.session_operation_authority.release(obligation.context)
        fixture.app.state.session_engine.dispose()


@pytest.mark.asyncio
async def test_actual_executor_finalizer_receipt_waits_for_counter_callback_return(tmp_path, monkeypatch):
    from elspeth.web.application_finalizers import ApplicationFinalizerKind

    fixture, registry = await _core(tmp_path)
    executor = None
    capability = None
    shutdown_returns = []

    def join_executor():
        assert executor is not None and capability is not None
        executor.shutdown(wait=True, cancel_futures=False)
        shutdown_returns.append(executor)
        registry.record_executor_join_return(capability, executor)

    capability = registry.owner.register(ApplicationFinalizerKind.EXECUTION_EXECUTOR_JOIN, join_executor)
    registry.bind_executor_finalizer(capability)
    registry.declare_executor_allocation(capability)
    executor = ThreadPoolExecutor(max_workers=1)
    registry.bind_execution_executor(capability, executor)
    entered, release = threading.Event(), threading.Event()
    original_release = async_workers._release_admission

    def held_counter_release():
        entered.set()
        release.wait()
        original_release()

    monkeypatch.setattr(async_workers, "_release_admission", held_counter_release)
    registry.seal()
    registry.owner.seal()
    fixture.app.state.process_recovery.begin_shutdown()
    joining = asyncio.create_task(async_workers.run_application_finalizer_in_worker(capability))
    try:
        while not entered.is_set():
            await asyncio.sleep(0.001)
        assert shutdown_returns == [executor]
        assert capability._physical_future is not None and capability._physical_future.done()
        assert capability._physical_reservation is not None and capability._physical_reservation.released
        assert not capability.physical_completion_known
        assert not registry.executor_join_succeeded
        assert async_workers.outstanding_admissions() == 1
        assert not joining.done()
        release.set()
        await joining
        assert registry.executor_join_succeeded
        assert capability.physical_completion_known and capability.physical_completion_succeeded
        assert async_workers.outstanding_admissions() == 0
        registry.assert_completed()
        assert not fixture.app.state.process_recovery.watchdog.completed
    finally:
        release.set()
        await asyncio.gather(joining, return_exceptions=True)
        executor.shutdown(wait=True, cancel_futures=False)
        fixture.app.state.session_engine.dispose()


@pytest.mark.asyncio
async def test_actual_issued_release_closes_after_registry_seal_and_process_drain(tmp_path):
    fixture, registry = await _core(tmp_path)
    obligation, lease = await _lease(fixture, registry, fixture.session_id)
    registry.seal()
    fixture.app.state.process_recovery.begin_shutdown()
    try:
        await lease.close()
        registry.observe_ready()
        submission = obligation.release_submission
        assert submission is not None and submission.callback_return_observed
        assert submission.observed and obligation.release_succeeded and obligation.retired
        assert obligation.lifecycle_observed
        assert fixture.app.state.process_recovery.instance_draining.is_set()
        assert async_workers.outstanding_admissions() == 0
        with fixture.app.state.session_engine.connect() as connection:
            fence = (
                connection.execute(
                    select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(fixture.session_id))
                )
                .mappings()
                .one()
            )
        assert fence["released_at"] is not None
        assert not fixture.app.state.process_recovery.watchdog.completed
    finally:
        await lease.close()
        fixture.app.state.session_engine.dispose()


@pytest.mark.asyncio
async def test_copied_issued_sql_carrier_cannot_borrow_registered_release(tmp_path):
    from copy import copy

    fixture, registry = await _core(tmp_path)
    obligation, lease = await _lease(fixture, registry, fixture.session_id)
    actual = obligation.issue_release(lease.context)
    copied = copy(actual)
    try:
        with pytest.raises(AuditIntegrityError):
            await async_workers.run_execution_lease_sql_finish_once(copied)
        assert actual.future is None and actual.reservation is None
        assert not actual.observed and not obligation.release_succeeded
        assert async_workers.outstanding_admissions() == 0
        await lease.close()
        registry.observe_ready()
        assert obligation.retired and obligation.release_succeeded
    finally:
        await lease.close()
        fixture.app.state.session_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["_acquire", "_release", "context"])
async def test_issued_release_refuses_replaced_authority_or_context_before_admission(tmp_path, monkeypatch, field):
    from dataclasses import replace

    fixture, registry = await _core(tmp_path)
    obligation, lease = await _lease(fixture, registry, fixture.session_id)
    submission = obligation.issue_release(lease.context)
    actual = {"_acquire": obligation._acquire, "_release": obligation._release, "context": obligation.context}[field]
    calls = []

    def substituted(*args, **kwargs):
        calls.append((args, kwargs))
        return None

    replacement = replace(lease.context) if field == "context" else substituted
    monkeypatch.setattr(obligation, field, replacement)
    try:
        with pytest.raises(AuditIntegrityError):
            await async_workers.run_execution_lease_sql_finish_once(submission)
        assert calls == []
        assert submission.future is None and submission.reservation is None
        assert not submission.observed and not obligation.release_succeeded
        assert async_workers.outstanding_admissions() == 0
    finally:
        monkeypatch.setattr(obligation, field, actual)
        await lease.close()
        fixture.app.state.session_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("sql_fails", [False, True])
async def test_actual_execute_close_joins_held_release_with_three_original_cancellations(tmp_path, monkeypatch, sql_fails):
    from elspeth.web.coordination import lifecycle

    fixture, registry = await _core(tmp_path)
    service = fixture.app.state.session_service
    obligation, lease = await _lease(fixture, registry, fixture.session_id)
    entered, release = threading.Event(), threading.Event()
    sql_original = OperationalError("controlled actual release failure", {}, RuntimeError("offline"))
    delivered = []
    original_transaction = SQLiteLocalSessionOperationAuthority._locked_transaction
    original_retain = lifecycle._retain_cancellation

    @contextmanager
    def held_release(self, session_id):
        with original_transaction(self, session_id) as connection:
            entered.set()
            release.wait()
            yield connection
            if sql_fails:
                raise sql_original

    def retain_original(target, original):
        original_retain(target, original)
        if all(original is not old for old in delivered):
            delivered.append(original)

    monkeypatch.setattr(SQLiteLocalSessionOperationAuthority, "_locked_transaction", held_release)
    monkeypatch.setattr(lifecycle, "_retain_cancellation", retain_original)
    closing = asyncio.create_task(lease.close())
    try:
        while not entered.is_set():
            await asyncio.sleep(0.001)
        assert async_workers.outstanding_admissions() == 1
        for index in range(3):
            closing.cancel(f"actual execute-close original {index}")
            while len(delivered) != index + 1:
                await asyncio.sleep(0.001)
        assert not closing.done()
        release.set()
        with pytest.raises(BaseExceptionGroup) as failure:
            await closing

        def leaves(error):
            if isinstance(error, BaseExceptionGroup):
                return tuple(leaf for child in error.exceptions for leaf in leaves(child))
            return (error,)

        actual = leaves(failure.value)
        expected = (*delivered, *((sql_original,) if sql_fails else ()))
        assert len(actual) == len(expected)
        assert all(left is right for left, right in zip(actual, expected, strict=True))
        assert obligation.release_submission is not None
        assert obligation.release_submission.future is not None and obligation.release_submission.future.done()
        assert obligation.release_submission.callback_return_observed
        assert async_workers.outstanding_admissions() == 0
        registry.observe_ready()
        if sql_fails:
            assert obligation.release_submission.original_error is sql_original
            assert registry.has_pending_physical_owners() and not obligation.retired
            assert not fixture.app.state.process_recovery.watchdog.completed
        else:
            assert obligation.release_succeeded and obligation.retired
    finally:
        release.set()
        await asyncio.gather(closing, return_exceptions=True)
        monkeypatch.setattr(SQLiteLocalSessionOperationAuthority, "_locked_transaction", original_transaction)
        if sql_fails:
            # Temporary fixture cleanup uses the exact known source context;
            # it does not erase the registry's retained original/Unknown.
            service.session_operation_authority.release(lease.context)
        fixture.app.state.session_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["copied", "foreign_owner", "wrong_kind"])
async def test_executor_finalizer_binding_refuses_noncanonical_capability(tmp_path, variant):
    from copy import copy

    from elspeth.web.application_finalizers import ApplicationFinalizerKind, ApplicationFinalizerOwner

    fixture, registry = await _core(tmp_path)
    calls = []
    actual = registry.owner.register(ApplicationFinalizerKind.EXECUTION_EXECUTOR_JOIN, lambda: calls.append("actual"))
    if variant == "copied":
        supplied = copy(actual)
    elif variant == "foreign_owner":
        foreign = ApplicationFinalizerOwner()
        supplied = foreign.register(ApplicationFinalizerKind.EXECUTION_EXECUTOR_JOIN, lambda: calls.append("foreign"))
    else:
        supplied = registry.owner.register(ApplicationFinalizerKind.MEMBERSHIP_DRAIN, lambda: calls.append("unrelated"))
    try:
        with pytest.raises(AuditIntegrityError):
            registry.bind_executor_finalizer(supplied)
        assert registry.executor_finalizer is None
        assert calls == [] and async_workers.outstanding_admissions() == 0
        registry.bind_executor_finalizer(actual)
        assert registry.executor_finalizer is actual
    finally:
        fixture.app.state.session_engine.dispose()


@pytest.mark.asyncio
async def test_literal_shared_submit_queue_then_raise_joins_actual_generation_without_sql(tmp_path, monkeypatch):
    from elspeth.web.required_executor import RequiredInvocationAborted
    from elspeth.web.required_sql_outcomes import RequiredSQLRaised

    fixture, registry = await _core(tmp_path)
    service = fixture.app.state.session_service
    obligation = registry.admit(
        service.session_operation_authority,
        session_id=fixture.session_id,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=30,
    )
    actual_submit = obligation.executor.submit
    actual_transaction = SQLiteLocalSessionOperationAuthority._locked_transaction
    submitted = []
    sql_calls = []
    original = RuntimeError("actual queued wrapper with no returned Future")

    def queue_then_raise(*args, **kwargs):
        submitted.append(actual_submit(*args, **kwargs))
        raise original

    @contextmanager
    def observe_actual_sql(self, session_id):
        sql_calls.append(session_id)
        with actual_transaction(self, session_id) as connection:
            yield connection

    monkeypatch.setattr(obligation.executor, "submit", queue_then_raise)
    monkeypatch.setattr(SQLiteLocalSessionOperationAuthority, "_locked_transaction", observe_actual_sql)
    try:
        outcome = await async_workers.run_execution_lease_sql_finish_once(obligation.acquire_submission)
        assert type(outcome) is RequiredSQLRaised and outcome.error is original
        assert len(submitted) == 1 and submitted[0].done()
        assert isinstance(submitted[0].exception(), RequiredInvocationAborted)
        assert sql_calls == []
        assert obligation.generation.joined.is_set()
        submission = obligation.acquire_submission
        assert submission.future is None and submission.source_value is None
        assert submission.reservation is not None and submission.reservation.released
        assert submission.reservation.witness.snapshot().valid_aborted_exit
        assert obligation.retired and obligation.no_resource
        assert async_workers.outstanding_admissions() == 0
        registry.seal()
        with pytest.raises(BaseExceptionGroup) as failure:
            registry.assert_completed()
        assert any(error is original for error in failure.value.exceptions)
        assert not fixture.app.state.process_recovery.watchdog.completed
    finally:
        monkeypatch.setattr(obligation.executor, "submit", actual_submit)
        fixture.app.state.session_engine.dispose()


@pytest.mark.asyncio
async def test_subclass_sql_carrier_cannot_borrow_owned_release_dispatch(tmp_path):
    from elspeth.web.execution_lease_cleanup import _ISSUANCE_SEAL, ExecutionLeaseSQLSubmission

    class SubclassCarrier(ExecutionLeaseSQLSubmission):
        pass

    fixture, registry = await _core(tmp_path)
    obligation, lease = await _lease(fixture, registry, fixture.session_id)
    actual = obligation.issue_release(lease.context)
    supplied = SubclassCarrier(_ISSUANCE_SEAL, obligation, actual.arm)
    try:
        with pytest.raises(AuditIntegrityError):
            await async_workers.run_execution_lease_sql_finish_once(supplied)
        assert supplied.future is None and actual.future is None
        assert async_workers.outstanding_admissions() == 0
        assert not obligation.release_succeeded
        await lease.close()
        registry.observe_ready()
        assert obligation.retired and obligation.release_succeeded
    finally:
        await lease.close()
        fixture.app.state.session_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("sql_fails", [False, True])
async def test_finalizer_failed_callback_exits_only_after_actual_release_and_generation_join(tmp_path, monkeypatch, sql_fails):
    from elspeth.web.application_finalizers import ApplicationFinalizerKind

    fixture, registry = await _core(tmp_path)
    generation = async_workers._GENERATION_CUSTODIAN
    assert generation is not None
    callback_original = AuditIntegrityError("actual selected generation observer failed once")
    sql_original = RuntimeError("actual selected finalizer invocation failed")
    original_record = generation.record_completed
    original_shutdown = generation.executor.shutdown
    observer_failed = threading.Event()
    generation_entered = threading.Event()
    release_generation = threading.Event()
    delivered = []
    joining = None
    calls = []
    original_retain = async_workers._retain_cancellation

    def physical_finalizer():
        calls.append("physical invocation")
        if sql_fails:
            raise sql_original
        return "actual result"

    capability = registry.owner.register(ApplicationFinalizerKind.MEMBERSHIP_STOP, physical_finalizer)

    def fail_selected_observer_once(reservation):
        if reservation is capability._physical_reservation and not observer_failed.is_set():
            observer_failed.set()
            raise callback_original
        return original_record(reservation)

    def hold_actual_generation_join(*args, **kwargs):
        generation_entered.set()
        release_generation.wait()
        return original_shutdown(*args, **kwargs)

    def retain_delivered(target, original):
        original_retain(target, original)
        if asyncio.current_task() is joining and all(original is not earlier for earlier in delivered):
            delivered.append(original)

    monkeypatch.setattr(generation, "record_completed", fail_selected_observer_once)
    monkeypatch.setattr(generation.executor, "shutdown", hold_actual_generation_join)
    monkeypatch.setattr(async_workers, "_retain_cancellation", retain_delivered)
    registry.owner.seal()
    fixture.app.state.process_recovery.begin_shutdown()
    joining = asyncio.create_task(async_workers.run_application_finalizer_in_worker(capability))
    try:
        while not generation_entered.is_set():
            await asyncio.sleep(0.001)
        assert observer_failed.is_set()
        assert calls == ["physical invocation"]
        assert capability._physical_future is not None and capability._physical_future.done()
        assert capability._physical_admission_release_return_observed
        assert capability._physical_callback_failure_exit_observed
        assert async_workers.outstanding_admissions() == 0
        assert not capability.physical_completion_known
        assert not capability.physical_failure_completion_known
        assert not capability.physical_completion_succeeded
        assert not joining.done() and not generation.joined.is_set()
        for index in range(3):
            joining.cancel(f"failed finalizer actual cancellation {index}")
            while len(delivered) != index + 1:
                await asyncio.sleep(0.001)
        release_generation.set()
        with pytest.raises(BaseExceptionGroup) as raised:
            await joining
        assert generation.joined.is_set()
        assert capability.physical_failure_completion_known
        assert not capability.physical_completion_succeeded
        from copy import copy

        from elspeth.web.application_finalizers import _FINALIZER_PHYSICAL_ISSUER_SEAL, ApplicationFinalizerCapability

        copied = copy(capability)
        assert not copied.physical_failure_completion_known
        assert not copied.physical_completion_known and not copied.physical_completion_succeeded
        assert capability._physical_reservation is not None
        with pytest.raises(AuditIntegrityError):
            ApplicationFinalizerCapability._observe_physical_callback_failure_exit(
                copied, _FINALIZER_PHYSICAL_ISSUER_SEAL, capability._physical_reservation
            )
        expected = (*delivered, *((sql_original,) if sql_fails else ()), callback_original)
        assert len(raised.value.exceptions) == len(expected)
        assert all(actual is original for actual, original in zip(raised.value.exceptions, expected, strict=True))
        assert not fixture.app.state.process_recovery.watchdog.completed
    finally:
        release_generation.set()
        await asyncio.gather(joining, return_exceptions=True)
        original_shutdown(wait=True, cancel_futures=False)
        fixture.app.state.session_engine.dispose()


@pytest.mark.asyncio
async def test_finalizer_early_release_flag_does_not_prove_held_counter_completion(tmp_path, monkeypatch):
    from elspeth.web.application_finalizers import ApplicationFinalizerKind

    fixture, registry = await _core(tmp_path)
    generation = async_workers._GENERATION_CUSTODIAN
    assert generation is not None
    entered = threading.Event()
    release_counter = threading.Event()
    callback_original = AuditIntegrityError("actual callback after held counter failed once")
    original_counter = async_workers._release_admission
    original_record = generation.record_completed
    failed = threading.Event()

    def held_actual_counter():
        entered.set()
        release_counter.wait()
        original_counter()

    capability = registry.owner.register(ApplicationFinalizerKind.MEMBERSHIP_STOP, lambda: None)

    def fail_after_counter(reservation):
        if reservation is capability._physical_reservation and not failed.is_set():
            failed.set()
            raise callback_original
        return original_record(reservation)

    registry.owner.seal()
    fixture.app.state.process_recovery.begin_shutdown()
    monkeypatch.setattr(async_workers, "_release_admission", held_actual_counter)
    monkeypatch.setattr(generation, "record_completed", fail_after_counter)
    joining = asyncio.create_task(async_workers.run_application_finalizer_in_worker(capability))
    try:
        while not entered.is_set():
            await asyncio.sleep(0.001)
        assert capability._physical_reservation is not None and capability._physical_reservation.released
        assert not capability._physical_admission_release_return_observed
        assert not capability.physical_failure_completion_known
        assert not failed.is_set()
        assert async_workers.outstanding_admissions() == 1
        assert not joining.done() and not generation.joined.is_set()
        assert not fixture.app.state.process_recovery.watchdog.completed
        release_counter.set()
        with pytest.raises(AuditIntegrityError) as raised:
            await joining
        assert raised.value is callback_original
        assert generation.joined.is_set()
        assert capability.physical_failure_completion_known
        assert not capability.physical_completion_succeeded
        assert async_workers.outstanding_admissions() == 0
    finally:
        release_counter.set()
        await asyncio.gather(joining, return_exceptions=True)
        fixture.app.state.session_engine.dispose()


@pytest.mark.asyncio
async def test_finalizer_postarm_setup_failure_waits_actual_counter_and_keeps_sql_original(tmp_path, monkeypatch):
    from elspeth.web.application_finalizers import ApplicationFinalizerKind
    from elspeth.web.required_executor import RequiredInvocationWitness

    fixture, registry = await _core(tmp_path)
    generation = async_workers._GENERATION_CUSTODIAN
    assert generation is not None
    original_arm = RequiredInvocationWitness.arm
    original_counter = async_workers._release_admission
    setup_original = RuntimeError("actual selected arm raised after publication")
    sql_original = OperationalError("actual selected finalizer failed", {}, RuntimeError("offline"))
    counter_entered, counter_release = threading.Event(), threading.Event()
    calls = []

    def invocation():
        calls.append("actual invocation")
        raise sql_original

    capability = registry.owner.register(ApplicationFinalizerKind.MEMBERSHIP_STOP, invocation)

    def arm_then_raise(self):
        original_arm(self)
        if capability._physical_reservation is not None and self is capability._physical_reservation.witness:
            raise setup_original

    def held_counter():
        counter_entered.set()
        counter_release.wait()
        original_counter()

    registry.owner.seal()
    fixture.app.state.process_recovery.begin_shutdown()
    monkeypatch.setattr(RequiredInvocationWitness, "arm", arm_then_raise)
    monkeypatch.setattr(async_workers, "_release_admission", held_counter)
    joining = asyncio.create_task(async_workers.run_application_finalizer_in_worker(capability))
    try:
        while not counter_entered.is_set():
            await asyncio.sleep(0.001)
        assert not joining.done()
        assert capability._physical_reservation is not None and capability._physical_reservation.released
        assert not capability.physical_submission_failure_completion_known
        assert async_workers.outstanding_admissions() == 1
        counter_release.set()
        with pytest.raises(BaseExceptionGroup) as raised:
            await joining
        assert len(raised.value.exceptions) == 2
        assert raised.value.exceptions[0] is setup_original
        assert raised.value.exceptions[1] is sql_original
        assert calls == ["actual invocation"]
        assert generation.joined.is_set() and capability.physical_submission_failure_completion_known
        assert not capability.physical_completion_succeeded
        assert async_workers.outstanding_admissions() == 0
        assert not fixture.app.state.process_recovery.watchdog.completed
    finally:
        counter_release.set()
        await asyncio.gather(joining, return_exceptions=True)
        fixture.app.state.session_engine.dispose()
