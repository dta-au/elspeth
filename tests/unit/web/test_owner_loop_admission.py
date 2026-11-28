"""Owner loops retain their bounded retry when worker admission is refused."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from sqlalchemy.exc import OperationalError

from elspeth.web import async_workers
from elspeth.web.app import _periodic_orphan_cleanup
from elspeth.web.application_finalizers import ApplicationFinalizerOwner
from elspeth.web.coordination import membership_lifecycle
from elspeth.web.coordination.membership_authority import RepositoryWebInstanceMembershipAuthority
from elspeth.web.coordination.membership_lifecycle import RegisteredWebInstanceMembership, heartbeat_interval_seconds
from elspeth.web.process_recovery import ProcessRecovery
from elspeth.web.sessions.composer_async_worker import ComposerAsyncWorker
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog
from tests.unit.web.coordination.test_membership_authority import _identity
from tests.unit.web.execution.test_session_operation_lease import _eventually


class _PoolHold:
    def __init__(self, *, upkeep: bool = False) -> None:
        self.upkeep = upkeep
        self.release = threading.Event()
        self.holders: list[asyncio.Task[bool]] = []

    def outstanding(self) -> int:
        if self.upkeep:
            return async_workers._LEASE_UPKEEP_WORKERS.outstanding
        return async_workers.outstanding_admissions()

    async def hold(self) -> None:
        capacity = async_workers.LEASE_UPKEEP_ADMISSION_CAPACITY if self.upkeep else async_workers.ADMISSION_CAPACITY
        runner = async_workers.run_lease_upkeep_in_worker if self.upkeep else async_workers.run_sync_in_worker
        self.holders = [asyncio.create_task(runner(self.release.wait, 30)) for _ in range(capacity)]
        assert await _eventually(lambda: self.outstanding() == capacity, seconds=5)

    async def free(self) -> None:
        self.release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(*self.holders, return_exceptions=True), timeout=10)
        assert all(outcome is True for outcome in outcomes), outcomes
        assert await _eventually(lambda: self.outstanding() == 0, seconds=10)


def _sessions(tmp_path: Path) -> SessionServiceImpl:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(engine)
    return SessionServiceImpl(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test"))


def _sweep(sessions: SessionServiceImpl, recover: Any) -> asyncio.Task[None]:
    coordinator = SimpleNamespace(recover=recover)
    execution = SimpleNamespace(get_live_run_ids=lambda: frozenset())
    return asyncio.create_task(
        _periodic_orphan_cleanup(
            sessions,
            execution,
            build_sessions_telemetry(),
            interval_seconds=0,
            max_age_seconds=3600,
            recovery_coordinator=coordinator,
        )
    )


async def _stop(task: asyncio.Task[None]) -> None:
    """Cancel a loop that is still running; a finished loop's outcome stays with the test's assertions."""
    if task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
@pytest.mark.timing_sensitive
async def test_sweep_retries_preacquire_refusal(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    calls = 0
    recovered = asyncio.Event()

    async def recover() -> None:
        nonlocal calls
        calls += 1
        await sessions.list_recoverable_run_records()
        recovered.set()

    hold = _PoolHold()
    task: asyncio.Task[None] | None = None
    try:
        try:
            await hold.hold()
            sweep = task = _sweep(sessions, recover)
            assert await _eventually(lambda: calls >= 2 or sweep.done(), seconds=5)
            assert calls >= 2 and not sweep.done(), "a refused recovery must leave the sweep active"
        finally:
            await hold.free()
        assert await _eventually(lambda: recovered.is_set() or sweep.done(), seconds=5)
        assert recovered.is_set() and not sweep.done(), "the sweep must recover once the pool frees"
    finally:
        if task is not None:
            await _stop(task)


@pytest.mark.asyncio
@pytest.mark.timing_sensitive
async def test_sweep_escalates_at_refusal_cap(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    calls = 0
    fifth_entered = asyncio.Event()
    fifth_allowed = asyncio.Event()

    async def recover() -> None:
        nonlocal calls
        calls += 1
        if calls == 5:
            fifth_entered.set()
            await fifth_allowed.wait()
        await sessions.list_recoverable_run_records()

    hold = _PoolHold()
    task: asyncio.Task[None] | None = None
    try:
        await hold.hold()
        sweep = task = _sweep(sessions, recover)
        assert await _eventually(lambda: fifth_entered.is_set() or sweep.done(), seconds=9)
        assert calls == 5 and not sweep.done(), "four refusals must leave the sweep active"
        fifth_allowed.set()
        with pytest.raises(async_workers.AsyncWorkerAdmissionTimeoutError):
            await asyncio.wait_for(sweep, timeout=3)
        assert calls == 5
    finally:
        fifth_allowed.set()
        try:
            await hold.free()
        finally:
            if task is not None:
                await _stop(task)


@pytest.mark.asyncio
async def test_archive_refusal_ends_sweep_on_fifth_attempt() -> None:
    attempts = 0
    recovered = 0

    async def reconcile_consumed_archives() -> None:
        nonlocal attempts
        attempts += 1
        raise async_workers.AsyncWorkerAdmissionTimeoutError("admission refused")

    async def recover() -> None:
        nonlocal recovered
        recovered += 1

    sessions = SimpleNamespace(reconcile_consumed_archives=reconcile_consumed_archives)
    task = _sweep(sessions, recover)
    try:
        with pytest.raises(async_workers.AsyncWorkerAdmissionTimeoutError):
            await asyncio.wait_for(task, timeout=2)
        assert attempts == 5
        assert recovered == 4
    finally:
        if not task.done():
            await _stop(task)


@pytest.mark.asyncio
@pytest.mark.timing_sensitive
async def test_sweep_counts_only_all_refusal_exception_groups(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    hold = _PoolHold()
    try:
        await hold.hold()
        refusals = await asyncio.gather(
            async_workers.run_sync_in_worker(lambda: None),
            async_workers.run_sync_in_worker(lambda: None),
            return_exceptions=True,
        )
        assert all(type(failure) is async_workers.AsyncWorkerAdmissionTimeoutError for failure in refusals)
    finally:
        await hold.free()
    calls = 0
    fifth_entered = asyncio.Event()
    fifth_allowed = asyncio.Event()

    async def recover() -> None:
        nonlocal calls
        calls += 1
        if calls == 5:
            fifth_entered.set()
            await fifth_allowed.wait()
        raise ExceptionGroup("admission refused", refusals)

    task = _sweep(sessions, recover)
    try:
        assert await _eventually(lambda: fifth_entered.is_set() or task.done(), seconds=5)
        assert calls == 5 and not task.done(), "four refusal groups must leave the sweep active"
        fifth_allowed.set()
        with pytest.raises(ExceptionGroup):
            await asyncio.wait_for(task, timeout=2)
        assert calls == 5
    finally:
        fifth_allowed.set()
        await _stop(task)


@pytest.mark.asyncio
@pytest.mark.timing_sensitive
async def test_sweep_rejects_mixed_exception_group(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    hold = _PoolHold()
    try:
        await hold.hold()
        refusal = await asyncio.gather(async_workers.run_sync_in_worker(lambda: None), return_exceptions=True)
        assert type(refusal[0]) is async_workers.AsyncWorkerAdmissionTimeoutError
    finally:
        await hold.free()

    calls = 0

    async def recover() -> None:
        nonlocal calls
        calls += 1
        raise ExceptionGroup("mixed", [refusal[0], ValueError("standing failure")])

    task = _sweep(sessions, recover)
    try:
        with pytest.raises(ExceptionGroup):
            await asyncio.wait_for(task, timeout=2)
        assert calls == 1
    finally:
        if not task.done():
            await _stop(task)


def _composer_loop() -> SimpleNamespace:
    wake = asyncio.Event()

    async def reap_once() -> None:
        await async_workers.run_sync_in_worker(lambda: None)

    async def claim_once() -> None:
        return None

    return SimpleNamespace(
        _stopping=False,
        _instance_draining=threading.Event(),
        _wake=wake,
        _scan_interval=0.01,
        reap_once=reap_once,
        _claim_once=claim_once,
    )


@pytest.mark.asyncio
@pytest.mark.timing_sensitive
async def test_composer_loop_survives_refusal_below_cap() -> None:
    hold = _PoolHold()
    owner = _composer_loop()
    task: asyncio.Task[None] | None = None
    refusals = 0
    actual = async_workers._acquire_admission

    async def observe_refusal() -> None:
        nonlocal refusals
        try:
            await actual()
        except async_workers.AsyncWorkerAdmissionTimeoutError:
            refusals += 1
            raise

    try:
        await hold.hold()
        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(async_workers, "_acquire_admission", observe_refusal)
            task = asyncio.create_task(ComposerAsyncWorker._loop(owner))
            assert await _eventually(lambda: refusals >= 1, seconds=5)
            assert not task.done(), "a refused poll must leave the composer loop active"
    finally:
        try:
            await hold.free()
        finally:
            owner._stopping = True
            owner._wake.set()
            if task is not None and not task.done():
                await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
@pytest.mark.timing_sensitive
async def test_composer_loop_escalates_at_refusal_cap() -> None:
    hold = _PoolHold()
    owner = _composer_loop()
    calls = 0
    fifth_entered = asyncio.Event()
    fifth_allowed = asyncio.Event()

    async def reap_once() -> None:
        nonlocal calls
        calls += 1
        if calls == 5:
            fifth_entered.set()
            await fifth_allowed.wait()
        await async_workers.run_sync_in_worker(lambda: None)

    owner.reap_once = reap_once
    task: asyncio.Task[None] | None = None
    try:
        await hold.hold()
        loop_task = task = asyncio.create_task(ComposerAsyncWorker._loop(owner))
        assert await _eventually(lambda: fifth_entered.is_set() or loop_task.done(), seconds=9)
        assert calls == 5 and not loop_task.done(), "four refusals must leave the composer loop active"
        fifth_allowed.set()
        with pytest.raises(async_workers.AsyncWorkerAdmissionTimeoutError):
            await asyncio.wait_for(loop_task, timeout=3)
        assert calls == 5
    finally:
        fifth_allowed.set()
        try:
            await hold.free()
        finally:
            if task is not None and not task.done():
                owner._stopping = True
                owner._wake.set()
                await _stop(task)


@pytest.mark.asyncio
@pytest.mark.timing_sensitive
async def test_membership_heartbeat_retries_then_takes_loss_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'membership.db'}")
    initialize_session_schema(engine)
    draining = threading.Event()
    owner = ApplicationFinalizerOwner()
    recovery = ProcessRecovery(watchdog=OwnedTestProcessWatchdog(draining), instance_draining=draining)
    membership = RegisteredWebInstanceMembership(
        RepositoryWebInstanceMembershipAuthority(engine),
        _identity(),
        lease_seconds=5,
        interval_seconds=1,
        process_recovery=recovery,
        instance_draining=draining,
        finalizer_owner=owner,
    )
    owner.seal()
    monkeypatch.setattr(async_workers, "_APPLICATION_FINALIZER_OWNER", owner)
    monkeypatch.setattr(async_workers, "_INSTANCE_DRAINING", draining)
    refusals = 0
    actual_wait = async_workers._wait_for_admission

    async def count_refusals(try_admit: Callable[[], bool], capacity: int, pool_name: str) -> None:
        nonlocal refusals
        try:
            await actual_wait(try_admit, capacity, pool_name)
        except async_workers.AsyncWorkerAdmissionTimeoutError:
            refusals += 1
            raise

    monkeypatch.setattr(async_workers, "_wait_for_admission", count_refusals)
    hold = _PoolHold(upkeep=True)
    started = False
    try:
        await membership.start()
        started = True
        task = membership._heartbeat_task
        assert task is not None
        await hold.hold()
        # Lease 5 s, interval 1 s: the retries stop one interval before the lease would lapse.
        assert await _eventually(task.done, seconds=8), "a sustained refusal must end the heartbeat"
        assert refusals >= 2, "a refusal inside the membership lease must be retried"
        assert await _eventually(membership.draining.is_set, seconds=2)
        assert type(task.exception()) is async_workers.AsyncWorkerAdmissionTimeoutError
    finally:
        try:
            await hold.free()
        finally:
            try:
                if started:
                    # The stop write runs only while the instance drains, and only the heartbeat's
                    # loss path sets draining. Set it here so this cleanup cannot replace an
                    # assertion raised above when the heartbeat has not failed.
                    draining.set()
                    # stop() re-raises the heartbeat's own failure; expect exactly what the task holds.
                    heartbeat = membership._heartbeat_task
                    failure = heartbeat.exception() if heartbeat is not None and heartbeat.done() and not heartbeat.cancelled() else None
                    if failure is None:
                        await asyncio.wait_for(membership.stop(), timeout=5)
                    else:
                        with pytest.raises(type(failure)) as raised:
                            await asyncio.wait_for(membership.stop(), timeout=5)
                        assert raised.value is failure
            finally:
                engine.dispose()


def _fake_heartbeat_clock(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sleep: Callable[[float], Awaitable[None]],
    now: Callable[[], float],
    heartbeat: Callable[..., Awaitable[None]],
) -> None:
    """Drive the heartbeat loop on a fake clock through the module's asyncio and worker seams."""
    monkeypatch.setattr(
        membership_lifecycle,
        "asyncio",
        SimpleNamespace(sleep=sleep, get_running_loop=lambda: SimpleNamespace(time=now)),
    )
    monkeypatch.setattr(membership_lifecycle, "run_lease_upkeep_in_worker", heartbeat)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call_seconds", "expected_calls"),
    [
        # Every check after the slow first database error reads a clock that
        # the call did not move: escalation comes at the check that first
        # reaches the deadline exactly, so a threshold one short delay early,
        # or a strict comparison, moves it.
        pytest.param({3: 0.5}, 7, id="instant-deciding-call"),
        # The deciding call itself takes 0.25 s: only a check that reads the
        # clock after the call reaches the deadline on that call.
        pytest.param({3: 0.5, 6: 0.25}, 6, id="slow-deciding-call"),
    ],
)
async def test_heartbeat_refusal_then_database_errors_obeys_lease_deadline(
    monkeypatch: pytest.MonkeyPatch, call_seconds: dict[int, float], expected_calls: int
) -> None:
    clock = 0.0
    calls = 0
    sleeps: list[float] = []
    logs: list[tuple[str, str]] = []
    real_sleep = asyncio.sleep
    refusal = async_workers.AsyncWorkerAdmissionTimeoutError("admission refused")
    database_error = OperationalError("heartbeat", {}, RuntimeError("database unavailable"))

    async def fake_sleep(seconds: float) -> None:
        nonlocal clock
        sleeps.append(seconds)
        clock += seconds
        await real_sleep(0)

    async def heartbeat(*_args: Any, **_kwargs: Any) -> None:
        nonlocal clock, calls
        calls += 1
        if calls == 1:
            clock += 1.0  # The successful submission takes time before returning.
            return
        if calls == 2:
            raise refusal
        clock += call_seconds.get(calls, 0.0)
        raise database_error

    _fake_heartbeat_clock(monkeypatch, sleep=fake_sleep, now=lambda: clock, heartbeat=heartbeat)
    owner = SimpleNamespace(
        _interval_seconds=1,
        _lease_seconds=5,
        _heartbeat_authority=SimpleNamespace(heartbeat=lambda: None),
        _identity=SimpleNamespace(instance_id="instance"),
        _log=SimpleNamespace(
            warning=lambda event, **_fields: logs.append(("warning", event)),
            error=lambda event, **_fields: logs.append(("error", event)),
        ),
    )
    with pytest.raises((OperationalError, async_workers.AsyncWorkerAdmissionTimeoutError)) as raised:
        await asyncio.wait_for(RegisteredWebInstanceMembership._heartbeat_loop(owner), timeout=5)
    assert raised.value is database_error, "a refusal inside the lease must be retried"
    # After a refusal, database errors are retried after the short delay too,
    # bounded by the lease deadline: the success at t=1 plus lease 5 less one
    # interval, so t=5. A failed attempt escalates once its check, the clock
    # after the call plus 0.25, reaches 5.0; both cases end at clock 4.75.
    assert calls == expected_calls
    assert sleeps == [1, 1] + [0.25] * (expected_calls - 2)
    assert clock == 4.75
    assert logs[0] == ("warning", "web_instance_heartbeat_failed")
    assert logs[-1] == ("error", "web_instance_heartbeat_escalating")


