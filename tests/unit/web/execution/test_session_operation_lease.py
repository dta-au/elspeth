"""Execution owns one renewable session-operation lease until terminal cleanup.

These tests deliberately separate the three authority seams:

* the HTTP route acquires and transfers exactly one EXECUTE lease;
* the background worker propagates that lease's immutable context through every
  durable or external effect and closes it only after terminal cleanup;
* the real Sessions UoW rejects stale or mismatched execution contexts before
  target-table DML, local broadcast, or run-event sequence allocation.

The controllable doubles below model resources, not return values.  In
particular, a lease close can be held open and cancelled a second time, and a
worker future can complete synchronously on the event-loop thread.  This keeps
the gate sensitive to the lifetime bugs that ordinary AsyncMock assertions
miss.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import operator
import re
import textwrap
import threading
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import create_autospec, patch
from uuid import UUID, uuid4

import pytest
import structlog
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Engine, event, func, select
from starlette.requests import Request

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.events import EventBus
from elspeth.engine.orchestrator.core import Orchestrator
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.coordination.contracts import (
    FenceLossReason,
    SessionOperationContext,
    SessionOperationFence,
    SessionOperationFenceLost,
    SessionOperationKind,
)
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.repository import SessionDerivedCustodyError, SessionOperationConflictError
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.execution import service as execution_service_module
from elspeth.web.execution.progress import ProgressBroadcaster
from elspeth.web.execution.protocol import ExecutionService
from elspeth.web.execution.routes import create_execution_router
from elspeth.web.execution.schemas import ProgressData, RunEvent, ValidationReadiness, ValidationResult
from elspeth.web.execution.service import ExecutionServiceImpl
from elspeth.web.sessions.models import run_events_table
from elspeth.web.sessions.protocol import CompositionStateData, SessionOperationAuthority, SessionServiceProtocol
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

_USER_ID = "execution-lease-user"


def _context(
    session_id: UUID,
    *,
    kind: SessionOperationKind = SessionOperationKind.EXECUTE,
    operation_id: str = "00000000-0000-4000-8000-000000000101",
    operation_epoch: int = 7,
) -> SessionOperationContext:
    return SessionOperationContext(
        fence=SessionOperationFence(
            session_id=str(session_id),
            operation_id=operation_id,
            lease_token="execution-lease-test-token",
            operation_epoch=operation_epoch,
        ),
        operation_kind=kind,
    )


class _ControllableLease:
    """A resource-owning lease double with observable close and loss joins."""

    def __init__(self, context: SessionOperationContext) -> None:
        self.context = context
        self.close_started = asyncio.Event()
        self.close_allowed = asyncio.Event()
        self.close_finished = asyncio.Event()
        self.loss_signalled = asyncio.Event()
        self.close_calls = 0
        self.close_cancelled = False

    async def close(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        try:
            await self.close_allowed.wait()
        except asyncio.CancelledError:
            self.close_cancelled = True
            raise
        self.close_finished.set()

    async def wait_until_lost(self) -> None:
        await self.loss_signalled.wait()

    def raise_if_lost(self) -> None:
        if self.loss_signalled.is_set():
            raise SessionOperationFenceLost


class _ControllableAuthority:
    """Synchronous authority collaborator for a real SessionOperationLease."""

    def __init__(self, context: SessionOperationContext) -> None:
        self.context = context
        self.release_allowed = threading.Event()
        self.release_called = threading.Event()
        self.release_calls: list[SessionOperationContext] = []
        self.renew_called = threading.Event()
        self.renew_error: BaseException | None = None

    def compare_and_swap(self, context: SessionOperationContext) -> None:
        assert context is self.context

    def renew(
        self,
        context: SessionOperationContext,
        *,
        lease_seconds: int,
    ) -> SessionOperationContext:
        assert context is self.context
        assert lease_seconds == 30
        self.renew_called.set()
        if self.renew_error is not None:
            raise self.renew_error
        return context

    def release(self, context: SessionOperationContext) -> None:
        assert context is self.context
        self.release_calls.append(context)
        self.release_called.set()
        assert self.release_allowed.wait(timeout=2)


class _BlockedRenewalAuthority:
    """Real SQLite authority with a controllable in-flight renewal."""

    def __init__(self, authority: SQLiteLocalSessionOperationAuthority) -> None:
        self._authority = authority
        self.renew_started = threading.Event()
        self.renew_allowed = threading.Event()

    def compare_and_swap(self, context: SessionOperationContext) -> None:
        self._authority.compare_and_swap(context)

    def renew(
        self,
        context: SessionOperationContext,
        *,
        lease_seconds: int,
    ) -> SessionOperationContext:
        self.renew_started.set()
        assert self.renew_allowed.wait(timeout=5)
        return self._authority.renew(context, lease_seconds=lease_seconds)

    def release(self, context: SessionOperationContext) -> None:
        self._authority.release(context)


class _ControllableExecutor:
    """Executor double whose worker completion and shutdown are explicit."""

    def __init__(self) -> None:
        self.future: Future[object] = Future()
        self.submit_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []
        self.shutdown_started = threading.Event()
        self.submit_error: BaseException | None = None
        self.trace: list[str] | None = None

    def submit(self, fn: object, *args: object, **kwargs: object) -> Future[object]:
        if self.trace is not None:
            self.trace.append("submit")
        self.submit_calls.append((fn, args, kwargs))
        if self.submit_error is not None:
            raise self.submit_error
        return self.future

    def shutdown(self, wait: bool = True) -> None:
        assert wait is True
        self.shutdown_started.set()


class _ExecutionSettings:
    auth_provider = "local"
    data_dir = Path("/tmp/execution-lease-gate")
    landscape_passphrase = None

    def get_landscape_url(self) -> str:
        return "sqlite:////tmp/execution-lease-gate-landscape.db"

    def get_payload_store_path(self) -> Path:
        return Path("/tmp/execution-lease-gate-payloads")


class _YamlGenerator:
    def generate_yaml(self, _state: object) -> str:
        return "source:\n  plugin: csv\n  options: {}\n"


def _execution_service(loop: asyncio.AbstractEventLoop) -> tuple[ExecutionServiceImpl, Any, _ControllableExecutor]:
    session_service = create_autospec(SessionServiceProtocol, instance=True)
    session_id = uuid4()
    state = SimpleNamespace(
        id=uuid4(),
        session_id=session_id,
        version=1,
        source=None,
        sources=None,
        nodes=None,
        edges=None,
        outputs=None,
        metadata_={"name": "Execution lease", "description": ""},
        is_valid=True,
        validation_errors=None,
        created_at=datetime.now(UTC),
        derived_from_state_id=None,
        composer_meta=None,
    )
    run = SimpleNamespace(id=uuid4(), session_id=session_id, state_id=state.id, status="pending")
    session_service.get_active_run.return_value = None
    session_service.get_current_state.return_value = state
    session_service.create_run.return_value = run
    session_service.update_run_status.return_value = None
    service = ExecutionServiceImpl.for_trained_operator(
        loop=loop,
        broadcaster=ProgressBroadcaster(loop),
        settings=cast(Any, _ExecutionSettings()),
        session_service=session_service,
        yaml_generator=_YamlGenerator(),
        telemetry=build_sessions_telemetry(),
    )
    executor = _ControllableExecutor()
    service._executor = cast(Any, executor)
    return service, session_service, executor


async def _real_lease(
    context: SessionOperationContext,
    *,
    renew_interval_seconds: float = 29,
    renew_error: BaseException | None = None,
) -> tuple[SessionOperationLease, _ControllableAuthority]:
    authority = _ControllableAuthority(context)
    authority.renew_error = renew_error
    lease = await SessionOperationLease.adopt(
        cast("SessionOperationAuthority", authority),
        context,
        lease_seconds=30,
        renew_interval_seconds=renew_interval_seconds,
    )
    return lease, authority


class _RouteSessionService:
    def __init__(self, session_id: UUID, *, trace: list[str] | None = None) -> None:
        self.session_operation_authority = cast("SessionOperationAuthority", object())
        self.session_operation_owner_instance_id = "execution-route-test"
        self.session_operation_lease_seconds = 41
        self._session_id = session_id
        self._trace = trace
        self.authorized = True

    async def get_session(self, session_id: UUID) -> SimpleNamespace:
        assert session_id == self._session_id
        if self._trace is not None:
            self._trace.append("ownership")
        return SimpleNamespace(
            id=session_id,
            user_id=_USER_ID if self.authorized else "different-user",
            auth_provider_type="local",
            archived_at=None,
        )


class _RouteExecutionService:
    def __init__(self, run_id: UUID, *, trace: list[str] | None = None) -> None:
        self.run_id = run_id
        self.started = asyncio.Event()
        self.allowed = asyncio.Event()
        self.error: BaseException | None = None
        self.calls: list[dict[str, object]] = []
        self._trace = trace

    async def execute(self, session_id: UUID, state_id: UUID | None = None, **kwargs: object) -> UUID:
        self.calls.append({"session_id": session_id, "state_id": state_id, **kwargs})
        if self._trace is not None:
            self._trace.append("execute")
        self.started.set()
        await self.allowed.wait()
        if self.error is not None:
            raise self.error
        return self.run_id


@dataclass
class _RouteHarness:
    endpoint: Any
    request: Request
    user: UserIdentity
    session_service: _RouteSessionService


def _route_harness(session_id: UUID) -> _RouteHarness:
    app = FastAPI()
    session_service = _RouteSessionService(session_id)
    app.state.session_service = session_service
    app.state.settings = SimpleNamespace(auth_provider="local")
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/api/sessions/{session_id}/execute",
            "headers": [],
            "app": app,
        }
    )
    endpoint = next(route.endpoint for route in create_execution_router().routes if route.name == "execute_pipeline")
    return _RouteHarness(
        endpoint=endpoint,
        request=request,
        user=UserIdentity(user_id=_USER_ID, username="execution-lease-user"),
        session_service=session_service,
    )


def _install_acquire(
    monkeypatch: pytest.MonkeyPatch,
    lease: _ControllableLease,
    *,
    trace: list[str] | None = None,
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    async def acquire(_cls: type[SessionOperationLease], authority: object, **kwargs: object) -> _ControllableLease:
        if trace is not None:
            trace.append("acquire")
        calls.append({"authority": authority, **kwargs})
        return lease

    monkeypatch.setattr(SessionOperationLease, "acquire", classmethod(acquire))
    return calls


def _http_route_app(
    *,
    session_service: _RouteSessionService,
    execution_service: _RouteExecutionService,
) -> FastAPI:
    app = FastAPI()
    app.state.session_service = session_service
    app.state.execution_service = execution_service
    app.state.settings = SimpleNamespace(auth_provider="local")

    async def authenticated_user() -> UserIdentity:
        return UserIdentity(user_id=_USER_ID, username="execution-lease-user")

    app.dependency_overrides[get_current_user] = authenticated_user
    app.include_router(create_execution_router())
    return app


async def _invoke_execute_route(
    harness: _RouteHarness,
    service: _RouteExecutionService,
    *,
    session_id: UUID,
) -> dict[str, str]:
    return await harness.endpoint(
        session_id=session_id,
        request=harness.request,
        state_id=None,
        execute_request=None,
        user=harness.user,
        service=service,
        session_service=harness.session_service,
    )


@pytest.mark.asyncio
async def test_execute_route_acquires_exact_execute_lease_and_transfers_it(monkeypatch: pytest.MonkeyPatch) -> None:
    session_id = uuid4()
    run_id = uuid4()
    harness = _route_harness(session_id)
    lease = _ControllableLease(_context(session_id))
    acquired = _install_acquire(monkeypatch, lease)
    service = _RouteExecutionService(run_id)
    service.allowed.set()

    response = await _invoke_execute_route(harness, service, session_id=session_id)

    assert response == {"run_id": str(run_id)}
    assert acquired == [
        {
            "authority": harness.session_service.session_operation_authority,
            "session_id": session_id,
            "operation_kind": SessionOperationKind.EXECUTE,
            "owner_instance_id": "execution-route-test",
            "lease_seconds": 41,
        }
    ]
    assert len(service.calls) == 1
    assert service.calls[0]["session_operation_lease"] is lease
    assert lease.close_calls == 0, "successful submission transfers ownership to background completion"


@pytest.mark.asyncio
async def test_fastapi_execute_orders_ownership_before_acquire_before_exact_service_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid4()
    trace: list[str] = []
    session_service = _RouteSessionService(session_id, trace=trace)
    execution_service = _RouteExecutionService(uuid4(), trace=trace)
    execution_service.allowed.set()
    lease = _ControllableLease(_context(session_id))
    acquired = _install_acquire(monkeypatch, lease, trace=trace)
    app = _http_route_app(session_service=session_service, execution_service=execution_service)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/sessions/{session_id}/execute")

    assert response.status_code == 202
    assert trace == ["ownership", "acquire", "execute"]
    assert len(acquired) == 1
    assert execution_service.calls == [
        {
            "session_id": session_id,
            "state_id": None,
            "session_operation_lease": lease,
            "user_id": _USER_ID,
            "auth_provider_type": "local",
            "fanout_ack_token": None,
        }
    ]


@pytest.mark.asyncio
async def test_fastapi_denied_ownership_never_acquires_or_invokes_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid4()
    trace: list[str] = []
    session_service = _RouteSessionService(session_id, trace=trace)
    session_service.authorized = False
    execution_service = _RouteExecutionService(uuid4(), trace=trace)
    lease = _ControllableLease(_context(session_id))
    acquired = _install_acquire(monkeypatch, lease, trace=trace)
    app = _http_route_app(session_service=session_service, execution_service=execution_service)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/sessions/{session_id}/execute")

    assert response.status_code == 404
    assert trace == ["ownership"]
    assert acquired == []
    assert execution_service.calls == []


@pytest.mark.asyncio
async def test_execute_route_pretransfer_failure_closes_and_joins_exact_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    session_id = uuid4()
    harness = _route_harness(session_id)
    lease = _ControllableLease(_context(session_id))
    acquired = _install_acquire(monkeypatch, lease)
    lease.close_allowed.set()
    service = _RouteExecutionService(uuid4())
    service.error = RuntimeError("submit failed")
    service.allowed.set()

    with pytest.raises(RuntimeError, match="submit failed"):
        await _invoke_execute_route(harness, service, session_id=session_id)

    assert len(acquired) == 1
    assert service.calls[0]["session_operation_lease"] is lease
    assert lease.close_calls == 1
    assert lease.close_finished.is_set()


@pytest.mark.asyncio
async def test_cancelled_execute_request_before_transfer_cannot_interrupt_lease_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid4()
    harness = _route_harness(session_id)
    lease = _ControllableLease(_context(session_id))
    acquired = _install_acquire(monkeypatch, lease)
    service = _RouteExecutionService(uuid4())
    task = asyncio.create_task(_invoke_execute_route(harness, service, session_id=session_id))
    await asyncio.wait_for(service.started.wait(), timeout=2)

    task.cancel()
    await asyncio.wait_for(lease.close_started.wait(), timeout=2)
    task.cancel()  # A second cancellation must not cancel close()/renewal-task join.
    await asyncio.sleep(0)
    assert not task.done()
    lease.close_allowed.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert len(acquired) == 1
    assert lease.close_calls == 1
    assert not lease.close_cancelled
    assert lease.close_finished.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "worker_outcome",
    ["success", "failure", "graceful_cancel", "cancelled_future", "already_done"],
)
async def test_submitted_worker_retains_exact_lease_until_every_terminal_outcome(
    worker_outcome: str,
) -> None:
    loop = asyncio.get_running_loop()
    service, session_service, executor = _execution_service(loop)
    session_id = session_service.get_current_state.return_value.session_id
    lease, authority = await _real_lease(_context(session_id))
    authority.release_allowed.set()
    validation = ValidationResult(
        is_valid=True,
        checks=[],
        errors=[],
        readiness=ValidationReadiness(
            authoring_valid=True,
            execution_ready=True,
            completion_ready=True,
            blockers=[],
        ),
    )
    if worker_outcome == "already_done":
        executor.future.set_result(None)

    with patch("elspeth.web.execution.validation.validate_pipeline", return_value=validation):
        run_id = await service.execute(
            session_id,
            session_operation_lease=lease,
        )

    assert run_id == session_service.create_run.return_value.id
    assert len(executor.submit_calls) == 1
    submitted = executor.submit_calls[0]
    assert submitted[0] == service._run_pipeline
    assert submitted[2] == {"session_operation_lease": lease}
    assert all(argument is not lease.context for argument in submitted[1])
    submitted_authorities = (*submitted[1], *submitted[2].values())
    assert sum(argument is lease for argument in submitted_authorities) == 1
    assert sum(argument is lease.context for argument in submitted_authorities) == 0
    if worker_outcome != "already_done":
        assert authority.release_calls == []

    if worker_outcome == "failure":
        executor.future.set_exception(RuntimeError("worker failed"))
    elif worker_outcome == "cancelled_future":
        executor.future.cancel()
    elif worker_outcome == "graceful_cancel":
        executor.future.set_result("graceful_shutdown_handled")
    elif worker_outcome != "already_done":
        executor.future.set_result(None)

    await asyncio.wait_for(asyncio.to_thread(authority.release_called.wait, 2), timeout=2)
    for _ in range(100):
        if lease.closed:
            break
        await asyncio.sleep(0.01)
    assert lease.closed
    assert authority.release_calls == [lease.context]


@pytest.mark.asyncio
async def test_execute_rejects_lease_subclass_before_any_effect() -> None:
    class _LeaseSubclass(SessionOperationLease):
        pass

    service, session_service, executor = _execution_service(asyncio.get_running_loop())
    session_id = session_service.get_current_state.return_value.session_id
    forged = object.__new__(_LeaseSubclass)

    with pytest.raises(TypeError, match="exact SessionOperationLease"):
        await service.execute(session_id, session_operation_lease=forged)

    session_service.get_active_run.assert_not_awaited()
    session_service.create_run.assert_not_awaited()
    assert executor.submit_calls == []


@pytest.mark.asyncio
async def test_execute_wires_real_renewal_loss_to_exact_worker_shutdown_and_retains_lease() -> None:
    service, session_service, executor = _execution_service(asyncio.get_running_loop())
    session_id = session_service.get_current_state.return_value.session_id
    loss = SessionOperationFenceLost(FenceLossReason.LEASE_EXPIRED)
    lease, authority = await _real_lease(
        _context(session_id),
        renew_interval_seconds=0.01,
        renew_error=loss,
    )
    validation = ValidationResult(
        is_valid=True,
        checks=[],
        errors=[],
        readiness=ValidationReadiness(
            authoring_valid=True,
            execution_ready=True,
            completion_ready=True,
            blockers=[],
        ),
    )

    with patch("elspeth.web.execution.validation.validate_pipeline", return_value=validation):
        await service.execute(session_id, session_operation_lease=lease)

    submitted = executor.submit_calls[0]
    assert submitted[2]["session_operation_lease"] is lease
    worker_shutdown_event = submitted[1][2]
    assert isinstance(worker_shutdown_event, threading.Event)
    assert not worker_shutdown_event.is_set()
    effect_counts_after_submit = {
        name: getattr(session_service, name).call_count
        for name in ("create_run", "update_run_status", "append_run_event", "record_blob_inline_resolutions")
    }

    await asyncio.wait_for(asyncio.to_thread(authority.renew_called.wait, 2), timeout=2)
    assert authority.renew_called.is_set()
    await asyncio.wait_for(asyncio.to_thread(worker_shutdown_event.wait, 2), timeout=2)
    assert executor.future.running() or not executor.future.done()
    assert authority.release_calls == [], "renewal loss signals cancellation but completion still owns the lease"
    await asyncio.sleep(0.02)
    assert {name: getattr(session_service, name).call_count for name in effect_counts_after_submit} == effect_counts_after_submit, (
        "renewal loss must not originate a second durable effect path"
    )
    assert not lease.closed, "renewal loss signals the worker but cannot retire authority before worker completion"
    with pytest.raises(SessionOperationFenceLost) as raised:
        lease.raise_if_lost()
    assert raised.value is loss

    executor.future.set_result(None)
    for _ in range(100):
        if lease.closed:
            break
        await asyncio.sleep(0.01)
    assert lease.closed
    assert authority.release_calls == [], "a proven-lost lease closes without releasing a successor's authority"


@pytest.mark.asyncio
async def test_submit_failure_after_run_creation_terminalizes_before_return() -> None:
    service, session_service, executor = _execution_service(asyncio.get_running_loop())
    session_id = session_service.get_current_state.return_value.session_id
    lease, authority = await _real_lease(_context(session_id))
    authority.release_allowed.set()
    trace: list[str] = []
    run = session_service.create_run.return_value

    def create_run(**_kwargs: object) -> object:
        trace.append("create_run")
        return run

    def update_run_status(*_args: object, **_kwargs: object) -> None:
        trace.append("terminalize")

    session_service.create_run.side_effect = create_run
    session_service.update_run_status.side_effect = update_run_status
    executor.trace = trace
    executor.submit_error = RuntimeError("executor unavailable")
    validation = ValidationResult(
        is_valid=True,
        checks=[],
        errors=[],
        readiness=ValidationReadiness(
            authoring_valid=True,
            execution_ready=True,
            completion_ready=True,
            blockers=[],
        ),
    )

    with (
        patch("elspeth.web.execution.validation.validate_pipeline", return_value=validation),
        pytest.raises(RuntimeError, match="executor unavailable"),
    ):
        await service.execute(session_id, session_operation_lease=lease)

    assert trace == ["create_run", "submit", "terminalize"]
    terminal = session_service.update_run_status.await_args
    assert terminal.args == (run.id,)
    assert terminal.kwargs["status"] == "failed"
    assert terminal.kwargs["session_operation_context"] is lease.context
    assert authority.release_calls == [], "route still owns a failed pre-transfer lease"
    await lease.close()


@pytest.mark.asyncio
async def test_completion_cancellation_still_joins_exact_close_once() -> None:
    service, _session_service, executor = _execution_service(asyncio.get_running_loop())
    lease, authority = await _real_lease(_context(uuid4()))
    watcher_cancelled = asyncio.Event()
    watcher_allowed = asyncio.Event()

    async def stubborn_watcher() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            watcher_cancelled.set()
            await watcher_allowed.wait()
            raise

    watcher = asyncio.create_task(stubborn_watcher())
    await asyncio.sleep(0)
    worker: Future[object] = Future()
    worker.set_result(None)
    service._on_pipeline_done(
        cast(Any, worker),
        session_operation_lease=lease,
        loss_watcher=watcher,
    )
    await asyncio.wait_for(watcher_cancelled.wait(), timeout=2)
    with service._shutdown_events_lock:
        completion = next(iter(service._lease_completion_futures))
    completion.cancel()
    completion.cancel()  # Repeated observer cancellation must not retire the underlying join.

    shutdown = asyncio.create_task(service.shutdown())
    try:
        await asyncio.wait_for(asyncio.to_thread(executor.shutdown_started.wait, 2), timeout=2)
        await asyncio.sleep(0)
        assert not shutdown.done(), "shutdown lost the cancelled completion before its lease close/join"
    finally:
        watcher_allowed.set()
        authority.release_allowed.set()
        await asyncio.wait_for(shutdown, timeout=2)

    await asyncio.wait_for(asyncio.to_thread(authority.release_called.wait, 2), timeout=2)
    assert authority.release_calls == [lease.context]
    assert lease.closed


@pytest.mark.asyncio
async def test_runtime_shutdown_waits_for_blocked_lease_completion() -> None:
    service, _session_service, executor = _execution_service(asyncio.get_running_loop())
    lease, authority = await _real_lease(_context(uuid4()))
    worker: Future[object] = Future()
    worker.set_result(None)
    service._on_pipeline_done(cast(Any, worker), session_operation_lease=lease)
    await asyncio.wait_for(asyncio.to_thread(authority.release_called.wait, 2), timeout=2)

    shutdown = asyncio.create_task(service.shutdown())
    await asyncio.wait_for(asyncio.to_thread(executor.shutdown_started.wait, 2), timeout=2)
    await asyncio.sleep(0)
    assert not shutdown.done()

    authority.release_allowed.set()
    await asyncio.wait_for(shutdown, timeout=2)
    assert lease.closed
    assert authority.release_calls == [lease.context]


def _function_node(owner: type[object], name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(owner, name))))
    return next(node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)


def _class_node(owner: type[object]) -> ast.ClassDef:
    tree = ast.parse(textwrap.dedent(inspect.getsource(owner)))
    return next(node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == owner.__name__)


def _required_parameter(owner: type[object], name: str, parameter: str) -> inspect.Parameter:
    found = inspect.signature(getattr(owner, name)).parameters.get(parameter)
    assert found is not None, f"{owner.__name__}.{name} has no required {parameter}"
    assert found.default is inspect.Parameter.empty, f"{owner.__name__}.{name}.{parameter} is optional"
    return found


@pytest.mark.parametrize("owner", [ExecutionService, ExecutionServiceImpl])
def test_execute_contract_requires_one_transferred_lease(owner: type[object]) -> None:
    parameter = _required_parameter(owner, "execute", "session_operation_lease")
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.annotation is SessionOperationLease or parameter.annotation == "SessionOperationLease"


def test_worker_and_completion_contracts_retain_exact_authority() -> None:
    worker_lease = _required_parameter(ExecutionServiceImpl, "_run_pipeline", "session_operation_lease")
    assert worker_lease.annotation is SessionOperationLease or worker_lease.annotation == "SessionOperationLease"
    setup_lease = _required_parameter(ExecutionServiceImpl, "_execute_locked", "session_operation_lease")
    assert setup_lease.annotation is SessionOperationLease or setup_lease.annotation == "SessionOperationLease"
    lease = _required_parameter(ExecutionServiceImpl, "_on_pipeline_done", "session_operation_lease")
    assert lease.annotation is SessionOperationLease or lease.annotation == "SessionOperationLease"


def test_orchestrator_run_accepts_optional_caller_coordination_latch() -> None:
    parameter = inspect.signature(Orchestrator.run).parameters.get("check_coordination_latch")
    assert parameter is not None
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is None


@pytest.mark.parametrize(
    "method_name",
    ["create_run", "update_run_status", "append_run_event", "record_blob_inline_resolutions"],
)
def test_legacy_session_run_writers_require_exact_context(method_name: str) -> None:
    parameter = _required_parameter(SessionServiceProtocol, method_name, "session_operation_context")
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.annotation is SessionOperationContext or parameter.annotation == "SessionOperationContext"


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _walk_function_body_without_nested_functions(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[ast.AST, ...]:
    visited: list[ast.AST] = []

    class _Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, child: ast.FunctionDef) -> None:
            if child is node:
                self.generic_visit(child)

        def visit_AsyncFunctionDef(self, child: ast.AsyncFunctionDef) -> None:
            if child is node:
                self.generic_visit(child)

        def generic_visit(self, child: ast.AST) -> None:
            visited.append(child)
            super().generic_visit(child)

    _Visitor().visit(node)
    return tuple(visited)


def _exact_context_keyword(call: ast.Call) -> bool:
    return any(
        keyword.arg == "session_operation_context"
        and isinstance(keyword.value, ast.Name)
        and keyword.value.id == "session_operation_context"
        for keyword in call.keywords
    )


def _exact_lease_keyword(call: ast.Call) -> bool:
    return any(
        keyword.arg == "session_operation_lease" and isinstance(keyword.value, ast.Name) and keyword.value.id == "session_operation_lease"
        for keyword in call.keywords
    )


_FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef
_UNKNOWN_STATIC_VALUE = object()


def _static_value(node: ast.AST) -> object:
    """Evaluate a name-free constant expression; return a sentinel otherwise."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        values = [_static_value(element) for element in node.elts]
        if any(value is _UNKNOWN_STATIC_VALUE for value in values):
            return _UNKNOWN_STATIC_VALUE
        constructor = {ast.Tuple: tuple, ast.List: list, ast.Set: set}[type(node)]
        return constructor(values)
    if isinstance(node, ast.Dict):
        keys = [_static_value(key) for key in node.keys if key is not None]
        values = [_static_value(value) for value in node.values]
        if len(keys) != len(node.keys) or any(value is _UNKNOWN_STATIC_VALUE for value in (*keys, *values)):
            return _UNKNOWN_STATIC_VALUE
        return dict(zip(keys, values, strict=True))
    if isinstance(node, ast.UnaryOp):
        operand = _static_value(node.operand)
        operation = {
            ast.Not: operator.not_,
            ast.UAdd: operator.pos,
            ast.USub: operator.neg,
            ast.Invert: operator.invert,
        }.get(type(node.op))
        if operand is _UNKNOWN_STATIC_VALUE or operation is None:
            return _UNKNOWN_STATIC_VALUE
        try:
            return operation(operand)
        except (ArithmeticError, TypeError, ValueError):
            return _UNKNOWN_STATIC_VALUE
    if isinstance(node, ast.BinOp):
        left = _static_value(node.left)
        right = _static_value(node.right)
        operation = {
            ast.Add: operator.add,
            ast.Sub: operator.sub,
            ast.Mult: operator.mul,
            ast.Div: operator.truediv,
            ast.FloorDiv: operator.floordiv,
            ast.Mod: operator.mod,
            ast.BitAnd: operator.and_,
            ast.BitOr: operator.or_,
            ast.BitXor: operator.xor,
        }.get(type(node.op))
        if left is _UNKNOWN_STATIC_VALUE or right is _UNKNOWN_STATIC_VALUE or operation is None:
            return _UNKNOWN_STATIC_VALUE
        try:
            return operation(left, right)
        except (ArithmeticError, TypeError, ValueError):
            return _UNKNOWN_STATIC_VALUE
    if isinstance(node, ast.BoolOp):
        values = [_static_value(value) for value in node.values]
        if any(value is _UNKNOWN_STATIC_VALUE for value in values):
            return _UNKNOWN_STATIC_VALUE
        return all(map(bool, values)) if isinstance(node.op, ast.And) else any(map(bool, values))
    if isinstance(node, ast.Compare):
        operands = [_static_value(node.left), *(_static_value(comparator) for comparator in node.comparators)]
        operations = {
            ast.Eq: operator.eq,
            ast.NotEq: operator.ne,
            ast.Lt: operator.lt,
            ast.LtE: operator.le,
            ast.Gt: operator.gt,
            ast.GtE: operator.ge,
            ast.Is: operator.is_,
            ast.IsNot: operator.is_not,
            ast.In: operator.contains,
            ast.NotIn: lambda container, member: not operator.contains(container, member),
        }
        if any(value is _UNKNOWN_STATIC_VALUE for value in operands):
            return _UNKNOWN_STATIC_VALUE
        results: list[bool] = []
        for index, comparison in enumerate(node.ops):
            operation = operations.get(type(comparison))
            if operation is None:
                return _UNKNOWN_STATIC_VALUE
            left, right = operands[index : index + 2]
            try:
                result = operation(right, left) if isinstance(comparison, (ast.In, ast.NotIn)) else operation(left, right)
            except (ArithmeticError, TypeError, ValueError):
                return _UNKNOWN_STATIC_VALUE
            results.append(result)
        return all(results)
    return _UNKNOWN_STATIC_VALUE


