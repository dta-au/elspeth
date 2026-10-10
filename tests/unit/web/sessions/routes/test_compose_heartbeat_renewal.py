"""Detached worker lifecycle: metrics, lease renewal and durable observation.

Legacy test identifiers remain stable; scopes are worker-owned, and HTTP
admission/GET never owns provider cancellation. Scripted clocks control retry
headroom; real-file SQLite cases exercise202→worker→GET and explicit Stop.
"""

from __future__ import annotations

import asyncio
import gc
import inspect
from collections import deque
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyPoolTimeoutError

from elspeth.web.composer.progress import ComposerRequestLease
from elspeth.web.coordination.composer_progress_authority import ComposerRequestLeaseLost, SessionComposerProgressAuthority
from elspeth.web.sessions.routes import _helpers

_INSTANT_FAILURES_TO_CANCEL = 3
"""Instant transient failures, at 15 s, 30 s and 45 s after admission, that cancel.

At a 60 s lease and a 15 s interval the third failure leaves no room for
another attempt inside the lease. ``test_headroom_matches_lease_arithmetic``
binds it to the production constants.
"""


def _operational_error() -> OperationalError:
    return OperationalError("UPDATE composer_inflight_requests", {}, Exception("server closed the connection unexpectedly"))


@dataclass
class _FakeHeartbeatClock:
    """Monotonic clock the heartbeat reads; its wait advances time instead of sleeping."""

    now_seconds: float = 0.0

    def now(self) -> float:
        return self.now_seconds

    async def wait(self, seconds: float) -> None:
        self.now_seconds += seconds
        await asyncio.sleep(0)


@dataclass
class _ScriptedRegistry:
    """Registry whose ``renew_request`` replays a failure script, then succeeds.

    Each renewal advances the fake clock by ``call_seconds`` before it
    resolves: how long the call takes to succeed or fail. The default models
    an instant renewal.
    """

    script: deque[BaseException | None]
    renew_calls: int = 0
    events: list[str] = field(default_factory=list)
    renewed: asyncio.Event = field(default_factory=asyncio.Event)
    succeed_after: int = 0
    clock: _FakeHeartbeatClock = field(default_factory=_FakeHeartbeatClock)
    call_seconds: float = 0.0

    async def start_request(self, session_id: str, user_id: str) -> ComposerRequestLease:
        self.events.append("begin")
        return ComposerRequestLease(str(uuid4()), session_id, user_id)

    async def renew_request(self, lease: ComposerRequestLease) -> None:
        del lease
        self.clock.now_seconds += self.call_seconds
        self.renew_calls += 1
        outcome = self.script.popleft() if self.script else None
        if outcome is not None:
            self.events.append(f"renew_failed:{type(outcome).__name__}")
            raise outcome
        self.events.append("renew_ok")
        if self.renew_calls >= self.succeed_after:
            self.renewed.set()

    async def finish_request(self, lease: ComposerRequestLease) -> None:
        del lease
        self.events.append("end")


@dataclass
class _RouteOutcome:
    heartbeat_cancel: object | None = None
    cancelled_seen: bool = False
    dependency_error: BaseException | None = None
    completed: bool = False
    cancelling_after_teardown: int = -1


def _install(monkeypatch: pytest.MonkeyPatch, registry: _ScriptedRegistry) -> list[tuple[str, BaseException | None]]:
    # The production interval and lease stand; only the clock and the wait
    # between renewals are fake, so no test sleeps for real.
    monkeypatch.setattr(
        _helpers,
        "_COMPOSER_HEARTBEAT_TIMER",
        _helpers._ComposerHeartbeatTimer(now=registry.clock.now, wait=registry.clock.wait),
    )
    finished: list[tuple[str, BaseException | None]] = []
    monkeypatch.setattr(_helpers, "begin_composer_request_metrics", lambda *, surface: object())

    def finish_metrics(token: object, *, status: str, primary_error: BaseException | None) -> None:
        del token
        finished.append((status, primary_error))

    monkeypatch.setattr(_helpers, "finish_composer_request_metrics", finish_metrics)
    return finished


