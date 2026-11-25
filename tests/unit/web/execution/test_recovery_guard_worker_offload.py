"""Run recovery reproves its EXECUTE lease through the shared worker pool.

``_reconcile_token_usage`` and ``_recover_candidate`` hand
``lease.guard_external_effect`` to ``run_sync_in_worker``. For each call these
tests pin that the guard runs on a worker thread while the event loop keeps
serving other tasks, that ``SessionOperationFenceLost`` raised by the guard
reaches the caller unchanged and stops the step it guards, and that a
cancelled caller sees ``CancelledError`` while the running guard completes.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from elspeth.contracts.enums import RunStatus
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.quota_authority import TokenUsageEntry
from elspeth.web.execution import recovery as recovery_module
from elspeth.web.execution.recovery import RecoveryObservation, RunRecoveryCoordinator
from elspeth.web.execution.service import ExecutionServiceImpl, close_execute_lease_before_transfer
from elspeth.web.execution_lease_cleanup import ExecutionAcquisitionObligation, ExecutionLeaseReleaseRegistry
from elspeth.web.sessions.protocol import RunRecord, SessionOperationAuthority
from elspeth.web.sessions.service import SessionServiceImpl
from tests.helpers.worker_probe import WorkerProbe, run_with_probe


def _run() -> Mock:
    run = Mock(spec=RunRecord)
    run.id = uuid4()
    run.session_id = uuid4()
    return run


def _sessions(run: Mock) -> Mock:
    sessions = Mock(spec=SessionServiceImpl)
    sessions.record_token_usage = AsyncMock(spec=SessionServiceImpl.record_token_usage, return_value=None)
    sessions.get_run = AsyncMock(spec=SessionServiceImpl.get_run, return_value=run)
    sessions.session_operation_authority = Mock(spec=SessionOperationAuthority)
    sessions.session_operation_owner_instance_id = "recoverer"
    sessions.session_operation_lease_seconds = 30
    return sessions


def _coordinator(sessions: Mock, execution: Mock | None = None) -> RunRecoveryCoordinator:
    return RunRecoveryCoordinator(
        sessions,
        execution if execution is not None else Mock(spec=ExecutionServiceImpl),
        Mock(spec=BlobServiceImpl),
        landscape_url="sqlite://",
        create_tables=False,
    )


def _token_usage_observation() -> RecoveryObservation:
    return RecoveryObservation(RunStatus.COMPLETED, False, None, token_usage_entries=(Mock(spec=TokenUsageEntry),))


def _no_effect() -> None:
    """The guard's stand-in when the fence is intact."""


def _lease(guard: Any) -> Mock:
    lease = Mock(spec=SessionOperationLease)
    lease.guard_external_effect.side_effect = guard
    return lease


def _candidate_harness(monkeypatch: pytest.MonkeyPatch, lease: Mock, run: Mock) -> tuple[RunRecoveryCoordinator, AsyncMock]:
    """A recovery coordinator whose candidate acquires ``lease`` and observes a live leader.

    With a live leader ``_recover_candidate`` returns right after the guard,
    then closes the lease through ``close_execute_lease_before_transfer``.
    """
    obligation = Mock(spec=ExecutionAcquisitionObligation)
    obligation.completion_required = False
    registry = Mock(spec=ExecutionLeaseReleaseRegistry)
    registry.admit.return_value = obligation
    execution = Mock(spec=ExecutionServiceImpl)
    execution.execution_lease_release_registry = registry
    close = AsyncMock(spec=close_execute_lease_before_transfer, return_value=None)
    monkeypatch.setattr(SessionOperationLease, "acquire", AsyncMock(spec=SessionOperationLease.acquire, return_value=lease))
    monkeypatch.setattr(
        recovery_module,
        "observe_run",
        Mock(spec=recovery_module.observe_run, return_value=RecoveryObservation(RunStatus.RUNNING, True, None)),
    )
    monkeypatch.setattr(recovery_module, "close_execute_lease_before_transfer", close)
    return _coordinator(_sessions(run), execution), close