def _static_truth(node: ast.AST | None) -> bool | None:
    if node is None:
        return True
    value = _static_value(node)
    return None if value is _UNKNOWN_STATIC_VALUE else bool(value)


def _static_pattern_matches(pattern: ast.pattern, subject: object) -> bool | None:
    if isinstance(pattern, ast.MatchSingleton):
        return subject is pattern.value
    if isinstance(pattern, ast.MatchValue):
        pattern_value = _static_value(pattern.value)
        return None if pattern_value is _UNKNOWN_STATIC_VALUE else subject == pattern_value
    if isinstance(pattern, ast.MatchOr):
        alternatives = [_static_pattern_matches(alternative, subject) for alternative in pattern.patterns]
        if any(result is True for result in alternatives):
            return True
        return None if any(result is None for result in alternatives) else False
    if isinstance(pattern, ast.MatchAs) and pattern.pattern is None:
        return True
    return None


def _expression_nodes(statement: ast.stmt) -> tuple[ast.AST, ...]:
    """Return a statement's evaluated expressions, excluding nested statement bodies."""
    nodes: list[ast.AST] = [statement]
    for _field, value in ast.iter_fields(statement):
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if not isinstance(candidate, ast.AST) or isinstance(candidate, (ast.stmt, ast.ExceptHandler, ast.match_case)):
                continue
            nodes.extend(ast.walk(candidate))
    return tuple(nodes)


