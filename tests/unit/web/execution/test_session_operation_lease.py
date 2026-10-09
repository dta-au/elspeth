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
import json
import operator
import os
import re
import subprocess
import sys
import textwrap
import threading
import time
import traceback
from asyncio.tasks import run_coroutine_threadsafe
from asyncio.threads import to_thread
from collections.abc import Coroutine, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
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

from elspeth.contracts.errors import AuditIntegrityError, FrameworkBugError
from elspeth.core.events import EventBus
from elspeth.engine.orchestrator.core import Orchestrator
from elspeth.web import async_workers
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.coordination import lifecycle as lifecycle_module
from elspeth.web.coordination.contracts import (
    FenceLossReason,
    SessionOperationContext,
    SessionOperationFence,
    SessionOperationFenceLost,
    SessionOperationKind,
    StartPermitState,
)
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.repository import (
    CanonicalExecutionReleaseLoss,
    SessionDerivedCustodyError,
    SessionOperationConflictError,
)
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.execution import service as execution_service_module
from elspeth.web.execution.envelope import restore_execution_envelope
from elspeth.web.execution.progress import ProgressBroadcaster
from elspeth.web.execution.protocol import ExecutionService
from elspeth.web.execution.routes import create_execution_router
from elspeth.web.execution.schemas import ProgressData, RunEvent, ValidationReadiness, ValidationResult
from elspeth.web.execution.service import ExecutionServiceImpl
from elspeth.web.sessions.models import run_events_table
from elspeth.web.sessions.protocol import (
    CompositionStateData,
    RunRecord,
    RunStartPermitRecord,
    SessionOperationAuthority,
    SessionServiceProtocol,
)
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity, wire_test_pipeline_user_authority
from tests.helpers import execution_custody
from tests.helpers.execution_custody import (
    ActualPrivatePipelineExecutorControl,
    CanonicalExecutionAuthorityObservation,
    ExecutionTestCustody,
    PhysicalPipelineCompletionControl,
)
from tests.unit.web.sessions.session_test_authority import FencedSessionServiceHarness

execution_fixture = execution_custody.execution_fixture


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
        self.close_error: BaseException | None = None

    async def close(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        try:
            await self.close_allowed.wait()
        except asyncio.CancelledError:
            self.close_cancelled = True
            raise
        self.close_finished.set()
        if self.close_error is not None:
            raise self.close_error

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
        self.release_error: BaseException | None = None

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
        if self.release_error is not None:
            raise self.release_error


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
    workflow_governance = "off"
    data_dir = Path("/tmp/execution-lease-gate")
    landscape_passphrase = None
    # Deny-by-default secret wiring (WebSettings.secret_wiring_allowlist): the
    # service builds its runtime wiring policy from this at construction.
    secret_wiring_allowlist = ()

    def get_landscape_url(self) -> str:
        return "sqlite:////tmp/execution-lease-gate-landscape.db"

    def get_payload_store_path(self) -> Path:
        return Path("/tmp/execution-lease-gate-payloads")


class _YamlGenerator:
    def generate_yaml(self, _state: object) -> str:
        return "source:\n  plugin: csv\n  options: {}\n"


def _execution_service(
    loop: asyncio.AbstractEventLoop, execution_fixture: ExecutionTestCustody
) -> tuple[ExecutionServiceImpl, Any, ActualPrivatePipelineExecutorControl]:
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
    service = execution_fixture.bind(
        ExecutionServiceImpl.for_trained_operator(
            loop=loop,
            broadcaster=ProgressBroadcaster(loop),
            settings=cast(Any, _ExecutionSettings()),
            session_service=session_service,
            yaml_generator=_YamlGenerator(),
            telemetry=build_sessions_telemetry(),
            execution_lease_release_registry=execution_fixture.registry(execution_fixture.loop),
        )
    )
    executor = ActualPrivatePipelineExecutorControl(service._executor, execution_fixture)
    return service, session_service, executor


async def _submit_terminal_callback(
    service: ExecutionServiceImpl,
    lease: SessionOperationLease,
    executor: ActualPrivatePipelineExecutorControl,
    *,
    error: BaseException | None = None,
) -> tuple[asyncio.Task[None], Future[object]]:
    obligation = lease.execution_obligation
    assert obligation is not None
    watcher = service._create_loss_watcher(lease, threading.Event(), run_id=uuid4())
    service._submit_owned_pipeline(obligation, lease, watcher, partial(lambda: None))
    actual = obligation.pipeline
    assert actual is not None and actual is executor.future.actual
    if error is None:
        executor.future.set_result(None)
    else:
        executor.future.set_exception(error)
    for _ in range(200):
        if obligation.completion is not None:
            break
        await asyncio.sleep(0.01)
    completion = obligation.completion
    assert completion is not None, "source callback did not bind its actual completion wrapper"
    # Genuine exact duplicate after the production observer's first handoff.
    service._on_pipeline_done(actual, session_operation_lease=lease, loss_watcher=watcher)
    return watcher, completion


async def _canonical_execute_lease(
    service: ExecutionServiceImpl,
    execution_fixture: ExecutionTestCustody,
    *,
    session_id: UUID,
    renew_interval_seconds: float = 10.0,
) -> tuple[SessionOperationLease, CanonicalExecutionAuthorityObservation]:
    observed = execution_fixture.observe_authority(session_id)
    observed.release_allowed.clear()
    lease = await execution_fixture.acquire(
        service.execution_lease_release_registry,
        observed.authority,
        session_id=session_id,
        owner_instance_id="execution-lease-test",
        lease_seconds=30,
        renew_interval_seconds=renew_interval_seconds,
    )
    return lease, observed


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


class _CanonicalRouteSignal:
    """Observe actual producer state; this Event-like API grants no receipt."""

    def __init__(self, predicate):
        self.predicate = predicate

    def is_set(self):
        return self.predicate()

    async def wait(self):
        while not self.predicate():
            await asyncio.sleep(0.001)
        return True


class _CanonicalRouteLeaseControl:
    """Test driver only: the route always receives the actual nominal lease."""

    def __init__(self, sessions: _RouteSessionService, execution_fixture: ExecutionTestCustody):
        self.observation = sessions.observation
        self.execution_fixture = execution_fixture
        self.actual: SessionOperationLease | None = None
        self.close_allowed = self.observation.release_allowed
        self.close_allowed.clear()
        self.close_started = _CanonicalRouteSignal(self.observation.release_called.is_set)
        self.close_finished = _CanonicalRouteSignal(self._actual_close_finished)

    def bind(self, actual: SessionOperationLease) -> None:
        assert type(actual) is SessionOperationLease and self.actual is None
        assert actual.execution_obligation is not None
        assert actual.execution_obligation.authority is self.observation.authority
        self.actual = actual
        self.execution_fixture.track_route_lease(actual)

    def _actual_close_finished(self):
        actual = self.actual
        if actual is None:
            return False
        obligation = actual.execution_obligation
        assert obligation is not None
        task = obligation.lifecycle_task
        return actual.closed and task is not None and task.done() and obligation.lifecycle_outcome_recorded

    @property
    def context(self):
        assert self.actual is not None
        return self.actual.context

    @property
    def close_calls(self):
        return len(self.observation.release_calls)

    @property
    def close_cancelled(self):
        if self.actual is None:
            return False
        obligation = self.actual.execution_obligation
        assert obligation is not None
        return isinstance(obligation.lifecycle_original_error, asyncio.CancelledError)

    @property
    def close_error(self):
        return self.observation.release_error

    @close_error.setter
    def close_error(self, original):
        self.observation.release_error = original


class _RouteSessionService:
    def __init__(self, session_id: UUID, *, execution_fixture: ExecutionTestCustody, trace: list[str] | None = None) -> None:
        self.observation = execution_fixture.observe_authority(session_id)
        self.session_operation_authority = self.observation.authority
        self.session_operation_owner_instance_id = "execution-route-test"
        self.session_operation_lease_seconds = 41
        self.registry = execution_fixture.registry(asyncio.get_running_loop())
        self._session_id, self._trace = session_id, trace
        self.authorized = True

    async def get_session(self, session_id: UUID) -> SimpleNamespace:
        assert session_id == self._session_id
        if self._trace is not None:
            self._trace.append("ownership")
        return SimpleNamespace(
            id=session_id, user_id=_USER_ID if self.authorized else "different-user", auth_provider_type="local", archived_at=None
        )


class _RouteExecutionService:
    def __init__(self, run_id: UUID, *, trace: list[str] | None = None) -> None:
        self.run_id = run_id
        self.started = asyncio.Event()
        self.allowed = asyncio.Event()
        self.error: BaseException | None = None
        self.cancellation_originals: list[asyncio.CancelledError] = []
        self.calls: list[dict[str, object]] = []
        self._trace = trace

    async def execute(self, session_id: UUID, state_id: UUID | None = None, **kwargs: object) -> UUID:
        self.calls.append({"session_id": session_id, "state_id": state_id, **kwargs})
        if self._trace is not None:
            self._trace.append("execute")
        self.started.set()
        try:
            await self.allowed.wait()
        except asyncio.CancelledError as original:
            self.cancellation_originals.append(original)
            raise
        if self.error is not None:
            raise self.error
        return self.run_id


class _ExactCloseWaitCancellationObserver:
    """Observe the request's delivered cancellation at the real close join."""

    def __init__(self, lease: _CanonicalRouteLeaseControl) -> None:
        self.lease = lease
        self.request_task: asyncio.Task[object] | None = None
        self.delivered: list[asyncio.CancelledError] = []
        self.original_wait = asyncio.wait

    async def __call__(self, fs: set[asyncio.Task[object]], *args: object, **kwargs: object) -> object:
        try:
            return await self.original_wait(fs, *args, **kwargs)
        except asyncio.CancelledError as original:
            if asyncio.current_task() is self.request_task and len(fs) == 1:
                close_task = next(iter(fs))
                coroutine = close_task.get_coro()
                frame = coroutine.cr_frame
                if (
                    close_task.get_name() == "execution-pretransfer-lease-close"
                    and coroutine.cr_code is SessionOperationLease.close.__code__
                    and frame is not None
                    and self.lease.actual is not None
                    and frame.f_locals["self"] is self.lease.actual
                ):
                    self.delivered.append(original)
            raise


@dataclass
class _RouteHarness:
    endpoint: Any
    request: Request
    user: UserIdentity
    session_service: _RouteSessionService


def _route_harness(session_id: UUID, execution_fixture: ExecutionTestCustody) -> _RouteHarness:
    app = FastAPI()
    session_service = _RouteSessionService(session_id, execution_fixture=execution_fixture)
    app.state.session_service = session_service
    app.state.execution_lease_release_registry = session_service.registry
    app.state.settings = SimpleNamespace(auth_provider="local")
    wire_test_pipeline_user_authority(app, identity_id=_USER_ID)
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
    monkeypatch: pytest.MonkeyPatch, lease: _CanonicalRouteLeaseControl, *, trace: list[str] | None = None
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    canonical_acquire = SessionOperationLease.acquire

    async def acquire(_cls: type[SessionOperationLease], authority: object, **kwargs: object) -> SessionOperationLease:
        if trace is not None:
            trace.append("acquire")
        calls.append({"authority": authority, **kwargs})
        actual = await canonical_acquire(authority, **kwargs)
        lease.bind(actual)
        return actual

    monkeypatch.setattr(SessionOperationLease, "acquire", classmethod(acquire))
    return calls


def _http_route_app(
    *,
    session_service: _RouteSessionService,
    execution_service: _RouteExecutionService,
) -> FastAPI:
    app = FastAPI()
    app.state.session_service = session_service
    app.state.execution_lease_release_registry = session_service.registry
    app.state.execution_service = execution_service
    app.state.settings = SimpleNamespace(auth_provider="local")
    wire_test_pipeline_user_authority(app, identity_id=_USER_ID)

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
async def test_execute_route_acquires_exact_execute_lease_and_transfers_it(
    monkeypatch: pytest.MonkeyPatch, execution_fixture: ExecutionTestCustody
) -> None:
    session_id = uuid4()
    run_id = uuid4()
    harness = _route_harness(session_id, execution_fixture)
    lease = _CanonicalRouteLeaseControl(harness.session_service, execution_fixture)
    acquired = _install_acquire(monkeypatch, lease)
    service = _RouteExecutionService(run_id)
    service.allowed.set()

    response = await _invoke_execute_route(harness, service, session_id=session_id)

    assert response == {"run_id": str(run_id)}
    assert lease.actual is not None
    obligation = lease.actual.execution_obligation
    assert obligation is not None
    assert obligation.lease is lease.actual
    assert obligation.registry is harness.session_service.registry
    assert obligation.authority is harness.session_service.session_operation_authority
    assert obligation.session_id == session_id
    assert obligation.owner_instance_id == "execution-route-test"
    assert obligation.lease_seconds == 41
    assert acquired == [
        {
            "authority": harness.session_service.session_operation_authority,
            "session_id": session_id,
            "operation_kind": SessionOperationKind.EXECUTE,
            "owner_instance_id": "execution-route-test",
            "lease_seconds": 41,
            "execution_obligation": obligation,
        }
    ]
    assert len(service.calls) == 1
    assert service.calls[0]["session_operation_lease"] is lease.actual
    assert lease.close_calls == 0, "successful submission transfers ownership to background completion"


@pytest.mark.asyncio
async def test_fastapi_execute_orders_ownership_before_acquire_before_exact_service_handoff(
    monkeypatch: pytest.MonkeyPatch,
    execution_fixture: ExecutionTestCustody,
) -> None:
    session_id = uuid4()
    trace: list[str] = []
    session_service = _RouteSessionService(session_id, execution_fixture=execution_fixture, trace=trace)
    execution_service = _RouteExecutionService(uuid4(), trace=trace)
    execution_service.allowed.set()
    lease = _CanonicalRouteLeaseControl(session_service, execution_fixture)
    acquired = _install_acquire(monkeypatch, lease, trace=trace)
    app = _http_route_app(session_service=session_service, execution_service=execution_service)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/sessions/{session_id}/execute")

    assert response.status_code == 202
    assert trace == ["ownership", "acquire", "execute"]
    assert len(acquired) == 1
    assert lease.actual is not None
    assert acquired[0]["execution_obligation"] is lease.actual.execution_obligation
    assert execution_service.calls == [
        {
            "session_id": session_id,
            "state_id": None,
            "session_operation_lease": lease.actual,
            "user_id": _USER_ID,
            "auth_provider_type": "local",
            "fanout_ack_token": None,
            "secret_ack_token": None,
        }
    ]


@pytest.mark.asyncio
async def test_fastapi_denied_ownership_never_acquires_or_invokes_execution(
    monkeypatch: pytest.MonkeyPatch,
    execution_fixture: ExecutionTestCustody,
) -> None:
    session_id = uuid4()
    trace: list[str] = []
    session_service = _RouteSessionService(session_id, execution_fixture=execution_fixture, trace=trace)
    session_service.authorized = False
    execution_service = _RouteExecutionService(uuid4(), trace=trace)
    lease = _CanonicalRouteLeaseControl(session_service, execution_fixture)
    acquired = _install_acquire(monkeypatch, lease, trace=trace)
    app = _http_route_app(session_service=session_service, execution_service=execution_service)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/sessions/{session_id}/execute")

    assert response.status_code == 404
    assert trace == ["ownership"]
    assert acquired == []
    assert execution_service.calls == []


@pytest.mark.asyncio
async def test_execute_route_pretransfer_failure_closes_and_joins_exact_lease(
    monkeypatch: pytest.MonkeyPatch, execution_fixture: ExecutionTestCustody
) -> None:
    session_id = uuid4()
    harness = _route_harness(session_id, execution_fixture)
    lease = _CanonicalRouteLeaseControl(harness.session_service, execution_fixture)
    acquired = _install_acquire(monkeypatch, lease)
    lease.close_allowed.set()
    service = _RouteExecutionService(uuid4())
    service.error = RuntimeError("submit failed")
    service.allowed.set()

    with pytest.raises(RuntimeError, match="submit failed"):
        await _invoke_execute_route(harness, service, session_id=session_id)

    assert len(acquired) == 1
    assert lease.actual is not None
    assert acquired[0]["execution_obligation"] is lease.actual.execution_obligation
    assert service.calls[0]["session_operation_lease"] is lease.actual
    assert lease.close_calls == 1
    assert lease.close_finished.is_set()


@pytest.mark.asyncio
async def test_cancelled_execute_request_before_transfer_cannot_interrupt_lease_join(
    monkeypatch: pytest.MonkeyPatch,
    execution_fixture: ExecutionTestCustody,
) -> None:
    session_id = uuid4()
    harness = _route_harness(session_id, execution_fixture)
    lease = _CanonicalRouteLeaseControl(harness.session_service, execution_fixture)
    acquired = _install_acquire(monkeypatch, lease)
    service = _RouteExecutionService(uuid4())
    observer = _ExactCloseWaitCancellationObserver(lease)
    with patch("elspeth.web.execution.service.asyncio.wait", new=observer):
        task = asyncio.create_task(_invoke_execute_route(harness, service, session_id=session_id))
        observer.request_task = task
        await asyncio.wait_for(service.started.wait(), timeout=2)

        try:
            task.cancel()
            await asyncio.wait_for(lease.close_started.wait(), timeout=2)
            assert task.cancel()  # A second cancellation must not cancel close()/renewal-task join.
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            lease.close_allowed.set()
            service.allowed.set()

        with pytest.raises(BaseExceptionGroup) as retained:
            await asyncio.wait_for(task, timeout=2)
    assert len(service.cancellation_originals) == 1
    assert len(observer.delivered) == 1
    _assert_exact_cancellation_originals(retained.value, [service.cancellation_originals[0], observer.delivered[0]])
    assert len(_original_leaves(retained.value)) == 2
    assert len(acquired) == 1
    assert lease.close_calls == 1
    assert not lease.close_cancelled
    assert lease.close_finished.is_set()


def _original_leaves(original: BaseException) -> list[BaseException]:
    if isinstance(original, BaseExceptionGroup):
        return [leaf for child in original.exceptions for leaf in _original_leaves(child)]
    return [original]


def _assert_exact_cancellation_originals(original: BaseException, delivered: list[asyncio.CancelledError]) -> None:
    cancellations = [leaf for leaf in _original_leaves(original) if isinstance(leaf, asyncio.CancelledError)]
    assert len(cancellations) == len(delivered)
    assert len({id(leaf) for leaf in cancellations}) == len(cancellations)
    assert all(actual is expected for actual, expected in zip(cancellations, delivered, strict=True))


def _atomic_child_checkpoint(path: Path, proof: dict[str, object]) -> None:
    from tests.unit.web.execution.test_canonical_execution_fixture import _publish_checkpoint

    _publish_checkpoint(path, proof)


async def _child_with_failure_checkpoint(root: Path, child: Coroutine[object, object, None]) -> None:
    try:
        await child
    except BaseException as original:
        _atomic_child_checkpoint(
            root / "failure.json",
            {
                "type": type(original).__name__,
                "message": str(original),
                "traceback": "".join(traceback.format_exception(original)),
            },
        )
        raise


def _assert_child_checkpoint_or_failure(checkpoint: Path, failure: Path, log: Path) -> dict[str, object]:
    assert not failure.exists() or not checkpoint.exists(), "child published both success and failure checkpoints"
    if failure.exists():
        evidence = json.loads(failure.read_text())
        raise AssertionError(
            "actual owned custody child assertion failed: " + evidence["type"] + ": " + evidence["message"] + "\n" + evidence["traceback"]
        )
    assert checkpoint.exists(), "actual custody child failed before checkpoint: " + log.read_text()
    return json.loads(checkpoint.read_text())


async def _failed_http_release_child(
    root: Path, cleanup_error_type: type[Exception], logger_error_type: type[Exception] | None, repeat_cancellation: bool
) -> None:
    custody = ExecutionTestCustody(asyncio.get_running_loop(), root)
    session_id = uuid4()
    session_service = _RouteSessionService(session_id, execution_fixture=custody)
    lease = _CanonicalRouteLeaseControl(session_service, custody)
    cleanup_error = (
        SessionOperationFenceLost(FenceLossReason.STALE_EPOCH)
        if cleanup_error_type is SessionOperationFenceLost
        else cleanup_error_type("private lease failure detail")
    )
    lease.close_error = cleanup_error
    monkeypatch = pytest.MonkeyPatch()
    acquired = _install_acquire(monkeypatch, lease)
    execution_service = _RouteExecutionService(uuid4())
    app = _http_route_app(session_service=session_service, execution_service=execution_service)
    logger_error = None if logger_error_type is None else logger_error_type("private logger failure detail")
    observer = _ExactCloseWaitCancellationObserver(lease)
    with (
        patch("elspeth.web.execution.routes.slog.error", side_effect=logger_error) as log_error,
        patch("elspeth.web.execution.service.asyncio.wait", new=observer),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            request_task = asyncio.create_task(client.post(f"/api/sessions/{session_id}/execute"))
            observer.request_task = request_task
            try:
                await asyncio.wait_for(execution_service.started.wait(), timeout=2)
                assert request_task.cancel("initial request cancellation")
                await asyncio.wait_for(lease.close_started.wait(), timeout=2)
                if repeat_cancellation:
                    assert request_task.cancel("repeated request cancellation")
                    await asyncio.sleep(0)
                assert not request_task.done()
            finally:
                lease.close_allowed.set()
                execution_service.allowed.set()
            completed, _ = await asyncio.wait({request_task}, timeout=2)
            assert request_task in completed, "actual request remained pending after both gates opened"
            with pytest.raises(BaseExceptionGroup) as retained:
                request_task.result()
    leaves = _original_leaves(retained.value)
    assert len(execution_service.cancellation_originals) == 1
    primary = execution_service.cancellation_originals[0]
    assert leaves[0] is primary
    assert cleanup_error in leaves and sum(leaf is cleanup_error for leaf in leaves) == 1
    assert len(observer.delivered) == int(repeat_cancellation)
    _assert_exact_cancellation_originals(retained.value, [primary, *observer.delivered])
    assert len({id(leaf) for leaf in leaves}) == len(leaves)
    assert len(leaves) == 2 + int(repeat_cancellation)
    log_error.assert_not_called()
    assert len(acquired) == 1 and lease.actual is not None
    obligation = lease.actual.execution_obligation
    assert obligation is not None and acquired[0]["execution_obligation"] is obligation
    assert obligation.lease is lease.actual and obligation.registry is session_service.registry
    assert lease.close_calls == 1 and lease.close_finished.is_set() and not lease.close_cancelled
    assert obligation.lifecycle_original_error is cleanup_error
    release = obligation.release_submission
    assert release is not None and release.future is not None and release.reservation is not None
    session_service.registry.observe_ready()
    receipt = release.reservation.witness.snapshot()
    assert release.future.done() and release.future.exception() is cleanup_error
    assert release.original_error is cleanup_error and release.observed
    assert release.reservation.released and release.callback_return_observed
    assert receipt.callable_finished and receipt.exited and not receipt.impossible
    assert session_service.observation.release_calls == [lease.actual.context]
    assert not obligation.release_succeeded and not obligation.release_lost and not obligation.retired
    assert release.domain_refusal is None, "a pre-SQL exception cannot issue canonical terminal loss"
    assert session_service.registry.has_pending_physical_owners()
    assert session_service.registry.executor_finalizer is None
    assert not session_service.registry._executor_allocation_declared
    assert session_service.registry._execution_executor is None
    assert not session_service.registry.executor_join_succeeded
    custody.witness_cleanup_original(cleanup_error)
    shutdown = asyncio.create_task(custody.close())
    await asyncio.sleep(0)
    assert not shutdown.done()
    with pytest.raises(BaseExceptionGroup) as refused:
        session_service.registry.assert_completed()
    assert any(leaf is cleanup_error for leaf in _original_leaves(refused.value))
    assert not custody.recovery.watchdog.completed
    _atomic_child_checkpoint(
        root / "checkpoint.json",
        {
            "exact_primary_and_release_originals": True,
            "actual_failed_release_future_exited": True,
            "actual_registry_retained_pending": True,
            "no_private_executor_allocated": True,
            "shutdown_pending_and_complete_refused": True,
            "watchdog_completion_unsent": True,
        },
    )
    await asyncio.Event().wait()


def _run_failed_http_release_child(
    root: Path,
    cleanup_error_type: type[Exception],
    logger_error_type: type[Exception] | None,
    repeat_cancellation: bool,
    *,
    setup_after_arm: bool = False,
) -> None:
    from tests.unit.web.execution.test_canonical_execution_fixture import _project_reaping_failures, _reap_owned_child

    repo = Path(__file__).resolve().parents[4]
    environment = {
        "PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin",
        "PYTHONPATH": os.pathsep.join((str(repo / "src"), str(repo / "elspeth-lints/src"), str(repo))),
        "PYTHONUNBUFFERED": "1",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    }
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--failed-http-release-child",
        str(root),
        cleanup_error_type.__name__,
        logger_error_type.__name__ if logger_error_type is not None else "None",
        str(repeat_cancellation),
    ]
    if setup_after_arm:
        command.append("--setup-after-arm")
    primary = None
    log = root / "failed-http-release-child.log"
    with log.open("wb") as stream:
        process = subprocess.Popen(command, cwd=repo, env=environment, stdout=stream, stderr=subprocess.STDOUT)
        try:
            checkpoint = root / "checkpoint.json"
            failure = root / "failure.json"
            deadline = time.monotonic() + 15
            while not checkpoint.exists() and not failure.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            expected = {
                "exact_primary_and_release_originals": True,
                "actual_failed_release_future_exited": True,
                "actual_registry_retained_pending": True,
                "no_private_executor_allocated": True,
                "shutdown_pending_and_complete_refused": True,
                "watchdog_completion_unsent": True,
            }
            if setup_after_arm:
                expected = {
                    "actual_armed_setup_and_sql_originals": True,
                    "actual_release_future_callback_exited": True,
                    "mixed_lifecycle_group_not_flattened": True,
                    "faulty_receipt_refused_pending_retained": True,
                    "successor_authority_unchanged": True,
                    "watchdog_completion_unsent": True,
                }
            assert _assert_child_checkpoint_or_failure(checkpoint, failure, log) == expected
            assert process.poll() is None, "failed release silently reported completed custody"
        except BaseException as original:
            primary = original
            raise
        finally:
            originals = _reap_owned_child(process)
            if process.returncode is None:
                originals.append(AssertionError("exact owned route child was not waited"))
            _project_reaping_failures(primary, originals)


def _run_failed_shutdown_release_child(root: Path, error_type: type[Exception], peer_fails: bool) -> None:
    from tests.unit.web.execution.test_canonical_execution_fixture import _project_reaping_failures, _reap_owned_child

    repo = Path(__file__).resolve().parents[4]
    environment = {
        "PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin",
        "PYTHONPATH": os.pathsep.join((str(repo / "src"), str(repo / "elspeth-lints/src"), str(repo))),
        "PYTHONUNBUFFERED": "1",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    }
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--failed-shutdown-release-child",
        str(root),
        error_type.__name__,
        str(peer_fails),
    ]
    primary = None
    log = root / "failed-shutdown-release-child.log"
    with log.open("wb") as stream:
        process = subprocess.Popen(command, cwd=repo, env=environment, stdout=stream, stderr=subprocess.STDOUT)
        try:
            checkpoint = root / "checkpoint.json"
            failure = root / "failure.json"
            deadline = time.monotonic() + 15
            while not checkpoint.exists() and not failure.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert _assert_child_checkpoint_or_failure(checkpoint, failure, log) == {
                "exact_failed_completion_original": True,
                "failed_release_retained_pending": True,
                "peer_release_observed": True,
                "private_executor_joined": True,
                "shutdown_pending_and_complete_refused": True,
                "watchdog_completion_unsent": True,
            }
            assert process.poll() is None, "failed release silently reported completed custody"
        except BaseException as original:
            primary = original
            raise
        finally:
            originals = _reap_owned_child(process)
            if process.returncode is None:
                originals.append(AssertionError("exact owned shutdown child was not waited"))
            _project_reaping_failures(primary, originals)


