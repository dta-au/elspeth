"""U-37 drain preparation; no V2 driver, database, signal or process proof."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import Mock
from uuid import uuid4

import pytest
from starlette.applications import Starlette

from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.process_recovery import ProcessRecovery
from elspeth.web.sessions.composer_async_worker import ComposerAsyncWorker
from elspeth.web.sessions.composer_drain import ComposerDrainDeadline
from elspeth.web.sessions.composer_operations import ComposerOperationClaim


def test_drain_deadline_is_absolute_and_remaining_time_never_restarts() -> None:
    deadline = ComposerDrainDeadline.from_start(started_at=100.0, drain_seconds=10.0)
    assert deadline.expires_at == 110.0
    assert deadline.remaining_seconds(now=100.0) == 10.0
    assert deadline.remaining_seconds(now=105.0) == 5.0
    assert deadline.remaining_seconds(now=110.0) == 0.0
    assert deadline.remaining_seconds(now=111.0) == 0.0


@pytest.mark.parametrize("invalid", (float("inf"), float("-inf"), float("nan"), True))
def test_drain_deadline_refuses_nonfinite_or_boolean_times(invalid: float) -> None:
    with pytest.raises(ValueError):
        ComposerDrainDeadline(invalid)
    with pytest.raises(ValueError):
        ComposerDrainDeadline.from_start(started_at=invalid, drain_seconds=10.0)
    with pytest.raises(ValueError):
        ComposerDrainDeadline.from_start(started_at=100.0, drain_seconds=invalid)
    with pytest.raises(ValueError):
        ComposerDrainDeadline(110.0).remaining_seconds(now=invalid)


@pytest.mark.parametrize("duration", (0.0, -1.0))
def test_drain_deadline_refuses_nonpositive_duration(duration: float) -> None:
    with pytest.raises(ValueError):
        ComposerDrainDeadline.from_start(started_at=100.0, drain_seconds=duration)


def test_drain_deadline_refuses_overflow_at_construction() -> None:
    with pytest.raises(ValueError):
        ComposerDrainDeadline.from_start(started_at=1e308, drain_seconds=1e308)


def _worker(*, drain_seconds: float = 10.0) -> tuple[ComposerAsyncWorker, Mock]:
    authority = Mock(spec=ComposerAsyncOperationAuthority)
    return (
        ComposerAsyncWorker(
            app=Starlette(),
            authority=authority,
            concurrency=1,
            scan_interval_seconds=1.0,
            claim_lease_seconds=30,
            drain_seconds=drain_seconds,
            owner_instance_id="drain-preparation",
            process_recovery=Mock(spec=ProcessRecovery),
            instance_draining=threading.Event(),
        ),
        authority,
    )


@pytest.mark.parametrize("duration", (float("inf"), float("nan"), True))
def test_worker_refuses_a_nonfinite_or_boolean_drain(duration: float) -> None:
    with pytest.raises(ValueError, match="resource limits"):
        _worker(drain_seconds=duration)


@pytest.mark.asyncio
async def test_begin_drain_shares_one_deadline_and_stops_claims() -> None:
    worker, authority = _worker()
    first = worker.begin_drain()
    assert worker.begin_drain() is first
    assert worker.drain_deadline is first
    assert worker._instance_draining.is_set()
    assert worker._wake.is_set()
    assert await worker._claim_once() == 0
    authority.claim_next.assert_not_called()
    await worker.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", (25.0, 120.0))
async def test_v1_claim_refusal_preserves_its_relative_stop_budget(duration: float) -> None:
    worker, authority = _worker(drain_seconds=duration)
    worker.stop_claiming()
    assert worker._instance_draining.is_set() and worker._wake.is_set()
    assert worker.drain_deadline is None
    assert worker._drain_seconds == duration
    assert await worker._claim_once() == 0
    authority.claim_next.assert_not_called()
    await worker.stop()


@pytest.mark.asyncio
async def test_begin_drain_preserves_the_signal_start_despite_callback_delay() -> None:
    worker, _authority = _worker()
    signal_start = asyncio.get_running_loop().time() - 4.0
    deadline = worker.begin_drain(started_at=signal_start)
    assert deadline.expires_at == signal_start + 10.0
    assert worker.begin_drain(started_at=signal_start + 3.0) is deadline
    await worker.stop()


@pytest.mark.asyncio
async def test_stop_after_begin_drain_does_not_cancel_a_job_at_http_completion() -> None:
    worker, _authority = _worker(drain_seconds=0.1)
    completed = asyncio.Event()

    async def job() -> None:
        await asyncio.sleep(0.02)
        completed.set()

    task = asyncio.create_task(job())
    key = (uuid4(), "draining-job")
    worker._jobs[key] = task
    worker._local_cancels[key] = asyncio.Event()
    task.add_done_callback(lambda _task: worker._jobs.pop(key))
    worker.begin_drain()
    await worker.stop()
    assert completed.is_set() and not task.cancelled()


@pytest.mark.asyncio
async def test_intentional_scan_loop_exit_during_drain_does_not_request_recovery() -> None:
    worker, authority = _worker()
    worker.start()
    worker.begin_drain()
    assert worker._loop_task is not None
    await worker._loop_task
    await asyncio.sleep(0)
    worker._process_recovery.request_shutdown.assert_not_called()
    authority.claim_next.assert_not_called()
    await worker.stop()


@pytest.mark.asyncio
async def test_a_scan_loop_error_during_drain_still_requests_recovery() -> None:
    worker, _authority = _worker()
    worker.begin_drain()

    async def fail() -> None:
        raise RuntimeError("lost scan")

    task = asyncio.create_task(fail())
    await asyncio.gather(task, return_exceptions=True)
    worker._loop_finished(task)
    worker._process_recovery.request_shutdown.assert_called_once_with()
    await worker.stop()


@pytest.mark.asyncio
async def test_unexpected_scan_loop_cancellation_during_drain_still_requests_recovery() -> None:
    worker, _authority = _worker()
    worker.begin_drain()
    task = asyncio.create_task(asyncio.Event().wait())
    task.cancel("unexpected-loop-cancellation")
    await asyncio.gather(task, return_exceptions=True)
    worker._loop_finished(task)
    worker._process_recovery.request_shutdown.assert_called_once_with()
    await worker.stop()


@pytest.mark.asyncio
async def test_claim_completed_after_drain_is_released_without_starting_a_job(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.web.sessions import composer_async_worker as module

    worker, authority = _worker()
    claim = ComposerOperationClaim(session_id=uuid4(), operation_id=str(uuid4()), claim_token="claim", attempt=1)
    entered = asyncio.Event()
    released = asyncio.Event()

    async def run_owned(function, *args, **kwargs):
        if function is authority.release_claim:
            return function(*args, **kwargs)
        assert function is authority.claim_next
        assert kwargs == {"limit": 1}
        entered.set()
        await released.wait()
        return (claim,)

    monkeypatch.setattr(module, "run_sync_in_worker", run_owned)
    claiming = asyncio.create_task(worker._claim_once())
    await entered.wait()
    worker.begin_drain()
    released.set()
    assert await claiming == 0
    authority.release_claim.assert_called_once_with(claim)
    assert worker._jobs == {}
    await worker.stop()


@pytest.mark.asyncio
async def test_stop_without_begin_drain_keeps_the_v1_cancellation_path() -> None:
    worker, _authority = _worker()
    entered = asyncio.Event()

    async def owned_work() -> None:
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(owned_work())
    await entered.wait()
    worker._jobs[(uuid4(), str(uuid4()))] = task
    await worker.stop()
    assert task.cancelled()
    assert worker.drain_deadline is None