def _reachable_block_nodes(statements: list[ast.stmt]) -> tuple[tuple[ast.AST, ...], bool]:
    """Conservatively walk executable statements and reject definite dead tails."""
    nodes: list[ast.AST] = []
    falls_through = True
    for statement in statements:
        if not falls_through:
            break
        nodes.extend(_expression_nodes(statement))
        if isinstance(statement, ast.If):
            condition = _static_truth(statement.test)
            if condition is not None:
                chosen = statement.body if condition else statement.orelse
                branch_nodes, falls_through = _reachable_block_nodes(chosen)
                nodes.extend(branch_nodes)
            else:
                body_nodes, body_falls = _reachable_block_nodes(statement.body)
                else_nodes, else_falls = _reachable_block_nodes(statement.orelse)
                nodes.extend((*body_nodes, *else_nodes))
                falls_through = body_falls or (else_falls if statement.orelse else True)
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            body_nodes, falls_through = _reachable_block_nodes(statement.body)
            nodes.extend(body_nodes)
        elif isinstance(statement, (ast.For, ast.AsyncFor)):
            iterator = _static_value(statement.iter)
            iterator_is_empty = (
                iterator is not _UNKNOWN_STATIC_VALUE and isinstance(iterator, (tuple, list, set, dict, str, bytes)) and not iterator
            )
            if iterator_is_empty:
                else_nodes, _else_falls = _reachable_block_nodes(statement.orelse)
                nodes.extend(else_nodes)
            else:
                body_nodes, _body_falls = _reachable_block_nodes(statement.body)
                else_nodes, _else_falls = _reachable_block_nodes(statement.orelse)
                nodes.extend((*body_nodes, *else_nodes))
            falls_through = True
        elif isinstance(statement, ast.While):
            condition = _static_truth(statement.test)
            if condition is False:
                else_nodes, falls_through = _reachable_block_nodes(statement.orelse)
                nodes.extend(else_nodes)
            else:
                body_nodes, _body_falls = _reachable_block_nodes(statement.body)
                reachable_break = any(isinstance(node, ast.Break) for node in body_nodes)
                if condition is True and not reachable_break:
                    nodes.extend(body_nodes)
                    falls_through = False
                else:
                    else_nodes, _else_falls = _reachable_block_nodes(statement.orelse)
                    nodes.extend((*body_nodes, *else_nodes))
                    falls_through = True
        elif isinstance(statement, (ast.Try, ast.TryStar)):
            body_nodes, body_falls = _reachable_block_nodes(statement.body)
            else_nodes, else_falls = _reachable_block_nodes(statement.orelse)
            handler_results = [_reachable_block_nodes(handler.body) for handler in statement.handlers]
            final_nodes, final_falls = _reachable_block_nodes(statement.finalbody)
            handler_binding_nodes = [
                candidate
                for handler in statement.handlers
                for candidate in (
                    handler,
                    *(ast.walk(handler.type) if handler.type is not None else ()),
                )
            ]
            nodes.extend(
                (
                    *body_nodes,
                    *else_nodes,
                    *handler_binding_nodes,
                    *(node for result, _falls in handler_results for node in result),
                    *final_nodes,
                )
            )
            ordinary_falls = body_falls and (else_falls if statement.orelse else True)
            handled_falls = any(handler_falls for _handler_nodes, handler_falls in handler_results)
            falls_through = final_falls and (ordinary_falls or handled_falls)
        elif isinstance(statement, ast.Match):
            subject = _static_value(statement.subject)
            selected_cases: list[ast.match_case] = []
            guaranteed_match = False
            for case in statement.cases:
                pattern_match = None if subject is _UNKNOWN_STATIC_VALUE else _static_pattern_matches(case.pattern, subject)
                guard_truth = _static_truth(case.guard)
                if pattern_match is False or guard_truth is False:
                    continue
                selected_cases.append(case)
                if pattern_match is True and guard_truth is True:
                    guaranteed_match = True
                    break
            case_results = [_reachable_block_nodes(case.body) for case in selected_cases]
            nodes.extend(
                candidate
                for case in selected_cases
                for candidate in (
                    *ast.walk(case.pattern),
                    *(ast.walk(case.guard) if case.guard is not None else ()),
                )
            )
            nodes.extend(node for result, _falls in case_results for node in result)
            falls_through = not guaranteed_match or any(case_falls for _case_nodes, case_falls in case_results)
        elif isinstance(statement, (ast.Return, ast.Raise, ast.Break, ast.Continue)) or (
            isinstance(statement, ast.Assert) and _static_truth(statement.test) is False
        ):
            falls_through = False
    return tuple(nodes), falls_through