@pytest.mark.parametrize(
    ("cleanup_error_type", "logger_error_type"),
    [
        (OSError, None),
        (OSError, OSError),
        (OSError, AuditIntegrityError),
        (OSError, FrameworkBugError),
        (AuditIntegrityError, None),
        (FrameworkBugError, None),
        (SessionOperationFenceLost, None),
    ],
)
@pytest.mark.parametrize("repeat_cancellation", [False, True])
def test_cancelled_http_execute_observes_cleanup_failure_without_losing_primary(
    tmp_path: Path,
    cleanup_error_type: type[Exception],
    logger_error_type: type[Exception] | None,
    repeat_cancellation: bool,
) -> None:
    _run_failed_http_release_child(tmp_path, cleanup_error_type, logger_error_type, repeat_cancellation)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "worker_outcome",
    ["success", "failure", "graceful_cancel", "cancelled_future", "already_done"],
)
async def test_submitted_worker_retains_exact_lease_until_every_terminal_outcome(
    worker_outcome: str, execution_fixture: ExecutionTestCustody
) -> None:
    loop = asyncio.get_running_loop()
    service, session_service, executor = _execution_service(loop, execution_fixture=execution_fixture)
    session_id = session_service.get_current_state.return_value.session_id
    lease, authority = await _canonical_execute_lease(service, execution_fixture, session_id=session_id)
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
    assert submitted[2] == {"session_operation_lease": lease, "durable_admission": True}
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

    await asyncio.wait_for(run_sync_in_worker(authority.release_called.wait, 2), timeout=2)
    for _ in range(100):
        if lease.closed:
            break
        await asyncio.sleep(0.01)
    assert lease.closed
    assert authority.release_calls == [lease.context]
    await asyncio.wait_for(execution_fixture.shutdown_service(service), timeout=2)


@pytest.mark.asyncio
async def test_execute_rejects_lease_subclass_before_any_effect(execution_fixture: ExecutionTestCustody) -> None:
    class _LeaseSubclass(SessionOperationLease):
        pass

    service, session_service, executor = _execution_service(asyncio.get_running_loop(), execution_fixture=execution_fixture)
    session_id = session_service.get_current_state.return_value.session_id
    forged = object.__new__(_LeaseSubclass)

    with pytest.raises(TypeError, match="exact SessionOperationLease"):
        await service.execute(session_id, session_operation_lease=forged)

    session_service.get_active_run.assert_not_awaited()
    session_service.create_run.assert_not_awaited()
    assert executor.submit_calls == []


def _durable_effect_call_counts(session_service: Any) -> dict[str, int]:
    """Call counts of the four session-service effects a run submission may originate."""
    return {
        "create_run": session_service.create_run.call_count,
        "update_run_status": session_service.update_run_status.call_count,
        "append_run_event": session_service.append_run_event.call_count,
        "record_blob_inline_resolutions": session_service.record_blob_inline_resolutions.call_count,
    }


@pytest.mark.asyncio
async def test_recovery_missing_yaml_rejects_before_watcher_or_shutdown_map(
    execution_fixture: ExecutionTestCustody,
) -> None:
    service, sessions, executor = _execution_service(asyncio.get_running_loop(), execution_fixture)
    session_id = uuid4()
    run_id = uuid4()
    run = RunRecord(
        id=run_id,
        session_id=session_id,
        state_id=uuid4(),
        status="running",
        started_at=datetime.now(UTC),
        finished_at=None,
        rows_processed=0,
        rows_succeeded=0,
        rows_failed=0,
        rows_routed_success=0,
        rows_routed_failure=0,
        rows_quarantined=0,
        error=None,
        landscape_run_id=None,
        pipeline_yaml=None,
    )
    sessions.assess_run_start_admission.return_value = RunStartPermitRecord(
        run_id=str(run_id),
        state=StartPermitState.START_PERMITTED,
        permit_id=str(uuid4()),
        permit_epoch=1,
        subject_hash="recover-missing-yaml",
        issued_at=datetime.now(UTC),
        cancelled_at=None,
    )
    sessions.get_session.return_value = SimpleNamespace(
        archived_at=None,
        user_id=_USER_ID,
        auth_provider_type="local",
    )
    lease, authority = await _canonical_execute_lease(service, execution_fixture, session_id=session_id)
    authority.release_allowed.set()
    try:
        with (
            patch.object(service, "_restore_admitted_run", return_value=SimpleNamespace(settings=object())) as restore,
            patch.object(service, "_create_loss_watcher", wraps=service._create_loss_watcher) as create_watcher,
            pytest.raises(AssertionError),
        ):
            await service.recover_run(run, lease, resume_existing=False)
        restore.assert_awaited_once()
        sessions.assess_run_start_admission.assert_awaited_once()
        create_watcher.assert_not_called()
        assert str(run_id) not in service._shutdown_events
        assert executor.submit_calls == []
        assert not lease.closed
    finally:
        await lease.close()


@pytest.mark.asyncio
async def test_execute_wires_real_renewal_loss_to_exact_worker_shutdown_and_retains_lease(execution_fixture: ExecutionTestCustody) -> None:
    service, session_service, executor = _execution_service(asyncio.get_running_loop(), execution_fixture=execution_fixture)
    session_id = session_service.get_current_state.return_value.session_id
    loss = SessionOperationFenceLost(FenceLossReason.LEASE_EXPIRED)
    lease, authority = await _canonical_execute_lease(service, execution_fixture, session_id=session_id, renew_interval_seconds=0.01)
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
    effect_counts_after_submit = _durable_effect_call_counts(session_service)
    authority.renew_called.clear()
    authority.renew_error = loss

    await asyncio.wait_for(run_sync_in_worker(authority.renew_called.wait, 2), timeout=2)
    assert authority.renew_called.is_set()
    await asyncio.wait_for(run_sync_in_worker(worker_shutdown_event.wait, 2), timeout=2)
    assert executor.future.running() or not executor.future.done()
    assert authority.release_calls == [], "renewal loss signals cancellation but completion still owns the lease"
    await asyncio.sleep(0.02)
    assert _durable_effect_call_counts(session_service) == effect_counts_after_submit, (
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
    assert authority.release_calls == [lease.context], "completion must join the original context's canonical release"
    obligation = lease.execution_obligation
    assert obligation is not None
    assert obligation.release_succeeded and not obligation.release_lost
    assert any(original is loss for original in service.execution_lease_release_registry._failures)
    with pytest.raises(ExceptionGroup) as cleanup_failure:
        await asyncio.wait_for(execution_fixture.shutdown_service(service), timeout=2)
    assert cleanup_failure.value.exceptions == (loss,)
    execution_fixture.witness_cleanup_original(loss)


@pytest.mark.asyncio
async def test_submit_failure_after_run_creation_retains_unknown_until_executor_join(execution_fixture: ExecutionTestCustody) -> None:
    service, session_service, executor = _execution_service(asyncio.get_running_loop(), execution_fixture=execution_fixture)
    session_id = session_service.get_current_state.return_value.session_id
    lease, authority = await _canonical_execute_lease(service, execution_fixture, session_id=session_id)
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
    submission_error = RuntimeError("executor unavailable")
    executor.submit_error = submission_error
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
        pytest.raises(RuntimeError, match="executor unavailable") as caught,
    ):
        await service.execute(session_id, session_operation_lease=lease)

    assert caught.value is submission_error
    assert trace == ["create_run", "submit"]
    session_service.update_run_status.assert_not_awaited()
    obligation = lease.execution_obligation
    assert obligation is not None and obligation.lease is lease
    assert obligation.completion_required
    assert obligation.pipeline_submission_unknown is submission_error
    assert obligation.pipeline is None and obligation.completion is None
    assert not obligation.unknown_pipeline_cleanup_declared
    assert not lease.closed and authority.release_calls == []
    assert service.execution_lease_release_registry.has_pending_physical_owners()
    with pytest.raises(BaseExceptionGroup) as refused:
        service.execution_lease_release_registry.assert_completed()
    assert any(leaf is submission_error for leaf in _original_leaves(refused.value))
    # The failed submit has no Future receipt. Only the actual executor join
    # permits cleanup; the fixture will join it and the retained original.
    execution_fixture.witness_cleanup_original(submission_error)


@pytest.mark.asyncio
async def test_completion_cancellation_still_joins_exact_close_once(execution_fixture: ExecutionTestCustody) -> None:
    service, _session_service, executor = _execution_service(asyncio.get_running_loop(), execution_fixture=execution_fixture)
    lease, authority = await _canonical_execute_lease(service, execution_fixture, session_id=uuid4())
    watcher_cancelled = asyncio.Event()
    watcher_allowed = asyncio.Event()

    actual_signal = service._signal_shutdown_on_operation_loss

    async def stubborn_watcher(current_lease, shutdown_event, *, run_id, close_requested) -> None:
        assert current_lease is lease and close_requested is not None
        await close_requested.wait()
        watcher_cancelled.set()  # Historical name: now the source cooperative-close request.
        await watcher_allowed.wait()
        await actual_signal(current_lease, shutdown_event, run_id=run_id, close_requested=close_requested)

    with patch.object(service, "_signal_shutdown_on_operation_loss", side_effect=stubborn_watcher):
        watcher, completion = await _submit_terminal_callback(service, lease, executor)
        await asyncio.wait_for(watcher_cancelled.wait(), timeout=2)
    assert watcher is service._loss_watcher_owner(watcher, lease).task
    completion.cancel()
    completion.cancel()  # Repeated observer cancellation must not retire the underlying join.

    shutdown = asyncio.create_task(execution_fixture.shutdown_service(service))
    try:
        await asyncio.wait_for(run_sync_in_worker(executor.shutdown_started.wait, 2), timeout=2)
        await asyncio.sleep(0)
        assert not shutdown.done(), "shutdown lost the cancelled completion before its lease close/join"
    finally:
        watcher_allowed.set()
        authority.release_allowed.set()
        await asyncio.wait_for(shutdown, timeout=2)

    await asyncio.wait_for(run_sync_in_worker(authority.release_called.wait, 2), timeout=2)
    assert authority.release_calls == [lease.context]
    assert lease.closed


@pytest.mark.asyncio
async def test_runtime_shutdown_waits_for_blocked_lease_completion(execution_fixture: ExecutionTestCustody) -> None:
    service, _session_service, executor = _execution_service(asyncio.get_running_loop(), execution_fixture=execution_fixture)
    lease, authority = await _canonical_execute_lease(service, execution_fixture, session_id=uuid4())
    _watcher, _completion = await _submit_terminal_callback(service, lease, executor)
    await asyncio.wait_for(run_sync_in_worker(authority.release_called.wait, 2), timeout=2)

    shutdown = asyncio.create_task(execution_fixture.shutdown_service(service))
    await asyncio.wait_for(run_sync_in_worker(executor.shutdown_started.wait, 2), timeout=2)
    await asyncio.sleep(0)
    assert not shutdown.done()

    authority.release_allowed.set()
    await asyncio.wait_for(shutdown, timeout=2)
    assert lease.closed
    assert authority.release_calls == [lease.context]


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [OSError, AuditIntegrityError, FrameworkBugError])
async def test_done_callback_logger_failure_is_tracked_after_exact_lease_close(
    error_type: type[Exception], execution_fixture: ExecutionTestCustody
) -> None:
    service, _session_service, executor = _execution_service(asyncio.get_running_loop(), execution_fixture=execution_fixture)
    lease, authority = await _canonical_execute_lease(service, execution_fixture, session_id=uuid4())
    authority.release_allowed.set()
    pipeline_error = RuntimeError("pipeline failed")
    logging_error = error_type("diagnostic unavailable")

    def fail_diagnostic(event: str, **kwargs: object) -> None:
        if event == "pipeline_done_callback_exception":
            assert lease.closed
            raise logging_error

    with patch("elspeth.web.execution.service.slog.error", side_effect=fail_diagnostic):
        _watcher, completion = await _submit_terminal_callback(service, lease, executor, error=pipeline_error)
        with pytest.raises(error_type) as caught:
            await asyncio.wait_for(asyncio.wrap_future(completion), timeout=2)
        assert caught.value is logging_error
        assert authority.release_calls == [lease.context]
        assert completion in service._lease_completion_futures
        with pytest.raises(ExceptionGroup) as shutdown_failure:
            await asyncio.wait_for(execution_fixture.shutdown_service(service), timeout=2)
        assert shutdown_failure.value.exceptions == (logging_error,)


