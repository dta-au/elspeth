"""Production-path detached work and exact progress custody controls."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from uuid import uuid4

import pytest

from elspeth.contracts.composer_progress import ComposerProgressEvent
from elspeth.web.auth.models import UserIdentity
from elspeth.web.composer.progress import ComposerProgressRegistry
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.process_recovery import ProcessRecovery
from elspeth.web.sessions.composer_async_worker import ComposerAsyncWorker
from elspeth.web.sessions.composer_operations import composer_operation_request_hash
from elspeth.web.sessions.schemas import SendMessageRequest
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog
from tests.unit.web.sessions.test_freeform_route_custody import _AuditedComposer
from tests.unit.web.sessions.test_freeform_route_custody import _file_app as _fixture_file_app


class _BudgetedComposer(_AuditedComposer):
    async def compose(self, *args, budget_seconds: float | None = None, **kwargs):
        assert budget_seconds is not None and budget_seconds > 0
        return await super().compose(*args, **kwargs)


class _HeldComposer(_BudgetedComposer):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.dispatches = 0

    async def compose(self, *args, **kwargs):
        self.dispatches += 1
        self.entered.set()
        await self.release.wait()
        return await super().compose(*args, **kwargs)


def _file_app(tmp_path):
    app, service, engine, _composer = _fixture_file_app(tmp_path)
    composer = _BudgetedComposer()
    app.state.composer_service = composer
    return app, service, engine, composer


@dataclass
class _SnapshotFactory:
    original: object

    def for_user_id(self, user_id):
        return self.original(UserIdentity(user_id=user_id, username=user_id))


@pytest.mark.asyncio
async def test_exact_progress_recompose_custody_and_finished_lease() -> None:
    registry = ComposerProgressRegistry()
    first = await registry.start_request("session", "alice")
    one = await registry.claim_request(
        session_id="session",
        user_id="alice",
        request_id="same-user",
        lease=first,
        operation_id="first",
        session_operation_id="sol-first",
        session_operation_epoch=1,
    )
    second = await registry.start_request("session", "alice")
    two = await registry.claim_request(
        session_id="session",
        user_id="alice",
        request_id="same-user",
        lease=second,
        operation_id="second",
        session_operation_id="sol-second",
        session_operation_epoch=2,
    )
    event = ComposerProgressEvent(phase="calling_model", headline="Safe model call")
    await two(event)
    await one(ComposerProgressEvent(phase="using_tools", headline="Stale tools"))
    assert (
        await registry.get_for_operation(
            session_id="session", user_id="alice", operation_id="first", session_operation_id="sol-first", session_operation_epoch=1
        )
        is None
    )
    snapshot = await registry.get_for_operation(
        session_id="session", user_id="alice", operation_id="second", session_operation_id="sol-second", session_operation_epoch=2
    )
    assert snapshot is not None and snapshot.headline == event.headline and snapshot.request_token == second.request_token
    await registry.finish_request(second)
    await two(ComposerProgressEvent(phase="using_tools", headline="After lease finish"))
    assert (await registry.get_latest("session")).headline == event.headline
    await registry.finish_request(first)


def _worker(app, authority) -> ComposerAsyncWorker:
    app.state.plugin_snapshot_factory = _SnapshotFactory(app.state.plugin_snapshot_factory)
    instance_draining = threading.Event()
    return ComposerAsyncWorker(
        app=app,
        authority=authority,
        concurrency=2,
        scan_interval_seconds=0.01,
        claim_lease_seconds=30,
        drain_seconds=0.05,
        owner_instance_id=app.state.session_service.session_operation_owner_instance_id,
        process_recovery=ProcessRecovery(watchdog=OwnedTestProcessWatchdog(instance_draining), instance_draining=instance_draining),
        instance_draining=instance_draining,
    )


async def _admit(app, service, authority, *, operation_id: str, deadline_seconds: float = 85.0):
    session = await service.create_session("alice", "Owned job", app.state.settings.auth_provider)
    request = SendMessageRequest(operation_id=operation_id, content="Answer plainly", state_id=None)
    record, fresh = authority.admit(
        session_id=session.id,
        operation_id=operation_id,
        kind="compose_message",
        request_hash=composer_operation_request_hash(session_id=session.id, kind="compose_message", request=request),
        actor_user_id="alice",
        request_id="accepted-request",
        base_state_id=None,
        request_json=request.model_dump_json(),
        deadline_seconds=deadline_seconds,
        max_nonterminal=64,
        auth_provider_type=app.state.settings.auth_provider,
    )
    assert fresh
    return record


@pytest.mark.asyncio
async def test_worker_completes_admitted_job_with_one_provider_call(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.web.sessions import composer_async_worker as worker_module

    failures = []
    project = worker_module.project_composer_operation_error

    def capture_failure(exc, **kwargs):
        failures.append(exc)
        return project(exc, **kwargs)

    monkeypatch.setattr(worker_module, "project_composer_operation_error", capture_failure)
    app, service, engine, composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    try:
        record = await _admit(app, service, authority, operation_id=str(uuid4()))
        worker = _worker(app, authority)
        await worker.run_until_idle()
        current = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert not failures, failures
        assert current is not None and current.status == "completed", current
        assert composer.calls == 1
        assert len([row for row in await service.get_messages(record.session_id, limit=None) if row.role == "user"]) == 1
        assert not worker._jobs
        saved_hash = current.result_sha256
        from elspeth.web.sessions.composer_operation_errors import request_cancelled_error

        late = authority.request_cancel(
            session_id=record.session_id, operation_id=record.operation_id, cancelled_failure=request_cancelled_error
        )
        assert late is not None and late.status == "completed" and late.result_sha256 == saved_hash
        assert late.cancel_requested_at is None
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ("stop", "shutdown"))
async def test_running_stop_and_shutdown_have_distinct_durable_causes(tmp_path, cause: str) -> None:
    from elspeth.web.sessions.composer_operation_errors import request_cancelled_error

    app, service, engine, _composer = _file_app(tmp_path)
    held = _HeldComposer()
    app.state.composer_service = held
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    task = None
    try:
        record = await _admit(app, service, authority, operation_id=str(uuid4()))
        worker = _worker(app, authority)
        task = asyncio.create_task(worker.run_until_idle())
        await asyncio.wait_for(held.entered.wait(), timeout=5)
        if cause == "stop":
            authority.request_cancel(
                session_id=record.session_id, operation_id=record.operation_id, cancelled_failure=request_cancelled_error
            )
            assert worker.signal_local_cancel(session_id=record.session_id, operation_id=record.operation_id)
        else:
            await worker.stop()
        await asyncio.wait_for(task, timeout=5)
        current = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert current is not None and current.status == "failed"
        assert current.failure_code == ("request_cancelled" if cause == "stop" else "worker_lost")
        assert (current.cancel_requested_at is not None) == (cause == "stop")
        assert held.dispatches == 1
        assert not worker._jobs
    finally:
        if task is not None and not task.done():
            held.release.set()
            await asyncio.wait_for(task, timeout=5)
        engine.dispose()


@pytest.mark.asyncio
async def test_queued_deadline_reaper_makes_zero_provider_calls(tmp_path) -> None:
    app, service, engine, composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    try:
        record = await _admit(app, service, authority, operation_id=str(uuid4()), deadline_seconds=0.01)
        for _ in range(50):
            _current, database_now = authority.get_with_database_now(session_id=record.session_id, operation_id=record.operation_id)
            if database_now >= record.deadline_at:
                break
            await asyncio.sleep(0.05)
        assert database_now >= record.deadline_at
        await _worker(app, authority).run_until_idle()
        current = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert current is not None and current.failure_code == "deadline_expired"
        assert current.started_at is None
        assert composer.calls == 0
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_cancel_unclaimed_job_never_dispatches_provider(tmp_path) -> None:
    from elspeth.web.sessions.composer_operation_errors import request_cancelled_error

    app, service, engine, composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    try:
        record = await _admit(app, service, authority, operation_id=str(uuid4()))
        authority.request_cancel(session_id=record.session_id, operation_id=record.operation_id, cancelled_failure=request_cancelled_error)
        await _worker(app, authority).run_until_idle()
        current = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert current is not None and current.failure_code == "request_cancelled"
        assert composer.calls == 0
    finally:
        engine.dispose()
