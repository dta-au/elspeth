"""Concrete archived/missing reaper and shutdown cancellation obligations."""

from __future__ import annotations

import asyncio
import signal
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, update

from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.process_recovery import ProcessRecovery
from elspeth.web.process_watchdog import ProcessWatchdogFailure
from elspeth.web.process_watchdog_codec import RecoveryReason
from elspeth.web.sessions.models import sessions_table
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog
from tests.unit.web.sessions.test_composer_async_worker import _admit, _file_app, _HeldComposer, _worker


async def _wait_for_real_expiry(authority: ComposerAsyncOperationAuthority, expected: set[str]) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        expired = {record.operation_id for record in authority.list_expired_running(limit=64)}
        if expected <= expired:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("The actual database clock did not expire the test leases")


async def _start_without_a_live_worker(app, service, authority):
    record = await _admit(app, service, authority, operation_id=str(uuid4()))
    (claim,) = authority.claim_next(limit=1)
    service.session_operation_authority.start_composer_async_operation(
        claim,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=1,
        auth_provider_type=app.state.settings.auth_provider,
    )
    return record


@pytest.mark.asyncio
async def test_archived_running_job_uses_inactive_recovery_without_replay(tmp_path: Path) -> None:
    app, service, engine, composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    worker = _worker(app, authority)
    try:
        record = await _start_without_a_live_worker(app, service, authority)
        await _wait_for_real_expiry(authority, {record.operation_id})
        # Set the authoritative archived branch directly. This controls the
        # reaper; it does not claim filesystem archive-workflow coverage.
        with engine.begin() as conn:
            assert (
                conn.execute(
                    update(sessions_table).where(sessions_table.c.id == str(record.session_id)).values(archived_at=database_now(conn))
                ).rowcount
                == 1
            )
        assert await worker.reap_once() == 1
        final = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert final is not None and final.status == "failed"
        assert final.failure_code == "worker_lost"
        assert final.attempt == 1 and final.cancel_requested_at is None
        assert final.result_json is not None
        assert composer.calls == 0
        assert await worker.reap_once() == 0
        replay = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert replay == final
    finally:
        await worker.stop()
        engine.dispose()


@pytest.mark.asyncio
async def test_missing_after_reaper_snapshot_does_not_kill_next_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, service, engine, composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    worker = _worker(app, authority)
    try:
        records = [await _start_without_a_live_worker(app, service, authority) for _ in range(2)]
        await _wait_for_real_expiry(authority, {record.operation_id for record in records})
        snapshot = authority.list_expired_running(limit=2)
        assert len(snapshot) == 2
        missing = snapshot[0]
        surviving = snapshot[1]
        repository = service.session_operation_authority
        acquire = repository.acquire
        visited = []

        def delete_at_acquire(**kwargs):
            session_id = kwargs["session_id"]
            visited.append(session_id)
            if session_id == missing.session_id:
                with engine.begin() as conn:
                    assert conn.execute(delete(sessions_table).where(sessions_table.c.id == str(session_id))).rowcount == 1
            return acquire(**kwargs)

        monkeypatch.setattr(repository, "acquire", delete_at_acquire)
        assert await worker.reap_once() == 1
        assert visited == [missing.session_id, surviving.session_id]
        assert authority.get(session_id=missing.session_id, operation_id=missing.operation_id) is None
        final = authority.get(session_id=surviving.session_id, operation_id=surviving.operation_id)
        assert final is not None and final.status == "failed" and final.failure_code == "worker_lost"
        assert final.attempt == 1 and composer.calls == 0
        assert await worker.reap_once() == 0
    finally:
        await worker.stop()
        engine.dispose()


@pytest.mark.asyncio
async def test_reaper_does_not_hide_nonmissing_authority_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, service, engine, composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    worker = _worker(app, authority)
    try:
        record = await _start_without_a_live_worker(app, service, authority)
        await _wait_for_real_expiry(authority, {record.operation_id})
        fault = SessionOperationFenceLost(FenceLossReason.TOKEN_MISMATCH)

        def refuse(**_kwargs):
            raise fault

        monkeypatch.setattr(service.session_operation_authority, "acquire", refuse)
        with pytest.raises(SessionOperationFenceLost) as observed:
            await worker.reap_once()
        assert observed.value is fault
        current = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert current is not None and current.status == "running"
        assert composer.calls == 0
    finally:
        await worker.stop()
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("watchdog_available", (True, False))
async def test_begin_shutdown_latches_before_ack_and_is_idempotent(watchdog_available: bool) -> None:
    draining = threading.Event()
    watchdog = OwnedTestProcessWatchdog(draining)
    recovery = ProcessRecovery(watchdog=watchdog, instance_draining=draining)
    if not watchdog_available:
        watchdog.completed = True
    assert not draining.is_set()
    recovery.begin_shutdown()
    assert draining.is_set()
    assert not watchdog.signals
    escalation = recovery._escalation_task
    recovery.begin_shutdown()
    assert recovery._escalation_task is escalation
    if watchdog_available:
        await recovery.join_escalation()
        assert watchdog.reasons == [RecoveryReason.NORMAL_SHUTDOWN]
        assert not watchdog.signals
    else:
        with pytest.raises(ProcessWatchdogFailure):
            await recovery.join_escalation()
        assert watchdog.signals == [signal.SIGKILL]
    assert draining.is_set()


@pytest.mark.asyncio
async def test_run_until_idle_propagates_caller_cancel_after_owned_job_settlement(tmp_path: Path) -> None:
    app, service, engine, _composer = _file_app(tmp_path)
    held = _HeldComposer()
    app.state.composer_service = held
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    worker = _worker(app, authority)
    task = None
    try:
        record = await _admit(app, service, authority, operation_id=str(uuid4()))
        task = asyncio.create_task(worker.run_until_idle())
        await asyncio.wait_for(held.entered.wait(), timeout=5)
        task.cancel("historical caller cancellation")
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert task.cancelled()
        assert not worker._jobs
        final = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert final is not None and final.status == "failed" and final.failure_code == "worker_lost"
        assert final.cancel_requested_at is None and held.dispatches == 1
        await worker.run_until_idle()
        assert authority.get(session_id=record.session_id, operation_id=record.operation_id) == final
        assert held.dispatches == 1
    finally:
        held.release.set()
        if task is not None and not task.done():
            await asyncio.wait_for(task, timeout=5)
        await worker.stop()
        engine.dispose()
