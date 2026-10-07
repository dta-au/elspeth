"""Actual lifecycle custody for explicit direct ExecutionService constructors.

No provider/pipeline outcome is authored here. The no-signal watchdog is the
existing nominal test helper. Unknown physical owners remain pending.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from types import MethodType
from uuid import UUID, uuid4

import pytest_asyncio
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web import async_workers
from elspeth.web.application_finalizers import ApplicationFinalizerOwner
from elspeth.web.coordination.contracts import SessionOperationContext
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.repository import _SessionOperationAuthorityRepository
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.execution.service import ExecutionServiceImpl
from elspeth.web.execution_lease_cleanup import ExecutionLeaseReleaseRegistry
from elspeth.web.process_recovery import ProcessRecovery
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import session_operation_fences_table
from elspeth.web.sessions.protocol import SessionOperationAuthority
from elspeth.web.sessions.schema import initialize_session_schema
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog
from tests.helpers.session_fences import seed_session_operation_fence
from tests.unit.web.conftest import _make_session


def _leaves(original: BaseException) -> list[BaseException]:
    if isinstance(original, BaseExceptionGroup):
        return [leaf for child in original.exceptions for leaf in _leaves(child)]
    return [original]


@dataclass(slots=True)
class _ExecutionFixtureJoin:
    name: str
    invocation: Callable[[], Awaitable[None]]
    task: asyncio.Task[None] | None = None
    coroutine: Awaitable[None] | None = None
    allocation_original: BaseException | None = None
    entered: bool = False
    outcome_recorded: bool = False
    outcome: BaseException | None = None


class ExecutionTestCustody:
    """Retain actual owners even if a constructor never returns its service."""

    def __init__(self, loop: asyncio.AbstractEventLoop, data_dir: Path) -> None:
        self.loop = loop
        self.data_dir = data_dir
        self.authority_engines = []
        self.authority_observers = []
        self.gates: list[threading.Event] = []
        self.witnessed_cleanup_originals: list[BaseException] = []

        selected_owner = async_workers._APPLICATION_FINALIZER_OWNER
        selected_callback = async_workers._RECOVERY_CALLBACK
        if selected_owner is None:
            # Standalone pytest installs only a recording recovery callback.
            # Installation must precede its first shared generation, never
            # replace active/unknown physical ownership to repair a test.
            if async_workers._GENERATION_CUSTODIAN is not None or async_workers.outstanding_admissions():
                raise AuditIntegrityError("EXECUTE fixture lifecycle installation is too late")
            owner = ApplicationFinalizerOwner()
            draining = threading.Event()
            recovery = ProcessRecovery(watchdog=OwnedTestProcessWatchdog(draining), instance_draining=draining)
            async_workers.configure_required_executor_recovery(
                drain_seconds=10,
                instance_draining=draining,
                generation_unavailable=threading.Event(),
                recovery_callback=recovery.required_generation_expired,
                application_finalizer_owner=owner,
            )
            self.owns_lifecycle = True
        else:
            if (
                type(selected_owner) is not ApplicationFinalizerOwner
                or type(selected_callback) is not MethodType
                or type(selected_callback.__self__) is not ProcessRecovery
                or selected_callback.__func__ is not ProcessRecovery.required_generation_expired
            ):
                raise AuditIntegrityError("EXECUTE fixture selected lifecycle is not the actual registered owner")
            owner, recovery = selected_owner, selected_callback.__self__
            self.owns_lifecycle = False
        self.owner, self.recovery = owner, recovery
        self.registries: list[ExecutionLeaseReleaseRegistry] = []
        self.services: list[ExecutionServiceImpl] = []
        self.route_leases: list[SessionOperationLease] = []
        self._recovery_declared = False
        self._close_declared = False
        self._lifecycle_joins_declared = False
        self._joins: list[_ExecutionFixtureJoin] = []
        self._failures: list[BaseException] = []
        self._caller_cancellations: list[asyncio.CancelledError] = []

    def registry(self, loop: asyncio.AbstractEventLoop) -> ExecutionLeaseReleaseRegistry:
        registry = ExecutionLeaseReleaseRegistry(owner=self.owner, recovery=self.recovery, loop=loop)
        self.registries.append(registry)
        return registry

    def bind(self, service: ExecutionServiceImpl) -> ExecutionServiceImpl:
        if type(service) is not ExecutionServiceImpl or service.execution_lease_release_registry not in self.registries:
            raise AuditIntegrityError("EXECUTE fixture replaced its actual constructor registration")
        self.services.append(service)
        return service

    def track_route_lease(self, lease: SessionOperationLease) -> None:
        if type(lease) is not SessionOperationLease or lease.execution_obligation is None:
            raise AuditIntegrityError("Route fixture lost canonical lease")
        if lease.execution_obligation.registry not in self.registries or lease.execution_obligation.lease is not lease:
            raise AuditIntegrityError("Route fixture lost actual registry admission")
        self.route_leases.append(lease)

    def observe_authority(self, session_id: UUID) -> CanonicalExecutionAuthorityObservation:
        engine = create_session_engine(f"sqlite:///{self.data_dir / (uuid4().hex + '-execution-authority.db')}")
        initialize_session_schema(engine)
        with engine.begin() as connection:
            _make_session(connection, session_id=str(session_id))
            seed_session_operation_fence(connection, session_id, owner_instance_id="execution-authority-fixture")
        self.authority_engines.append(engine)
        observation = CanonicalExecutionAuthorityObservation(SQLiteLocalSessionOperationAuthority(engine), session_id)
        self.authority_observers.append(observation)
        return observation

    def observe_existing_authority(
        self, authority: SQLiteLocalSessionOperationAuthority, session_id: UUID
    ) -> CanonicalExecutionAuthorityObservation:
        if type(authority) is not SQLiteLocalSessionOperationAuthority:
            raise TypeError("fixture requires the actual selected SQLite authority")
        observation = CanonicalExecutionAuthorityObservation(authority, session_id)
        self.authority_observers.append(observation)
        return observation

    def witness_cleanup_original(self, original: BaseException) -> None:
        """Call only after a test asserts the exact deliberately injected root."""
        if not any(original is leaf for registry in self.registries for failure in registry._failures for leaf in _leaves(failure)):
            raise AssertionError("test cleanup witness is absent from actual registry originals")
        self.witnessed_cleanup_originals.append(original)

    async def acquire(
        self,
        registry: ExecutionLeaseReleaseRegistry,
        authority: SessionOperationAuthority,
        *,
        session_id: UUID,
        owner_instance_id: str,
        lease_seconds: int,
        renew_interval_seconds: float | None = None,
    ) -> SessionOperationLease:
        if registry not in self.registries:
            raise AuditIntegrityError("EXECUTE fixture does not own its actual registry")
        obligation = registry.admit(
            authority,
            session_id=session_id,
            owner_instance_id=owner_instance_id,
            lease_seconds=lease_seconds,
        )
        return await SessionOperationLease.acquire(
            authority,
            session_id=session_id,
            operation_kind=SessionOperationKind.EXECUTE,
            owner_instance_id=owner_instance_id,
            lease_seconds=lease_seconds,
            execution_obligation=obligation,
            renew_interval_seconds=renew_interval_seconds,
        )

    async def callback_stage(
        self,
        service: ExecutionServiceImpl,
        *,
        error: BaseException | None = None,
        watcher_name: str | None = None,
    ) -> ActualTerminalPipelineStage:
        if not any(service is owned for owned in self.services):
            raise AuditIntegrityError("Callback fixture replaced actual service")
        observed = self.observe_authority(uuid4())
        lease = await self.acquire(
            service.execution_lease_release_registry,
            observed.authority,
            session_id=observed.session_id,
            owner_instance_id="actual-callback-unit",
            lease_seconds=30,
        )
        obligation = lease.execution_obligation
        assert obligation is not None
        watcher = service._create_loss_watcher(lease, threading.Event(), run_id=uuid4())
        if watcher_name is not None:
            watcher.set_name(watcher_name)
        allowed = threading.Event()
        self.gates.append(allowed)

        def physical_endpoint():
            assert allowed.wait(5), "actual callback pipeline endpoint was never released"
            if error is not None:
                raise error
            return None

        service._submit_owned_pipeline(obligation, lease, watcher, partial(physical_endpoint))
        future = obligation.pipeline
        assert future is not None
        return ActualTerminalPipelineStage(service, lease, watcher, future, allowed)

    async def shutdown_service(self, service: ExecutionServiceImpl) -> None:
        if not any(service is owned for owned in self.services):
            raise AuditIntegrityError("EXECUTE fixture shutdown replaced its actual service")
        self.owner.seal()
        self._begin_selected_shutdown()
        failures: list[BaseException] = list(self._failures)
        try:
            await service.shutdown()
        except BaseException as original:
            failures.append(original)
        async_workers._raise_lifecycle_originals([], failures)

    def _begin_selected_shutdown(self) -> None:
        if self._recovery_declared:
            return
        self._recovery_declared = True
        try:
            # Exact selected real owner, including borrowed child lifecycles.
            # ProcessRecovery sets draining synchronously before its own Task.
            self.recovery.begin_shutdown()
        except BaseException as original:
            self._failures.append(original)

    async def _observe_join(self, owner: _ExecutionFixtureJoin) -> None:
        task = asyncio.current_task()
        if task is None or not any(owner is selected for selected in self._joins):
            raise AuditIntegrityError("Fixture cleanup lacks predeclared owner")
        if owner.task is not None and owner.task is not task:
            raise AuditIntegrityError("Fixture cleanup factory replaced actual Task")
        owner.task = task
        owner.entered = True
        try:
            await owner.invocation()
        except BaseException as original:
            owner.outcome = original
            owner.outcome_recorded = True
            raise
        else:
            owner.outcome = None
            owner.outcome_recorded = True

    def _issue_join(self, invocation: Callable[[], Awaitable[None]], name: str) -> None:
        owner = _ExecutionFixtureJoin(name, invocation)
        self._joins.append(owner)  # BEFORE coroutine/Task allocation.
        try:
            coroutine = self._observe_join(owner)
            owner.coroutine = coroutine
            task = asyncio.create_task(coroutine, name=name)
        except BaseException as original:
            owner.allocation_original = original
            self._failures.append(original)
            # No return says nothing about allocation. Keep the coroutine and
            # owner rooted; actual producer entry can bind its hidden Task.
            # Do not retry, close the coroutine, or retire an absent owner.
            return
        if owner.task is not None and owner.task is not task:
            self._failures.append(AuditIntegrityError("Fixture cleanup factory changed Task identity"))
            return
        owner.task = task

    async def _join_issued(self) -> None:
        while not all(owner.task is not None and owner.task.done() and owner.entered and owner.outcome_recorded for owner in self._joins):
            pending = {owner.task for owner in self._joins if owner.task is not None and not owner.task.done()}
            try:
                if pending:
                    await asyncio.wait(pending)
                else:
                    # Allocation-before/after return Unknown remains supervised;
                    # no absent Task or cancelled-before-entry grants completion.
                    await asyncio.sleep(0.001)
            except asyncio.CancelledError as original:
                if not any(original is retained for retained in self._caller_cancellations):
                    self._caller_cancellations.append(original)
        for owner in self._joins:
            task = owner.task
            assert task is not None and owner.entered and owner.outcome_recorded
            if owner.outcome is not None:
                self._failures.append(owner.outcome)
            try:
                task.result()
            except BaseException as projected:
                if owner.outcome is None:
                    self._failures.append(projected)

    async def close(self) -> None:
        for gate in self.gates:
            gate.set()
        for observation in self.authority_observers:
            observation.release_allowed.set()
            observation.renew_allowed.set()
        self.owner.seal()
        for registry in self.registries:
            registry.seal()
        self._begin_selected_shutdown()  # BEFORE any finalizer Task issuance.
        if not self._close_declared:
            self._close_declared = True
            # Allocation errors retain their own Unknown owner and cannot skip
            # any later independent service/registry cleanup issuance.
            for ordinal, lease in enumerate(self.route_leases):
                obligation = lease.execution_obligation
                assert obligation is not None
                if not lease.closed and not obligation.completion_required:
                    self._issue_join(lease.close, f"fixture-route-close-{ordinal}")
            for ordinal, service in enumerate(self.services):
                self._issue_join(service.shutdown, f"fixture-execution-shutdown-{ordinal}")
            for ordinal, registry in enumerate(self.registries):
                capability = registry.executor_finalizer
                if (
                    capability is not None
                    and not capability.claimed
                    and not any(service.execution_lease_release_registry is registry for service in self.services)
                ):
                    self._issue_join(
                        lambda capability=capability: async_workers.run_application_finalizer_in_worker(capability),
                        f"fixture-partial-executor-{ordinal}",
                    )
                self._issue_join(registry.join_all, f"fixture-execution-registry-{ordinal}")
        await self._join_issued()
        for registry in self.registries:
            try:
                # Known failed cleanup is never asserted COMPLETE. join_all
                # must nevertheless observe its actual physical owners first.
                if not registry._failures:
                    registry.assert_completed()
            except BaseException as original:
                self._failures.append(original)
        if self.owns_lifecycle and not self._lifecycle_joins_declared:
            self._lifecycle_joins_declared = True
            # Independent escalation and generation shutdown are both issued
            # before observing either; borrowed scopes keep their outer owner.
            self._issue_join(self.recovery.join_escalation, "fixture-recovery-join")
            self._issue_join(async_workers.shutdown_async_workers, "fixture-shared-executor-join")
            await self._join_issued()
        originals: list[BaseException] = []
        for original in (*self._failures, *self._caller_cancellations):
            if not any(original is retained for retained in originals):
                originals.append(original)
        for observation in self.authority_observers:
            observation.restore_private_observers()
        for engine in self.authority_engines:
            try:
                engine.dispose()
            except BaseException as original:
                originals.append(original)
        unexpected = [
            leaf
            for original in originals
            for leaf in _leaves(original)
            if not any(leaf is witnessed for witnessed in self.witnessed_cleanup_originals)
        ]
        if unexpected:
            raise BaseExceptionGroup("EXECUTE fixture retained cleanup failures", unexpected)


@pytest_asyncio.fixture
async def execution_fixture(tmp_path: Path) -> AsyncIterator[ExecutionTestCustody]:
    custody = ExecutionTestCustody(asyncio.get_running_loop(), tmp_path)
    try:
        yield custody
    finally:
        await custody.close()


class CanonicalExecutionAuthorityObservation:
    """Observe canonical SQL commit without changing public dispatch methods.

    Every context comes from the actual repository's private advance method.
    Acquisition is recorded after that transaction committed. Release is
    recorded only after the actual exact row was marked released and committed.
    The actual public bound acquire/release methods remain unchanged.
    """

    def __init__(self, authority: SQLiteLocalSessionOperationAuthority, session_id: UUID) -> None:
        self.authority = authority
        self.session_id = session_id
        self.calls = []
        self.context = None
        self.release_calls = []
        self.release_allowed = threading.Event()
        self.release_allowed.set()
        self.release_called = threading.Event()
        self.release_error: BaseException | None = None
        self.renew_called = threading.Event()
        self.renew_allowed = threading.Event()
        self.renew_allowed.set()
        self.renew_error: BaseException | None = None
        original_advance, original_transaction = authority._advance_exclusive_fence_on_connection, authority._locked_transaction
        self.original_advance, self.original_transaction = original_advance, original_transaction
        pending_contexts = {}
        acquired_contexts = {}

        def advance(connection, **kwargs):
            context = original_advance(connection, **kwargs)
            pending_contexts[threading.get_ident()] = context
            return context

        @contextmanager
        def transaction(requested_session):
            producer_frame = sys._getframe(2)
            producer = producer_frame.f_code
            if producer is _SessionOperationAuthorityRepository.release.__code__:
                source_context = producer_frame.f_locals["context"]
                assert type(source_context) is SessionOperationContext
                assert source_context.fence.session_id == requested_session
                self.release_calls.append(source_context)
                self.release_called.set()
                assert self.release_allowed.wait(5), "actual release SQL fixture gate was never opened"
                if self.release_error is not None:
                    raise self.release_error
            elif producer is _SessionOperationAuthorityRepository.renew.__code__:
                self.renew_called.set()
                assert self.renew_allowed.wait(5), "actual renewal SQL fixture gate was never opened"
                if self.renew_error is not None:
                    raise self.renew_error
            context = None
            released_context = None
            try:
                with original_transaction(requested_session) as connection:
                    yield connection
                    context = pending_contexts.pop(threading.get_ident(), None)
                    row = (
                        connection.execute(
                            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == requested_session)
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if row is not None and row["released_at"] is not None:
                        released_context = acquired_contexts.get(row["operation_id"])
            finally:
                pending_contexts.pop(threading.get_ident(), None)
            if context is not None:
                self.context = context
                acquired_contexts[context.fence.operation_id] = context
                self.calls.append(("acquire", context))
            if released_context is not None and not any(
                name == "release" and observed is released_context for name, observed in self.calls
            ):
                self.calls.append(("release", released_context))

        authority._advance_exclusive_fence_on_connection = advance
        authority._locked_transaction = transaction

    def restore_private_observers(self) -> None:
        self.authority._advance_exclusive_fence_on_connection = self.original_advance
        self.authority._locked_transaction = self.original_transaction


class PhysicalPipelineCompletionControl:
    """Control the result of one actual queued private-executor invocation.

    set_result/set_exception signal the physical endpoint; they never write a
    Future's state. cancel performs the actual queued Future cancellation.
    """

    def __init__(self, custody: ExecutionTestCustody) -> None:
        self.actual: Future | None = None
        self.value = None
        self.error: BaseException | None = None
        self.selected = False
        self.allowed = threading.Event()
        self.queue_allowed = threading.Event()
        custody.gates.extend((self.allowed, self.queue_allowed))

    def set_result(self, value) -> None:
        assert not self.selected
        self.value, self.selected = value, True
        self.allowed.set()
        self.queue_allowed.set()

    def set_exception(self, original: BaseException) -> None:
        assert not self.selected
        self.error, self.selected = original, True
        self.allowed.set()
        self.queue_allowed.set()

    def cancel(self) -> bool:
        assert self.actual is not None and not self.selected
        cancelled = self.actual.cancel()
        assert cancelled, "actual private invocation was not still queued"
        self.selected = True
        self.allowed.set()
        self.queue_allowed.set()
        return cancelled

    def running(self) -> bool:
        assert self.actual is not None
        return self.actual.running()

    def done(self) -> bool:
        return self.actual is not None and self.actual.done()

    def invoke(self):
        assert self.allowed.wait(5), "physical pipeline result gate was never opened"
        if self.error is not None:
            raise self.error
        return self.value


class ActualPrivatePipelineExecutorControl:
    """Forward through the exact registered executor; preserve physical custody.

    The unit endpoint returns a scripted result on a real private worker. This
    proves execution/lifecycle ownership, not engine or provider behavior.
    """

    def __init__(self, pool: ThreadPoolExecutor, custody: ExecutionTestCustody) -> None:
        if type(pool) is not ThreadPoolExecutor:
            raise TypeError("pipeline fixture requires actual registered private ThreadPoolExecutor")
        self.pool = pool
        self.future = PhysicalPipelineCompletionControl(custody)
        self.submit_calls = []
        self.shutdown_started = threading.Event()
        self.submit_error: BaseException | None = None
        self.trace: list[str] | None = None
        self._submit, self._shutdown = pool.submit, pool.shutdown
        self.blockers = [self._submit(self.future.queue_allowed.wait, 5) for _ in range(pool._max_workers)]

        def submit(invocation, *args, **kwargs):
            if self.trace is not None:
                self.trace.append("submit")
            if type(invocation) is not partial or args or kwargs:
                raise AssertionError("production submission did not carry its exact physical partial")
            self.submit_calls.append((invocation.func, invocation.args, invocation.keywords))
            if self.submit_error is not None:
                raise self.submit_error
            actual = self._submit(self.future.invoke)
            self.future.actual = actual
            if self.future.selected:
                # A real already-done Future returned before callback setup.
                assert wait((actual,), timeout=5).done == {actual}
            return actual

        def shutdown(wait=True, *, cancel_futures=False):
            assert wait is True
            self.shutdown_started.set()
            return self._shutdown(wait=wait, cancel_futures=cancel_futures)

        pool.submit = submit
        pool.shutdown = shutdown


@dataclass(frozen=True)
class ActualTerminalPipelineStage:
    service: ExecutionServiceImpl
    lease: SessionOperationLease
    watcher: asyncio.Task[None]
    future: Future
    allowed: threading.Event

    async def join(self) -> None:
        self.allowed.set()
        obligation = self.lease.execution_obligation
        assert obligation is not None
        for _ in range(200):
            if obligation.completion is not None:
                break
            await asyncio.sleep(0.01)
        completion = obligation.completion
        assert completion is not None, "actual terminal callback did not publish its wrapper"
        # This is an exact late duplicate against the original admitted Future.
        # It must never create a second completion/release owner.
        self.service._on_pipeline_done(self.future, session_operation_lease=self.lease, loss_watcher=self.watcher)
        await asyncio.wait_for(asyncio.wrap_future(completion), timeout=2)