@pytest.mark.asyncio
async def test_heartbeat_database_errors_after_refusal_retry_until_the_lease_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    # The default ratio: lease 30 s, interval 10 s.
    clock = 0.0
    calls = 0
    real_sleep = asyncio.sleep
    database_error = OperationalError("heartbeat", {}, RuntimeError("database unavailable"))

    async def fake_sleep(seconds: float) -> None:
        nonlocal clock
        clock += seconds
        await real_sleep(0)

    async def heartbeat(*_args: Any, **_kwargs: Any) -> None:
        nonlocal clock, calls
        calls += 1
        if calls == 1:
            clock += 1.0  # The refusal arrives after the full admission wait.
            raise async_workers.AsyncWorkerAdmissionTimeoutError("admission refused")
        raise database_error

    _fake_heartbeat_clock(monkeypatch, sleep=fake_sleep, now=lambda: clock, heartbeat=heartbeat)
    owner = SimpleNamespace(
        _interval_seconds=heartbeat_interval_seconds(30),
        _lease_seconds=30,
        _heartbeat_authority=SimpleNamespace(heartbeat=lambda: None),
        _identity=SimpleNamespace(instance_id="instance"),
        _log=structlog.get_logger("test"),
    )
    with pytest.raises((OperationalError, async_workers.AsyncWorkerAdmissionTimeoutError)) as raised:
        await asyncio.wait_for(RegisteredWebInstanceMembership._heartbeat_loop(owner), timeout=5)
    assert raised.value is database_error, "a refusal inside the lease must be retried"
    # Refused at 11 s; database errors at 12 s to 19 s, one short delay apart. The
    # one at 19 s escalates: one more delay would leave less than one interval.
    assert (calls, clock) == (9, 19.0), "database errors after a refusal must retry until the lease deadline"
    assert clock < 30, "the loss path must begin before the membership lease lapses"