async def _await_owned_thread_signal(signal: threading.Event, *, name: str, timeout: float) -> None:
    """Observe a producer's thread event without making another worker admission."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not signal.is_set() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert signal.is_set(), f"actual {name} did not occur before its bounded deadline"


async def _failed_shutdown_release_child(root: Path, error_type: type[Exception], peer_fails: bool) -> None:
    custody = ExecutionTestCustody(asyncio.get_running_loop(), root)
    service, _session_service, executor = _execution_service(asyncio.get_running_loop(), execution_fixture=custody)
    failed_lease, failed_authority = await _canonical_execute_lease(service, custody, session_id=uuid4())
    cleanup_error = error_type("authority release failure")
    failed_authority.release_error = cleanup_error
    failed_authority.release_allowed.set()
    peer_lease, peer_authority = await _canonical_execute_lease(service, custody, session_id=uuid4())
    peer_error = OSError("peer authority release unavailable")
    if peer_fails:
        peer_authority.release_error = peer_error
    failed_obligation = failed_lease.execution_obligation
    peer_obligation = peer_lease.execution_obligation
    assert failed_obligation is not None and peer_obligation is not None
    failed_watcher = service._create_loss_watcher(failed_lease, threading.Event(), run_id=uuid4())
    peer_watcher = service._create_loss_watcher(peer_lease, threading.Event(), run_id=uuid4())
    failed_physical = executor.future
    service._submit_owned_pipeline(failed_obligation, failed_lease, failed_watcher, partial(lambda: None))
    executor.future = PhysicalPipelineCompletionControl(custody)
    peer_physical = executor.future
    service._submit_owned_pipeline(peer_obligation, peer_lease, peer_watcher, partial(lambda: None))
    failed_physical.set_result(None)
    deadline = asyncio.get_running_loop().time() + 5
    while failed_obligation.completion is None and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    failed_completion = failed_obligation.completion
    assert failed_completion is not None
    completed, _ = await asyncio.wait({asyncio.wrap_future(failed_completion)}, timeout=2)
    assert completed, "actual failed completion did not finish"
    with pytest.raises(error_type) as completed_failure:
        failed_completion.result()
    assert completed_failure.value is cleanup_error
    assert async_workers._INSTANCE_DRAINING is custody.recovery.instance_draining
    await _await_owned_thread_signal(custody.recovery.instance_draining, name="selected process drain", timeout=3)
    peer_physical.set_result(None)
    try:
        await _await_owned_thread_signal(peer_authority.release_called, name="peer SQL release entry", timeout=3)
        shutdown = asyncio.create_task(custody.shutdown_service(service))
        await _await_owned_thread_signal(executor.shutdown_started, name="private executor shutdown entry", timeout=3)
        assert not shutdown.done(), "failed release cannot produce registry completion"
    finally:
        peer_authority.release_allowed.set()
    registry = service.execution_lease_release_registry
    deadline = asyncio.get_running_loop().time() + 5
    while not registry.executor_join_succeeded and asyncio.get_running_loop().time() < deadline:
        registry.observe_ready()
        await asyncio.sleep(0.01)
    assert registry.executor_join_succeeded
    peer_completion = peer_obligation.completion
    assert peer_completion is not None
    peer_completed, _ = await asyncio.wait({asyncio.wrap_future(peer_completion)}, timeout=2)
    assert peer_completed, "actual peer completion did not finish"
    if peer_fails:
        with pytest.raises(OSError) as completed_peer_failure:
            peer_completion.result()
        assert completed_peer_failure.value is peer_error
    else:
        assert peer_completion.result() is None
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        registry.observe_ready()
        observed_peer_release = peer_obligation.release_submission
        if observed_peer_release is not None and observed_peer_release.future is not None and observed_peer_release.reservation is not None:
            peer_receipt = observed_peer_release.reservation.witness.snapshot()
            if (
                observed_peer_release.future.done()
                and observed_peer_release.observed
                and observed_peer_release.callback_return_observed
                and observed_peer_release.reservation.released
                and peer_receipt.callable_finished
                and peer_receipt.exited
                and peer_lease.closed
                and peer_obligation.completion_outcome_recorded
                and peer_obligation.completion_observed
                and peer_obligation.lifecycle_outcome_recorded
                and peer_obligation.lifecycle_observed
                and (peer_fails or peer_obligation.retired)
            ):
                break
        await asyncio.sleep(0.01)
    assert peer_lease.closed and peer_authority.release_calls == [peer_lease.context]
    assert peer_obligation.completion_outcome_recorded and peer_obligation.completion_observed
    assert peer_obligation.lifecycle_outcome_recorded and peer_obligation.lifecycle_observed
    assert peer_obligation.completion_original_error is (peer_error if peer_fails else None)
    assert peer_obligation.lifecycle_original_error is (peer_error if peer_fails else None)
    failed_release = failed_obligation.release_submission
    peer_release = peer_obligation.release_submission
    assert failed_release is not None and failed_release.future is not None and failed_release.reservation is not None
    assert peer_release is not None and peer_release.future is not None and peer_release.reservation is not None
    registry.observe_ready()
    assert failed_release.future.done() and failed_release.future.exception() is cleanup_error
    assert failed_release.original_error is cleanup_error and failed_release.observed
    failed_trace = failed_release.reservation.witness.snapshot()
    assert failed_trace.callable_finished and failed_trace.exited and not failed_trace.impossible
    assert not failed_obligation.release_succeeded and not failed_obligation.retired
    assert peer_release.future.done() and peer_release.observed
    peer_trace = peer_release.reservation.witness.snapshot()
    assert peer_trace.callable_finished and peer_trace.exited and not peer_trace.impossible
    if peer_fails:
        assert peer_release.future.exception() is peer_error and peer_release.original_error is peer_error
        assert not peer_obligation.release_succeeded and not peer_obligation.retired
    else:
        assert peer_release.future.exception() is None
        assert peer_obligation.release_succeeded and peer_obligation.retired
    expected_errors = {cleanup_error, peer_error} if peer_fails else {cleanup_error}
    assert {leaf for original in registry._failures for leaf in _original_leaves(original)} == expected_errors
    assert registry.has_pending_physical_owners() and not shutdown.done()
    with pytest.raises(BaseExceptionGroup) as refused:
        registry.assert_completed()
    assert expected_errors <= set(_original_leaves(refused.value))
    assert not custody.recovery.watchdog.completed
    _atomic_child_checkpoint(
        root / "checkpoint.json",
        {
            "exact_failed_completion_original": True,
            "failed_release_retained_pending": True,
            "peer_release_observed": True,
            "private_executor_joined": True,
            "shutdown_pending_and_complete_refused": True,
            "watchdog_completion_unsent": True,
        },
    )
    await asyncio.Event().wait()


@pytest.mark.parametrize("error_type", [RuntimeError, AuditIntegrityError, FrameworkBugError])
@pytest.mark.parametrize("peer_fails", [False, True])
def test_shutdown_preserves_completed_lease_failure_and_joins_peer(tmp_path: Path, error_type: type[Exception], peer_fails: bool) -> None:
    _run_failed_shutdown_release_child(tmp_path, error_type, peer_fails)


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


# Callback-delegation edges (elspeth-01e919e13e). Each admits ONE exact shape
# through which a local function is invoked by a consumer the service does not
# call directly. Admission makes the local function LIVE: its body is walked
# and every effect inside it is held to the same context/receiver rules as a
# direct call. Nothing else about the shape is admitted — a different keyword,
# callee, member, or binding is still an escape.
_VALIDATE_PIPELINE_MODULE = "elspeth.web.execution.validation"
_PROOF_DIAGNOSTICS_MODULE = "elspeth.web.composer.tools.generation"
_PROOF_RESOLVER_MEMBER = "_authoritative_proof_blob_resolver"


def _is_exact_worker_delegation_edge(call: ast.Call, callback: ast.AST) -> bool:
    """``run_sync_in_worker(<local def>)``: the sole argument runs, once, on the worker pool."""
    return (
        isinstance(call.func, ast.Name)
        and call.func.id == "run_sync_in_worker"
        and len(call.args) == 1
        and not call.keywords
        and call.args[0] is callback
    )


def _is_exact_approval_binding_worker_edge(call: ast.Call, callback: ast.AST) -> bool:
    """The pre-create approval compiler runs on a worker with the transferred context."""
    expected_keywords = {"user_id", "session_id", "session_operation_context"}
    keywords = {keyword.arg: keyword.value for keyword in call.keywords}
    return (
        isinstance(call.func, ast.Name)
        and call.func.id == "run_sync_in_worker"
        and len(call.args) == 2
        and call.args[0] is callback
        and isinstance(call.args[1], ast.Name)
        and call.args[1].id == "frozen_run_settings"
        and isinstance(callback, ast.Attribute)
        and isinstance(callback.value, ast.Name)
        and callback.value.id == "self"
        and callback.attr == "_approval_inputs_from_frozen"
        and len(call.keywords) == len(expected_keywords)
        and set(keywords) == expected_keywords
        and all(isinstance(value, ast.Name) and value.id == name for name, value in keywords.items())
    )


def _unique_local_callable(function: _FunctionNode, name: str, enclosing: dict[int, _FunctionNode]) -> _FunctionNode | None:
    scope: _FunctionNode | None = function
    while scope is not None:
        nodes = _walk_function_body_without_nested_functions(scope)
        bindings = _binding_nodes(scope, nodes, name=name)
        definitions = [
            node
            for node in _reachable_function_nodes(scope)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        ]
        deletions = [node for node in nodes if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Del)]
        if bindings or definitions or deletions:
            if len(definitions) == 1 and not bindings and not deletions and not definitions[0].decorator_list:
                return definitions[0]
            return None
        scope = enclosing.get(id(scope))
    return None


def _is_exact_asyncio_thread_edge(
    call: ast.Call,
    callback: ast.AST,
    function: _FunctionNode,
    enclosing: dict[int, _FunctionNode],
    parent: dict[ast.AST, ast.AST],
) -> bool:
    """Admit a lexically bound local callable passed alone to stdlib to_thread."""
    if not (
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "asyncio"
        and call.func.attr == "to_thread"
        and len(call.args) == 1
        and not call.keywords
        and call.args[0] is callback
        and isinstance(callback, ast.Name)
    ):
        return False
    definition = _unique_local_callable(function, callback.id, enclosing)
    if not isinstance(definition, ast.FunctionDef):
        return False
    consumer = parent.get(call)
    if not isinstance(consumer, ast.Await):
        if not (
            isinstance(consumer, ast.Call)
            and isinstance(consumer.func, ast.Attribute)
            and isinstance(consumer.func.value, ast.Name)
            and consumer.func.attr == "create_task"
            and consumer.args == [call]
            and all(keyword.arg == "name" for keyword in consumer.keywords)
        ):
            return False
        lease_bindings = _binding_nodes(function, _walk_function_body_without_nested_functions(function), name=consumer.func.value.id)
        if not (
            len(lease_bindings) == 1
            and isinstance(lease_bindings[0], ast.arg)
            and ast.unparse(lease_bindings[0].annotation or ast.Constant(None)) == "SessionOperationLease"
        ):
            return False
    scope: _FunctionNode | None = function
    while scope is not None:
        nodes = _walk_function_body_without_nested_functions(scope)
        if _binding_nodes(scope, nodes, name="asyncio"):
            return False
        if any(
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "asyncio"
            and isinstance(node.ctx, (ast.Store, ast.Del))
            for node in nodes
        ):
            return False
        scope = enclosing.get(id(scope))
    return True


def _is_scheduled_local_coroutine(
    call: ast.Call, parent: dict[ast.AST, ast.AST], function: _FunctionNode, enclosing: dict[int, _FunctionNode]
) -> bool:
    if not isinstance(call.func, ast.Name) or not isinstance(
        _unique_local_callable(function, call.func.id, enclosing), ast.AsyncFunctionDef
    ):
        return False
    consumer = parent.get(call)
    if isinstance(consumer, ast.Await):
        return True
    if isinstance(consumer, ast.GeneratorExp):
        expansion = parent.get(consumer)
        gather = parent.get(expansion) if isinstance(expansion, ast.Starred) else None
        if (
            isinstance(gather, ast.Call)
            and isinstance(gather.func, ast.Attribute)
            and isinstance(gather.func.value, ast.Name)
            and gather.func.value.id == "asyncio"
            and gather.func.attr == "gather"
            and isinstance(parent.get(gather), ast.Await)
        ):
            return True
    if (
        isinstance(consumer, ast.Call)
        and isinstance(consumer.func, ast.Attribute)
        and isinstance(consumer.func.value, ast.Name)
        and consumer.func.value.id == "self"
        and consumer.func.attr == "_call_async"
        and consumer.args == [call]
        and not consumer.keywords
    ):
        return True
    if not (
        isinstance(consumer, ast.Call)
        and isinstance(consumer.func, ast.Attribute)
        and isinstance(consumer.func.value, ast.Name)
        and consumer.func.value.id == "asyncio"
        and consumer.func.attr == "run_coroutine_threadsafe"
        and len(consumer.args) == 2
        and consumer.args[0] is call
        and not consumer.keywords
    ):
        return False
    scope: _FunctionNode | None = function
    while scope is not None:
        nodes = _walk_function_body_without_nested_functions(scope)
        if _binding_nodes(scope, nodes, name="asyncio") or any(
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "asyncio"
            and isinstance(node.ctx, (ast.Store, ast.Del))
            for node in nodes
        ):
            return False
        scope = enclosing.get(id(scope))
    return True


def _is_exact_envelope_blob_verifier_edge(call: ast.Call, keyword: ast.keyword) -> bool:
    """The trusted worker forwards this verifier to the trusted envelope restorer."""
    expected_keywords = {
        "current_snapshot",
        "user_id",
        "auth_provider_type",
        "resolver",
        "implementation_fingerprint",
        "deployment_generation",
        "blob_verifier",
    }
    return (
        isinstance(call.func, ast.Name)
        and call.func.id == "run_sync_in_worker"
        and len(call.args) == 2
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "restore_execution_envelope"
        and not isinstance(call.args[1], ast.Starred)
        and len(call.keywords) == len(expected_keywords)
        and {candidate.arg for candidate in call.keywords} == expected_keywords
        and keyword.arg == "blob_verifier"
        and any(candidate is keyword for candidate in call.keywords)
        and isinstance(keyword.value, ast.Name)
    )


def _is_exact_blob_metadata_callback_edge(call: ast.Call, keyword: ast.keyword) -> bool:
    """Follow the validator's exact metadata and content-read callbacks."""
    return (
        isinstance(call.func, ast.Name)
        and call.func.id == "validate_pipeline"
        and keyword.arg in {"blob_get_metadata", "blob_get_content"}
        and any(candidate is keyword for candidate in call.keywords)
        and isinstance(keyword.value, ast.Name)
    )


def _is_exact_proof_resolver_consumer_call(call: ast.Call) -> bool:
    """``compute_proof_diagnostics(..., blob_resolver=self._authoritative_proof_blob_resolver(...))``."""
    if not (isinstance(call.func, ast.Name) and call.func.id == "compute_proof_diagnostics"):
        return False
    resolver_keywords = [keyword for keyword in call.keywords if keyword.arg == "blob_resolver"]
    if len(resolver_keywords) != 1:
        return False
    resolver = resolver_keywords[0].value
    return (
        isinstance(resolver, ast.Call)
        and isinstance(resolver.func, ast.Attribute)
        and isinstance(resolver.func.value, ast.Name)
        and resolver.func.value.id == "self"
        and resolver.func.attr == _PROOF_RESOLVER_MEMBER
    )


def _is_exact_proof_resolver_return_edge(
    function: _FunctionNode,
    statement: ast.Return,
    *,
    exact_consumer_calls: tuple[ast.Call, ...],
) -> bool:
    """``return <local def>`` from the proof resolver member, consumed live as ``blob_resolver=``."""
    return function.name == _PROOF_RESOLVER_MEMBER and isinstance(statement.value, ast.Name) and bool(exact_consumer_calls)


def _has_unique_exact_import_binding(
    function: _FunctionNode,
    live_nodes: tuple[ast.AST, ...],
    *,
    name: str,
    module: str,
) -> bool:
    """``name`` is bound exactly once in ``function``, by ``from <module> import <name>``."""
    bindings = _binding_nodes(function, live_nodes, name=name)
    exact_imports = [
        node
        for node in live_nodes
        if isinstance(node, ast.ImportFrom)
        and node.module == module
        and node.level == 0
        and any(alias.name == name and alias.asname is None for alias in node.names)
    ]
    return len(bindings) == 1 and len(exact_imports) == 1


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


def _enclosing_function_map(owner: ast.ClassDef) -> dict[int, _FunctionNode]:
    """Map every function nested in a member of ``owner`` to its immediately enclosing function."""
    enclosing: dict[int, _FunctionNode] = {}

    def visit(node: ast.AST, function: _FunctionNode) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                enclosing[id(child)] = function
                visit(child, child)
            else:
                visit(child, function)

    for member in owner.body:
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
            visit(member, member)
    return enclosing


def _is_unique_exact_blob_service_alias(
    function: _FunctionNode,
    *,
    name: str,
    live_nodes_of: dict[int, tuple[ast.AST, ...]],
    enclosing: dict[int, _FunctionNode],
) -> bool:
    """``name`` is bound exactly once in its scope, and that binding is ``self._blob_service``.

    A blob effect may reach the service through a local alias
    (``staging_blob_service = self._blob_service``) but never through a name
    that is rebound, a parameter, or assigned from anything else: one binding,
    and it is the exact attribute read. A name free in a nested function is
    resolved in the lexically enclosing function under the same rule.
    """
    if id(function) not in live_nodes_of:
        live_nodes_of[id(function)] = _reachable_function_nodes(function)
    live_nodes = live_nodes_of[id(function)]
    bindings = _binding_nodes(function, live_nodes, name=name)
    if not bindings:
        outer = enclosing.get(id(function))
        if outer is None:
            return False
        return _is_unique_exact_blob_service_alias(outer, name=name, live_nodes_of=live_nodes_of, enclosing=enclosing)
    exact_assignments = [
        node
        for node in live_nodes
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == name
        and ast.unparse(node.value) == "self._blob_service"
    ]
    return len(bindings) == 1 and len(exact_assignments) == 1


@dataclass(frozen=True)
class _ExecutionReachability:
    """Functions on the execution call graph plus the local-callable edges that put them there."""

    reachable: tuple[_FunctionNode, ...]
    admitted_callback_edges: frozenset[int]
    """IDs of exact local or owned method callbacks admitted for inspection."""


def _execution_reachability(owner: ast.ClassDef) -> _ExecutionReachability:
    enclosing = _enclosing_function_map(owner)
    parent = {child: node for node in ast.walk(owner) for child in ast.iter_child_nodes(node)}
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
    admitted: set[int] = set()
    exact_consumer_calls: list[ast.Call] = []
    while pending:
        function = pending.pop()
        if id(function) in reached:
            continue
        reached.add(id(function))
        result.append(function)
        live_nodes = _reachable_function_nodes(function)
        exact_event_bus_bound = _has_unique_exact_event_bus_binding(function, live_nodes)
        exact_validator_bound = _has_unique_exact_import_binding(
            function, live_nodes, name="validate_pipeline", module=_VALIDATE_PIPELINE_MODULE
        )
        exact_diagnostics_bound = _has_unique_exact_import_binding(
            function, live_nodes, name="compute_proof_diagnostics", module=_PROOF_DIAGNOSTICS_MODULE
        )
        exact_worker_bound = _binding_nodes(function, live_nodes, name="run_sync_in_worker") == ()
        exact_envelope_bound = _binding_nodes(function, live_nodes, name="restore_execution_envelope") == ()
        for call in (candidate for candidate in live_nodes if isinstance(candidate, ast.Call)):
            if (
                isinstance(call.func, ast.Name)
                and call.func.id == "Orchestrator"
                and all(_binding_nodes(function, live_nodes, name=name) == () for name in ("Orchestrator", "LLMCallGovernance", "partial"))
            ):
                expected = ast.parse(
                    "LLMCallGovernance("
                    "before_call=partial(self._admit_run_llm_call, run_uuid, session_operation_lease), "
                    "after_call=partial(self._settle_run_llm_call, run_uuid, session_operation_lease, "
                    "landscape_db=landscape_db, landscape_run_id=run_id))",
                    mode="eval",
                ).body
                for keyword in call.keywords:
                    if keyword.arg == "llm_call_governance" and ast.dump(keyword.value) == ast.dump(expected):
                        assert isinstance(keyword.value, ast.Call)
                        for callback in keyword.value.keywords:
                            assert isinstance(callback.value, ast.Call)
                            edge = callback.value.args[0]
                            assert isinstance(edge, ast.Attribute)
                            pending.append(members[edge.attr])
                            admitted.add(id(edge))
            if (
                isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "self"
                and call.func.attr in members
            ):
                pending.append(members[call.func.attr])
            if exact_diagnostics_bound and _is_exact_proof_resolver_consumer_call(call):
                exact_consumer_calls.append(call)
            callable_edges: list[ast.expr] = []
            if isinstance(call.func, ast.Name):
                if isinstance(local_functions.get(call.func.id), ast.AsyncFunctionDef):
                    if _is_scheduled_local_coroutine(call, parent, function, enclosing):
                        definition = _unique_local_callable(function, call.func.id, enclosing)
                        assert definition is not None
                        pending.append(definition)
                else:
                    callable_edges.append(call.func)
            delegated_edges: list[ast.expr] = []
            delegated_edges.extend(
                edge
                for edge in call.args
                if exact_event_bus_bound and isinstance(edge, ast.Name) and _is_exact_progress_callback_edge(call, edge)
            )
            delegated_edges.extend(
                edge
                for edge in call.args
                if exact_worker_bound and isinstance(edge, ast.Name) and _is_exact_worker_delegation_edge(call, edge)
            )
            if function.name == "_execute_locked" and exact_worker_bound:
                for edge in call.args:
                    if isinstance(edge, ast.Attribute) and _is_exact_approval_binding_worker_edge(call, edge):
                        pending.append(members[edge.attr])
                        admitted.add(id(edge))
            thread_edges = [edge for edge in call.args if _is_exact_asyncio_thread_edge(call, edge, function, enclosing, parent)]
            for edge in thread_edges:
                assert isinstance(edge, ast.Name)
                definition = _unique_local_callable(function, edge.id, enclosing)
                assert definition is not None
                pending.append(definition)
                admitted.add(id(edge))
            delegated_edges.extend(
                keyword.value
                for keyword in call.keywords
                if exact_validator_bound and isinstance(keyword.value, ast.Name) and _is_exact_blob_metadata_callback_edge(call, keyword)
            )
            delegated_edges.extend(
                keyword.value
                for keyword in call.keywords
                if exact_worker_bound and exact_envelope_bound and _is_exact_envelope_blob_verifier_edge(call, keyword)
            )
            admitted.update(id(edge) for edge in delegated_edges)
            callable_edges.extend(delegated_edges)
            pending.extend(local_functions[edge.id] for edge in callable_edges if isinstance(edge, ast.Name) and edge.id in local_functions)
        for statement in (candidate for candidate in live_nodes if isinstance(candidate, ast.Return)):
            if (
                isinstance(statement.value, ast.Name)
                and statement.value.id in local_functions
                and _is_exact_proof_resolver_return_edge(function, statement, exact_consumer_calls=tuple(exact_consumer_calls))
            ):
                admitted.add(id(statement.value))
                pending.append(local_functions[statement.value.id])
    return _ExecutionReachability(reachable=tuple(result), admitted_callback_edges=frozenset(admitted))