def _reachable_function_nodes(node: _FunctionNode) -> tuple[ast.AST, ...]:
    return _reachable_block_nodes(node.body)[0]


def _is_exact_progress_callback_edge(call: ast.Call, callback: ast.AST) -> bool:
    return (
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "event_bus"
        and call.func.attr == "subscribe"
        and len(call.args) == 2
        and not call.keywords
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "ProgressEvent"
        and call.args[1] is callback
    )


def _binding_nodes(
    function: _FunctionNode,
    live_nodes: tuple[ast.AST, ...],
    *,
    name: str,
) -> tuple[ast.AST, ...]:
    parameters = [
        argument
        for argument in (
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
            *((function.args.vararg,) if function.args.vararg is not None else ()),
            *((function.args.kwarg,) if function.args.kwarg is not None else ()),
        )
        if argument.arg == name
    ]
    bindings = [
        node
        for node in live_nodes
        if (isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Store))
        or (isinstance(node, ast.ExceptHandler) and node.name == name)
        or (isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == name)
        or (isinstance(node, ast.MatchMapping) and node.rest == name)
        or (isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names)
        or (isinstance(node, (ast.Import, ast.ImportFrom)) and any((alias.asname or alias.name) == name for alias in node.names))
        or (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name)
    ]
    return (*parameters, *bindings)