@pytest.mark.asyncio
async def test_heartbeat_success_after_refusal_restores_the_count_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    # The default ratio: lease 30 s, interval 10 s.
    clock = 0.0
    calls = 0
    sleeps: list[float] = []
    escalations: list[dict[str, Any]] = []
    real_sleep = asyncio.sleep
    database_error = OperationalError("heartbeat", {}, RuntimeError("database unavailable"))

    async def fake_sleep(seconds: float) -> None:
        nonlocal clock
        sleeps.append(seconds)
        clock += seconds
        await real_sleep(0)

    async def heartbeat(*_args: Any, **_kwargs: Any) -> None:
        nonlocal clock, calls
        calls += 1
        if calls == 1:
            clock += 1.0  # The refusal arrives after the full admission wait.
            raise async_workers.AsyncWorkerAdmissionTimeoutError("admission refused")
        if calls == 2:
            return
        raise database_error

    def error(event: str, **fields: Any) -> None:
        if event == "web_instance_heartbeat_escalating":
            escalations.append(fields)

    _fake_heartbeat_clock(monkeypatch, sleep=fake_sleep, now=lambda: clock, heartbeat=heartbeat)
    owner = SimpleNamespace(
        _interval_seconds=heartbeat_interval_seconds(30),
        _lease_seconds=30,
        _heartbeat_authority=SimpleNamespace(heartbeat=lambda: None),
        _identity=SimpleNamespace(instance_id="instance"),
        _log=SimpleNamespace(warning=lambda _event, **_fields: None, error=error),
    )
    with pytest.raises((OperationalError, async_workers.AsyncWorkerAdmissionTimeoutError)) as raised:
        await asyncio.wait_for(RegisteredWebInstanceMembership._heartbeat_loop(owner), timeout=5)
    assert raised.value is database_error
    # Refused at 11 s, renewed at 12 s, then database errors at 22, 32, 42, 52
    # and 62 s: after the success they are retried at the regular interval and
    # the fifth reaches the count cap.
    assert sleeps == [10, 1, 10, 10, 10, 10, 10], "a success must return database errors to the regular interval"
    assert (calls, clock) == (7, 62.0)
    assert escalations == [{"exc_class": "OperationalError", "consecutive_failures": 5}]


