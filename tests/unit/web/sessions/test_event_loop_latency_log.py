"""A blocked event loop is visible in ordinary logs before it costs a lease.

The gold-3 failures left no trace of why a renewal missed its lease: the
renewal task simply never ran while the loop was held. Two warnings now name
it: the renewal's start against its due time (and how long it took), and a
probe Task in the worker loop whose short sleep wakes far later than asked.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from elspeth.web.coordination import lifecycle
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.repository import _SessionOperationAuthorityRepository
from elspeth.web.sessions import composer_async_worker as worker_module
from tests.unit.web.sessions.test_composer_async_worker import _file_app, _worker
from tests.unit.web.sessions.test_composer_owner_unsettled import (
    _STALL_SECONDS,
    _AwaitingComposer,
    _LoopStallingComposer,
    _stall_one_operation,
)
from tests.unit.web.sessions.test_composer_settled_log_pointer import _RecordingLog


class _LateStallingComposer(_LoopStallingComposer):
    """Stalls after a renewal finishes, so normally no renewal is in flight."""

    def __init__(self, renewal_finished: asyncio.Event) -> None:
        super().__init__()
        self.renewal_finished = renewal_finished

    async def compose(self, *args, **kwargs):
        await self.renewal_finished.wait()
        # Let the parent renewal loop observe the finished attempt and arm its next tick. The loop
        # joins its attempt with a 0.01 s poll armed before the attempt finished, asyncio fires due
        # timers in deadline order, and the loop re-arms its tick without awaiting anything else
        # first, so the tick is armed before this 50 ms sleep ends. That tick (1 s) fires first only
        # if the process is paused for about 0.95 s between the loop re-arming and this task running.
        await asyncio.sleep(0.05)
        return await super().compose(*args, **kwargs)


@pytest.mark.asyncio
async def test_a_blocked_loop_logs_the_late_renewal(tmp_path, monkeypatch) -> None:
    renewals = _RecordingLog()
    monkeypatch.setattr(lifecycle, "slog", renewals)
    renewal_finished = asyncio.Event()
    real_observe = lifecycle._observe_renewal_attempt
    renewals_finished = 0

    def record_finished_renewal(context, **timing):
        nonlocal renewals_finished
        real_observe(context, **timing)
        if timing["outcome"] == "renewed":
            renewals_finished += 1
            if renewals_finished == 1:
                renewal_finished.set()

    monkeypatch.setattr(lifecycle, "_observe_renewal_attempt", record_finished_renewal)
    record, _terminal, _worker_log = await _stall_one_operation(tmp_path, monkeypatch, lambda: _LateStallingComposer(renewal_finished))
    slow = [fields for level, fields in renewals.named("session_operation.renewal_slow") if fields["session_id"] == str(record.session_id)]
    assert slow, renewals.calls
    failed = [fields for fields in slow if fields["outcome"] == "failed"]
    assert failed, slow
    # 3 s lease renewed every 1 s; the loop is held 4.5 s, so the renewal starts about 3.5 s late.
    assert _STALL_SECONDS == 4.5
    assert failed[0]["start_late_seconds"] is not None and failed[0]["start_late_seconds"] >= 2.5
    assert failed[0]["lease_seconds"] == 3 and failed[0]["renew_interval_seconds"] == 1.0
    assert failed[0]["operation_kind"] == "compose" and "lease_token" not in failed[0]


class _InFlightStallingComposer(_LoopStallingComposer):
    """Stalls while a renewal's SQL is running in its worker thread."""

    def __init__(self, renewal_in_flight: asyncio.Event, stall_started: threading.Event) -> None:
        super().__init__()
        self.renewal_in_flight = renewal_in_flight
        self.stall_started = stall_started

    async def compose(self, *args, **kwargs):
        # This wait PRODUCES the asserted shape: the block starts only once a renewal has
        # started, so that attempt is observed after the block, as slow and renewed.
        await self.renewal_in_flight.wait()
        self.stall_started.set()
        return await super().compose(*args, **kwargs)


@pytest.mark.asyncio
async def test_a_renewal_in_flight_across_a_blocked_loop_logs_one_slow_attempt_that_renewed(tmp_path, monkeypatch) -> None:
    """The other ordering: the block lands while a renewal is already running.

    Its SQL finishes in its thread during the block, so the attempt renews on
    time but is only observed afterwards: it is logged as slow and renewed, with
    the block inside its duration, and not as a late, failed start.
    """
    renewals = _RecordingLog()
    monkeypatch.setattr(lifecycle, "slog", renewals)
    loop = asyncio.get_running_loop()
    renewal_in_flight = asyncio.Event()
    stall_started = threading.Event()
    real_renew = _SessionOperationAuthorityRepository.renew
    calls = 0
    began_before_the_block: list[bool] = []

    def renew_across_the_stall(self, context, *, lease_seconds):
        nonlocal calls
        calls += 1
        # The second renewal has a due time to start against.
        if calls == 2:
            began_before_the_block.append(not stall_started.is_set())
            loop.call_soon_threadsafe(renewal_in_flight.set)
            # This hold keeps the SQL from finishing before the composer blocks the loop; without it,
            # a starved loop could observe the finished SQL first and log a late start instead.
            assert stall_started.wait(10), "the composer never blocked the loop"
        return real_renew(self, context, lease_seconds=lease_seconds)

    monkeypatch.setattr(_SessionOperationAuthorityRepository, "renew", renew_across_the_stall)
    record, _terminal, _worker_log = await _stall_one_operation(
        tmp_path, monkeypatch, lambda: _InFlightStallingComposer(renewal_in_flight, stall_started)
    )
    slow = [fields for level, fields in renewals.named("session_operation.renewal_slow") if fields["session_id"] == str(record.session_id)]
    in_flight = [fields for fields in slow if fields["outcome"] == "renewed"]
    # The second renewal entered the repository before the composer blocked the loop; its SQL runs
    # only once the block has begun (the hold above).
    assert began_before_the_block == [True]
    assert len(in_flight) == 1, slow
    assert _STALL_SECONDS == 4.5
    assert in_flight[0]["start_late_seconds"] is not None
    assert in_flight[0]["attempt_seconds"] >= _STALL_SECONDS
    assert in_flight[0]["lease_seconds"] == 3 and in_flight[0]["renew_interval_seconds"] == 1.0
    # What CI saw: a slow attempt that renewed, and no late start that failed.
    assert [fields for fields in slow if fields["outcome"] == "failed"] == [], slow


