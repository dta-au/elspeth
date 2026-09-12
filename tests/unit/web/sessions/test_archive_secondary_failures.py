"""Archive recovery failures remain visible when a request is cancelled."""

from __future__ import annotations

import asyncio

import pytest
import structlog
from sqlalchemy.pool import StaticPool

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions import service as service_module
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web.sessions.guided_test_authority import DualFencedSessionServiceHarness


@pytest.fixture
def service(tmp_path):
    engine = create_session_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    yield DualFencedSessionServiceHarness(
        engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test").bind(), data_dir=tmp_path
    )
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["phase", "compensation", "close"])
@pytest.mark.parametrize("failure_type", [AuditIntegrityError, RuntimeError])
@pytest.mark.parametrize("additional_close_failure", [False, True])
async def test_cancelled_archive_propagates_failure_and_closes_lease(
    service, monkeypatch, failure_phase, failure_type, additional_close_failure
):
    session = await service.create_session("alice", "archive cancellation", "local")
    entered = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()
    worker = service_module.run_sync_in_worker
    real_close = SessionOperationLease.close
    secondary = failure_type("private filesystem detail must not reach logs")
    close_failure = AuditIntegrityError("independent close failure")

    async def controlled_worker(function, *args, **kwargs):
        pause_at = (
            service_module.stage_archive_quarantine if failure_phase == "compensation" else service_module.list_archive_quarantine_manifests
        )
        if function is pause_at:
            entered.set()
            await release.wait()
            if failure_phase == "phase":
                raise secondary
        if failure_phase == "compensation" and function is service_module.restore_archive_quarantine:
            raise secondary
        return await worker(function, *args, **kwargs)

    async def controlled_close(lease):
        try:
            await real_close(lease)
        finally:
            closed.set()
            if additional_close_failure:
                raise close_failure
        if failure_phase == "close":
            raise secondary

    monkeypatch.setattr(service_module, "run_sync_in_worker", controlled_worker)
    monkeypatch.setattr(SessionOperationLease, "close", controlled_close)
    with structlog.testing.capture_logs() as logs:
        task = asyncio.create_task(service.archive_session(session.id))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        # The owned phase must remain joined before lease cleanup can occur.
        await asyncio.sleep(0)
        assert not task.done()
        assert not closed.is_set()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not closed.is_set()
        release.set()
        expected_error = failure_type if failure_phase == "phase" and not additional_close_failure else BaseExceptionGroup
        with pytest.raises(expected_error) as caught:
            await asyncio.wait_for(task, timeout=5)

    assert closed.is_set()
    if failure_phase == "phase" and not additional_close_failure:
        assert caught.value is secondary
        assert isinstance(secondary.__cause__, asyncio.CancelledError)
    else:
        failures = caught.value.exceptions
        if failure_phase == "phase":
            assert failures == (secondary, close_failure)
            assert isinstance(secondary.__cause__, asyncio.CancelledError)
        else:
            assert isinstance(failures[0], asyncio.CancelledError)
            if additional_close_failure:
                assert failures[1:] == ((secondary, close_failure) if failure_phase == "compensation" else (close_failure,))
            else:
                assert failures[1:] == (secondary,)
    assert not any(entry["event"] == "session_archive_secondary_failure" for entry in logs)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_function", [service_module.purge_archive_quarantine, service_module.retire_archive_quarantine])
async def test_archive_finalization_preserves_integrity_failure(service, monkeypatch, failure_function):
    session = await service.create_session("alice", "archive finalization", "local")
    worker = service_module.run_sync_in_worker
    failure = AuditIntegrityError("untrustworthy quarantine state")

    async def fail_finalization(function, *args, **kwargs):
        if function is failure_function:
            raise failure
        return await worker(function, *args, **kwargs)

    monkeypatch.setattr(service_module, "run_sync_in_worker", fail_finalization)
    with pytest.raises(AuditIntegrityError) as caught:
        await service.archive_session(session.id)
    assert caught.value is failure


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["worker", "projection"])
@pytest.mark.parametrize("logging_failure", [None, "ordinary", "tier1"])
async def test_cancelled_post_commit_failure_is_visible_after_worker_settles(service, monkeypatch, failure_phase, logging_failure):
    entered = asyncio.Event()
    release = asyncio.Event()
    settled = asyncio.Event()
    projected = []
    secondary = AuditIntegrityError("private persisted payload")
    diagnostic_failure = AuditIntegrityError("diagnostic integrity failure")

    async def controlled_worker(function):
        entered.set()
        await release.wait()
        settled.set()
        if failure_phase == "worker":
            raise secondary
        return function()

    def project(result):
        projected.append(result)
        raise secondary

    def fail_logging(event, **fields):
        assert event == "session_post_commit_secondary_failure"
        assert settled.is_set()
        if logging_failure == "tier1":
            raise diagnostic_failure
        raise RuntimeError("ordinary diagnostic failure")

    monkeypatch.setattr(service, "_run_sync", controlled_worker)
    if logging_failure is not None:
        monkeypatch.setattr(service._log, "error", fail_logging)
    with structlog.testing.capture_logs() as logs:
        task = asyncio.create_task(service._run_sync_with_post_commit_projection(lambda: 37, project=project))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not settled.is_set()
        assert projected == []
        release.set()
        expected_error = AuditIntegrityError if logging_failure == "tier1" else asyncio.CancelledError
        with pytest.raises(expected_error) as caught:
            await asyncio.wait_for(task, timeout=5)
    assert settled.is_set()
    assert projected == ([37] if failure_phase == "projection" else [])
    if logging_failure == "tier1":
        assert caught.value is diagnostic_failure
    else:
        assert caught.value.__cause__ is secondary
        if logging_failure == "ordinary":
            assert any("secondary-failure logging also failed with RuntimeError" in note for note in caught.value.__notes__)
        else:
            assert logs == [
                {
                    "event": "session_post_commit_secondary_failure",
                    "phase": failure_phase,
                    "error_type": "AuditIntegrityError",
                    "log_level": "error",
                }
            ]