@pytest.mark.asyncio
async def test_heartbeat_success_resets_the_database_error_count(monkeypatch: pytest.MonkeyPatch) -> None:
    # The default ratio, lease 30 s and interval 10 s, with no refusal anywhere:
    # two database errors, a success, then database errors until the cap.
    cap = membership_lifecycle._HEARTBEAT_MAX_CONSECUTIVE_FAILURES
    clock = 0.0
    calls = 0
    sleeps: list[float] = []
    escalations: list[dict[str, Any]] = []
    real_sleep = asyncio.sleep
    database_error = OperationalError("heartbeat", {}, RuntimeError("database unavailable"))

    async def fake_sleep(seconds: float) -> None:
        nonlocal clock
        sleeps.append(seconds)
        clock += seconds
        await real_sleep(0)

    async def heartbeat(*_args: Any, **_kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            return
        raise database_error

    def error(event: str, **fields: Any) -> None:
        if event == "web_instance_heartbeat_escalating":
            escalations.append(fields)

    _fake_heartbeat_clock(monkeypatch, sleep=fake_sleep, now=lambda: clock, heartbeat=heartbeat)
    owner = SimpleNamespace(
        _interval_seconds=heartbeat_interval_seconds(30),
        _lease_seconds=30,
        _heartbeat_authority=SimpleNamespace(heartbeat=lambda: None),
        _identity=SimpleNamespace(instance_id="instance"),
        _log=SimpleNamespace(warning=lambda _event, **_fields: None, error=error),
    )
    with pytest.raises(OperationalError) as raised:
        await asyncio.wait_for(RegisteredWebInstanceMembership._heartbeat_loop(owner), timeout=5)
    assert raised.value is database_error
    # The success resets the count, so the two earlier errors do not count
    # towards the cap: escalation comes on the cap-th error after the success.
    assert calls == 3 + cap, "the database errors before a success must not count towards the cap"
    assert sleeps == [10] * (3 + cap)
    assert escalations == [{"exc_class": "OperationalError", "consecutive_failures": cap}]


@pytest.mark.asyncio
async def test_heartbeat_refusals_take_the_loss_path_before_the_lease_lapses(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = 0.0
    calls = 0
    renewed_at: float | None = None
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float) -> None:
        nonlocal clock
        clock += seconds
        await real_sleep(0)

    async def heartbeat(*_args: Any, **_kwargs: Any) -> None:
        nonlocal clock, calls, renewed_at
        calls += 1
        if calls == 1:
            renewed_at = clock
            return
        clock += 1.0  # Each refusal arrives after the full admission wait.
        raise async_workers.AsyncWorkerAdmissionTimeoutError("admission refused")

    _fake_heartbeat_clock(monkeypatch, sleep=fake_sleep, now=lambda: clock, heartbeat=heartbeat)
    owner = SimpleNamespace(
        _interval_seconds=1,
        _lease_seconds=5,
        _heartbeat_authority=SimpleNamespace(heartbeat=lambda: None),
        _identity=SimpleNamespace(instance_id="instance"),
        _log=structlog.get_logger("test"),
    )
    with pytest.raises(async_workers.AsyncWorkerAdmissionTimeoutError):
        await asyncio.wait_for(RegisteredWebInstanceMembership._heartbeat_loop(owner), timeout=5)
    assert calls >= 3, "a refusal inside the membership lease must be retried"
    assert renewed_at is not None
    assert clock < renewed_at + 5, "the loss path must begin before the membership lease lapses"


@pytest.mark.asyncio
async def test_heartbeat_database_errors_keep_existing_count_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = 0.0
    calls = 0
    real_sleep = asyncio.sleep
    database_error = OperationalError("heartbeat", {}, RuntimeError("database unavailable"))

    async def fake_sleep(seconds: float) -> None:
        nonlocal clock
        clock += seconds
        await real_sleep(0)

    async def heartbeat(*_args: Any, **_kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        raise database_error

    _fake_heartbeat_clock(monkeypatch, sleep=fake_sleep, now=lambda: clock, heartbeat=heartbeat)
    owner = SimpleNamespace(
        _interval_seconds=1,
        _lease_seconds=5,
        _heartbeat_authority=SimpleNamespace(heartbeat=lambda: None),
        _identity=SimpleNamespace(instance_id="instance"),
        _log=structlog.get_logger("test"),
    )
    with pytest.raises(OperationalError) as raised:
        await asyncio.wait_for(RegisteredWebInstanceMembership._heartbeat_loop(owner), timeout=5)
    assert raised.value is database_error
    assert calls == 5
    assert clock == 5