async def _run_fake_route(*, registry, until: asyncio.Event, extra_cancel: bool = False) -> _RouteOutcome:
    """Play the route: enter the dependency, park on ``until``, settle teardown."""
    outcome = _RouteOutcome()

    async def route() -> None:
        dependency = cast(
            "AsyncGenerator[None, None]",
            _owned_lifecycle(registry),
        )
        await anext(dependency)
        try:
            await until.wait()
        except asyncio.CancelledError as exc:
            outcome.cancelled_seen = True
            outcome.heartbeat_cancel = exc.args[0] if len(exc.args) == 1 else None
            task = asyncio.current_task()
            assert task is not None
            if extra_cancel:
                # An external cancel (server shutdown) races the heartbeat's.
                task.cancel()
            try:
                await dependency.athrow(exc)
            except BaseException as teardown_exc:
                outcome.dependency_error = teardown_exc
            outcome.cancelling_after_teardown = task.cancelling()
            return
        try:
            await anext(dependency)
        except StopAsyncIteration:
            outcome.completed = True

    child = asyncio.create_task(route())
    await asyncio.wait({child})
    if extra_cancel:
        # The external cancel is still pending when the fake route returns,
        # so the task legitimately ends cancelled.
        assert child.cancelled()
        return outcome
    # The fake route swallows everything it observes into ``outcome``; a
    # child that itself ended cancelled or raised means teardown leaked.
    assert not child.cancelled()
    assert child.exception() is None
    return outcome


@pytest.mark.asyncio
@pytest.mark.parametrize("transient", [_operational_error, lambda: SQLAlchemyPoolTimeoutError("QueuePool limit reached")])
async def test_one_transient_renew_failure_then_success_keeps_the_turn_alive(monkeypatch: pytest.MonkeyPatch, transient: Any) -> None:
    registry = _ScriptedRegistry(script=deque([transient(), None]), succeed_after=2)
    finished = _install(monkeypatch, registry)

    outcome = await _run_fake_route(registry=registry, until=registry.renewed)

    assert outcome.completed is True
    assert outcome.cancelled_seen is False
    assert registry.events[:3] == ["begin", f"renew_failed:{type(transient()).__name__}", "renew_ok"]
    assert registry.events[-1] == "end"
    assert finished == [("completed", None)]