def _reachable_execution_functions(owner: ast.ClassDef) -> tuple[_FunctionNode, ...]:
    return _execution_reachability(owner).reachable


_EXECUTION_EFFECT_NAMES = frozenset(
    {
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
        "record_token_usage",
        "begin_provider_attempt",
        "settle_provider_attempt",
    }
)
_EXECUTION_GATE_CALL_NAMES = _EXECUTION_EFFECT_NAMES | {"_persist_and_broadcast_run_event", "_finalize_output_blobs"}
_SESSION_SERVICE_EFFECTS = frozenset(
    {
        "create_run",
        "update_run_status",
        "append_run_event",
        "record_blob_inline_resolutions",
        "record_token_usage",
        "begin_provider_attempt",
        "settle_provider_attempt",
    }
)
_BLOB_SERVICE_EFFECTS = frozenset({"get_blob", "link_blob_to_run", "read_blob_content", "finalize_run_output_blobs"})


@dataclass(frozen=True)
class _ExecutionEffectFindings:
    """Structural findings of the session-operation-lease gate over one class body.

    Every ``*_offenders``/``escaped_*``/``unreachable_*`` field is empty for a
    class that passes; the production gate asserts each is empty, and the
    callback-edge control tests assert exactly which finding a near-miss shape
    produces.
    """

    reachable: tuple[_FunctionNode, ...]
    live_nodes: tuple[ast.AST, ...]
    live_calls: tuple[ast.Call, ...]
    effect_calls: tuple[ast.Call, ...]
    parent: dict[ast.AST, ast.AST]
    unreachable_decoys: tuple[ast.Call, ...]
    unresolved_receivers: tuple[str, ...]
    context_offenders: tuple[str, ...]
    escaped_effects: tuple[str, ...]
    escaped_local_helpers: tuple[str, ...]
    escaped_class_helpers: tuple[str, ...]


def _stable_ast_bytes(node: ast.AST) -> bytes:
    """Serialize every AST field, including empty ones, across Python 3.12/3.13."""

    def encode(value: object) -> object:
        if isinstance(value, ast.AST):
            return [type(value).__name__, [[name, encode(child)] for name, child in ast.iter_fields(value)]]
        if isinstance(value, list):
            return [encode(child) for child in value]
        return value

    return json.dumps(encode(node), ensure_ascii=False, separators=(",", ":")).encode()


def _exact_recovery_lease_owner_edges(member: _FunctionNode) -> frozenset[int]:
    """Prove recover_run's bound submit and awaited no-completion cleanup."""
    if member.name != "recover_run":
        return frozenset()
    parents = {child: node for node in ast.walk(member) for child in ast.iter_child_nodes(node)}
    calls = [node for node in ast.walk(member) if isinstance(node, ast.Call)]

    def one(target: str) -> ast.Call | None:
        matching = [call for call in calls if ast.unparse(call.func) == target]
        return matching[0] if len(matching) == 1 else None

    obligation = one("self._execution_obligation")
    watcher = one("self._create_loss_watcher")
    submit = one("self._submit_owned_pipeline")
    join = one("self._join_loss_watcher")
    if any(call is None for call in (obligation, watcher, submit, join)):
        return frozenset()
    assert obligation is not None and watcher is not None and submit is not None and join is not None

    def assigned_once(call: ast.Call, name: str) -> bool:
        parent = parents[call]
        return (
            isinstance(parent, ast.Assign)
            and len(parent.targets) == 1
            and isinstance(parent.targets[0], ast.Name)
            and parent.targets[0].id == name
            and sum(isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Store) for node in ast.walk(member)) == 1
            and not any(isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Del) for node in ast.walk(member))
        )

    if not (
        assigned_once(obligation, "obligation")
        and assigned_once(watcher, "watcher")
        and len(obligation.args) == 1
        and ast.unparse(obligation.args[0]) == "session_operation_lease"
        and not obligation.keywords
        and tuple(ast.unparse(arg) for arg in watcher.args) == ("session_operation_lease", "shutdown_event")
        and len(watcher.keywords) == 1
        and watcher.keywords[0].arg == "run_id"
        and ast.unparse(watcher.keywords[0].value) == "run.id"
        and tuple(ast.unparse(arg) for arg in submit.args[:3]) == ("obligation", "session_operation_lease", "watcher")
        and len(submit.args) == 4
        and not submit.keywords
        and tuple(ast.unparse(arg) for arg in join.args) == ("watcher", "session_operation_lease")
        and not join.keywords
    ):
        return frozenset()
    partial_call = submit.args[3]
    if not (
        isinstance(partial_call, ast.Call)
        and isinstance(partial_call.func, ast.Name)
        and partial_call.func.id == "partial"
        and tuple(ast.unparse(arg) for arg in partial_call.args)
        == (
            "self._run_pipeline",
            "str(run.id)",
            "run.pipeline_yaml",
            "shutdown_event",
            "restored.settings",
            "session.user_id",
            "session.auth_provider_type",
        )
        and [keyword.arg for keyword in partial_call.keywords]
        == ["session_operation_lease", "durable_admission", "resume_existing", "restored_envelope"]
        and tuple(ast.unparse(keyword.value) for keyword in partial_call.keywords)
        == ("session_operation_lease", "True", "resume_existing", "restored")
    ):
        return frozenset()

    # The nominal obligation contains the exact lease. Account for its every
    # read, not just loads of the raw lease parameter. The watcher likewise
    # remains only on the physical submit/join edges.
    submit_obligation = submit.args[0]
    submit_watcher = submit.args[2]
    join_watcher = join.args[0]
    if not (
        isinstance(parents[submit], ast.Expr) and isinstance(parents[join], ast.Await) and isinstance(parents[parents[join]], ast.Assign)
    ):
        return frozenset()
    submit_statement = parents[submit]
    submit_try = parents[submit_statement]
    if not (
        isinstance(submit_try, ast.Try)
        and parents[submit_try] is member
        and submit_try.body == [submit_statement]
        and len(submit_try.handlers) == 1
        and not submit_try.orelse
        and not submit_try.finalbody
        and len(member.body) >= 2
        and member.body[-2] is submit_try
        and isinstance(member.body[-1], ast.Return)
        and isinstance(member.body[-1].value, ast.Constant)
        and member.body[-1].value.value is True
    ):
        return frozenset()
    # The lease owner is created on the method's executed path. A lexical
    # assignment nested under a skipped branch is not an initialized owner.
    obligation_statement = parents[obligation]
    watcher_statement = parents[watcher]
    if parents[obligation_statement] is not member or parents[watcher_statement] is not member:
        return frozenset()
    obligation_index = member.body.index(obligation_statement)
    watcher_index = member.body.index(watcher_statement)
    submit_index = member.body.index(submit_try)
    if not (obligation_index < watcher_index < submit_index):
        return frozenset()
    # Before the first owner is acquired, only the docstring and imports may
    # execute. In particular no branch can skip its assignment and continue.
    if any(
        not (
            isinstance(statement, (ast.ImportFrom, ast.Import))
            or (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str))
        )
        for statement in member.body[:obligation_index]
    ):
        return frozenset()
    # Before the watcher exists, the caller may still retain a refused lease,
    # but it must not report a successful transfer without a submission.
    if any(
        isinstance(node, ast.Return) and not (isinstance(node.value, ast.Constant) and node.value.value is False)
        for statement in member.body[obligation_index + 1 : watcher_index]
        for node in ast.walk(statement)
    ):
        return frozenset()
    # The optional YAML assertion must precede both shutdown-map registration
    # and watcher creation, so a failed assertion cannot strand either owner.
    if watcher_index < 3:
        return frozenset()
    expected_assert = ast.parse("assert run.pipeline_yaml is not None").body[0]
    expected_event = ast.parse("shutdown_event = threading.Event()").body[0]
    map_statement = member.body[watcher_index - 1]
    expected_map = ast.parse("with self._shutdown_events_lock:\n    self._shutdown_events[str(run.id)] = shutdown_event\n").body[0]
    if any(
        ast.dump(actual, include_attributes=False) != ast.dump(expected, include_attributes=False)
        for actual, expected in zip(
            member.body[watcher_index - 3 : watcher_index], (expected_assert, expected_event, expected_map), strict=True
        )
    ):
        return frozenset()

    # Once the watcher exists, only inert, fresh diagnostic facts may precede
    # the checked submission. They cannot return, raise, await, call, branch,
    # rebind a consumed name, or strand the watcher outside the handler.
    def inert_fact(statement: ast.stmt) -> bool:
        if isinstance(statement, ast.Assign):
            if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                return False
            target = statement.targets[0]
            value = statement.value
        elif isinstance(statement, ast.AnnAssign):
            if (
                not isinstance(statement.target, ast.Name)
                or statement.simple != 1
                or not isinstance(statement.annotation, ast.Name)
                or statement.annotation.id != "int"
            ):
                return False
            target = statement.target
            value = statement.value
        else:
            return False
        if not isinstance(value, ast.Constant) or not isinstance(value.value, (str, int, float, bool, type(None))):
            return False
        return sum(isinstance(node, ast.Name) and node.id == target.id for node in ast.walk(member)) == 1

    if any(not inert_fact(statement) for statement in member.body[watcher_index + 1 : submit_index]):
        return frozenset()
    handler = submit_try.handlers[0]
    if not (
        isinstance(handler.type, ast.Name)
        and handler.type.id == "BaseException"
        and handler.name is not None
        and len(handler.body) == 2
        and isinstance(handler.body[0], ast.If)
        and isinstance(handler.body[1], ast.Raise)
        and handler.body[1].exc is None
    ):
        return frozenset()
    no_completion = handler.body[0]
    condition = no_completion.test
    if not (
        isinstance(condition, ast.UnaryOp)
        and isinstance(condition.op, ast.Not)
        and isinstance(condition.operand, ast.Attribute)
        and condition.operand.attr == "completion_required"
        and isinstance(condition.operand.value, ast.Name)
        and condition.operand.value.id == "obligation"
        and not no_completion.orelse
        and no_completion.body
        and parents[parents[join]] is no_completion.body[0]
    ):
        return frozenset()
    join_assignment = parents[parents[join]]
    if not (
        len(join_assignment.targets) == 1
        and isinstance(join_assignment.targets[0], ast.Tuple)
        and len(join_assignment.targets[0].elts) == 2
        and all(isinstance(item, ast.Name) for item in join_assignment.targets[0].elts)
    ):
        return frozenset()
    cancellation_name, watcher_error_name = (item.id for item in join_assignment.targets[0].elts)
    if cancellation_name == watcher_error_name:
        return frozenset()
    # This is the entire no-completion arm. No unproved middle statement may
    # return success, erase retained errors, or bypass shutdown-map cleanup.
    if len(no_completion.body) != 5:
        return frozenset()
    failures_assignment = no_completion.body[1]
    if not (
        isinstance(failures_assignment, ast.AnnAssign)
        and isinstance(failures_assignment.target, ast.Name)
        and ast.unparse(failures_assignment.annotation) == "list[BaseException]"
        and isinstance(failures_assignment.value, ast.List)
        and len(failures_assignment.value.elts) == 1
        and isinstance(failures_assignment.value.elts[0], ast.Name)
        and failures_assignment.value.elts[0].id == handler.name
    ):
        return frozenset()
    failure_name = failures_assignment.target.id
    watcher_error_arm = no_completion.body[2]
    if not (
        isinstance(watcher_error_arm, ast.If)
        and ast.unparse(watcher_error_arm.test) == f"{watcher_error_name} is not None"
        and len(watcher_error_arm.body) == 1
        and isinstance(watcher_error_arm.body[0], ast.Expr)
        and ast.unparse(watcher_error_arm.body[0].value) == f"{failure_name}.append({watcher_error_name})"
        and not watcher_error_arm.orelse
        and isinstance(no_completion.body[4], ast.Expr)
        and ast.unparse(no_completion.body[4].value) == f"_raise_lifecycle_originals({cancellation_name}, {failure_name})"
    ):
        return frozenset()
    cleanup_try = no_completion.body[3]
    if not (isinstance(cleanup_try, ast.Try) and len(cleanup_try.handlers) == 1 and cleanup_try.handlers[0].name is not None):
        return frozenset()
    cleanup_original_name = cleanup_try.handlers[0].name
    expected_cleanup = ast.parse(
        "try:\n"
        "    with self._shutdown_events_lock:\n"
        "        del self._shutdown_events[str(run.id)]\n"
        f"except BaseException as {cleanup_original_name}:\n"
        f"    {failure_name}.append({cleanup_original_name})\n"
    ).body[0]
    if ast.dump(cleanup_try, include_attributes=False) != ast.dump(expected_cleanup, include_attributes=False):
        return frozenset()
    # Bound callable lookups must still resolve on the proven receiver. In
    # this reviewed method the only attribute/subscript mutations are the
    # exact shutdown-map registration and deletion already checked above.
    # Any other store/delete can replace an owned callable, carrier or input.
    mutating_targets = [
        node for node in ast.walk(member) if isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.ctx, (ast.Store, ast.Del))
    ]
    map_targets = [
        node
        for node in ast.walk(map_statement)
        if isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.ctx, (ast.Store, ast.Del))
    ]
    cleanup_targets = [
        node
        for node in ast.walk(cleanup_try)
        if isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.ctx, (ast.Store, ast.Del))
    ]
    if (
        len(map_targets) != 1
        or len(cleanup_targets) != 1
        or not isinstance(map_targets[0], ast.Subscript)
        or not isinstance(map_targets[0].ctx, ast.Store)
        or not isinstance(cleanup_targets[0], ast.Subscript)
        or not isinstance(cleanup_targets[0].ctx, ast.Del)
        or {id(node) for node in mutating_targets} != {id(map_targets[0]), id(cleanup_targets[0])}
    ):
        return frozenset()
    # Dynamic mutation or reflection can replace the checked method lookup
    # without a direct AST store. This method uses none of those operations;
    # fail closed rather than attempting to interpret arbitrary aliases.
    reflective_names = {"setattr", "delattr", "vars", "getattr", "globals", "locals", "eval", "exec"}
    reflective_methods = {"__setattr__", "__delattr__", "__getattribute__"}
    if any(
        (isinstance(node, ast.Attribute) and node.attr in reflective_names | reflective_methods | {"__dict__"})
        or (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in reflective_names)
        or (isinstance(node, ast.alias) and node.name.split(".")[-1] in reflective_names)
        for node in ast.walk(member)
    ):
        return frozenset()
    # A lexical ledger proves where every protected value is bound and read.
    # Name(Store) alone misses Python's exception, import, match and definition
    # bindings; a global declaration changes where an otherwise exact store
    # writes. Walk nested scopes too and reject extra bindings conservatively.
    bindings: dict[str, set[int]] = {}

    def bind(name: str, node: ast.AST) -> None:
        bindings.setdefault(name, set()).add(id(node))

    declarations: set[str] = set()
    deleted: set[str] = set()
    for node in ast.walk(member):
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Store):
                bind(node.id, node)
            elif isinstance(node.ctx, ast.Del):
                deleted.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name is not None:
            bind(node.name, node)
        elif isinstance(node, ast.arg):
            bind(node.arg, node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node is not member:
            bind(node.name, node)
        elif isinstance(node, ast.alias):
            bind(node.asname if node.asname is not None else node.name.split(".")[0], node)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name is not None:
            bind(node.name, node)
        elif isinstance(node, ast.MatchMapping) and node.rest is not None:
            bind(node.rest, node)
        elif isinstance(node, (ast.TypeVar, ast.ParamSpec, ast.TypeVarTuple)):
            bind(node.name, node)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            declarations.update(node.names)

    watcher_test = watcher_error_arm.test
    watcher_append = watcher_error_arm.body[0].value
    cleanup_append = cleanup_try.handlers[0].body[0].value
    lifecycle_raise = no_completion.body[4].value
    if not (
        isinstance(watcher_test, ast.Compare)
        and isinstance(watcher_test.left, ast.Name)
        and isinstance(watcher_append, ast.Call)
        and isinstance(watcher_append.func, ast.Attribute)
        and isinstance(watcher_append.func.value, ast.Name)
        and len(watcher_append.args) == 1
        and isinstance(watcher_append.args[0], ast.Name)
        and isinstance(cleanup_append, ast.Call)
        and isinstance(cleanup_append.func, ast.Attribute)
        and isinstance(cleanup_append.func.value, ast.Name)
        and len(cleanup_append.args) == 1
        and isinstance(cleanup_append.args[0], ast.Name)
        and isinstance(lifecycle_raise, ast.Call)
        and len(lifecycle_raise.args) == 2
        and all(isinstance(arg, ast.Name) for arg in lifecycle_raise.args)
    ):
        return frozenset()
    # The event is a local owner carrier, while the receiver, run, lease and
    # resume input are roots of the checked physical graph. Derive the root
    # parameter set from actual checked call arguments, not example mutation
    # names. Every such name must retain its original ast.arg binding.
    event_assignment = member.body[watcher_index - 2]
    if not (
        isinstance(event_assignment, ast.Assign)
        and len(event_assignment.targets) == 1
        and isinstance(event_assignment.targets[0], ast.Name)
        and isinstance(obligation.func, ast.Attribute)
        and isinstance(obligation.func.value, ast.Name)
        and isinstance(obligation.args[0], ast.Name)
        and isinstance(watcher.keywords[0].value, ast.Attribute)
        and isinstance(watcher.keywords[0].value.value, ast.Name)
        and isinstance(partial_call.keywords[2].value, ast.Name)
        and isinstance(watcher.args[1], ast.Name)
        and isinstance(partial_call.args[3], ast.Name)
    ):
        return frozenset()
    event_name = ast.unparse(event_assignment.targets[0])
    event_reads_in_map = [
        node for node in ast.walk(map_statement) if isinstance(node, ast.Name) and node.id == event_name and isinstance(node.ctx, ast.Load)
    ]
    if len(event_reads_in_map) != 1 or ast.unparse(watcher.args[1]) != event_name or ast.unparse(partial_call.args[3]) != event_name:
        return frozenset()
    root_uses = (
        obligation.func.value,
        obligation.args[0],
        watcher.keywords[0].value.value,
        partial_call.keywords[2].value,
    )
    root_names = {ast.unparse(node) for node in root_uses}
    parameter_nodes = (
        member.args.posonlyargs
        + member.args.args
        + member.args.kwonlyargs
        + ([member.args.vararg] if member.args.vararg is not None else [])
        + ([member.args.kwarg] if member.args.kwarg is not None else [])
    )
    parameter_origins = {arg.arg: {id(arg)} for arg in parameter_nodes}
    if len(root_names) != len(root_uses) or root_names != parameter_origins.keys():
        return frozenset()
    # Stable origin is insufficient if a new consumer receives the receiver
    # or one of the bound methods as a first-class value. Every receiver read
    # must remain an attribute receiver in the reviewed method-use graph.
    receiver_name = ast.unparse(root_uses[0])
    receiver_counts: dict[str, int] = {}
    for node in ast.walk(member):
        if isinstance(node, ast.Name) and node.id == receiver_name and isinstance(node.ctx, ast.Load):
            parent = parents[node]
            if not isinstance(parent, ast.Attribute) or parent.value is not node:
                return frozenset()
            receiver_counts[parent.attr] = receiver_counts.get(parent.attr, 0) + 1
    # This is the reviewed receiver-use inventory, not a list of forbidden
    # examples. A new use of even an existing attribute changes the graph.
    expected_receiver_counts = {
        "_approval_inputs_from_frozen": 1,
        "_create_loss_watcher": 1,
        "_execution_obligation": 1,
        "_join_loss_watcher": 1,
        "_materialize_durable_cancellation": 1,
        "_principal_is_active": 2,
        "_record_recovery_refusal": 2,
        "_restore_admitted_run": 2,
        "_run_pipeline": 1,
        "_session_service": 6,
        "_settings": 1,
        "_settle_admission_refusal": 1,
        "_shutdown_events": 2,
        "_shutdown_events_lock": 2,
        "_submit_owned_pipeline": 1,
        "_trained_operator_mode": 2,
    }
    if receiver_counts != expected_receiver_counts:
        return frozenset()
    # Check the complete in-method consumer path for each reviewed receiver
    # and receiver-derived local read. A bound method, collaborator, settings
    # object or restore result must reach only its original AST consumer.
    # The path records each ancestor's field/index and the exact call function;
    # one-for-one wrappers, inline captures, and new aliases all change it.
    derived_names = {"session", "active", "approval_inputs", "permit", "restored"}
    owned_roles = {"obligation", "watcher"}
    derived_origins: dict[str, set[int]] = {name: set() for name in derived_names}
    for assignment in ast.walk(member):
        if isinstance(assignment, ast.Assign) and len(assignment.targets) == 1 and isinstance(assignment.targets[0], ast.Name):
            target = assignment.targets[0]
            value = assignment.value
        elif isinstance(assignment, ast.AnnAssign) and isinstance(assignment.target, ast.Name):
            target = assignment.target
            value = assignment.value
        else:
            continue
        if (
            value is not None
            and any(isinstance(part, ast.Name) and part.id == receiver_name and isinstance(part.ctx, ast.Load) for part in ast.walk(value))
            and target.id not in owned_roles
        ):
            if target.id not in derived_names:
                return frozenset()
            derived_origins[target.id].add(id(target))
        elif target.id in derived_names and isinstance(value, ast.Constant) and value.value is None:
            derived_origins[target.id].add(id(target))
    if {name: len(origins) for name, origins in derived_origins.items()} != {
        "session": 3,
        "active": 2,
        "approval_inputs": 2,
        "permit": 2,
        "restored": 3,
    }:
        return frozenset()

    def consumer_path(node: ast.AST) -> str:
        segments: list[str] = []
        while node is not member:
            parent = parents[node]
            position = None
            for field, value in ast.iter_fields(parent):
                if value is node:
                    position = field
                    break
                if isinstance(value, list):
                    for index, item in enumerate(value):
                        if item is node:
                            position = f"{field}[{index}]"
                            break
                    if position is not None:
                        break
            if position is None:
                return "<unknown>"
            label = type(parent).__name__
            if isinstance(parent, ast.Attribute):
                label += f"[{parent.attr}]"
            if isinstance(parent, ast.Call):
                label += f"[{ast.unparse(parent.func)}]"
            segments.append(f"{label}.{position}")
            node = parent
            if isinstance(parent, ast.stmt):
                break
        return "/".join(segments)

    actual_paths: dict[str, dict[str, int]] = {}
    for node in ast.walk(member):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in derived_names | {receiver_name}:
            by_path = actual_paths.setdefault(node.id, {})
            path = consumer_path(node)
            by_path[path] = by_path.get(path, 0) + 1
    expected_paths = {
        "active": {
            "Call[run_sync_in_worker].args[0]/Await.value/UnaryOp.operand/BoolOp.values[1]/BoolOp.values[1]/BoolOp.values[1]/If.test": 2,
            "Compare.left/BoolOp.values[0]/BoolOp.values[1]/BoolOp.values[1]/If.test": 2,
        },
        "approval_inputs": {
            "Compare.left/If.test": 1,
            "keyword.value/Call[self._session_service.assess_run_start_admission].keywords[1]/Await.value/Assign.value": 1,
        },
        "permit": {
            "Attribute[execution_refusal].value/Compare.left/BoolOp.values[1]/If.test": 1,
            "Attribute[state].value/Compare.left/BoolOp.values[0]/If.test": 1,
            "Attribute[state].value/Compare.left/If.test": 1,
        },
        "restored": {
            "Attribute[settings].value/Call[partial].args[4]/Call[self._submit_owned_pipeline].args[3]/Expr.value": 1,
            "Attribute[settings].value/Call[run_sync_in_worker].args[1]/Await.value/Assign.value": 1,
            "Compare.left/If.test": 1,
            "keyword.value/Call[partial].keywords[3]/Call[self._submit_owned_pipeline].args[3]/Expr.value": 1,
        },
        "self": {
            "Attribute[_approval_inputs_from_frozen].value/Call[run_sync_in_worker].args[0]/Await.value/Assign.value": 1,
            "Attribute[_create_loss_watcher].value/Call[self._create_loss_watcher].func/Assign.value": 1,
            "Attribute[_execution_obligation].value/Call[self._execution_obligation].func/Assign.value": 1,
            "Attribute[_join_loss_watcher].value/Call[self._join_loss_watcher].func/Await.value/Assign.value": 1,
            "Attribute[_materialize_durable_cancellation].value/Call[self._materialize_durable_cancellation].func/Await.value/Expr.value": 1,
            "Attribute[_principal_is_active].value/Assign.value": 2,
            "Attribute[_record_recovery_refusal].value/Call[self._record_recovery_refusal].func/Await.value/Expr.value": 2,
            "Attribute[_restore_admitted_run].value/Call[self._restore_admitted_run].func/Await.value/Assign.value": 2,
            "Attribute[_run_pipeline].value/Call[partial].args[0]/Call[self._submit_owned_pipeline].args[3]/Expr.value": 1,
            "Attribute[_session_service].value/Attribute[assess_run_start_admission].value/Call[self._session_service.assess_run_start_admission].func/Await.value/Assign.value": 2,
            "Attribute[_session_service].value/Attribute[get_session].value/Call[self._session_service.get_session].func/Await.value/Assign.value": 2,
            "Attribute[_session_service].value/Attribute[session_operation_authority].value/Attribute[mutate].value/Call[run_sync_in_worker].args[0]/Await.value/Expr.value": 2,
            "Attribute[_settings].value/Attribute[workflow_governance].value/Compare.left/If.test": 1,
            "Attribute[_settle_admission_refusal].value/Call[self._settle_admission_refusal].func/Await.value/Expr.value": 1,
            "Attribute[_shutdown_events].value/Subscript.value/Assign.targets[0]": 1,
            "Attribute[_shutdown_events].value/Subscript.value/Delete.targets[0]": 1,
            "Attribute[_shutdown_events_lock].value/withitem.context_expr/With.items[0]": 2,
            "Attribute[_submit_owned_pipeline].value/Call[self._submit_owned_pipeline].func/Expr.value": 1,
            "Attribute[_trained_operator_mode].value/UnaryOp.operand/BoolOp.values[0]/BoolOp.values[1]/If.test": 2,
        },
        "session": {
            "Attribute[archived_at].value/Compare.left/BoolOp.values[0]/If.test": 2,
            "Attribute[auth_provider_type].value/Call[partial].args[6]/Call[self._submit_owned_pipeline].args[3]/Expr.value": 1,
            "Attribute[auth_provider_type].value/keyword.value/Call[self._restore_admitted_run].keywords[1]/Await.value/Assign.value": 2,
            "Attribute[user_id].value/Call[partial].args[5]/Call[self._submit_owned_pipeline].args[3]/Expr.value": 1,
            "Attribute[user_id].value/Call[run_sync_in_worker].args[1]/Await.value/UnaryOp.operand/BoolOp.values[1]/BoolOp.values[1]/BoolOp.values[1]/If.test": 2,
            "Attribute[user_id].value/keyword.value/Call[run_sync_in_worker].keywords[0]/Await.value/Assign.value": 1,
            "Attribute[user_id].value/keyword.value/Call[self._restore_admitted_run].keywords[0]/Await.value/Assign.value": 2,
            "Compare.left/If.test": 1,
        },
    }
    if actual_paths != expected_paths:
        return frozenset()
    protected_callable_lookups = (obligation.func, watcher.func, submit.func, join.func, partial_call.args[0])
    if not all(
        isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == receiver_name
        for node in protected_callable_lookups
    ):
        return frozenset()
    lookup_names = {node.attr for node in protected_callable_lookups if isinstance(node, ast.Attribute)}
    if len(lookup_names) != len(protected_callable_lookups):
        return frozenset()
    seen_lookups = {
        id(node)
        for node in ast.walk(member)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == receiver_name
        and node.attr in lookup_names
        and isinstance(node.ctx, ast.Load)
    }
    if seen_lookups != {id(node) for node in protected_callable_lookups}:
        return frozenset()
    protected_origins = {
        "obligation": {id(parents[obligation].targets[0])},
        "watcher": {id(parents[watcher].targets[0])},
        event_name: {id(event_assignment.targets[0])},
        handler.name: {id(handler)},
        cancellation_name: {id(join_assignment.targets[0].elts[0])},
        watcher_error_name: {id(join_assignment.targets[0].elts[1])},
        failure_name: {id(failures_assignment.target)},
        cleanup_original_name: {id(cleanup_try.handlers[0])},
    }
    if len(protected_origins) != 8 or protected_origins.keys() & root_names:
        return frozenset()
    protected_reads = {
        "obligation": {id(submit_obligation), id(condition.operand.value)},
        "watcher": {id(submit_watcher), id(join_watcher)},
        event_name: {id(event_reads_in_map[0]), id(watcher.args[1]), id(partial_call.args[3])},
        handler.name: {id(failures_assignment.value.elts[0])},
        cancellation_name: {id(lifecycle_raise.args[0])},
        watcher_error_name: {id(watcher_test.left), id(watcher_append.args[0])},
        failure_name: {id(watcher_append.func.value), id(cleanup_append.func.value), id(lifecycle_raise.args[1])},
        cleanup_original_name: {id(cleanup_append.args[0])},
    }
    if len(protected_reads) != 8:
        return frozenset()

    # These names are taken from helper calls, exception types and the list
    # annotation in the *verified* graph. None may become a local binding or
    # be captured by an owned role. This protects partial/str/raiser/type
    # lookup without maintaining a list of sample collision strings.
    dependency_names = {call.func.id for call in ast.walk(submit_try) if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)}
    dependency_names.update(
        node.id
        for expression in (handler.type, cleanup_try.handlers[0].type, failures_assignment.annotation)
        for node in ast.walk(expression)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    )
    if dependency_names & (protected_origins.keys() | root_names):
        return frozenset()
    if any(name in bindings or name in declarations or name in deleted for name in dependency_names):
        return frozenset()
    # Check the ledger against Python's own lexical scope classification.
    # A newly supported binding form that changes a helper into a local must
    # not be silently treated as the original module/built-in dependency.
    import symtable

    try:
        module_symbols = symtable.symtable(ast.unparse(member), "<recovery-lease-proof>", "exec")
        function_symbols = module_symbols.get_children()
        if len(function_symbols) != 1 or function_symbols[0].get_name() != member.name:
            return frozenset()
        lexical_scope = function_symbols[0]
        for name in protected_origins:
            symbol = lexical_scope.lookup(name)
            if not symbol.is_local() or symbol.is_global() or symbol.is_nonlocal() or symbol.is_parameter():
                return frozenset()
        for name in root_names:
            symbol = lexical_scope.lookup(name)
            if not symbol.is_local() or symbol.is_global() or symbol.is_nonlocal() or not symbol.is_parameter():
                return frozenset()
        for name in dependency_names:
            symbol = lexical_scope.lookup(name)
            if symbol.is_local() or symbol.is_parameter() or symbol.is_nonlocal():
                return frozenset()
    except (KeyError, SyntaxError, ValueError):
        return frozenset()
    for name, origins in protected_origins.items():
        reads = {id(node) for node in ast.walk(member) if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load)}
        if bindings.get(name) != origins or reads != protected_reads[name] or name in declarations or name in deleted:
            return frozenset()
    for name, origins in derived_origins.items():
        if bindings.get(name) != origins or name in declarations or name in deleted:
            return frozenset()
    for name, origins in parameter_origins.items():
        if bindings.get(name) != origins or name in declarations or name in deleted:
            return frozenset()
    # The semantic owner proof above checks the five physical edges, live
    # bindings, cleanup and exact consumer paths. Close the remaining
    # in-method effect surface by comparing the entire executed source shape
    # with the reviewed method. Only independently proved inert fresh facts
    # and collision-free cleanup role alpha-renames are canonicalized. Thus a
    # new class/global consumer, call, with statement, comprehension or alias
    # cannot ride on the fact that it avoids the receiver-use inventory.
    import copy
    import hashlib

    inert_indices: set[int] = set()
    for index in range(obligation_index + 1, submit_index):
        statement = member.body[index]
        if not inert_fact(statement):
            continue
        target = statement.targets[0] if isinstance(statement, ast.Assign) else statement.target
        assert isinstance(target, ast.Name)
        if (
            target.id in declarations
            or target.id in deleted
            or target.id in parameter_origins
            or target.id in protected_origins
            or target.id in derived_origins
            or target.id in dependency_names
            or bindings.get(target.id) != {id(target)}
        ):
            return frozenset()
        inert_indices.add(index)
    canonical = copy.deepcopy(member)
    canonical.body = [statement for index, statement in enumerate(canonical.body) if index not in inert_indices]
    alpha_roles = {
        handler.name: "primary",
        cancellation_name: "cancellations",
        watcher_error_name: "watcher_error",
        failure_name: "failures",
        cleanup_original_name: "original",
    }
    if len(alpha_roles) != 5:
        return frozenset()
    for node in ast.walk(canonical):
        if isinstance(node, ast.Name) and node.id in alpha_roles:
            node.id = alpha_roles[node.id]
        elif isinstance(node, ast.ExceptHandler) and node.name in alpha_roles:
            node.name = alpha_roles[node.name]
    reviewed_shape = "4bc769cd586408eead0c595fa8225cca0f0013bd981e8c1dd52b50cc6a9e73ea"
    if hashlib.sha256(_stable_ast_bytes(canonical)).hexdigest() != reviewed_shape:
        return frozenset()
    return frozenset(
        id(node)
        for node in (
            obligation.args[0],
            watcher.args[0],
            submit.args[1],
            partial_call.keywords[0].value,
            join.args[1],
        )
    )