def _has_unique_exact_event_bus_binding(
    function: _FunctionNode,
    live_nodes: tuple[ast.AST, ...],
) -> bool:
    event_bus_bindings = _binding_nodes(function, live_nodes, name="event_bus")
    exact_assignments = [
        node
        for node in live_nodes
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "event_bus"
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "EventBus"
        and not node.value.args
        and not node.value.keywords
    ]
    event_bus_constructor_bindings = _binding_nodes(function, live_nodes, name="EventBus")
    return len(event_bus_bindings) == 1 and len(exact_assignments) == 1 and event_bus_constructor_bindings == ()


def _reachable_execution_functions(owner: ast.ClassDef) -> tuple[_FunctionNode, ...]:
    members = {member.name: member for member in owner.body if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))}
    local_functions = {
        nested.name: nested
        for member in members.values()
        for nested in ast.walk(member)
        if isinstance(nested, (ast.FunctionDef, ast.AsyncFunctionDef)) and nested is not member
    }
    pending: list[_FunctionNode] = [members[name] for name in ("_execute_locked", "_run_pipeline", "_handle_pipeline_submission_failure")]
    reached: set[int] = set()
    result: list[_FunctionNode] = []
    while pending:
        function = pending.pop()
        if id(function) in reached:
            continue
        reached.add(id(function))
        result.append(function)
        live_nodes = _reachable_function_nodes(function)
        exact_event_bus_bound = _has_unique_exact_event_bus_binding(function, live_nodes)
        for call in (candidate for candidate in live_nodes if isinstance(candidate, ast.Call)):
            if (
                isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "self"
                and call.func.attr in members
            ):
                pending.append(members[call.func.attr])
            callable_edges: list[ast.expr] = []
            if isinstance(call.func, ast.Name):
                callable_edges.append(call.func)
            callable_edges.extend(
                edge
                for edge in call.args
                if exact_event_bus_bound and isinstance(edge, ast.Name) and _is_exact_progress_callback_edge(call, edge)
            )
            pending.extend(local_functions[edge.id] for edge in callable_edges if isinstance(edge, ast.Name) and edge.id in local_functions)
    return tuple(result)


