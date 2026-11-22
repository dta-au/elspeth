"""The pipeline thread's bridge to the event loop never gives up on work that is still running.

``ExecutionServiceImpl._call_async`` used to wait 30 seconds and then raise,
leaving the scheduled coroutine running. The pipeline thread then recorded
the run as failed while the abandoned call could still write its own outcome.
These tests drive the real bridge against a real loop in a second thread.
Every wait is cut to 50 ms so "the wait ran out" happens without a stall.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Coroutine, Iterator
from concurrent.futures import Future
from typing import Any
from unittest.mock import MagicMock

import pytest

from elspeth.web.execution import service as service_module
from elspeth.web.execution.progress import ProgressBroadcaster
from elspeth.web.execution.service import ExecutionServiceImpl
from elspeth.web.sessions.telemetry import _FakeCounter, build_sessions_telemetry
from tests.helpers.execution_custody import ExecutionTestCustody
from tests.unit.web.execution import test_service as _service_tests
from tests.unit.web.execution.test_service import _YamlGeneratorStub

broadcaster = _service_tests.broadcaster
execution_fixture = _service_tests.execution_fixture
mock_loop = _service_tests.mock_loop
mock_session_service = _service_tests.mock_session_service
mock_settings = _service_tests.mock_settings

_WAIT_SECONDS = 0.05
_BOUND_SECONDS = 10.0


class _ImpatientFuture:
    """The bridge's future with every bounded wait cut to 50 ms. ``waits`` counts the waits that ran out."""

    def __init__(self, real: Future[Any], waits: list[float]) -> None:
        self._real = real
        self._waits = waits

    def result(self, timeout: float | None = None) -> Any:
        if timeout is None:
            return self._real.result()
        try:
            return self._real.result(_WAIT_SECONDS)
        except TimeoutError:
            if not self._real.done():
                self._waits.append(timeout)
            raise

    def done(self) -> bool:
        return self._real.done()

    def cancel(self) -> bool:
        return self._real.cancel()


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Cut every bridge wait short and return the list of waits that ran out."""
    ran_out: list[float] = []
    real = asyncio.run_coroutine_threadsafe

    def impatient(coro: Coroutine[Any, Any, Any], loop: asyncio.AbstractEventLoop) -> Any:
        return _ImpatientFuture(real(coro, loop), ran_out)

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", impatient)
    return ran_out


@pytest.fixture
def bridge_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """A real event loop running in its own thread, as the server's loop does for a pipeline thread."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="bridge-test-loop", daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        thread.join(_BOUND_SECONDS)
        # A test that stopped the loop under a held call leaves that call's task pending.
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.wait(pending))
        loop.close()


@pytest.fixture
def bridged_service(
    bridge_loop: asyncio.AbstractEventLoop,
    broadcaster: ProgressBroadcaster,
    mock_settings: MagicMock,
    mock_session_service: MagicMock,
    execution_fixture: ExecutionTestCustody,
) -> ExecutionServiceImpl:
    return execution_fixture.bind(
        ExecutionServiceImpl.for_trained_operator(
            loop=bridge_loop,
            broadcaster=broadcaster,
            settings=mock_settings,
            session_service=mock_session_service,
            yaml_generator=_YamlGeneratorStub(),
            telemetry=build_sessions_telemetry(),
            execution_lease_release_registry=execution_fixture.registry(execution_fixture.loop),
        )
    )


class _Caller:
    """One ``_call_async`` made from its own thread, as the pipeline thread makes it."""

    def __init__(self, service: ExecutionServiceImpl, coro: Coroutine[Any, Any, Any], *, run_id: str = "") -> None:
        self.outcome: tuple[str, Any] | None = None
        self.finished = threading.Event()

        def call() -> None:
            if run_id:
                service_module._BRIDGED_RUN_ID.set(run_id)
            try:
                self.outcome = ("returned", service._call_async(coro))
            except BaseException as exc:
                self.outcome = ("raised", exc)
            finally:
                self.finished.set()

        self._thread = threading.Thread(target=call, name="bridge-test-caller", daemon=True)
        self._thread.start()

    def join(self) -> tuple[str, Any]:
        self._thread.join(_BOUND_SECONDS)
        assert self.outcome is not None, "the bridged call never returned to its caller"
        return self.outcome