# ── _reconcile_token_usage ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_token_usage_guard_runs_in_a_worker_while_the_loop_runs() -> None:
    run = _run()
    sessions = _sessions(run)
    probe = WorkerProbe()
    lease = _lease(probe.wrap(_no_effect))
    loop_thread = threading.get_ident()

    await run_with_probe(probe, _coordinator(sessions)._reconcile_token_usage(run, _token_usage_observation(), lease))

    assert probe.entered.is_set()
    # True only if a loop task released the guard while it was running.
    assert probe.outcomes == [True]
    assert loop_thread not in probe.threads
    sessions.record_token_usage.assert_awaited_once()


@pytest.mark.asyncio
async def test_token_usage_guard_refusal_reaches_the_caller_and_nothing_is_recorded() -> None:
    run = _run()
    sessions = _sessions(run)
    guard_error = SessionOperationFenceLost(FenceLossReason.TOKEN_MISMATCH)
    loop_thread = threading.get_ident()
    raising_threads: list[int] = []

    def refuse_guard() -> None:
        raising_threads.append(threading.get_ident())
        raise guard_error

    with pytest.raises(SessionOperationFenceLost) as raised:
        await _coordinator(sessions)._reconcile_token_usage(run, _token_usage_observation(), _lease(refuse_guard))

    assert raised.value is guard_error
    assert len(raising_threads) == 1
    assert loop_thread not in raising_threads
    sessions.record_token_usage.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_token_usage_reconcile_raises_cancelled_error_and_the_guard_completes() -> None:
    run = _run()
    sessions = _sessions(run)
    probe = WorkerProbe()
    lease = _lease(probe.wrap(_no_effect))

    call = asyncio.ensure_future(_coordinator(sessions)._reconcile_token_usage(run, _token_usage_observation(), lease))
    try:
        await probe.wait_entered()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
    finally:
        probe.released.set()
    await probe.wait_finished()

    assert probe.outcomes == [True]
    sessions.record_token_usage.assert_not_awaited()


# ── _recover_candidate ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_candidate_guard_runs_in_a_worker_while_the_loop_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run()
    probe = WorkerProbe()
    lease = _lease(probe.wrap(_no_effect))
    coordinator, close = _candidate_harness(monkeypatch, lease, run)
    loop_thread = threading.get_ident()

    await run_with_probe(probe, coordinator._recover_candidate(run))

    assert probe.entered.is_set()
    assert probe.outcomes == [True]
    assert loop_thread not in probe.threads
    close.assert_awaited_once()
    assert close.await_args is not None
    assert close.await_args.args == (lease,)


@pytest.mark.asyncio
async def test_candidate_guard_refusal_reaches_the_caller_and_the_lease_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run()
    guard_error = SessionOperationFenceLost(FenceLossReason.TOKEN_MISMATCH)
    loop_thread = threading.get_ident()
    raising_threads: list[int] = []

    def refuse_guard() -> None:
        raising_threads.append(threading.get_ident())
        raise guard_error

    coordinator, close = _candidate_harness(monkeypatch, _lease(refuse_guard), run)

    with pytest.raises(SessionOperationFenceLost) as raised:
        await coordinator._recover_candidate(run)

    assert raised.value is guard_error
    assert len(raising_threads) == 1
    assert loop_thread not in raising_threads
    close.assert_awaited_once()
    assert close.await_args is not None
    assert close.await_args.kwargs == {"primary": guard_error}


@pytest.mark.asyncio
async def test_cancelled_candidate_recovery_raises_cancelled_error_and_the_guard_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run()
    probe = WorkerProbe()
    lease = _lease(probe.wrap(_no_effect))
    coordinator, close = _candidate_harness(monkeypatch, lease, run)

    call = asyncio.ensure_future(coordinator._recover_candidate(run))
    try:
        await probe.wait_entered()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
    finally:
        probe.released.set()
    await probe.wait_finished()

    assert probe.outcomes == [True]
    close.assert_awaited_once()
    assert close.await_args is not None
    assert type(close.await_args.kwargs["primary"]) is asyncio.CancelledError