def test_every_worker_run_blob_progress_output_and_terminal_effect_uses_same_context() -> None:
    """No helper may mint a context or silently fall back to legacy writers."""
    owner = _class_node(ExecutionServiceImpl)
    effect_names = {
        "create_run",
        "create_pending_run",
        "update_run_status",
        "transition_run_status",
        "append_run_event",
        "link_blob_to_run",
        "insert_blob_run_link",
        "record_blob_inline_resolutions",
        "insert_blob_inline_resolutions",
        "get_blob",
        "read_blob",
        "read_blob_content",
        "_fetch_blob_contents",
        "finalize_run_output_blobs",
    }
    reachable = _reachable_execution_functions(owner)
    live_nodes = tuple(node for function in reachable for node in _reachable_function_nodes(function))
    live_calls = tuple(node for node in live_nodes if isinstance(node, ast.Call))
    calls = [call for call in live_calls if _call_name(call) in effect_names]
    gate_call_names = effect_names | {"_persist_and_broadcast_run_event", "_finalize_output_blobs"}
    all_gate_calls = {
        id(call): call
        for function in reachable
        for call in ast.walk(function)
        if isinstance(call, ast.Call) and _call_name(call) in gate_call_names
    }
    live_gate_call_ids = {id(call) for call in live_calls if _call_name(call) in gate_call_names}
    unreachable_decoys = [call for call_id, call in all_gate_calls.items() if call_id not in live_gate_call_ids]
    assert unreachable_decoys == [], (
        "execution effects cannot hide in dead tails, false branches, or uncalled local functions: "
        f"{[(call.lineno, _call_name(call), ast.unparse(call)) for call in unreachable_decoys]}"
    )

    for call in calls:
        if _call_name(call) == "_fetch_blob_contents":
            assert isinstance(call.func, ast.Name)
            continue
        assert isinstance(call.func, ast.Attribute)
        receiver = ast.unparse(call.func.value)
        if call.func.attr in {
            "create_run",
            "update_run_status",
            "append_run_event",
            "record_blob_inline_resolutions",
        }:
            assert receiver == "self._session_service"
        elif call.func.attr in {
            "get_blob",
            "link_blob_to_run",
            "read_blob_content",
            "finalize_run_output_blobs",
        }:
            assert receiver in {"self._blob_service", "blob_service"}
    observed = {_call_name(call) for call in calls}
    assert observed & {"create_run", "create_pending_run"}, "run creation effect disappeared from execution"
    assert observed & {"update_run_status", "transition_run_status"}, "run status effects disappeared from execution"
    assert "append_run_event" in observed, "durable progress/terminal event effect disappeared"
    assert observed & {"link_blob_to_run", "insert_blob_run_link"}, "blob linkage effect disappeared"
    assert observed & {"get_blob", "read_blob"}, "input metadata read disappeared"
    assert observed & {"read_blob_content", "_fetch_blob_contents"}, "inline content read disappeared"
    assert observed & {
        "record_blob_inline_resolutions",
        "insert_blob_inline_resolutions",
    }, "inline-resolution audit effect disappeared"
    assert "finalize_run_output_blobs" in observed, "output finalization effect disappeared"
    offenders = sorted({_call_name(call) for call in calls if not _exact_context_keyword(call)})
    assert offenders == [], f"execution effects omit the exact transferred context: {offenders}"

    parent = {child: node for function in reachable for node in ast.walk(function) for child in ast.iter_child_nodes(node)}
    escaped = sorted(
        {
            node.attr
            for node in live_nodes
            if isinstance(node, ast.Attribute)
            and node.attr in effect_names
            and not (isinstance(parent.get(node), ast.Call) and cast(ast.Call, parent[node]).func is node)
        }
    )
    assert escaped == [], f"execution effect callable is aliased or escapes direct inspection: {escaped}"

    local_function_names = {
        nested.name
        for member in owner.body
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
        for nested in ast.walk(member)
        if isinstance(nested, (ast.FunctionDef, ast.AsyncFunctionDef)) and nested is not member
    }
    exact_event_bus_functions = [
        function for function in reachable if _has_unique_exact_event_bus_binding(function, _reachable_function_nodes(function))
    ]
    assert execution_service_module.EventBus is EventBus, "execution callback bus constructor provenance changed"
    assert [function.name for function in exact_event_bus_functions] == ["_run_pipeline"]

    def is_direct_callable_edge(node: ast.AST) -> bool:
        container = parent.get(node)
        if isinstance(container, ast.Call):
            return container.func is node or (bool(exact_event_bus_functions) and _is_exact_progress_callback_edge(container, node))
        return False

    escaped_local_helpers = [
        node.id
        for node in live_nodes
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id in local_function_names
        and not is_direct_callable_edge(node)
    ]
    assert escaped_local_helpers == [], f"local execution helper callable escapes direct analysis: {escaped_local_helpers}"

    class_member_names = {member.name for member in owner.body if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))}
    escaped_class_helpers = [
        node.attr
        for node in live_nodes
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr in class_member_names
        and not (isinstance(parent.get(node), ast.Call) and cast(ast.Call, parent[node]).func is node)
    ]
    assert escaped_class_helpers == [], f"class execution helper callable is aliased or escapes: {escaped_class_helpers}"

    forbidden_dynamic_symbols = {
        "attrgetter",
        "eval",
        "exec",
        "getattr",
        "getattr_static",
        "globals",
        "locals",
        "methodcaller",
        "super",
        "vars",
        "__builtins__",
        "__getattr__",
        "__getattribute__",
    }
    dynamic_self_access = [
        node
        for node in live_nodes
        if (isinstance(node, ast.Name) and node.id in forbidden_dynamic_symbols)
        or (isinstance(node, ast.Attribute) and node.attr in forbidden_dynamic_symbols)
        or (isinstance(node, ast.alias) and node.name.rsplit(".", maxsplit=1)[-1] in forbidden_dynamic_symbols)
        or (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == owner.name)
        or (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id == "type"
            and not (isinstance(parent.get(node), ast.Call) and cast(ast.Call, parent[node]).func is node)
        )
        or (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id == "self"
            and not (isinstance(parent.get(node), ast.Attribute) and cast(ast.Attribute, parent[node]).value is node)
        )
        or (isinstance(node, ast.Attribute) and node.attr in {"__class__", "__dict__", "__getattr__", "__getattribute__"})
        or (
            isinstance(node, ast.Attribute)
            and node.attr in class_member_names
            and not (isinstance(node.value, ast.Name) and node.value.id == "self")
        )
    ]
    assert dynamic_self_access == [], "execution helpers must not escape static authority analysis through dynamic self access"

    unsafe_effect_lambdas = [
        node
        for node in live_nodes
        if isinstance(node, ast.Lambda)
        and (
            any(_call_name(candidate) in gate_call_names for candidate in ast.walk(node) if isinstance(candidate, ast.Call))
            or any(
                (isinstance(candidate, ast.Name) and candidate.id == "session_operation_context")
                or (isinstance(candidate, ast.arg) and candidate.arg == "session_operation_context")
                for candidate in ast.walk(node)
            )
        )
    ]
    assert unsafe_effect_lambdas == [], "context-bearing execution effects cannot hide inside lambdas"

    minted_contexts = [call for call in live_calls if _call_name(call) in {"SessionOperationContext", "SessionOperationFence"}]
    assert minted_contexts == [], "execution helpers must not mint replacement operation authority"

    functions_by_name = {function.name: function for function in reachable}
    context_callee_names = {
        name
        for name, function in functions_by_name.items()
        if "session_operation_context"
        in {argument.arg for argument in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)}
    }
    for function in reachable:
        for call in (candidate for candidate in _reachable_function_nodes(function) if isinstance(candidate, ast.Call)):
            callee: _FunctionNode | None = None
            if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name) and call.func.value.id == "self":
                callee = functions_by_name.get(call.func.attr)
            elif isinstance(call.func, ast.Name):
                callee = functions_by_name.get(call.func.id)
            if callee is None:
                continue
            callee_parameters = {argument.arg for argument in (*callee.args.posonlyargs, *callee.args.args, *callee.args.kwonlyargs)}
            if "session_operation_context" in callee_parameters:
                assert _exact_context_keyword(call), f"{function.name} must forward its exact context to reachable helper {callee.name}"

    class_member_ids = {id(member) for member in owner.body if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for member in reachable:
        parameter_names = {argument.arg for argument in (*member.args.posonlyargs, *member.args.args, *member.args.kwonlyargs)}
        rebindings = [
            node
            for node in ast.walk(member)
            if isinstance(node, ast.Name) and node.id == "session_operation_context" and isinstance(node.ctx, ast.Store)
        ]
        if rebindings:
            assert "session_operation_lease" in parameter_names, f"{member.name} may derive context only from its exact transferred lease"
            assert len(rebindings) == 1
            assignment = parent[rebindings[0]]
            assert isinstance(assignment, ast.Assign)
            assert ast.unparse(assignment.value) == "session_operation_lease.context"
        context_scope_escapes = [
            node
            for node in ast.walk(member)
            if (isinstance(node, (ast.Global, ast.Nonlocal)) and "session_operation_context" in node.names)
            or (
                isinstance(node, (ast.Import, ast.ImportFrom))
                and any((alias.asname or alias.name) == "session_operation_context" for alias in node.names)
            )
            or (isinstance(node, ast.ExceptHandler) and node.name == "session_operation_context")
            or (isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == "session_operation_context")
            or (isinstance(node, ast.MatchMapping) and node.rest == "session_operation_context")
            or (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node is not member
                and node.name == "session_operation_context"
            )
        ]
        assert context_scope_escapes == [], f"{member.name} replaces context authority through scope/import binding"
        member_calls = [candidate for candidate in _reachable_function_nodes(member) if isinstance(candidate, ast.Call)]
        has_context_edge = any(_call_name(call) in effect_names | context_callee_names for call in member_calls)
        if id(member) in class_member_ids and has_context_edge:
            assert parameter_names & {"session_operation_context", "session_operation_lease"}, (
                f"class helper {member.name} participates in execution effects without explicit context or lease authority"
            )

    terminal_event_types: set[str] = set()
    for call in live_calls:
        if _call_name(call) != "_persist_and_broadcast_run_event" or not _exact_lease_keyword(call):
            continue
        for value in (*call.args, *(keyword.value for keyword in call.keywords)):
            for event_call in (candidate for candidate in ast.walk(value) if isinstance(candidate, ast.Call)):
                if _call_name(event_call) != "RunEvent":
                    continue
                for keyword in event_call.keywords:
                    if keyword.arg == "event_type" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                        terminal_event_types.add(keyword.value.value)
    assert {"completed", "failed", "cancelled"} <= terminal_event_types, (
        "each terminal event must be bound to a live persisted+broadcast effect under the exact lease"
    )

    terminal_status_values = {
        keyword.value.value if isinstance(keyword.value, ast.Constant) else keyword.value.id
        for call in live_calls
        if _call_name(call) == "update_run_status" and _exact_context_keyword(call)
        for keyword in call.keywords
        if keyword.arg == "status" and isinstance(keyword.value, (ast.Constant, ast.Name))
    }
    assert {"failed", "cancelled", "session_status"} <= terminal_status_values

    assert any(
        _call_name(call) == "_finalize_output_blobs"
        and _exact_lease_keyword(call)
        and keyword.arg == "success"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is False
        for call in live_calls
        for keyword in call.keywords
    ), "failure/cancellation compensation must be a live output-finalization effect under the exact lease"


def test_loss_watcher_signals_shutdown_without_becoming_a_lease_owned_task() -> None:
    owner = _class_node(ExecutionServiceImpl)
    watchers: list[ast.AsyncFunctionDef] = []
    for member in owner.body:
        if not isinstance(member, ast.AsyncFunctionDef):
            continue
        names = {_call_name(node) for node in ast.walk(member) if isinstance(node, ast.Call)}
        if "wait_until_lost" in names:
            watchers.append(member)
    assert len(watchers) == 1, "execution must have one explicit lease-loss watcher"
    watcher = watchers[0]
    call_names = [_call_name(node) for node in ast.walk(watcher) if isinstance(node, ast.Call)]
    assert "set" in call_names, "lease loss must signal the worker shutdown event"
    assert "create_task" not in call_names, "a lease-owned watcher can be cancelled before it signals loss"


def test_done_callback_is_nonblocking_and_async_cleanup_closes_exactly_once() -> None:
    callback = _function_node(ExecutionServiceImpl, "_on_pipeline_done")
    callback_body = _walk_function_body_without_nested_functions(callback)
    callback_calls = [_call_name(node) for node in callback_body if isinstance(node, ast.Call)]
    assert "run_coroutine_threadsafe" in callback_calls
    assert callback_calls.count("exception") + callback_calls.count("result") == 1, "worker Future must be consumed exactly once"
    assert not any(isinstance(node, ast.Await) for node in callback_body)

    owner = _class_node(ExecutionServiceImpl)
    close_sites: list[ast.AsyncFunctionDef] = []
    for member in ast.walk(owner):
        if not isinstance(member, ast.AsyncFunctionDef):
            continue
        closes = [
            node
            for node in ast.walk(member)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "close"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "session_operation_lease"
        ]
        if closes:
            close_sites.extend([member] * len(closes))
    assert len(close_sites) == 1, "one async completion path must own the sole execution-lease close"
    close_function = close_sites[0]
    finally_closes = [
        call
        for try_node in ast.walk(close_function)
        if isinstance(try_node, ast.Try)
        for statement in try_node.finalbody
        for call in ast.walk(statement)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "close"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "session_operation_lease"
    ]
    assert len(finally_closes) == 1, "the exact lease close must be structurally inside cleanup finally"


def test_submit_failure_terminalizes_under_same_context_before_authority_returns() -> None:
    nodes = [
        _function_node(ExecutionServiceImpl, name)
        for name in ("execute", "_execute_locked", "_handle_pipeline_submission_failure")
        if hasattr(ExecutionServiceImpl, name)
    ]
    handlers = [handler for node in nodes for handler in ast.walk(node) if isinstance(handler, ast.ExceptHandler)]
    assert handlers, "execution setup needs an explicit failure path"
    terminal_calls = [
        call
        for node in nodes
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and _call_name(call) in {"update_run_status", "transition_run_status"}
    ]
    assert terminal_calls, "submit/setup failure can leave a permanently pending run"
    assert all(_exact_context_keyword(call) for call in terminal_calls)


def test_shutdown_drains_executor_then_awaits_all_lease_completions() -> None:
    node = _function_node(ExecutionServiceImpl, "shutdown")
    executor_statements = [
        index
        for index, statement in enumerate(node.body)
        if any(isinstance(call, ast.Call) and _call_name(call) == "shutdown" for call in ast.walk(statement))
        or any(isinstance(attribute, ast.Attribute) and attribute.attr == "shutdown" for attribute in ast.walk(statement))
    ]
    completion_statements = [
        index
        for index, statement in enumerate(node.body)
        if any(isinstance(attribute, ast.Attribute) and attribute.attr == "_lease_completion_futures" for attribute in ast.walk(statement))
    ]
    assert len(executor_statements) == 1
    assert completion_statements and min(completion_statements) > executor_statements[0]
    completion_nodes = node.body[min(completion_statements) :]
    assert any(isinstance(candidate, ast.Await) for statement in completion_nodes for candidate in ast.walk(statement))


def _real_session_service(
    engine: Engine,
    authority: SQLiteLocalSessionOperationAuthority,
) -> SessionServiceImpl:
    return SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.execution-lease-uow"),
        session_operation_authority=authority,
        owner_instance_id="execution-uow-test",
        session_operation_lease_seconds=30,
    )