class _HeldCall:
    """A coroutine that starts, waits to be released, then writes its effect."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._release = asyncio.Event()
        self.started = threading.Event()
        self.ended = threading.Event()
        self.effects: list[str] = []

    async def write_terminal_status(self) -> str:
        self.started.set()
        try:
            await self._release.wait()
            self.effects.append("status written")
            return "written"
        finally:
            self.ended.set()

    def release(self) -> None:
        self._loop.call_soon_threadsafe(self._release.set)


def _until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + _BOUND_SECONDS
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.005)


def test_a_call_still_running_when_the_wait_runs_out_is_not_abandoned(
    bridged_service: ExecutionServiceImpl, bridge_loop: asyncio.AbstractEventLoop, waits: list[float]
) -> None:
    held = _HeldCall(bridge_loop)
    caller = _Caller(bridged_service, held.write_terminal_status())
    assert held.started.wait(_BOUND_SECONDS)

    # Let the wait run out three times, or let the caller give up, whichever comes first.
    _until(lambda: len(waits) >= 3 or caller.finished.is_set())
    gave_up_while_running = caller.finished.is_set()
    held.release()
    kind, value = caller.join()
    assert held.ended.wait(_BOUND_SECONDS)

    # The call did write. So the caller must have been told so, and not before it happened.
    assert held.effects == ["status written"]
    assert not gave_up_while_running, f"the caller got {value!r} while its call was still running, and the call wrote afterwards"
    assert (kind, value) == ("returned", "written")


def test_a_stopped_loop_ends_the_wait_with_an_error_no_handler_treats_as_transient(
    bridged_service: ExecutionServiceImpl, bridge_loop: asyncio.AbstractEventLoop, waits: list[float]
) -> None:
    held = _HeldCall(bridge_loop)
    caller = _Caller(bridged_service, held.write_terminal_status())
    assert held.started.wait(_BOUND_SECONDS)
    bridge_loop.call_soon_threadsafe(bridge_loop.stop)

    kind, error = caller.join()

    assert kind == "raised"
    assert type(error) is service_module.ExecutionLoopUnavailableError
    # The failure-path handlers catch OSError (TimeoutError included) as a transient store fault.
    assert not isinstance(error, OSError)
    assert "write_terminal_status" in str(error)
    assert held.effects == []


def test_a_timeout_raised_by_the_call_itself_reaches_the_caller(bridged_service: ExecutionServiceImpl, waits: list[float]) -> None:
    pool_exhausted = TimeoutError("connection pool exhausted")

    async def refused() -> None:
        raise pool_exhausted

    kind, error = _Caller(bridged_service, refused()).join()

    assert (kind, error) == ("raised", pool_exhausted)
    assert waits == []


def test_a_slow_call_is_counted_once_with_its_run_and_operation(
    bridged_service: ExecutionServiceImpl,
    bridge_loop: asyncio.AbstractEventLoop,
    waits: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_module, "_LOOP_BRIDGE_SLOW_SECONDS", 0.0)
    held = _HeldCall(bridge_loop)
    caller = _Caller(bridged_service, held.write_terminal_status(), run_id="run-slow")
    assert held.started.wait(_BOUND_SECONDS)

    _until(lambda: len(waits) >= 4 or caller.finished.is_set())
    held.release()

    assert caller.join() == ("returned", "written")
    counter = bridged_service._telemetry.execution_loop_bridge_slow_total
    assert type(counter) is _FakeCounter
    assert [(amount, attributes) for amount, attributes, _context in counter.calls] == [
        (1, {"run_id": "run-slow", "operation": "_HeldCall.write_terminal_status"})
    ]


def test_a_call_that_finishes_in_time_is_not_counted(bridged_service: ExecutionServiceImpl) -> None:
    async def quick() -> int:
        return 7

    assert _Caller(bridged_service, quick()).join() == ("returned", 7)
    counter = bridged_service._telemetry.execution_loop_bridge_slow_total
    assert type(counter) is _FakeCounter
    assert counter.calls == []
