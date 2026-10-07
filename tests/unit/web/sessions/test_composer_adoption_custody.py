"""Owned adoption failure evidence survives cancellation and reaches durable terminal."""

from __future__ import annotations

import asyncio
import errno
import threading
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

from elspeth.contracts.errors import ComposerOwnedSettlementFailure
from elspeth.web.async_workers import outstanding_admissions
from elspeth.web.coordination import lifecycle
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions.composer_async_worker import _authoritative_failure, _failure_leaves
from elspeth.web.sessions.composer_operation_errors import request_cancelled_error
from elspeth.web.sessions.composer_operations import COMPOSER_SHUTDOWN, ComposerOperationError
from tests.unit.web.sessions.test_composer_async_worker import _admit, _file_app, _worker


def _failure(kind: str) -> BaseException:
    if kind == "sql":
        return OperationalError("CAS", {}, RuntimeError("private SQL failure"))
    if kind == "accounting":
        original = RuntimeError("private accounting failure")
        failure = ComposerOwnedSettlementFailure()
        failure.__cause__ = original
        return failure
    if kind == "disk":
        return OSError(errno.ENOSPC, "private disk failure")
    if kind == "nested":
        return ExceptionGroup("owned nested failures", [ValueError("ordinary"), _failure("sql"), _failure("accounting")])
    return OSError(errno.ENOENT, "ordinary missing file")


@pytest.mark.parametrize("kind", ("sql", "accounting", "disk", "nested", "ordinary"))
def test_lifecycle_preserves_nominal_secondary_priority_without_promoting_ordinary(kind: str) -> None:
    secondary = _failure(kind)
    primary = RuntimeError("ordinary renewal failure")
    escaped = lifecycle._preserve_failures(primary, (secondary,), note_prefix="close", group_message="owned close")
    assert _authoritative_failure(escaped) is (kind != "ordinary")
    if kind != "ordinary":
        assert any(leaf is secondary for leaf in _failure_leaves(escaped)) or isinstance(secondary, BaseExceptionGroup)
    else:
        assert escaped is primary


@pytest.mark.parametrize("kind", ("sql", "accounting", "disk", "nested", "ordinary"))
def test_cancelled_adoption_preserves_nominal_failure_instances(kind: str) -> None:
    failure = _failure(kind)
    cancelled = asyncio.CancelledError(COMPOSER_SHUTDOWN)
    escaped = lifecycle._failure_after_cancellation(cancelled, (("adoption SQL", failure),), group_message="owned adoption")
    assert _authoritative_failure(escaped) is (kind != "ordinary")
    assert escaped is (cancelled if kind == "ordinary" else failure)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("sql", "accounting", "ordinary"))
@pytest.mark.parametrize("cancel_site", ("owner", "sql_child"))
async def test_actual_worker_cancelled_post_start_adoption_retains_sql_custody_and_priority(
    tmp_path, monkeypatch, kind: str, cancel_site: str
) -> None:
    app, service, engine, composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    entered = threading.Event()
    release = threading.Event()
    failure = _failure(kind)

    def failed_cas(context):
        entered.set()
        assert release.wait(timeout=10)
        raise failure

    monkeypatch.setattr(service.session_operation_authority, "compare_and_swap", failed_cas)
    worker = _worker(app, authority)
    task = None
    baseline = outstanding_admissions()
    try:
        record = await _admit(app, service, authority, operation_id=str(uuid4()))
        task = asyncio.create_task(worker.run_until_idle())
        for _ in range(500):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        started = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert started is not None and started.status == "running" and started.attempt == 1
        authority.request_cancel(session_id=record.session_id, operation_id=record.operation_id, cancelled_failure=request_cancelled_error)
        owned = tuple(worker._jobs.values())
        assert len(owned) == 1
        cancelled_task = owned[0]
        if cancel_site == "sql_child":
            children = [task for task in asyncio.all_tasks() if task.get_name() == "session-operation-adopt-compare-and-swap"]
            assert len(children) == 1
            cancelled_task = children[0]
        cancelled_task.cancel(COMPOSER_SHUTDOWN)
        await asyncio.sleep(0.02)
        cancelled_task.cancel(COMPOSER_SHUTDOWN)
        await asyncio.sleep(0.02)
        assert not owned[0].done() and len(worker._jobs) == 1
        assert outstanding_admissions() >= baseline + 1 and composer.calls == 0
        release.set()
        await asyncio.wait_for(task, timeout=5)
        terminal = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert terminal is not None and terminal.status == "failed"
        error = ComposerOperationError.model_validate_json(terminal.result_json)
        assert error.http_status == (503 if kind == "sql" else 500 if kind == "accounting" else 499)
        assert (terminal.failure_code == "request_cancelled") is (kind == "ordinary")
        assert terminal.attempt == 1 and composer.calls == 0 and not worker._jobs
        assert outstanding_admissions() == baseline
    finally:
        release.set()
        if task is not None and not task.done():
            await task
        engine.dispose()