async def _seed_run_through_service(
    service: SessionServiceImpl,
    authority: SQLiteLocalSessionOperationAuthority,
    *,
    title: str,
) -> tuple[UUID, UUID]:
    """Seed only through reviewed SessionService writers, never direct table DML."""
    session = await service.create_session(_USER_ID, title, "local")
    state = await service.save_composition_state(
        session.id,
        CompositionStateData(is_valid=True),
        provenance="session_seed",
    )
    context = _acquire(authority, session_id=session.id, kind=SessionOperationKind.EXECUTE)
    try:
        run = await service.create_run(
            session.id,
            state.id,
            session_operation_context=context,
        )
    finally:
        authority.release(context)
    return session.id, run.id


def _acquire(
    authority: SQLiteLocalSessionOperationAuthority,
    *,
    session_id: UUID,
    kind: SessionOperationKind,
) -> SessionOperationContext:
    return authority.acquire(
        session_id=session_id,
        operation_kind=kind,
        owner_instance_id="execution-uow-test",
        lease_seconds=30,
    )


def _append_progress(
    authority: SQLiteLocalSessionOperationAuthority,
    context: SessionOperationContext,
    *,
    run_id: UUID,
) -> None:
    authority.mutate(
        context,
        lambda transaction: transaction.runs.append_run_event(
            run_id=run_id,
            timestamp=datetime.now(UTC),
            event_type="progress",
            data={"phase": "running"},
        ),
    )


def _is_run_event_dml(statement: str) -> bool:
    without_comments = re.sub(r"--[^\r\n]*|/\*.*?\*/", " ", statement.lower(), flags=re.DOTALL)
    normalized = re.sub(r'["`\[\]]', "", without_comments)
    return (
        re.search(
            r"\b(?:"
            r"insert(?:\s+or\s+\w+)?\s+into|"
            r"replace(?:\s+or\s+\w+)?\s+into|"
            r"update|delete\s+from|merge\s+into"
            r"|copy|truncate(?:\s+table)?"
            r")\s+(?:only\s+)?(?:\w+\s*\.\s*)?run_events\b",
            normalized,
        )
        is not None
    )


@contextmanager
def _capture_run_event_dml(engine: Engine) -> Iterator[list[str]]:
    statements: list[str] = []

    def capture(_connection: object, _cursor: object, statement: str, _parameters: object, _context: object, _many: bool) -> None:
        if _is_run_event_dml(statement):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO run_events (id) VALUES (?)",
        "UPDATE run_events SET event_sequence = 2",
        "DELETE FROM run_events WHERE id = ?",
        'WITH marker AS (SELECT 1) INSERT INTO "run_events" (id) VALUES (?)',
        "WITH target AS (SELECT id FROM run_events) DELETE FROM main.run_events WHERE id IN (SELECT id FROM target)",
        "REPLACE INTO run_events (id) VALUES (?)",
        "INSERT /* admission */ INTO ONLY public.run_events (id) VALUES (?)",
        "UPDATE /* fenced */ ONLY run_events SET event_sequence = 2",
        "DELETE /* fenced */ FROM ONLY run_events WHERE id = ?",
        "MERGE /* fenced */ INTO public.run_events target USING candidate ON target.id = candidate.id WHEN MATCHED THEN DELETE",
        "COPY public.run_events (id) FROM STDIN",
        "TRUNCATE TABLE ONLY public.run_events",
        "INSERT INTO main . run_events (id) VALUES (?)",
        "UPDATE main . run_events SET event_sequence = 2",
        "DELETE FROM main . run_events WHERE id = ?",
    ],
)
def test_run_event_dml_capture_classifies_prefixed_and_cte_writes(statement: str) -> None:
    assert _is_run_event_dml(statement)


def test_run_event_dml_capture_observes_qualified_write_even_when_transaction_rolls_back(engine: Engine) -> None:
    with _capture_run_event_dml(engine) as target_dml, engine.connect() as connection:
        transaction = connection.begin()
        connection.exec_driver_sql(
            "UPDATE main . run_events SET sequence = sequence",
        )
        transaction.rollback()

    assert len(target_dml) == 1
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(run_events_table)).scalar_one() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wrong_kind",
    [
        SessionOperationKind.ARCHIVE,
        SessionOperationKind.BLOB_READ,
        SessionOperationKind.COMPOSE,
        SessionOperationKind.CREATE,
        SessionOperationKind.PROGRESS,
        SessionOperationKind.PROPOSAL,
        SessionOperationKind.SESSION_FORK,
    ],
)
async def test_run_uow_wrong_live_kind_performs_zero_event_dml(
    engine: Engine,
    wrong_kind: SessionOperationKind,
) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session_id, run_id = await _seed_run_through_service(
        _real_session_service(engine, authority),
        authority,
        title=f"wrong kind {wrong_kind.value}",
    )
    context = _acquire(authority, session_id=session_id, kind=wrong_kind)

    with _capture_run_event_dml(engine) as target_dml, pytest.raises(AuditIntegrityError, match=r"operation kind|authoriz"):
        _append_progress(authority, context, run_id=run_id)

    assert target_dml == []
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(run_events_table)).scalar_one() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("forgery", ["session", "operation_id", "lease_token", "epoch"])
async def test_run_uow_forged_context_performs_zero_event_dml(
    engine: Engine,
    forgery: str,
) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session_id, run_id = await _seed_run_through_service(
        _real_session_service(engine, authority),
        authority,
        title=f"forged {forgery}",
    )
    live = _acquire(authority, session_id=session_id, kind=SessionOperationKind.EXECUTE)
    fence = live.fence
    forged = SessionOperationContext(
        fence=SessionOperationFence(
            session_id=str(uuid4()) if forgery == "session" else fence.session_id,
            operation_id=str(uuid4()) if forgery == "operation_id" else fence.operation_id,
            lease_token="forged-lease-token" if forgery == "lease_token" else fence.lease_token,
            operation_epoch=fence.operation_epoch + 1 if forgery == "epoch" else fence.operation_epoch,
        ),
        operation_kind=SessionOperationKind.EXECUTE,
    )

    with _capture_run_event_dml(engine) as target_dml, pytest.raises(SessionOperationFenceLost):
        _append_progress(authority, forged, run_id=run_id)

    assert target_dml == []
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(run_events_table)).scalar_one() == 0