def _caller_lease_escapes(member: _FunctionNode) -> bool:
    """Keep the transferred lease within the reviewed execution consumers."""
    recovery_edges = _exact_recovery_lease_owner_edges(member)
    parents = {child: node for node in ast.walk(member) for child in ast.iter_child_nodes(node)}
    positional_consumers = {
        "self._signal_shutdown_on_operation_loss": 0,
        "self._settle_admission_refusal": 1,
        "self._materialize_durable_cancellation": 1,
        "self._record_recovery_refusal": 1,
        "self._record_run_token_usage": 1,
        "self._admit_run_llm_call": 1,
        "self._settle_run_llm_call": 1,
    }
    keyword_consumers = {
        "self._broadcast_progress_event",
        "self._finalize_output_blobs",
        "self._persist_and_broadcast_run_event",
        "self._persist_failed_run_status",
        "self._probe_run_already_terminal",
    }
    for node in ast.walk(member):
        if not (isinstance(node, ast.Name) and node.id == "session_operation_lease" and isinstance(node.ctx, ast.Load)):
            continue
        if id(node) in recovery_edges:
            continue
        consumer = parents[node]
        if isinstance(consumer, ast.Attribute) and consumer.value is node and isinstance(consumer.ctx, ast.Load):
            continue
        if isinstance(consumer, ast.Call):
            position = positional_consumers.get(ast.unparse(consumer.func))
            if position is not None and len(consumer.args) > position and consumer.args[position] is node:
                continue
            if isinstance(consumer.func, ast.Name) and consumer.func.id == "partial" and consumer.args:
                position = positional_consumers.get(ast.unparse(consumer.args[0]))
                if position is not None and len(consumer.args) > position + 1 and consumer.args[position + 1] is node:
                    continue
        if isinstance(consumer, ast.keyword) and consumer.arg == "session_operation_lease":
            call = parents[consumer]
            if isinstance(call, ast.Call):
                target = ast.unparse(call.func)
                if target in keyword_consumers:
                    continue
                if call.args and (
                    (target == "partial" and ast.unparse(call.args[0]) == "self._on_pipeline_done")
                    or (target == "self._executor.submit" and ast.unparse(call.args[0]) == "self._run_pipeline")
                ):
                    continue
        return True
    return False


def _has_transferred_lease_context(
    call: ast.Call, function: _FunctionNode, owner: ast.ClassDef, enclosing: dict[int, _FunctionNode]
) -> bool:
    """Prove a closure's lease.context comes from an unchanged transferred parameter."""
    values = [keyword.value for keyword in call.keywords if keyword.arg == "session_operation_context"]
    if len(values) != 1:
        return False
    value = values[0]
    if not (isinstance(value, ast.Attribute) and value.attr == "context" and isinstance(value.value, ast.Name)):
        return False
    name = value.value.id
    scope = function
    while id(scope) in enclosing:
        if _binding_nodes(scope, _walk_function_body_without_nested_functions(scope), name=name):
            return False
        scope = enclosing[id(scope)]
    parameters = [*scope.args.posonlyargs, *scope.args.args]
    matching = [parameter for parameter in parameters if parameter.arg == name]
    if len(matching) != 1 or ast.unparse(matching[0].annotation or ast.Constant(None)) != "SessionOperationLease":
        return False
    if _binding_nodes(scope, _walk_function_body_without_nested_functions(scope), name=name) != (matching[0],):
        return False
    parents = {child: node for node in ast.walk(scope) for child in ast.iter_child_nodes(node)}
    if any(
        isinstance(node, ast.Name)
        and node.id == name
        and isinstance(node.ctx, ast.Load)
        and not (
            isinstance(parents.get(node), ast.Attribute)
            and cast(ast.Attribute, parents[node]).value is node
            and isinstance(cast(ast.Attribute, parents[node]).ctx, ast.Load)
        )
        for node in ast.walk(scope)
    ):
        return False
    position = parameters.index(matching[0]) - 1  # self is supplied by attribute dispatch
    callers = [
        (member, candidate, candidate.args if isinstance(candidate.func, ast.Attribute) else candidate.args[1:])
        for member in owner.body
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
        for candidate in _reachable_function_nodes(member)
        if isinstance(candidate, ast.Call)
        and (
            ast.unparse(candidate.func) == f"self.{scope.name}"
            or (
                isinstance(candidate.func, ast.Name)
                and candidate.func.id == "partial"
                and candidate.args
                and ast.unparse(candidate.args[0]) == f"self.{scope.name}"
                and scope.name in {"_admit_run_llm_call", "_settle_run_llm_call"}
            )
        )
    ]
    return bool(callers) and all(
        position >= 0
        and len(arguments) > position
        and isinstance(arguments[position], ast.Name)
        and arguments[position].id == "session_operation_lease"
        and not any(keyword.arg == name for keyword in candidate.keywords)
        and any(parameter.arg == "session_operation_lease" for parameter in (*member.args.args, *member.args.kwonlyargs))
        and len(_binding_nodes(member, _walk_function_body_without_nested_functions(member), name="session_operation_lease")) == 1
        and not _caller_lease_escapes(member)
        and not any(
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "session_operation_lease"
            and isinstance(node.ctx, (ast.Store, ast.Del))
            for node in ast.walk(member)
        )
        and not any(
            isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
            and isinstance(node.value, ast.Name)
            and node.value.id == "session_operation_lease"
            for node in ast.walk(member)
        )
        for member, candidate, arguments in callers
    )