@pytest.mark.asyncio
async def test_the_worker_loop_logs_a_held_event_loop(tmp_path, monkeypatch) -> None:
    app, service, engine, _composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    worker = _worker(app, authority)
    log = _RecordingLog()
    monkeypatch.setattr(worker_module, "slog", log)
    try:
        worker.start()
        await asyncio.sleep(0.2)
        time.sleep(2.0)  # hold the loop, as a long synchronous render does
        await asyncio.sleep(0.2)
        await worker.stop()
        worker.assert_shutdown_complete()
    finally:
        engine.dispose()
    lags = log.named("composer_operation.event_loop_lag")
    assert len(lags) == 1, log.calls
    # The probe's 0.5 s sleep began at most 0.2 s before the 2.0 s hold.
    assert lags[0][0] == "warning" and lags[0][1]["lag_seconds"] >= 1.4


@pytest.mark.asyncio
async def test_an_awaited_delay_logs_no_failed_renewal(tmp_path, monkeypatch) -> None:
    renewals = _RecordingLog()
    monkeypatch.setattr(lifecycle, "slog", renewals)
    _record, terminal, _worker_log = await _stall_one_operation(tmp_path, monkeypatch, _AwaitingComposer)
    assert terminal is not None and terminal.status == "completed"
    assert [fields for _level, fields in renewals.named("session_operation.renewal_slow") if fields["outcome"] == "failed"] == []


def test_the_renewal_line_is_quiet_under_its_threshold(monkeypatch) -> None:
    renewals = _RecordingLog()
    monkeypatch.setattr(lifecycle, "slog", renewals)
    timing = {"lease_seconds": 30, "interval_seconds": 10.0, "pool_outstanding": 0, "outcome": "renewed"}
    lifecycle._log_slow_renewal(_context(), start_late_seconds=1.9, attempt_seconds=1.9, **timing)
    lifecycle._log_slow_renewal(_context(), start_late_seconds=None, attempt_seconds=1.9, **timing)
    assert renewals.calls == []
    lifecycle._log_slow_renewal(_context(), start_late_seconds=0.0, attempt_seconds=2.0, **timing)
    lifecycle._log_slow_renewal(_context(), start_late_seconds=2.0, attempt_seconds=0.0, **timing)
    assert len(renewals.named("session_operation.renewal_slow")) == 2


def test_lateness_is_measured_from_the_previous_attempts_end(monkeypatch) -> None:
    renewals = _RecordingLog()
    monkeypatch.setattr(lifecycle, "slog", renewals)
    monkeypatch.setattr(lifecycle, "_RENEWAL_FINISHED_AT", {})
    clock = iter([100.0, 113.5])
    monkeypatch.setattr(lifecycle.time, "monotonic", lambda: next(clock))
    context = _context()
    timing = {"lease_seconds": 30, "interval_seconds": 10.0, "pool_outstanding": 3, "outcome": "renewed"}
    # First attempt: nothing to compare with, and fast, so quiet.
    lifecycle._observe_renewal_attempt(context, started_at=99.5, **timing)
    assert renewals.calls == []
    # Due at 110.0 (100.0 + 10 s); it started at 113.0.
    lifecycle._observe_renewal_attempt(context, started_at=113.0, **timing)
    [(level, fields)] = renewals.named("session_operation.renewal_slow")
    assert level == "warning"
    assert fields["start_late_seconds"] == 3.0 and fields["attempt_seconds"] == 0.5
    assert fields["pool_outstanding_at_start"] == 3 and fields["operation_epoch"] == 1


def test_the_renewal_timing_record_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(lifecycle, "slog", _RecordingLog())
    monkeypatch.setattr(lifecycle, "_RENEWAL_FINISHED_AT", {})
    timing = {"lease_seconds": 30, "interval_seconds": 10.0, "pool_outstanding": 0, "outcome": "renewed"}
    for _ in range(lifecycle._RENEWAL_FINISHED_RETAINED + 5):
        lifecycle._observe_renewal_attempt(_context(), started_at=time.monotonic(), **timing)
    assert len(lifecycle._RENEWAL_FINISHED_AT) == lifecycle._RENEWAL_FINISHED_RETAINED


def _context():
    from uuid import uuid4

    from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind

    return SessionOperationContext(
        fence=SessionOperationFence(session_id=str(uuid4()), operation_id=str(uuid4()), lease_token="token", operation_epoch=1),
        operation_kind=SessionOperationKind.COMPOSE,
    )
