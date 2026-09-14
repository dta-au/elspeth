"""Loss watcher poll interval: capped backoff sequence and counter reset.

``ExecutionServiceImpl._signal_shutdown_on_operation_loss`` waits for lease
loss with ``asyncio.wait_for(lease.wait_until_lost(), timeout=poll_seconds)``
and polls the session DB each time that wait times out. These tests replace
that one wait with a fake that records the requested timeout and times out
immediately, so the exact interval sequence is observed without a wall clock:

* the interval doubles per consecutive transient failure and stops at
  ``_LOSS_WATCHER_MAX_BACKOFF_SECONDS``;
* one successful poll resets the failure counter, so the next failure backs
  off from the healthy interval again;
* the exponent itself is capped, so a long outage cannot overflow the float.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Iterator
from datetime import UTC, datetime
from types import CoroutineType, SimpleNamespace
from typing import Any, Literal
from unittest.mock import MagicMock, create_autospec, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import OperationalError

from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.execution.progress import ProgressBroadcaster
from elspeth.web.execution.service import ExecutionServiceImpl
from elspeth.web.sessions.protocol import SessionServiceProtocol
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.helpers.session_fences import adopt_execute_lease, close_adopted_lease
from tests.unit.web.execution.test_service import _run_record_stub, _WebSettingsStub, _YamlGeneratorStub

Outcome = Literal["transient", "running", "cancelled"]
TRANSIENT: Outcome = "transient"
RUNNING: Outcome = "running"
CANCELLED: Outcome = "cancelled"

# Hard stop for a watcher that never returns: the fake raises instead of
# letting it spin, so a broken watcher fails promptly and deterministically.
_MAX_WAITS = 2000


@pytest.fixture
def real_loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


@pytest.fixture
def session_service() -> MagicMock:
    mock: MagicMock = create_autospec(SessionServiceProtocol, instance=True)
    return mock


@pytest.fixture
def service(session_service: MagicMock) -> ExecutionServiceImpl:
    mock_loop = MagicMock(spec=asyncio.AbstractEventLoop)
    return ExecutionServiceImpl.for_trained_operator(
        loop=mock_loop,
        broadcaster=ProgressBroadcaster(mock_loop),
        settings=_WebSettingsStub(),
        session_service=session_service,
        yaml_generator=_YamlGeneratorStub(),
        telemetry=build_sessions_telemetry(),
    )


@pytest.fixture
def lease(real_loop: asyncio.AbstractEventLoop) -> Iterator[SessionOperationLease]:
    adopted = adopt_execute_lease(real_loop, uuid4())
    try:
        yield adopted
    finally:
        if not adopted.closed:
            close_adopted_lease(real_loop, adopted)


def _watch_and_record_poll_intervals(
    service: ExecutionServiceImpl,
    session_service: MagicMock,
    real_loop: asyncio.AbstractEventLoop,
    lease: SessionOperationLease,
    outcomes: list[Outcome],
    monkeypatch: pytest.MonkeyPatch,
) -> list[float]:
    """Run the watcher over scripted poll outcomes; return every requested wait timeout."""
    run_id = uuid4()
    shutdown_event = threading.Event()
    timeouts: list[float] = []
    real_wait_for = asyncio.wait_for

    async def fake_wait_for(awaitable: Awaitable[Any], timeout: float | None) -> Any:
        if not (isinstance(awaitable, CoroutineType) and awaitable.cr_code is SessionOperationLease.wait_until_lost.__code__):
            return await real_wait_for(awaitable, timeout)
        awaitable.close()
        if timeout is None:
            raise AssertionError("the loss watcher must always bound its lease-loss wait")
        timeouts.append(timeout)
        if len(timeouts) > _MAX_WAITS:
            raise AssertionError("loss watcher kept waiting past the scripted outcomes")
        raise TimeoutError

    remaining = list(outcomes)

    async def get_run(requested: UUID) -> SimpleNamespace:
        assert requested == run_id
        outcome = remaining.pop(0)
        if outcome == "transient":
            raise OperationalError("SELECT runs", {}, Exception("database is locked"))
        cancel_requested_at = datetime.now(UTC) if outcome == "cancelled" else None
        return _run_record_stub(id=run_id, status="running", cancel_requested_at=cancel_requested_at)

    session_service.get_run.side_effect = get_run
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

    with patch("elspeth.web.execution.service.slog"):
        real_loop.run_until_complete(service._signal_shutdown_on_operation_loss(lease, shutdown_event, run_id=run_id))

    assert remaining == [], "the watcher returned before consuming every scripted poll"
    assert shutdown_event.is_set()
    return timeouts


def test_consecutive_transient_failures_double_the_interval_up_to_the_cap(
    service: ExecutionServiceImpl,
    session_service: MagicMock,
    real_loop: asyncio.AbstractEventLoop,
    lease: SessionOperationLease,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = [TRANSIENT] * 7 + [CANCELLED]

    timeouts = _watch_and_record_poll_intervals(service, session_service, real_loop, lease, outcomes, monkeypatch)

    # Healthy 0.25s, then doubling per consecutive failure, capped at 5.0s.
    assert timeouts == [0.25, 0.5, 1.0, 2.0, 4.0, 5.0, 5.0, 5.0]


def test_a_successful_poll_resets_the_backoff_to_the_healthy_interval(
    service: ExecutionServiceImpl,
    session_service: MagicMock,
    real_loop: asyncio.AbstractEventLoop,
    lease: SessionOperationLease,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = [TRANSIENT, TRANSIENT, TRANSIENT, RUNNING, TRANSIENT, RUNNING, CANCELLED]

    timeouts = _watch_and_record_poll_intervals(service, session_service, real_loop, lease, outcomes, monkeypatch)

    # After the successful poll the counter is zero again: the next wait is the
    # healthy interval and the next failure backs off from 0.5s, not 4.0s.
    assert timeouts == [0.25, 0.5, 1.0, 2.0, 0.25, 0.5, 0.25]


def test_a_long_outage_stays_at_the_cap_without_overflowing(
    service: ExecutionServiceImpl,
    session_service: MagicMock,
    real_loop: asyncio.AbstractEventLoop,
    lease: SessionOperationLease,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 0.25 * 2**1024 does not fit a float; the capped exponent keeps the
    # interval computable for any outage length.
    failures = 1100
    outcomes = [TRANSIENT] * failures + [CANCELLED]

    timeouts = _watch_and_record_poll_intervals(service, session_service, real_loop, lease, outcomes, monkeypatch)

    assert len(timeouts) == failures + 1
    assert timeouts[:6] == [0.25, 0.5, 1.0, 2.0, 4.0, 5.0]
    assert set(timeouts[5:]) == {5.0}