def _execution_effect_findings(owner: ast.ClassDef) -> _ExecutionEffectFindings:
    reachability = _execution_reachability(owner)
    reachable = reachability.reachable
    function_live_nodes = {id(function): _reachable_function_nodes(function) for function in reachable}
    live_nodes = tuple(node for function in reachable for node in function_live_nodes[id(function)])
    call_owner = {id(node): function for function in reachable for node in function_live_nodes[id(function)] if isinstance(node, ast.Call)}
    enclosing_functions = _enclosing_function_map(owner)
    live_calls = tuple(node for node in live_nodes if isinstance(node, ast.Call))
    calls = tuple(call for call in live_calls if _call_name(call) in _EXECUTION_EFFECT_NAMES)
    all_gate_calls = {
        id(call): call
        for function in reachable
        for call in ast.walk(function)
        if isinstance(call, ast.Call) and _call_name(call) in _EXECUTION_GATE_CALL_NAMES
    }
    live_gate_call_ids = {id(call) for call in live_calls if _call_name(call) in _EXECUTION_GATE_CALL_NAMES}
    unreachable_decoys = tuple(call for call_id, call in all_gate_calls.items() if call_id not in live_gate_call_ids)

    unresolved_receivers: list[str] = []
    for call in calls:
        if _call_name(call) == "_fetch_blob_contents":
            if not isinstance(call.func, ast.Name):
                unresolved_receivers.append(f"line {call.lineno}: {ast.unparse(call)} is not the bare module-level inline read")
            continue
        if not isinstance(call.func, ast.Attribute):
            unresolved_receivers.append(f"line {call.lineno}: {ast.unparse(call)} is not an attribute call on an owned service")
            continue
        receiver = ast.unparse(call.func.value)
        if call.func.attr in _SESSION_SERVICE_EFFECTS and receiver != "self._session_service":
            unresolved_receivers.append(
                f"line {call.lineno}: {ast.unparse(call)} reaches the session service through an unresolved receiver {receiver!r}"
            )
        elif call.func.attr in _BLOB_SERVICE_EFFECTS and not (
            receiver == "self._blob_service"
            or (
                isinstance(call.func.value, ast.Name)
                and _is_unique_exact_blob_service_alias(
                    call_owner[id(call)],
                    name=call.func.value.id,
                    live_nodes_of=function_live_nodes,
                    enclosing=enclosing_functions,
                )
            )
        ):
            unresolved_receivers.append(
                f"line {call.lineno}: {ast.unparse(call)} reaches the blob service through an unresolved receiver {receiver!r}"
            )
    context_offenders = tuple(
        sorted(
            {
                _call_name(call) or ""
                for call in calls
                if not _exact_context_keyword(call)
                and not _has_transferred_lease_context(call, call_owner[id(call)], owner, enclosing_functions)
            }
        )
    )

    parent = {child: node for function in reachable for node in ast.walk(function) for child in ast.iter_child_nodes(node)}
    escaped_effects = tuple(
        sorted(
            {
                node.attr
                for node in live_nodes
                if isinstance(node, ast.Attribute)
                and node.attr in _EXECUTION_EFFECT_NAMES
                and not (isinstance(parent.get(node), ast.Call) and cast(ast.Call, parent[node]).func is node)
            }
        )
    )

    local_function_names = {
        nested.name
        for member in owner.body
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
        for nested in ast.walk(member)
        if isinstance(nested, (ast.FunctionDef, ast.AsyncFunctionDef)) and nested is not member
    }

    def is_direct_callable_edge(node: ast.AST) -> bool:
        container = parent.get(node)
        if isinstance(container, ast.Call) and container.func is node:
            return True
        return id(node) in reachability.admitted_callback_edges

    escaped_local_helpers = tuple(
        node.id
        for node in live_nodes
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id in local_function_names
        and not is_direct_callable_edge(node)
    )

    class_member_names = {member.name for member in owner.body if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))}
    escaped_class_helpers = tuple(
        node.attr
        for node in live_nodes
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr in class_member_names
        and not is_direct_callable_edge(node)
    )
    return _ExecutionEffectFindings(
        reachable=reachable,
        live_nodes=live_nodes,
        live_calls=live_calls,
        effect_calls=calls,
        parent=parent,
        unreachable_decoys=unreachable_decoys,
        unresolved_receivers=tuple(unresolved_receivers),
        context_offenders=context_offenders,
        escaped_effects=escaped_effects,
        escaped_local_helpers=escaped_local_helpers,
        escaped_class_helpers=escaped_class_helpers,
    )


@pytest.mark.parametrize("mutation", ["none", "lease", "constructor"])
def test_provider_quota_callback_edges_require_exact_governance_and_transferred_lease(mutation: str) -> None:
    owner = _class_node(ExecutionServiceImpl)
    governance = next(
        node
        for node in ast.walk(owner)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "LLMCallGovernance"
    )
    if mutation == "constructor":
        governance.func.id = "UnrelatedCallbackContainer"
    elif mutation == "lease":
        callback = governance.keywords[0].value
        assert isinstance(callback, ast.Call)
        callback.args[2] = ast.Name(id="other_lease", ctx=ast.Load())
    findings = _execution_effect_findings(owner)
    if mutation == "none":
        assert findings.escaped_class_helpers == ()
        assert findings.context_offenders == ()
    else:
        assert "_admit_run_llm_call" in findings.escaped_class_helpers


def test_every_worker_run_blob_progress_output_and_terminal_effect_uses_same_context() -> None:
    """No helper may mint a context or silently fall back to legacy writers."""
    owner = _class_node(ExecutionServiceImpl)
    effect_names = _EXECUTION_EFFECT_NAMES
    gate_call_names = _EXECUTION_GATE_CALL_NAMES
    findings = _execution_effect_findings(owner)
    reachable = findings.reachable
    live_nodes = findings.live_nodes
    live_calls = findings.live_calls
    calls = findings.effect_calls
    parent = findings.parent
    assert findings.unreachable_decoys == (), (
        "execution effects cannot hide in dead tails, false branches, or uncalled local functions: "
        f"{[(call.lineno, _call_name(call), ast.unparse(call)) for call in findings.unreachable_decoys]}"
    )
    assert findings.unresolved_receivers == (), (
        f"execution effects reach services through unresolved receivers: {findings.unresolved_receivers}"
    )
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
    assert findings.context_offenders == (), f"execution effects omit the exact transferred context: {findings.context_offenders}"
    assert findings.escaped_effects == (), f"execution effect callable is aliased or escapes direct inspection: {findings.escaped_effects}"

    exact_event_bus_functions = [
        function for function in reachable if _has_unique_exact_event_bus_binding(function, _reachable_function_nodes(function))
    ]
    assert execution_service_module.EventBus is EventBus, "execution callback bus constructor provenance changed"
    assert execution_service_module.run_sync_in_worker is run_sync_in_worker, "execution worker delegation provenance changed"
    assert execution_service_module.asyncio is asyncio, "execution thread/coroutine scheduler module provenance changed"
    assert execution_service_module.asyncio.to_thread is to_thread
    assert execution_service_module.asyncio.run_coroutine_threadsafe is run_coroutine_threadsafe
    assert execution_service_module.restore_execution_envelope is restore_execution_envelope, (
        "execution envelope consumer provenance changed"
    )
    assert [function.name for function in exact_event_bus_functions] == ["_run_pipeline"]
    assert findings.escaped_local_helpers == (), (
        f"local execution helper callable escapes direct analysis: {findings.escaped_local_helpers}"
    )

    class_member_names = {member.name for member in owner.body if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert findings.escaped_class_helpers == (), f"class execution helper callable is aliased or escapes: {findings.escaped_class_helpers}"

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


# ── Callback-delegation edge controls (elspeth-01e919e13e) ──────────────────
# Each edge is admitted for ONE exact shape. The controls below run the same
# structural findings the production gate asserts on, over synthetic class
# sources: (i) the exact shape admits the closure as live — its effect is a
# live effect call, held to the exact context and receiver; (ii) the same
# closure passed under any other keyword, callee, member, binding, or
# alias is still an escape (and its read a decoy); (iii) a read inside an
# admitted closure with the wrong context or receiver is still an offender.

_EDGE_CONTROL_HEAD = """
class Service:
    async def _execute_locked(self, state, *, session_operation_lease):
        session_operation_context = session_operation_lease.context
        other_context = session_operation_lease.context
        await self._entry(state, session_id=None, session_operation_context=session_operation_context)

    async def _run_pipeline(self, *, session_operation_lease):
        return None

    async def _handle_pipeline_submission_failure(self, *, session_operation_lease):
        return None
"""


@dataclass(frozen=True)
class _EdgeControlCase:
    body: str
    closure: str
    admitted: bool
    effect_name: str = "get_blob"
    context_offenders: tuple[str, ...] = ()
    unresolved_receiver_count: int = 0
    escaped_class_helpers: tuple[str, ...] = ()
    escaped_decoys: tuple[str, ...] = ("get_blob",)
    """Gate calls left unreachable when the closure escapes; empty when the read lives in a member reached only through it."""


def _synthetic_owner(body: str) -> ast.ClassDef:
    tree = ast.parse(_EDGE_CONTROL_HEAD + textwrap.indent(textwrap.dedent(body), "    "))
    return next(node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == "Service")


def _assert_edge_control(case: _EdgeControlCase) -> None:
    findings = _execution_effect_findings(_synthetic_owner(case.body))
    reachable_names = [function.name for function in findings.reachable]
    decoy_names = sorted(_call_name(call) or "" for call in findings.unreachable_decoys)
    live_effects = sorted(_call_name(call) or "" for call in findings.effect_calls)
    if case.admitted:
        assert case.closure in reachable_names, f"{case.closure} was not admitted as live: {reachable_names}"
        assert findings.escaped_local_helpers == (), findings.escaped_local_helpers
        assert decoy_names == [], decoy_names
        assert live_effects == [case.effect_name], live_effects
    else:
        assert case.closure not in reachable_names, f"{case.closure} was admitted through a non-exact shape: {reachable_names}"
        assert findings.escaped_local_helpers == (case.closure,), findings.escaped_local_helpers
        assert decoy_names == list(case.escaped_decoys), decoy_names
        assert live_effects == [], live_effects
    assert findings.context_offenders == case.context_offenders, findings.context_offenders
    assert len(findings.unresolved_receivers) == case.unresolved_receiver_count, findings.unresolved_receivers
    assert findings.escaped_class_helpers == case.escaped_class_helpers, findings.escaped_class_helpers


_BLOB_METADATA_EDGE = """
    def _entry(self, state, *, session_id, session_operation_context):
        from {module} import {validator}

        def _blob_get_metadata(blob_id):
            return self._call_async({receiver}.get_blob(blob_id, session_operation_context={context}))

        return {validator}(state, session_id=session_id, {keyword}=_blob_get_metadata)
"""


def _blob_metadata_case(
    *,
    module: str = _VALIDATE_PIPELINE_MODULE,
    validator: str = "validate_pipeline",
    keyword: str = "blob_get_metadata",
    receiver: str = "self._blob_service",
    context: str = "session_operation_context",
    admitted: bool,
    context_offenders: tuple[str, ...] = (),
    unresolved_receiver_count: int = 0,
) -> _EdgeControlCase:
    return _EdgeControlCase(
        body=_BLOB_METADATA_EDGE.format(module=module, validator=validator, keyword=keyword, receiver=receiver, context=context),
        closure="_blob_get_metadata",
        admitted=admitted,
        context_offenders=context_offenders,
        unresolved_receiver_count=unresolved_receiver_count,
    )


@pytest.mark.parametrize(
    ("case_id", "case"),
    [
        ("exact-shape-admitted", _blob_metadata_case(admitted=True)),
        ("other-keyword-escapes", _blob_metadata_case(keyword="blob_metadata", admitted=False)),
        ("other-callee-escapes", _blob_metadata_case(validator="check_pipeline", admitted=False)),
        ("other-module-binding-escapes", _blob_metadata_case(module="elspeth.web.execution.staging", admitted=False)),
        ("admitted-wrong-context-flagged", _blob_metadata_case(admitted=True, context="other_context", context_offenders=("get_blob",))),
        ("admitted-wrong-receiver-flagged", _blob_metadata_case(admitted=True, receiver="self._staging", unresolved_receiver_count=1)),
    ],
)
def test_blob_metadata_callback_edge_admits_only_the_exact_validate_pipeline_keyword(case_id: str, case: _EdgeControlCase) -> None:
    _assert_edge_control(case)


_BLOB_CONTENT_EDGE = """
    def _entry(self, state, *, session_id, session_operation_context):
        from {module} import validate_pipeline

        def _blob_get_content(blob_id):
            return self._call_async(self._blob_service.read_blob_content(blob_id, session_operation_context=session_operation_context))

        return validate_pipeline(state, session_id=session_id, {keyword}=_blob_get_content)
"""


@pytest.mark.parametrize(("keyword", "admitted"), [("blob_get_content", True), ("blob_content", False)])
def test_blob_content_callback_edge_requires_exact_validator_keyword(keyword: str, admitted: bool) -> None:
    """A renamed content callback is again an unreachable effect, not an accepted shortcut."""
    _assert_edge_control(
        _EdgeControlCase(
            body=_BLOB_CONTENT_EDGE.format(module=_VALIDATE_PIPELINE_MODULE, keyword=keyword),
            closure="_blob_get_content",
            admitted=admitted,
            effect_name="read_blob_content",
            escaped_decoys=("read_blob_content",),
        )
    )


_PROOF_RESOLVER_EDGE = """
    def _entry(self, state, *, session_id, session_operation_context):
        from {module} import {consumer}

        {consumption}

    def {member}(self, state, *, session_id, session_operation_context):
        def _resolve(blob_id):
            return self._call_async({receiver}.get_blob(blob_id, session_operation_context={context}))

        return _resolve
"""
_DIRECT_CONSUMPTION = (
    "return {consumer}(state, {keyword}=self.{member}(state, session_id=session_id, session_operation_context=session_operation_context))"
)
_ALIASED_CONSUMPTION = (
    "resolver = self.{member}(state, session_id=session_id, session_operation_context=session_operation_context)\n"
    "        return {consumer}(state, {keyword}=resolver)"
)


def _proof_resolver_case(
    *,
    module: str = _PROOF_DIAGNOSTICS_MODULE,
    consumer: str = "compute_proof_diagnostics",
    keyword: str = "blob_resolver",
    member: str = _PROOF_RESOLVER_MEMBER,
    consumption: str = _DIRECT_CONSUMPTION,
    receiver: str = "self._blob_service",
    context: str = "session_operation_context",
    admitted: bool,
    context_offenders: tuple[str, ...] = (),
    unresolved_receiver_count: int = 0,
) -> _EdgeControlCase:
    body = _PROOF_RESOLVER_EDGE.format(
        module=module,
        consumer=consumer,
        member=member,
        receiver=receiver,
        context=context,
        consumption=consumption.format(consumer=consumer, keyword=keyword, member=member),
    )
    return _EdgeControlCase(
        body=body,
        closure="_resolve",
        admitted=admitted,
        context_offenders=context_offenders,
        unresolved_receiver_count=unresolved_receiver_count,
    )


@pytest.mark.parametrize(
    ("case_id", "case"),
    [
        ("exact-shape-admitted", _proof_resolver_case(admitted=True)),
        ("other-member-escapes", _proof_resolver_case(member="_staging_blob_resolver", admitted=False)),
        ("other-callee-escapes", _proof_resolver_case(consumer="score_proof", admitted=False)),
        ("other-keyword-escapes", _proof_resolver_case(keyword="resolver", admitted=False)),
        ("aliased-consumption-escapes", _proof_resolver_case(consumption=_ALIASED_CONSUMPTION, admitted=False)),
        ("other-module-binding-escapes", _proof_resolver_case(module="elspeth.web.composer.tools.staging", admitted=False)),
        ("admitted-wrong-context-flagged", _proof_resolver_case(admitted=True, context="other_context", context_offenders=("get_blob",))),
        ("admitted-wrong-receiver-flagged", _proof_resolver_case(admitted=True, receiver="self._staging", unresolved_receiver_count=1)),
    ],
)
def test_proof_resolver_return_edge_admits_only_the_exact_blob_resolver_consumption(case_id: str, case: _EdgeControlCase) -> None:
    _assert_edge_control(case)


_WORKER_DELEGATION_EDGE = """
    async def _entry(self, state, *, session_id, session_operation_context):
        {rebinding}
        def _preflight():
            return self._preflight_sync(state, session_operation_context=session_operation_context)

        return await {handoff}

    def _preflight_sync(self, state, *, session_operation_context):
        return self._call_async({receiver}.get_blob(state.blob_id, session_operation_context={context}))
"""


def _worker_delegation_case(
    *,
    handoff: str = "run_sync_in_worker(_preflight)",
    rebinding: str = "",
    receiver: str = "self._blob_service",
    context: str = "session_operation_context",
    admitted: bool,
    context_offenders: tuple[str, ...] = (),
    unresolved_receiver_count: int = 0,
    escaped_class_helpers: tuple[str, ...] = (),
) -> _EdgeControlCase:
    return _EdgeControlCase(
        body=_WORKER_DELEGATION_EDGE.format(handoff=handoff, rebinding=rebinding, receiver=receiver, context=context),
        closure="_preflight",
        admitted=admitted,
        context_offenders=context_offenders,
        unresolved_receiver_count=unresolved_receiver_count,
        escaped_class_helpers=escaped_class_helpers,
        # The read sits in ``_preflight_sync``, a member reached only through
        # the closure: an unadmitted handoff leaves it unreached (the pre-fix
        # invisibility), and the escape is what the gate flags.
        escaped_decoys=(),
    )


@pytest.mark.parametrize(
    ("case_id", "case"),
    [
        ("exact-shape-admitted", _worker_delegation_case(admitted=True)),
        ("extra-argument-escapes", _worker_delegation_case(handoff="run_sync_in_worker(_preflight, state)", admitted=False)),
        ("keyword-handoff-escapes", _worker_delegation_case(handoff="run_sync_in_worker(func=_preflight)", admitted=False)),
        ("other-callee-escapes", _worker_delegation_case(handoff="run_in_thread(_preflight)", admitted=False)),
        (
            "local-rebinding-escapes",
            _worker_delegation_case(rebinding="run_sync_in_worker = self._runner", admitted=False),
        ),
        (
            "admitted-wrong-context-flagged",
            _worker_delegation_case(admitted=True, context="other_context", context_offenders=("get_blob",)),
        ),
        ("admitted-wrong-receiver-flagged", _worker_delegation_case(admitted=True, receiver="self._staging", unresolved_receiver_count=1)),
    ],
)
def test_worker_delegation_edge_admits_only_the_sole_local_callable(case_id: str, case: _EdgeControlCase) -> None:
    _assert_edge_control(case)


@pytest.mark.parametrize(
    "mutation",
    ["none", "other_worker", "rebound_worker", "other_helper", "missing_context", "wrong_context", "extra_argument"],
)
def test_approval_binding_worker_edge_admits_only_exact_transferred_context(mutation: str) -> None:
    owner = _class_node(ExecutionServiceImpl)
    locked = next(member for member in owner.body if isinstance(member, ast.AsyncFunctionDef) and member.name == "_execute_locked")
    handoffs = [
        call
        for call in ast.walk(locked)
        if isinstance(call, ast.Call)
        and call.args
        and isinstance(call.args[0], ast.Attribute)
        and call.args[0].attr == "_approval_inputs_from_frozen"
    ]
    assert len(handoffs) == 2
    target = handoffs[0]
    if mutation == "other_worker":
        target.func = ast.Name(id="run_in_thread", ctx=ast.Load())
    elif mutation == "rebound_worker":
        locked.body.insert(0, ast.parse("run_sync_in_worker = self._runner").body[0])
    elif mutation == "other_helper":
        callback = target.args[0]
        assert isinstance(callback, ast.Attribute)
        callback.attr = "_plugin_snapshot_for_user"
    elif mutation == "missing_context":
        target.keywords = [keyword for keyword in target.keywords if keyword.arg != "session_operation_context"]
    elif mutation == "wrong_context":
        context = next(keyword for keyword in target.keywords if keyword.arg == "session_operation_context")
        context.value = ast.Name(id="other_context", ctx=ast.Load())
    elif mutation == "extra_argument":
        target.args.append(ast.Name(id="unreviewed_argument", ctx=ast.Load()))

    findings = _execution_effect_findings(owner)
    if mutation == "none":
        assert findings.escaped_class_helpers == ()
        assert findings.context_offenders == ()
    else:
        escaped = "_plugin_snapshot_for_user" if mutation == "other_helper" else "_approval_inputs_from_frozen"
        assert escaped in findings.escaped_class_helpers


@pytest.mark.parametrize(
    ("handoff", "rebinding", "admitted"),
    [
        ("asyncio.to_thread(_preflight)", "", True),
        ("asyncio.to_thread(_preflight, state)", "", False),
        ("asyncio.to_thread(func=_preflight)", "", False),
        ("other.to_thread(_preflight)", "", False),
        ("asyncio.other(_preflight)", "", False),
        ("asyncio.to_thread(_preflight)", "asyncio = self._runner", False),
        ("asyncio.to_thread(_preflight)", "import other as asyncio", False),
        ("asyncio.to_thread(_preflight)", "asyncio.to_thread = self._runner", False),
        ("asyncio.to_thread(_preflight)", "_preflight = self._runner", False),
    ],
)
def test_asyncio_thread_delegation_requires_exact_scheduler_and_callback(handoff: str, rebinding: str, admitted: bool) -> None:
    _assert_edge_control(_worker_delegation_case(handoff=handoff, rebinding=rebinding, admitted=admitted))


@pytest.mark.parametrize(
    "mutation",
    [
        "none",
        "module",
        "method",
        "callback",
        "dead",
        "context",
        "caller",
        "rebind",
        "unscheduled",
        "decorated",
        "coroutine_rebound",
        "duplicate_callback",
        "lease_attribute",
        "lease_alias",
        "caller_attribute",
        "caller_alias",
        "caller_mutator",
        "caller_keyword_mutator",
    ],
)
def test_admission_cleanup_scanner_controls(mutation: str) -> None:
    owner = _class_node(ExecutionServiceImpl)
    settlement = next(node for node in owner.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_settle_admission_refusal")
    thread_call = next(node for node in ast.walk(settlement) if isinstance(node, ast.Call) and _call_name(node) == "to_thread")
    if mutation == "callback":
        thread_call.args = [ast.Name(id="unrelated_callback", ctx=ast.Load())]
    elif mutation == "unscheduled":
        for statement in settlement.body:
            if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Call) and _call_name(statement.value) == "create_task":
                statement.value = thread_call
    elif mutation == "decorated":
        callback = next(node for node in settlement.body if isinstance(node, ast.FunctionDef) and node.name == "reconcile_landscape")
        callback.decorator_list = [ast.parse("lambda fn: lambda: None", mode="eval").body]
    elif mutation == "coroutine_rebound":
        settlement.body.append(ast.parse("finish_cleanup = lambda: asyncio.sleep(0)").body[0])
    elif mutation == "duplicate_callback":
        callback = next(node for node in settlement.body if isinstance(node, ast.FunctionDef) and node.name == "reconcile_landscape")
        duplicate = ast.parse(ast.unparse(callback)).body[0]
        callback.body = [ast.Pass()]
        other = ast.parse("def unrelated(self): pass").body[0]
        assert isinstance(other, ast.FunctionDef)
        other.body = [duplicate]
        owner.body.append(other)
    elif mutation == "lease_attribute":
        settlement.body.insert(0, ast.parse("lease._context = unrelated_context").body[0])
    elif mutation == "lease_alias":
        settlement.body.insert(0, ast.parse("lease_alias = lease").body[0])
    elif mutation in {"caller_mutator", "caller_keyword_mutator"}:
        caller = next(node for node in owner.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "recover_run")
        injected = (
            "setattr(session_operation_lease, '_context', unrelated_context)"
            if mutation == "caller_mutator"
            else "mutate_context(lease=session_operation_lease)"
        )
        caller.body[0:0] = ast.parse(injected).body
    elif mutation in {"caller_attribute", "caller_alias"}:
        caller = next(node for node in owner.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "recover_run")
        injected = (
            "session_operation_lease._context = unrelated_context"
            if mutation == "caller_attribute"
            else "lease_alias = session_operation_lease\nlease_alias._context = unrelated_context"
        )
        caller.body[0:0] = ast.parse(injected).body
    elif mutation == "dead":
        settlement.body.insert(0, ast.Return(value=None))
    elif mutation in {"module", "method"}:
        for node in ast.walk(settlement):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "run_coroutine_threadsafe":
                if mutation == "module":
                    node.func.value = ast.Name(id="unrelated_scheduler", ctx=ast.Load())
                else:
                    node.func.attr = "unrelated_method"
    elif mutation == "context":
        for node in ast.walk(settlement):
            if isinstance(node, ast.keyword) and node.arg == "session_operation_context":
                node.value = ast.Name(id="other_context", ctx=ast.Load())
    elif mutation == "caller":
        for node in ast.walk(owner):
            if isinstance(node, ast.Call) and _call_name(node) == "_settle_admission_refusal":
                node.args[1] = ast.Name(id="other_lease", ctx=ast.Load())
    elif mutation == "rebind":
        settlement.body.insert(0, ast.parse("lease = other_lease").body[0])
    ast.fix_missing_locations(owner)
    findings = _execution_effect_findings(owner)
    decoys = [_call_name(call) for call in findings.unreachable_decoys]
    if mutation in {
        "module",
        "method",
        "callback",
        "dead",
        "unscheduled",
        "decorated",
        "coroutine_rebound",
        "duplicate_callback",
        "rebind",
    }:
        assert "finalize_run_output_blobs" in decoys
    elif mutation in {
        "context",
        "caller",
        "lease_attribute",
        "lease_alias",
        "caller_attribute",
        "caller_alias",
        "caller_mutator",
        "caller_keyword_mutator",
    }:
        assert "finalize_run_output_blobs" in findings.context_offenders
    else:
        assert findings.unreachable_decoys == ()
        assert findings.context_offenders == ()
        assert findings.escaped_local_helpers == ()


def test_worker_delegation_edge_does_not_admit_a_partial_over_a_class_member() -> None:
    """The withdrawn shape: ``run_sync_in_worker(partial(self.<member>, ...))`` hides the member from the walk."""
    body = """
    async def _entry(self, state, *, session_id, session_operation_context):
        return await run_sync_in_worker(partial(self._preflight_sync, state, session_operation_context=session_operation_context))

    def _preflight_sync(self, state, *, session_operation_context):
        return self._call_async(self._blob_service.get_blob(state.blob_id, session_operation_context=session_operation_context))
    """
    findings = _execution_effect_findings(_synthetic_owner(body))
    assert "_preflight_sync" not in [function.name for function in findings.reachable]
    assert findings.escaped_class_helpers == ("_preflight_sync",)
    assert [_call_name(call) for call in findings.effect_calls] == []


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


def _assert_async_completion_close_owner(owner: ast.ClassDef, helper: ast.AsyncFunctionDef) -> None:
    import hashlib

    callback = next(member for member in owner.body if isinstance(member, ast.FunctionDef) and member.name == "_on_pipeline_done")
    callback_body = _walk_function_body_without_nested_functions(callback)
    callback_calls = [_call_name(node) for node in callback_body if isinstance(node, ast.Call)]
    assert "run_coroutine_threadsafe" in callback_calls
    assert callback_calls.count("exception") + callback_calls.count("result") == 1, "worker Future must be consumed exactly once"
    assert not any(isinstance(node, ast.Await) for node in callback_body)

    close_sites: list[tuple[ast.AsyncFunctionDef, ast.Call]] = []
    for member in owner.body:
        if not isinstance(member, ast.AsyncFunctionDef):
            continue
        closes = [
            node
            for node in ast.walk(member)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "close_execute_lease_before_transfer"
        ]
        if closes:
            close_sites.extend((member, call) for call in closes)
    assert len(close_sites) == 1, "one async completion path must own the sole lease-close handoff"
    close_function, close_call = close_sites[0]
    assert close_function.name == "_close_execution_authority"
    assert [ast.unparse(arg) for arg in close_call.args] == ["session_operation_lease"] and close_call.keywords == []
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "close"
        and ast.unparse(node.func.value) == "session_operation_lease"
        for node in ast.walk(owner)
    ), "completion must not close the lease again outside its owned helper"

    finish = next(
        member for member in owner.body if isinstance(member, ast.AsyncFunctionDef) and member.name == "_finish_execution_authority"
    )
    scheduled = [
        node for node in callback_body if isinstance(node, ast.Call) and ast.unparse(node.func) == "asyncio.run_coroutine_threadsafe"
    ]
    assert len(scheduled) == 1
    assert ast.unparse(scheduled[0].args[0]) == "self._finish_execution_authority(obligation, session_operation_lease, loss_watcher, exc)"
    finish_calls = [
        node
        for node in ast.walk(finish)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and ast.unparse(node.value.func) == "self._close_execution_authority"
    ]
    assert len(finish_calls) == 1
    assert [ast.unparse(arg) for arg in finish_calls[0].value.args] == ["session_operation_lease", "loss_watcher", "exc"]

    join = next(statement for statement in close_function.body if isinstance(statement, ast.If))
    assert ast.unparse(join.test) == "loss_watcher is not None"
    join_try = join.body[0]
    assert isinstance(join_try, ast.Try) and len(join_try.handlers) == 1
    handler = join_try.handlers[0]
    assert handler.type is not None and ast.unparse(handler.type) == "BaseException"
    assert len(handler.body) == 1 and ast.unparse(handler.body[0]) == "failed_loss_watcher = original"
    close_try = close_function.body[close_function.body.index(join) + 1]
    assert isinstance(close_try, ast.Try) and len(close_try.body) == 1
    close_edge = close_try.body[0]
    assert isinstance(close_edge, ast.Expr) and isinstance(close_edge.value, ast.Await) and close_edge.value.value is close_call
    helper_closes = [node for node in ast.walk(helper) if isinstance(node, ast.Call) and ast.unparse(node.func) == "lease.close"]
    assert len(helper_closes) == 1
    create_tasks = [node for node in ast.walk(helper) if isinstance(node, ast.Call) and ast.unparse(node.func) == "asyncio.create_task"]
    assert len(create_tasks) == 1 and create_tasks[0].args == [helper_closes[0]]

    # Catch-all watcher settlement followed by the unconditional owned close has
    # finally semantics. Pin these producer bodies after proving the exact edges:
    # an early return, narrowed handler, lease substitution or unjoined close
    # requires review rather than a syntactically convincing helper reference.
    shapes = {
        "_close_execution_authority": "62563bc01a2af20846053d30bb70583462c71eb3ac76ad49a7cf910bd33859f2",
        "_finish_execution_authority": "e749535b4ff010c0b4063ce9a3d4f55f15f10633e42f5a09a7b91ff5760a89ec",
        "close_execute_lease_before_transfer": "2b58bf6b6eecb7aa94745bc0506939eb778cf4de1cfe95a9ae3fbb28cd13afda",
    }
    assert {node.name: hashlib.sha256(_stable_ast_bytes(node)).hexdigest() for node in (close_function, finish, helper)} == shapes