@pytest.mark.asyncio
async def test_run_uow_live_context_cannot_target_another_sessions_run(engine: Engine) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    service = _real_session_service(engine, authority)
    _first_session, foreign_run = await _seed_run_through_service(
        service,
        authority,
        title="foreign run owner",
    )
    second_session = (await service.create_session(_USER_ID, "live context owner", "local")).id
    live = _acquire(authority, session_id=second_session, kind=SessionOperationKind.EXECUTE)

    with _capture_run_event_dml(engine) as target_dml, pytest.raises(SessionDerivedCustodyError):
        _append_progress(authority, live, run_id=foreign_run)

    assert target_dml == []
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(run_events_table)).scalar_one() == 0


class _FencedEventSessionService:
    def __init__(self, authority: SQLiteLocalSessionOperationAuthority) -> None:
        self._authority = authority

    async def append_run_event(
        self,
        *,
        run_id: UUID,
        timestamp: datetime,
        event_type: str,
        data: object,
        session_operation_context: SessionOperationContext,
    ) -> object:
        return self._authority.mutate(
            session_operation_context,
            lambda transaction: transaction.runs.append_run_event(
                run_id=run_id,
                timestamp=timestamp,
                event_type=cast(Any, event_type),
                data=cast(Any, data),
            ),
        )


class _RecordingBroadcaster:
    def __init__(self) -> None:
        self.events: list[RunEvent] = []
        self.cleaned_runs: list[str] = []

    def broadcast(self, _run_id: str, run_event: RunEvent) -> SimpleNamespace:
        self.events.append(run_event)
        return SimpleNamespace(dropped_count=0, drop_reason=None)

    def cleanup_run(self, run_id: str) -> None:
        self.cleaned_runs.append(run_id)


@pytest.mark.asyncio
async def test_expired_execute_lease_takeover_stops_queued_real_worker_before_any_stale_effect(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """A queued worker may clean local state after takeover, but may publish nothing."""
    authority = SQLiteLocalSessionOperationAuthority(engine)
    real_sessions = _real_session_service(engine, authority)
    session_id, run_id = await _seed_run_through_service(
        real_sessions,
        authority,
        title="queued worker takeover",
    )
    stale_context = authority.acquire(
        session_id=session_id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id="queued-worker-a",
        lease_seconds=1,
    )
    blocked_authority = _BlockedRenewalAuthority(authority)
    lease = await SessionOperationLease.adopt(
        cast(Any, blocked_authority),
        stale_context,
        lease_seconds=1,
        renew_interval_seconds=0.05,
    )

    service, _mock_sessions, _executor_double = _execution_service(asyncio.get_running_loop())
    service._session_service = real_sessions
    broadcaster = _RecordingBroadcaster()
    service._broadcaster = cast(Any, broadcaster)
    worker_release = threading.Event()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stale-execute-worker")
    service._executor = pool
    blocker = pool.submit(worker_release.wait, 5)
    prepared = execution_service_module._PreparedPipelineExecution(
        run_id=run_id,
        pipeline_yaml="source:\n  plugin: csv\n  options: {}\n",
        shutdown_event=threading.Event(),
        frozen_run_settings=cast(Any, None),
        user_id=None,
        auth_provider_type=None,
    )
    canonical_output = tmp_path / "stale-output.jsonl"

    def stale_finalization(*_args: object, **_kwargs: object) -> None:
        canonical_output.write_bytes(b"stale canonical bytes")

    successor: SessionOperationContext | None = None
    try:
        with (
            patch.object(service, "_execute_locked", return_value=prepared),
            patch.object(service, "_finalize_output_blobs", side_effect=stale_finalization),
        ):
            returned_run_id = await service.execute(
                session_id,
                session_operation_lease=lease,
            )
            assert returned_run_id == run_id
            assert await asyncio.to_thread(blocked_authority.renew_started.wait, 2)

            deadline = asyncio.get_running_loop().time() + 4
            while successor is None:
                try:
                    successor = await asyncio.to_thread(
                        authority.acquire,
                        session_id=session_id,
                        operation_kind=SessionOperationKind.EXECUTE,
                        owner_instance_id="queued-worker-b",
                        lease_seconds=30,
                    )
                except SessionOperationConflictError:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise AssertionError("expired EXECUTE lease never became available for takeover") from None
                    await asyncio.sleep(0.05)

            blocked_authority.renew_allowed.set()
            loss = await asyncio.wait_for(lease.wait_until_lost(), timeout=2)
            assert isinstance(loss, SessionOperationFenceLost)
            assert loss.reason is FenceLossReason.STALE_EPOCH

            with _capture_run_event_dml(engine) as stale_event_dml:
                worker_release.set()
                for _ in range(200):
                    if lease.closed:
                        break
                    await asyncio.sleep(0.01)

            assert lease.closed
            assert stale_event_dml == []
            assert (await real_sessions.get_run(run_id)).status == "pending"
            assert not canonical_output.exists()
            assert broadcaster.events == []
            assert broadcaster.cleaned_runs == [str(run_id)]
    finally:
        worker_release.set()
        blocked_authority.renew_allowed.set()
        await asyncio.to_thread(blocker.result, 5)
        await asyncio.to_thread(pool.shutdown, True)
        if successor is not None:
            authority.release(successor)


@pytest.mark.asyncio
async def test_takeover_before_progress_dml_consumes_no_sequence_and_calls_no_production_broadcast(
    engine: Engine,
) -> None:
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session_id, run_id = await _seed_run_through_service(
        _real_session_service(engine, authority),
        authority,
        title="stale progress",
    )
    stale = _acquire(authority, session_id=session_id, kind=SessionOperationKind.EXECUTE)
    stale_lease = await SessionOperationLease.adopt(
        authority,
        stale,
        lease_seconds=30,
        renew_interval_seconds=29,
    )
    authority.release(stale)
    current = _acquire(authority, session_id=session_id, kind=SessionOperationKind.EXECUTE)
    current_lease = await SessionOperationLease.adopt(
        authority,
        current,
        lease_seconds=30,
        renew_interval_seconds=29,
    )
    broadcaster = _RecordingBroadcaster()
    service = object.__new__(ExecutionServiceImpl)
    service._loop = asyncio.get_running_loop()
    service._session_service = cast(Any, _FencedEventSessionService(authority))
    service._broadcaster = cast(Any, broadcaster)
    run_event = RunEvent(
        run_id=str(run_id),
        timestamp=datetime.now(UTC),
        event_type="progress",
        data=ProgressData(
            source_rows_processed=1,
            tokens_succeeded=0,
            tokens_failed=0,
            tokens_quarantined=0,
            tokens_routed_success=0,
            tokens_routed_failure=0,
        ),
    )

    with _capture_run_event_dml(engine) as target_dml:
        with pytest.raises(SessionOperationFenceLost):
            await asyncio.to_thread(
                service._persist_and_broadcast_run_event,
                str(run_id),
                run_event,
                session_operation_lease=stale_lease,
            )
        stale_target_dml = tuple(target_dml)
        assert broadcaster.events == []
        result = await asyncio.to_thread(
            service._persist_and_broadcast_run_event,
            str(run_id),
            run_event,
            session_operation_lease=current_lease,
        )

    assert stale_target_dml == ()
    assert len(target_dml) == 1, "only the winner may insert an event"
    assert result.dropped_count == 0
    assert len(broadcaster.events) == 1
    assert broadcaster.events[0].event_sequence == 1
    with pytest.raises(SessionOperationFenceLost):
        await stale_lease.close()
    await current_lease.close()


def test_controllable_executor_and_lease_doubles_model_real_resource_seams() -> None:
    """Guard the gate fixtures themselves against accidental mockification."""
    executor = _ControllableExecutor()
    lease = _ControllableLease(_context(uuid4()))
    future = executor.submit(lambda: None, lease.context)

    assert future is executor.future
    assert executor.submit_calls == [(executor.submit_calls[0][0], (lease.context,), {})]
    assert not future.done()
    assert not lease.close_started.is_set()