@pytest.mark.asyncio
async def test_transient_failures_within_headroom_are_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failures below the lease-headroom bound retry; the next success resets the count."""
    bound = _INSTANT_FAILURES_TO_CANCEL
    script: deque[BaseException | None] = deque([*[_operational_error() for _ in range(bound - 1)], None])
    script.extend([_operational_error() for _ in range(bound - 1)])
    script.append(None)
    registry = _ScriptedRegistry(script=script, succeed_after=2 * bound)
    finished = _install(monkeypatch, registry)

    outcome = await _run_fake_route(registry=registry, until=registry.renewed)

    assert outcome.completed is True
    assert outcome.cancelled_seen is False
    assert finished == [("completed", None)]


def test_headroom_matches_lease_arithmetic() -> None:
    """60 s lease / 15 s interval - 1: the third straight instant failure cancels."""
    # web/app.py builds SessionComposerProgressAuthority without lease_seconds,
    # so the lease the heartbeat renews is that constructor's default. Bind the
    # copy to the authority so a changed default reddens this test.
    authority_default = inspect.signature(SessionComposerProgressAuthority.__init__).parameters["lease_seconds"].default
    assert authority_default == _helpers._COMPOSER_REQUEST_LEASE_SECONDS
    assert _helpers._COMPOSER_HEARTBEAT_SECONDS == 15.0
    assert _helpers._COMPOSER_REQUEST_LEASE_SECONDS == 60
    expected_bound = int(_helpers._COMPOSER_REQUEST_LEASE_SECONDS // _helpers._COMPOSER_HEARTBEAT_SECONDS) - 1
    assert expected_bound == _INSTANT_FAILURES_TO_CANCEL


@pytest.mark.asyncio
async def test_repeated_transient_failures_past_headroom_cancel_as_server_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    bound = _INSTANT_FAILURES_TO_CANCEL
    registry = _ScriptedRegistry(script=deque(_operational_error() for _ in range(bound + 5)))
    finished = _install(monkeypatch, registry)

    outcome = await _run_fake_route(registry=registry, until=asyncio.Event())

    assert outcome.cancelled_seen is True
    assert registry.renew_calls == bound
    cancel = _helpers._composer_heartbeat_cancel_of(asyncio.CancelledError(outcome.heartbeat_cancel))
    assert cancel is not None
    assert cancel.kind == "transient_exhausted"
    # Teardown surfaces a structured 503, never the raw database error.
    error = outcome.dependency_error
    assert isinstance(error, HTTPException)
    assert error.status_code == 503
    assert cast("object", error.detail) == {
        "error_type": "database_unavailable",
        "detail": "Database is currently unavailable. Please retry in a moment.",
    }
    assert not isinstance(error.__cause__, OperationalError)
    assert outcome.cancelling_after_teardown == 0
    assert [status for status, _ in finished] == ["failed"]
    assert registry.events[-1] == "end"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [ComposerRequestLeaseLost("Composer request lease cannot be renewed"), PermissionError("Composer progress identity is inactive")],
)
async def test_lease_lost_cancels_immediately_as_server_fault(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    # Keep failing: a heartbeat that wrongly retried a lost lease must still
    # end (at the transient bound) so this test goes red instead of hanging.
    bound = _INSTANT_FAILURES_TO_CANCEL
    registry = _ScriptedRegistry(script=deque([failure] * (bound + 5)))
    finished = _install(monkeypatch, registry)

    outcome = await _run_fake_route(registry=registry, until=asyncio.Event())

    assert registry.renew_calls == 1
    cancel = _helpers._composer_heartbeat_cancel_of(asyncio.CancelledError(outcome.heartbeat_cancel))
    assert cancel is not None
    assert cancel.kind == "lease_lost"
    error = outcome.dependency_error
    assert isinstance(error, HTTPException)
    assert error.status_code == 503
    assert cast("object", error.detail) == {
        "error_type": "composer_request_lease_lost",
        "detail": "The server lost this composer request's lease before it finished. Please resubmit.",
    }
    assert outcome.cancelling_after_teardown == 0
    assert [status for status, _ in finished] == ["failed"]


@pytest.mark.asyncio
async def test_renewal_defect_cancels_immediately_and_surfaces_the_defect(monkeypatch: pytest.MonkeyPatch) -> None:
    defect = RuntimeError("Composer request lease mismatch")
    registry = _ScriptedRegistry(script=deque([defect]))
    finished = _install(monkeypatch, registry)

    outcome = await _run_fake_route(registry=registry, until=asyncio.Event())

    assert registry.renew_calls == 1
    cancel = _helpers._composer_heartbeat_cancel_of(asyncio.CancelledError(outcome.heartbeat_cancel))
    assert cancel is not None
    assert cancel.kind == "renewal_defect"
    assert outcome.dependency_error is defect
    assert outcome.cancelling_after_teardown == 0
    assert finished == [("failed", defect)]


@pytest.mark.asyncio
async def test_external_cancel_racing_the_heartbeat_keeps_unwinding_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep failing so a heartbeat that wrongly retried a lost lease still ends.
    bound = _INSTANT_FAILURES_TO_CANCEL
    registry = _ScriptedRegistry(script=deque([ComposerRequestLeaseLost("gone")] * (bound + 5)))
    finished = _install(monkeypatch, registry)

    outcome = await _run_fake_route(registry=registry, until=asyncio.Event(), extra_cancel=True)

    assert isinstance(outcome.dependency_error, asyncio.CancelledError)
    assert [status for status, _ in finished] == ["cancelled"]


def test_plain_cancelled_error_is_not_a_heartbeat_cancel() -> None:
    assert _helpers._composer_heartbeat_cancel_of(asyncio.CancelledError()) is None
    assert _helpers._composer_heartbeat_cancel_of(asyncio.CancelledError("shutdown")) is None


@pytest.mark.asyncio
async def test_dependency_teardown_503_reaches_the_http_client(tmp_path, monkeypatch):
    await _durable_heartbeat(tmp_path, monkeypatch)


@pytest.mark.asyncio
async def test_send_message_heartbeat_cancel_publishes_server_fault_not_client_cancelled(tmp_path, monkeypatch):
    await _durable_heartbeat(tmp_path, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize(("call_seconds", "expected_calls"), [(0.0, 3), (10.0, 2), (30.0, 1)])
async def test_slow_failing_renewals_cancel_before_the_lease_lapses(
    monkeypatch: pytest.MonkeyPatch, call_seconds: float, expected_calls: int
) -> None:
    """Retry headroom is lease time, not a failure count.

    A pool checkout that times out takes 30 s (``QueuePool``'s default) to
    fail. Counting failures let three of them run 135 s past the last good
    renewal, 75 s past the 60 s lease, while the turn's progress writes were
    refused for want of a live lease. Each renewal here is admission at 0 plus
    the 15 s interval plus ``call_seconds`` to fail.
    """
    registry = _ScriptedRegistry(
        script=deque(SQLAlchemyPoolTimeoutError("QueuePool limit reached") for _ in range(10)),
        call_seconds=call_seconds,
    )
    finished = _install(monkeypatch, registry)

    outcome = await _run_fake_route(registry=registry, until=asyncio.Event())

    assert registry.renew_calls == expected_calls
    # Admission is the last good renewal (t=0): the cancel lands with lease left.
    assert registry.clock.now_seconds < _helpers._COMPOSER_REQUEST_LEASE_SECONDS
    cancel = _helpers._composer_heartbeat_cancel_of(asyncio.CancelledError(outcome.heartbeat_cancel))
    assert cancel is not None
    assert cancel.kind == "transient_exhausted"
    error = outcome.dependency_error
    assert isinstance(error, HTTPException)
    assert error.status_code == 503
    assert cast("object", error.detail) == {
        "error_type": "database_unavailable",
        "detail": "Database is currently unavailable. Please retry in a moment.",
    }
    assert [status for status, _ in finished] == ["failed"]
    assert registry.events[-1] == "end"


@dataclass
class _HangingRegistry(_ScriptedRegistry):
    """Registry whose ``renew_request`` never resolves unless it is cancelled."""

    abandoned: bool = False

    async def renew_request(self, lease: ComposerRequestLease) -> None:
        del lease
        self.renew_calls += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.abandoned = True
            raise


@pytest.mark.asyncio
async def test_hung_renewal_is_abandoned_when_the_lease_runs_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """A renewal that never resolves is bounded by the lease the request still holds.

    The lease is patched to leave 0.05 s of real event-loop time after the
    first interval. The outer timeout only turns a hang into a red test;
    nothing here asserts a duration.
    """
    registry = _HangingRegistry(script=deque())
    finished = _install(monkeypatch, registry)
    monkeypatch.setattr(_helpers, "_COMPOSER_REQUEST_LEASE_SECONDS", _helpers._COMPOSER_HEARTBEAT_SECONDS + 0.05)

    async with asyncio.timeout(10):
        outcome = await _run_fake_route(registry=registry, until=asyncio.Event())

    assert registry.renew_calls == 1
    assert registry.abandoned is True
    cancel = _helpers._composer_heartbeat_cancel_of(asyncio.CancelledError(outcome.heartbeat_cancel))
    assert cancel is not None
    assert cancel.kind == "transient_exhausted"
    error = outcome.dependency_error
    assert isinstance(error, HTTPException)
    assert error.status_code == 503
    assert cast("object", error.detail) == {
        "error_type": "database_unavailable",
        "detail": "Database is currently unavailable. Please retry in a moment.",
    }
    assert [status for status, _ in finished] == ["failed"]
    assert registry.events[-1] == "end"


@pytest.mark.asyncio
async def test_timeout_error_raised_by_the_renewal_itself_stays_a_renewal_defect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the heartbeat's own lease deadline counts as exhausted headroom."""
    defect = TimeoutError("raised inside the renewal, not by the heartbeat's deadline")
    registry = _ScriptedRegistry(script=deque([defect] * 10))
    finished = _install(monkeypatch, registry)

    outcome = await _run_fake_route(registry=registry, until=asyncio.Event())

    assert registry.renew_calls == 1
    cancel = _helpers._composer_heartbeat_cancel_of(asyncio.CancelledError(outcome.heartbeat_cancel))
    assert cancel is not None
    assert cancel.kind == "renewal_defect"
    assert outcome.dependency_error is defect
    assert finished == [("failed", defect)]


async def _owned_lifecycle(registry):
    """Drive the owned worker scope without an HTTP dependency or Request."""
    owner = asyncio.current_task()
    assert owner is not None
    async with _helpers.composer_request_lifecycle(
        registry,
        session_id=str(uuid4()),
        user_id="user",
        owner_task=owner,
    ):
        yield None


async def _durable_heartbeat(tmp_path, monkeypatch, *, recompose=False, explicit_stop=False):
    from tests.helpers.composer_operations import build_composer_operation_app, install_composer_async_worker, message_body, recompose_body

    harness = await build_composer_operation_app(tmp_path)
    app = harness.app
    install_composer_async_worker(app)
    registry = app.state.composer_progress_registry
    original_renew = registry.renew_request

    async def renew(lease):
        if harness.composer.entered.is_set() and not explicit_stop:
            raise ComposerRequestLeaseLost("lost exact worker progress lease")
        await original_renew(lease)

    async def wait(seconds):
        while not harness.composer.entered.is_set():
            await asyncio.sleep(0)
        if explicit_stop:
            await asyncio.Event().wait()

    observed_metrics = []
    monkeypatch.setattr(_helpers, "begin_composer_request_metrics", lambda *, surface: object())

    def finish_metrics(token, *, status, primary_error):
        observed_metrics.append((status, primary_error))

    monkeypatch.setattr(_helpers, "finish_composer_request_metrics", finish_metrics)
    monkeypatch.setattr(registry, "renew_request", renew)
    monkeypatch.setattr(_helpers, "_COMPOSER_HEARTBEAT_TIMER", _helpers._ComposerHeartbeatTimer(now=lambda: 0.0, wait=wait))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {harness.token}"}
    ) as client:
        route = "messages"
        body = message_body("One detached provider call")
        if recompose:
            from tests.helpers.session_fences import acquire_compose_context

            async with acquire_compose_context(app.state.session_service, harness.session_id) as context:
                user = await app.state.session_service.add_message(
                    harness.session_id,
                    "user",
                    "Retry the same user",
                    writer_principal="route_user_message",
                    session_operation_context=context,
                )
            route = "recompose"
            body = recompose_body(user.id)
        accepted = await client.post(f"/api/sessions/{harness.session_id}/{route}", json=body)
        assert accepted.status_code == 202, accepted.text
        assert harness.composer.calls == 0
        poll = f"/api/sessions/{harness.session_id}/operations/{body['operation_id']}"
        assert (await client.get(poll)).json()["status"] == "queued"
        drive = asyncio.create_task(app.state.composer_async_worker.run_until_idle())
        try:
            async with asyncio.timeout(10):
                while not harness.composer.entered.is_set():
                    await asyncio.sleep(0.001)
                if explicit_stop:
                    from elspeth.web.sessions.routes.composer import operations

                    original_ownership = operations._verify_session_ownership
                    observer_entered = asyncio.Event()

                    async def held_observation(*args, **kwargs):
                        await original_ownership(*args, **kwargs)
                        observer_entered.set()
                        await asyncio.Event().wait()

                    with monkeypatch.context() as observer_patch:
                        observer_patch.setattr(operations, "_verify_session_ownership", held_observation)
                        observer = asyncio.create_task(client.get(poll))
                        await observer_entered.wait()
                        observer.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await observer
                    assert not drive.done()
                    running = await client.get(poll)
                    assert running.json()["status"] == "running"
                    assert running.json()["cancel_requested"] is False
                    assert harness.composer.calls == 1
                    cancelled = await client.post(f"{poll}/cancel")
                    assert cancelled.status_code == 202, cancelled.text
                await drive
            final = await client.get(poll)
            assert final.status_code == 200, final.text
            snapshot = final.json()
            assert snapshot["status"] == "failed"
            assert snapshot["error"]["failure_code"] == ("request_cancelled" if explicit_stop else "worker_lost")
            assert snapshot["error"]["http_status"] == (499 if explicit_stop else 503)
            assert harness.composer.calls == 1
            again = await client.get(poll)
            assert again.json() == snapshot
            assert harness.composer.calls == 1
            progress = await registry.get_latest(str(harness.session_id))
            assert progress.inflight_requests == 0
            assert len(observed_metrics) == 1
            assert observed_metrics[0][0] == ("cancelled" if explicit_stop else "failed")
            assert progress.reason == ("client_cancelled" if explicit_stop else "service_setup_failed")
        finally:
            if not drive.done():
                await client.post(f"{poll}/cancel")
                await drive


@pytest_asyncio.fixture(autouse=True)
async def _assert_joined_lifecycle_children():
    """A passing HTTP result must also retrieve every failed owned child."""
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    unobserved = []
    loop.set_exception_handler(lambda active_loop, context: unobserved.append(context))
    try:
        yield
        gc.collect()
        await asyncio.sleep(0)
        assert unobserved == [], unobserved
    finally:
        loop.set_exception_handler(previous)