def test_done_callback_is_nonblocking_and_async_cleanup_closes_exactly_once() -> None:
    helper = ast.parse(textwrap.dedent(inspect.getsource(execution_service_module.close_execute_lease_before_transfer))).body[0]
    assert isinstance(helper, ast.AsyncFunctionDef)
    _assert_async_completion_close_owner(_class_node(ExecutionServiceImpl), helper)


@pytest.mark.parametrize(
    "mutation",
    ["duplicate_handoff", "removed_handoff", "wrong_owner", "duplicate_close", "early_return", "narrowed_handler", "unjoined_close"],
)
def test_async_completion_close_owner_rejects_broken_handoff(mutation: str) -> None:
    import copy

    owner = _class_node(ExecutionServiceImpl)
    helper = ast.parse(textwrap.dedent(inspect.getsource(execution_service_module.close_execute_lease_before_transfer))).body[0]
    assert isinstance(helper, ast.AsyncFunctionDef)
    close = next(
        member for member in owner.body if isinstance(member, ast.AsyncFunctionDef) and member.name == "_close_execution_authority"
    )
    handoff = next(
        node for node in ast.walk(close) if isinstance(node, ast.Call) and _call_name(node) == "close_execute_lease_before_transfer"
    )
    if mutation == "duplicate_handoff":
        close.body.append(ast.Expr(value=ast.Await(value=copy.deepcopy(handoff))))
    elif mutation == "removed_handoff":
        handoff.func = ast.Name(id="unrelated_helper", ctx=ast.Load())
    elif mutation == "wrong_owner":
        handoff.args[0] = ast.Name(id="other_lease", ctx=ast.Load())
    elif mutation == "duplicate_close":
        helper.body.append(ast.parse("lease.close()").body[0])
    elif mutation == "early_return":
        close.body.insert(0, ast.Return(value=None))
    elif mutation == "narrowed_handler":
        join = next(statement for statement in close.body if isinstance(statement, ast.If))
        assert isinstance(join.body[0], ast.Try)
        join.body[0].handlers[0].type = ast.Name(id="Exception", ctx=ast.Load())
    else:
        edge = next(node for node in ast.walk(close) if isinstance(node, ast.Await) and node.value is handoff)
        edge.value = ast.Call(func=ast.Name(id="unrelated_helper", ctx=ast.Load()), args=[], keywords=[])
    ast.fix_missing_locations(owner)
    ast.fix_missing_locations(helper)
    with pytest.raises(AssertionError):
        _assert_async_completion_close_owner(owner, helper)


def test_submit_failure_terminalizes_under_same_context_before_authority_returns() -> None:
    nodes = [_function_node(ExecutionServiceImpl, name) for name in ("execute", "_execute_locked", "_handle_pipeline_submission_failure")]
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


def _assert_shutdown_join_graph(method_sources: dict[str, str] | None = None) -> None:
    import hashlib

    def owner_node(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
        if method_sources is None:
            return _function_node(ExecutionServiceImpl, name)
        tree = ast.parse(textwrap.dedent(method_sources[name]))
        return next(node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)

    node = owner_node("shutdown")
    calls = [call for call in ast.walk(node) if isinstance(call, ast.Call)]
    executor_joins = [call for call in calls if ast.unparse(call.func) == "self._join_executor_owner"]
    registry_joins = [call for call in calls if ast.unparse(call.func) == "self._join_registry_owner"]
    assert len(executor_joins) == len(registry_joins) == 1
    for join in (*executor_joins, *registry_joins):
        assert any(
            isinstance(parent, ast.Call) and ast.unparse(parent.func) == "asyncio.create_task" and join in ast.walk(parent)
            for parent in calls
        )
    assert any(ast.unparse(call.func) == "registry.observe_ready" for call in calls)
    completed_guards = [
        guard
        for guard in ast.walk(node)
        if isinstance(guard, ast.If)
        and isinstance(guard.test, ast.BoolOp)
        and isinstance(guard.test.op, ast.And)
        and {"executor_task in observed", "registry_task in observed"} <= {ast.unparse(term) for term in guard.test.values}
        and any(isinstance(statement, ast.Break) for statement in guard.body)
    ]
    assert len(completed_guards) == 1
    assert sum(isinstance(candidate, ast.Break) for candidate in ast.walk(node)) == 1
    executor_task_owner = owner_node("_join_executor_owner")
    executor_owner_tries = [statement for statement in executor_task_owner.body if isinstance(statement, ast.Try)]
    assert len(executor_owner_tries) == 1
    assert len(executor_owner_tries[0].body) == 1
    executor_edge = executor_owner_tries[0].body[0]
    assert isinstance(executor_edge, ast.Expr)
    assert isinstance(executor_edge.value, ast.Await)
    assert isinstance(executor_edge.value.value, ast.Call)
    assert ast.unparse(executor_edge.value.value.func) == "self._finish_executor_join"
    executor_owner = owner_node("_finish_executor_join")
    finalizer_tries = [statement for statement in executor_owner.body if isinstance(statement, ast.Try)]
    assert len(finalizer_tries) == 1 and len(finalizer_tries[0].body) == 1
    finalizer_edge = finalizer_tries[0].body[0]
    assert isinstance(finalizer_edge, ast.Expr)
    assert isinstance(finalizer_edge.value, ast.Await)
    assert isinstance(finalizer_edge.value.value, ast.Call)
    assert ast.unparse(finalizer_edge.value.value.func) == "run_application_finalizer_in_worker"
    assert [ast.unparse(arg) for arg in finalizer_edge.value.value.args] == ["self._shutdown_finalizer"]
    physical_join = owner_node("join_executor_shutdown")
    assert len(physical_join.body) == 3
    selected_executor = physical_join.body[1]
    assert isinstance(selected_executor, ast.If)
    assert ast.unparse(selected_executor.test) == "executor is not None"
    assert len(selected_executor.body) == 1 and selected_executor.orelse == []
    physical_edge = selected_executor.body[0]
    assert isinstance(physical_edge, ast.Expr) and isinstance(physical_edge.value, ast.Call)
    assert ast.unparse(physical_edge.value.func) == "executor.shutdown"
    assert [(keyword.arg, ast.unparse(keyword.value)) for keyword in physical_edge.value.keywords] == [("wait", "True")]
    registry_owner = owner_node("_join_registry_owner")
    registry_tries = [statement for statement in registry_owner.body if isinstance(statement, ast.Try)]
    assert len(registry_tries) == 1 and len(registry_tries[0].body) == 1
    registry_edge = registry_tries[0].body[0]
    assert isinstance(registry_edge, ast.Expr)
    assert isinstance(registry_edge.value, ast.Await)
    assert isinstance(registry_edge.value.value, ast.Call)
    assert ast.unparse(registry_edge.value.value.func) == "self.execution_lease_release_registry.join_all"

    # The direct edges above give the digest its meaning. Pin these five
    # reviewed producer bodies so an inserted return or conditional cannot
    # make a direct-looking edge unreachable without deliberate re-review.
    reviewed_shapes = {
        "shutdown": "dd521edd9cea565be1bfa2dd235cbd7fee7aff89d46e4b8dea79878c751fa7ae",
        "_join_executor_owner": "e585d33cab53810ae0d3ee97aa27c2ae38e4af5e509ae002a121ed9bf6d851d8",
        "_finish_executor_join": "3bd5602d6adcf3a3ee7b4726bd25f708fcd8a96df58b67292370bf6c35d8b9b1",
        "_join_registry_owner": "7dbbfa9300fe6712c96bdd5ff1d2dc87e29a2c61b03598137226a2ce607b70a3",
        "join_executor_shutdown": "427f99347964a622320f3b27643f7ceeca2469a9d382a6a53058f5dea8d145de",
    }
    assert {name: hashlib.sha256(_stable_ast_bytes(owner_node(name))).hexdigest() for name in reviewed_shapes} == reviewed_shapes


def test_shutdown_drains_executor_then_awaits_all_lease_completions() -> None:
    _assert_shutdown_join_graph()


@pytest.mark.parametrize(
    "method,before,after",
    [
        ("_join_executor_owner", "await self._finish_executor_join()", "pass"),
        (
            "_finish_executor_join",
            "await run_application_finalizer_in_worker(self._shutdown_finalizer)",
            "run_application_finalizer_in_worker(self._shutdown_finalizer)",
        ),
        (
            "_join_registry_owner",
            "await self.execution_lease_release_registry.join_all()",
            "self.execution_lease_release_registry.join_all()",
        ),
        ("shutdown", "and registry_task in observed", "or registry_task in observed"),
        ("join_executor_shutdown", "wait=True", "wait=False"),
    ],
)
def test_shutdown_join_graph_rejects_unjoined_owner_or_early_exit(method: str, before: str, after: str) -> None:
    sources = {
        "shutdown": inspect.getsource(ExecutionServiceImpl.shutdown),
        "_join_executor_owner": inspect.getsource(ExecutionServiceImpl._join_executor_owner),
        "_finish_executor_join": inspect.getsource(ExecutionServiceImpl._finish_executor_join),
        "_join_registry_owner": inspect.getsource(ExecutionServiceImpl._join_registry_owner),
        "join_executor_shutdown": inspect.getsource(ExecutionServiceImpl.join_executor_shutdown),
    }
    assert before in sources[method]
    _assert_shutdown_join_graph(sources)
    sources[method] = sources[method].replace(before, after)
    with pytest.raises(AssertionError):
        _assert_shutdown_join_graph(sources)


@pytest.mark.parametrize(
    "method,required_statement",
    [
        ("_join_executor_owner", "await self._finish_executor_join()"),
        ("_finish_executor_join", "await run_application_finalizer_in_worker(self._shutdown_finalizer)"),
        ("_join_registry_owner", "await self.execution_lease_release_registry.join_all()"),
        ("join_executor_shutdown", "executor.shutdown(wait=True)"),
    ],
)
def test_shutdown_join_graph_rejects_unreachable_owner_edge(method: str, required_statement: str) -> None:
    sources = {
        "shutdown": inspect.getsource(ExecutionServiceImpl.shutdown),
        "_join_executor_owner": inspect.getsource(ExecutionServiceImpl._join_executor_owner),
        "_finish_executor_join": inspect.getsource(ExecutionServiceImpl._finish_executor_join),
        "_join_registry_owner": inspect.getsource(ExecutionServiceImpl._join_registry_owner),
        "join_executor_shutdown": inspect.getsource(ExecutionServiceImpl.join_executor_shutdown),
    }
    _assert_shutdown_join_graph(sources)
    original_lines = [line for line in sources[method].splitlines() if line.strip() == required_statement]
    assert len(original_lines) == 1
    line = original_lines[0]
    indent = line[: len(line) - len(line.lstrip())]
    sources[method] = sources[method].replace(line, indent + "if False:\n" + indent + "    " + required_statement)
    ast.parse(textwrap.dedent(sources[method]))
    with pytest.raises(AssertionError):
        _assert_shutdown_join_graph(sources)


def _real_session_service(
    engine: Engine,
    authority: SQLiteLocalSessionOperationAuthority,
) -> SessionServiceImpl:
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id=_USER_ID)
    return FencedSessionServiceHarness(
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
    compose_context = _acquire(authority, session_id=session.id, kind=SessionOperationKind.COMPOSE)
    try:
        state = await service.save_composition_state(
            session.id,
            CompositionStateData(is_valid=True),
            provenance="session_seed",
            session_operation_context=compose_context,
        )
    finally:
        authority.release(compose_context)
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
    engine: Engine, tmp_path: Path, execution_fixture: ExecutionTestCustody
) -> None:
    """A queued worker may clean local state after takeover, but may publish nothing."""
    authority = SQLiteLocalSessionOperationAuthority(engine)
    real_sessions = _real_session_service(engine, authority)
    session_id, run_id = await _seed_run_through_service(
        real_sessions,
        authority,
        title="queued worker takeover",
    )
    service, _mock_sessions, executor_control = _execution_service(asyncio.get_running_loop(), execution_fixture=execution_fixture)
    blocked_authority = execution_fixture.observe_existing_authority(authority, session_id)
    blocked_authority.renew_allowed.clear()
    lease = await execution_fixture.acquire(
        service.execution_lease_release_registry,
        authority,
        session_id=session_id,
        owner_instance_id="queued-worker-a",
        lease_seconds=1,
        renew_interval_seconds=0.05,
    )
    service._session_service = real_sessions
    broadcaster = _RecordingBroadcaster()
    service._broadcaster = cast(Any, broadcaster)
    worker_release = threading.Event()
    execution_fixture.gates.append(worker_release)
    pool = service._executor
    assert pool is executor_control.pool
    # This actor runs the original production stale-fence worker, unlike the
    # endpoint-only completion controls. Keep the original registered pool and
    # its real submit; release/join the control's initial queued blockers first.
    executor_control.future.queue_allowed.set()
    await asyncio.gather(*(asyncio.to_thread(blocker.result, 5) for blocker in executor_control.blockers))
    pool.submit = executor_control._submit
    pool.shutdown = executor_control._shutdown
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
            assert await asyncio.to_thread(blocked_authority.renew_called.wait, 2)

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
            obligation = lease.execution_obligation
            assert obligation is not None
            for _ in range(200):
                if obligation.retired:
                    break
                await asyncio.sleep(0.01)
            assert obligation.retired, "known lost authority must retire only after its actual cleanup owners join"
            release = obligation.release_submission
            assert release is not None and release.observed and release.callback_return_observed
            assert type(release.original_error) is CanonicalExecutionReleaseLoss
            assert release.original_error is release.domain_refusal
            assert release.original_error.context is lease.context
            assert obligation.release_lost and obligation.release_settled and not obligation.release_succeeded
            canonical_loss = release.original_error
            try:
                canonical_loss.context = _context(uuid4())
                assert not obligation.release_lost, "a receipt for another context must not settle this lease"
            finally:
                canonical_loss.context = lease.context
            actual_future = release.future
            try:
                release.future = cast(Any, obligation.pipeline)
                assert not obligation.release_lost, "a different completed Future cannot reuse the captured receipt"
            finally:
                release.future = actual_future
            actual_generation = obligation.generation
            try:
                obligation.generation = cast(Any, object())
                assert not obligation.release_lost, "a receipt from another generation cannot settle current custody"
            finally:
                obligation.generation = actual_generation
            assert obligation.release_lost
            assert release.reservation is not None
            reservation = release.reservation
            injected = OSError("reservation setup or observation failed")
            try:
                reservation.submission_error = injected
                assert not obligation.release_lost
            finally:
                reservation.submission_error = None
            try:
                reservation.setup_error = injected
                assert not obligation.release_lost
            finally:
                reservation.setup_error = None
            try:
                reservation.setup_outcome_error = injected
                assert not obligation.release_lost
            finally:
                reservation.setup_outcome_error = None
            try:
                reservation.integrity_error = cast(Any, injected)
                assert not obligation.release_lost
            finally:
                reservation.integrity_error = None
            try:
                reservation.semantic_observation_error = injected
                assert not obligation.release_lost
            finally:
                reservation.semantic_observation_error = None
            assert obligation.release_lost
            assert blocked_authority.release_calls == [lease.context]
            assert not service.execution_lease_release_registry.has_pending_physical_owners()
            assert not async_workers._INSTANCE_DRAINING.is_set()
            assert not async_workers._GENERATION_UNAVAILABLE.is_set()
            assert lease.renewal_error is loss
            assert lease.disposition.value == "lost"
            assert obligation.pipeline is not None and obligation.pipeline.exception() is loss
            assert obligation.lifecycle_original_error is None and obligation.completion_original_error is None
            assert service.execution_lease_release_registry._failures == []
            assert successor is not None
            authority.compare_and_swap(successor)
            assert (await real_sessions.get_run(run_id)).status == "pending"
            assert not canonical_output.exists()
            assert broadcaster.events == []
            assert broadcaster.cleaned_runs == [str(run_id)]
    finally:
        worker_release.set()
        blocked_authority.renew_allowed.set()
        primary = sys.exception()
        cleanup_failures: list[BaseException] = []
        try:
            if successor is not None:
                authority.release(successor)
        except BaseException as original:
            cleanup_failures.append(original)
        try:
            await asyncio.to_thread(blocker.result, 5)
        except BaseException as original:
            cleanup_failures.append(original)
        pool_join = asyncio.create_task(asyncio.to_thread(pool.shutdown, True))
        completed, _ = await asyncio.wait({pool_join}, timeout=5)
        if pool_join not in completed:
            cleanup_failures.append(TimeoutError("Real executor join remains physically unresolved after successor release"))
        else:
            try:
                pool_join.result()
            except BaseException as original:
                cleanup_failures.append(original)
        if cleanup_failures:
            if primary is not None:
                raise BaseExceptionGroup("Takeover assertion and physical cleanup failed", [primary, *cleanup_failures]) from None
            if len(cleanup_failures) == 1:
                raise cleanup_failures[0]
            raise BaseExceptionGroup("Takeover physical cleanup failed", cleanup_failures) from None


def test_canonical_execution_release_loss_rejects_unissued_constructor() -> None:
    with pytest.raises(AuditIntegrityError, match="canonical release issuance"):
        CanonicalExecutionReleaseLoss(object(), _context(uuid4()))


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


_ENVELOPE_BLOB_VERIFIER_EDGE = """
    def _entry(self, state, *, session_id, session_operation_context):
        {rebinding}

        def verify_blob(blob_id):
            return self._call_async({receiver}.get_blob(blob_id, session_operation_context={context}))

        return {worker}({consumer}, state{extra_argument}, current_snapshot=state, user_id=None,
            auth_provider_type=None, resolver=None, implementation_fingerprint="implementation",
            deployment_generation="generation", {keyword}=verify_blob{extra_keyword})
"""


def _envelope_blob_verifier_case(
    *,
    worker: str = "run_sync_in_worker",
    consumer: str = "restore_execution_envelope",
    keyword: str = "blob_verifier",
    rebinding: str = "",
    extra_argument: str = "",
    extra_keyword: str = "",
    receiver: str = "self._blob_service",
    context: str = "session_operation_context",
    admitted: bool,
    context_offenders: tuple[str, ...] = (),
    unresolved_receiver_count: int = 0,
) -> _EdgeControlCase:
    return _EdgeControlCase(
        body=_ENVELOPE_BLOB_VERIFIER_EDGE.format(
            worker=worker,
            consumer=consumer,
            keyword=keyword,
            rebinding=rebinding,
            extra_argument=extra_argument,
            extra_keyword=extra_keyword,
            receiver=receiver,
            context=context,
        ),
        closure="verify_blob",
        admitted=admitted,
        context_offenders=context_offenders,
        unresolved_receiver_count=unresolved_receiver_count,
    )


@pytest.mark.parametrize(
    ("case_id", "case"),
    [
        ("exact-shape-admitted", _envelope_blob_verifier_case(admitted=True)),
        ("other-worker-escapes", _envelope_blob_verifier_case(worker="other_worker", admitted=False)),
        ("other-consumer-escapes", _envelope_blob_verifier_case(consumer="other_restore", admitted=False)),
        ("other-keyword-escapes", _envelope_blob_verifier_case(keyword="verify_blob", admitted=False)),
        ("extra-positional-escapes", _envelope_blob_verifier_case(extra_argument=", state", admitted=False)),
        ("expanded-keywords-escape", _envelope_blob_verifier_case(extra_keyword=", **state", admitted=False)),
        ("rebound-worker-escapes", _envelope_blob_verifier_case(rebinding="run_sync_in_worker = self._runner", admitted=False)),
        ("rebound-consumer-escapes", _envelope_blob_verifier_case(rebinding="restore_execution_envelope = self._restore", admitted=False)),
        (
            "other-module-consumer-escapes",
            _envelope_blob_verifier_case(
                rebinding="from elspeth.web.execution.staging import restore_execution_envelope",
                admitted=False,
            ),
        ),
        ("wrong-context-flagged", _envelope_blob_verifier_case(admitted=True, context="other_context", context_offenders=("get_blob",))),
        ("wrong-receiver-flagged", _envelope_blob_verifier_case(admitted=True, receiver="self._staging", unresolved_receiver_count=1)),
    ],
)
def test_envelope_blob_verifier_callback_requires_exact_worker_and_restore_consumer(case_id: str, case: _EdgeControlCase) -> None:
    _assert_edge_control(case)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    ["pure", "pure_no_renewal", "child", "cancel", "renewal_join", "setup_group", "outer_close", "outer_helper"],
)
async def test_canonical_release_loss_retains_mixed_cleanup_originals(
    execution_fixture: ExecutionTestCustody,
    fault: str,
) -> None:
    """Real canonical refusal; controlled mixed producer outcomes remain intact."""
    service, sessions, _executor = _execution_service(asyncio.get_running_loop(), execution_fixture=execution_fixture)
    session_id = sessions.get_current_state.return_value.session_id
    independent = OSError(f"independent {fault} cleanup fault")
    delivered_cancel = asyncio.CancelledError("deferred release cancellation")
    original_renew = SessionOperationLease._renew_forever

    async def renew_then_fault(lease: SessionOperationLease) -> None:
        await original_renew(lease)
        if fault == "renewal_join":
            raise independent

    with patch.object(SessionOperationLease, "_renew_forever", renew_then_fault):
        lease, observed = await _canonical_execute_lease(
            service,
            execution_fixture,
            session_id=session_id,
            renew_interval_seconds=20 if fault == "pure_no_renewal" else 0.01,
        )
    observed.renew_allowed.clear()
    observed.release_allowed.set()
    await asyncio.to_thread(observed.authority.release, lease.context)
    successor = await asyncio.to_thread(
        observed.authority.acquire,
        session_id=session_id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id="mixed-loss-successor",
        lease_seconds=30,
    )
    observed.renew_allowed.set()
    renewal = None if fault == "pure_no_renewal" else await asyncio.wait_for(lease.wait_until_lost(), timeout=2)
    if renewal is not None:
        assert type(renewal) is SessionOperationFenceLost and renewal.reason is FenceLossReason.STALE_EPOCH
    if fault == "child":

        async def fail_child() -> None:
            raise independent

        child = lease.create_task(fail_child())
        with pytest.raises(OSError) as child_error:
            await child
        assert child_error.value is independent

    original_bridge = lifecycle_module.run_execution_lease_sql_finish_once
    returned: list[BaseException] = []

    async def release_then_project(submission):
        outcome = await original_bridge(submission)
        assert type(outcome) is async_workers.RequiredSQLRaised
        returned.append(outcome.error)
        if fault == "setup_group":
            # This is the bridge's setup-after-arm outcome shape, with the
            # actual canonical SQL original retained as a nested member.
            # It does not claim to inject a real executor setup failure.
            grouped = BaseExceptionGroup("setup and actual SQL originals", [independent, outcome.error])
            returned.append(grouped)
            return async_workers.RequiredSQLRaised(grouped, outcome.deferred_cancellations)
        if fault == "cancel":
            return async_workers.RequiredSQLRaised(outcome.error, (delivered_cancel,))
        return outcome

    observed.release_called.clear()
    observed.release_allowed.clear()
    caught: BaseException | None = None
    try:
        with patch.object(lifecycle_module, "run_execution_lease_sql_finish_once", release_then_project):
            closer = execution_service_module.close_execute_lease_before_transfer(lease) if fault == "outer_helper" else lease.close()
            close_task = asyncio.create_task(closer)
            assert await asyncio.to_thread(observed.release_called.wait, 2)
            if fault in ("outer_close", "outer_helper"):
                close_task.cancel("outer close caller cancellation")
                await asyncio.sleep(0)
            observed.release_allowed.set()
            try:
                await close_task
            except BaseException as original:
                caught = original
        obligation = lease.execution_obligation
        assert obligation is not None and obligation.release_lost and not obligation.release_succeeded
        release = obligation.release_submission
        assert release is not None and release.original_error is returned[0]
        canonical = returned[0]
        if fault in ("pure", "pure_no_renewal"):
            assert caught is None and obligation.lifecycle_original_error is None
            assert service.execution_lease_release_registry._failures == []
            if fault == "pure_no_renewal":
                assert lease.renewal_error is None
                assert not observed.renew_called.is_set()
        else:
            assert caught is not None
            leaves = _original_leaves(caught)
            assert sum(original is canonical for original in leaves) == 1
            assert sum(original is renewal for original in leaves) == 1
            if fault in ("child", "renewal_join", "setup_group"):
                assert sum(original is independent for original in leaves) == 1
            if fault == "setup_group":
                assert isinstance(caught, BaseExceptionGroup)
                assert caught.exceptions == (returned[1], renewal)
            elif fault == "cancel":
                assert isinstance(caught, BaseExceptionGroup)
                assert caught.exceptions == (delivered_cancel, canonical, renewal)
            elif fault in ("child", "renewal_join"):
                assert isinstance(caught, BaseExceptionGroup)
                assert caught.exceptions == (independent, canonical, renewal)
            else:
                assert isinstance(caught, BaseExceptionGroup)
                assert isinstance(caught.exceptions[0], asyncio.CancelledError)
                assert caught.exceptions[1:] == (canonical, renewal)
                assert obligation.lifecycle_original_error is None
            for original in leaves:
                if not isinstance(original, asyncio.CancelledError):
                    execution_fixture.witness_cleanup_original(original)
            if fault == "cancel":
                execution_fixture.witness_cleanup_original(delivered_cancel)
        # Lost release never mutates or releases the successor's authority.
        await asyncio.to_thread(observed.authority.compare_and_swap, successor)
    finally:
        observed.release_allowed.set()
        observed.renew_allowed.set()
        await asyncio.to_thread(observed.authority.release, successor)


async def _setup_after_arm_canonical_loss_child(root: Path) -> None:
    from elspeth.web.required_executor import InvocationGate, RequiredInvocationWitness

    custody = ExecutionTestCustody(asyncio.get_running_loop(), root)
    service, sessions, _executor = _execution_service(asyncio.get_running_loop(), execution_fixture=custody)
    session_id = sessions.get_current_state.return_value.session_id
    lease, observed = await _canonical_execute_lease(
        service,
        custody,
        session_id=session_id,
        renew_interval_seconds=0.01,
    )
    observed.renew_allowed.clear()
    observed.release_allowed.set()
    await asyncio.to_thread(observed.authority.release, lease.context)
    successor = await asyncio.to_thread(
        observed.authority.acquire,
        session_id=session_id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id="setup-arm-successor",
        lease_seconds=30,
    )
    observed.renew_allowed.set()
    renewal = await asyncio.wait_for(lease.wait_until_lost(), timeout=2)
    obligation = lease.execution_obligation
    assert obligation is not None
    setup_error = OSError("actual RELEASE setup failed after publishing ARMED")
    original_arm = RequiredInvocationWitness.arm
    armed: list[RequiredInvocationWitness] = []

    def arm_then_fail(witness: RequiredInvocationWitness) -> None:
        original_arm(witness)
        release = obligation.release_submission
        if release is not None and release.reservation is not None and witness is release.reservation.witness:
            assert witness.snapshot().gate is InvocationGate.ARMED
            armed.append(witness)
            raise setup_error

    with patch.object(RequiredInvocationWitness, "arm", arm_then_fail), pytest.raises(BaseExceptionGroup) as caught:
        await lease.close()
    release = obligation.release_submission
    assert release is not None and release.reservation is not None and release.future is not None
    canonical = release.original_error
    assert type(canonical) is CanonicalExecutionReleaseLoss and canonical.context is lease.context
    assert release.domain_refusal is canonical and release.future.exception() is canonical
    assert release.reservation.setup_error is setup_error
    assert armed == [release.reservation.witness]
    trace = release.reservation.witness.snapshot()
    assert trace.exited and trace.callable_finished and not trace.impossible
    assert release.reservation.released and release.callback_return_observed and release.observed
    assert caught.value is obligation.lifecycle_original_error
    assert len(caught.value.exceptions) == 2
    bridge_group, retained_renewal = caught.value.exceptions
    assert isinstance(bridge_group, BaseExceptionGroup)
    assert bridge_group.exceptions == (setup_error, canonical) and retained_renewal is renewal
    assert _original_leaves(caught.value) == [setup_error, canonical, renewal]
    assert not obligation.release_lost and not obligation.release_succeeded and not obligation.retired
    assert service.execution_lease_release_registry.has_pending_physical_owners()
    assert async_workers._INSTANCE_DRAINING.is_set() and async_workers._GENERATION_UNAVAILABLE.is_set()
    for original in (setup_error, canonical, renewal):
        custody.witness_cleanup_original(original)
    await asyncio.to_thread(observed.authority.compare_and_swap, successor)
    await asyncio.to_thread(observed.authority.release, successor)
    shutdown = asyncio.create_task(custody.close())
    await asyncio.sleep(0)
    assert not shutdown.done()
    with pytest.raises(BaseExceptionGroup) as refused:
        service.execution_lease_release_registry.assert_completed()
    assert any(original is setup_error for original in _original_leaves(refused.value))
    assert not custody.recovery.watchdog.completed
    _atomic_child_checkpoint(
        root / "checkpoint.json",
        {
            "actual_armed_setup_and_sql_originals": True,
            "actual_release_future_callback_exited": True,
            "mixed_lifecycle_group_not_flattened": True,
            "faulty_receipt_refused_pending_retained": True,
            "successor_authority_unchanged": True,
            "watchdog_completion_unsent": True,
        },
    )
    # Intentionally unresolved failed setup stays owned until the existing
    # exact child reaper observes termination and physically waits it.
    await asyncio.Event().wait()


def test_actual_setup_after_arm_retains_canonical_loss_group_and_pending_owner(tmp_path: Path) -> None:
    _run_failed_http_release_child(tmp_path, OSError, None, False, setup_after_arm=True)


if __name__ == "__main__":
    failure_types = {
        "OSError": OSError,
        "AuditIntegrityError": AuditIntegrityError,
        "FrameworkBugError": FrameworkBugError,
        "SessionOperationFenceLost": SessionOperationFenceLost,
    }
    if len(sys.argv) == 7 and sys.argv[1] == "--failed-http-release-child" and sys.argv[6] == "--setup-after-arm":
        assert sys.argv[3:6] == ["OSError", "None", "False"]
        child_root = Path(sys.argv[2])
        asyncio.run(_child_with_failure_checkpoint(child_root, _setup_after_arm_canonical_loss_child(child_root)))
    elif len(sys.argv) == 6 and sys.argv[1] == "--failed-http-release-child":
        cleanup_type = failure_types[sys.argv[3]]
        logger_type = None if sys.argv[4] == "None" else failure_types[sys.argv[4]]
        assert sys.argv[5] in {"True", "False"}
        child_root = Path(sys.argv[2])
        asyncio.run(
            _child_with_failure_checkpoint(
                child_root, _failed_http_release_child(child_root, cleanup_type, logger_type, sys.argv[5] == "True")
            )
        )
    elif len(sys.argv) == 5 and sys.argv[1] == "--failed-shutdown-release-child":
        assert sys.argv[3] in {"RuntimeError", "AuditIntegrityError", "FrameworkBugError"}
        assert sys.argv[4] in {"True", "False"}
        shutdown_type = RuntimeError if sys.argv[3] == "RuntimeError" else failure_types[sys.argv[3]]
        child_root = Path(sys.argv[2])
        asyncio.run(
            _child_with_failure_checkpoint(child_root, _failed_shutdown_release_child(child_root, shutdown_type, sys.argv[4] == "True"))
        )
    else:
        raise AssertionError("unknown exact owned execution child")
