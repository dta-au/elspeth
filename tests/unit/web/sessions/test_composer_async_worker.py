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
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.process_recovery import ProcessRecovery
from elspeth.web.required_work import RequiredWorkSource, required_failure_leaves
from elspeth.web.sessions import composer_async_worker as worker_module
from elspeth.web.sessions.composer_async_worker import ComposerAsyncWorker
from elspeth.web.sessions.composer_operations import ComposerOperationError, ComposerTurnDeadlineExpired, composer_operation_request_hash
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
        quota=app.state.rate_limiter.composer_admission,
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
        assert current.result_json is not None
        error = ComposerOperationError.model_validate_json(current.result_json, strict=True)
        assert error.http_status == (499 if cause == "stop" else 503)
        assert error.body["detail"] == (
            "The composer request was stopped."
            if cause == "stop"
            else "The server stopped while composing this request. Reload to see what was saved, then resubmit."
        )
        assert held.dispatches == 1
        assert not worker._jobs
    finally:
        if task is not None and not task.done():
            held.release.set()
            await asyncio.wait_for(task, timeout=5)
        engine.dispose()


@pytest.mark.asyncio
async def test_running_deadline_keeps_truthful_durable_detail(tmp_path) -> None:
    app, service, engine, _composer = _file_app(tmp_path)
    held = _HeldComposer()
    app.state.composer_service = held
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    task = None
    try:
        record = await _admit(app, service, authority, operation_id=str(uuid4()), deadline_seconds=1.5)
        worker = _worker(app, authority)
        task = asyncio.create_task(worker.run_until_idle())
        await asyncio.wait_for(held.entered.wait(), timeout=5)
        running = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert running is not None and running.status == "running" and running.started_at is not None
        await asyncio.wait_for(task, timeout=8)
        terminal = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert terminal is not None and terminal.status == "failed" and terminal.failure_code == "deadline_expired"
        assert terminal.result_json is not None
        error = ComposerOperationError.model_validate_json(terminal.result_json, strict=True)
        assert error.http_status == 504 and error.error_type == "composer_operation_deadline_expired"
        assert error.body == {
            "error_type": "composer_operation_deadline_expired",
            "detail": "The composer request timed out. Reload to review the current session state before resubmitting.",
            "timeout_seconds": 1.5,
            "request_id": "accepted-request",
        }
        assert held.dispatches == 1 and not worker._jobs
    finally:
        if task is not None and not task.done():
            held.release.set()
            await asyncio.wait_for(task, timeout=5)
        engine.dispose()


@pytest.mark.asyncio
async def test_owned_running_timeout_keeps_deadline_when_sqlite_clock_precedes_fractional_deadline(tmp_path, monkeypatch) -> None:
    app, service, engine, _composer = _file_app(tmp_path)
    canonical_snapshot_factory = app.state.plugin_snapshot_factory
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    active_task = None
    active_held = None
    try:
        # SQLite reports whole seconds. Begin each bounded attempt just after
        # its real clock advances so the event-loop budget can expire while
        # its next read still precedes the admitted fractional deadline.
        for _attempt in range(3):
            with engine.connect() as conn:
                previous = database_now(conn)
            for _poll in range(150):
                await asyncio.sleep(0.01)
                with engine.connect() as conn:
                    current_time = database_now(conn)
                if current_time > previous:
                    break
            else:
                raise AssertionError("SQLite clock did not advance during bounded deadline setup")
            held = _HeldComposer()
            active_held = held
            app.state.composer_service = held
            record = await _admit(app, service, authority, operation_id=str(uuid4()), deadline_seconds=2.1)
            app.state.plugin_snapshot_factory = canonical_snapshot_factory
            worker = _worker(app, authority)
            selections = []
            owned_timeouts = []
            original_select = worker._failure_for
            original_owned_failure = worker_module._owned_deadline_failure
            admitted_operation_id = record.operation_id

            def observe_selection(
                current, now, exc, *, _operation_id=record.operation_id, _selections=selections, _original_select=original_select, **kwargs
            ):
                if current.operation_id == _operation_id:
                    _selections.append((now, exc))
                return _original_select(current, now, exc, **kwargs)

            def observe_owned_failure(
                original,
                *,
                timeout_scope,
                record,
                anchor,
                _operation_id=admitted_operation_id,
                _owned_timeouts=owned_timeouts,
                _original_owned_failure=original_owned_failure,
            ):
                if record.operation_id == _operation_id:
                    _owned_timeouts.append((timeout_scope, original))
                return _original_owned_failure(original, timeout_scope=timeout_scope, record=record, anchor=anchor)

            with monkeypatch.context() as patcher:
                patcher.setattr(worker, "_failure_for", observe_selection)
                patcher.setattr(worker_module, "_owned_deadline_failure", observe_owned_failure)
                active_task = asyncio.create_task(worker.run_until_idle())
                await asyncio.wait_for(held.entered.wait(), timeout=5)
                coordinator = worker._required_coordinators[(record.session_id, record.operation_id)]
                await asyncio.wait_for(active_task, timeout=8)
            terminal = authority.get(session_id=record.session_id, operation_id=record.operation_id)
            assert terminal is not None and terminal.status == "failed"
            assert held.dispatches == 1 and not worker._jobs
            assert len(selections) == 1
            observed_now, failure = selections[0]
            if observed_now >= record.deadline_at:
                assert terminal.failure_code == "deadline_expired"
                continue
            deadlines = tuple(leaf for leaf in required_failure_leaves(failure) if isinstance(leaf, ComposerTurnDeadlineExpired))
            assert len(deadlines) == 1
            original_timeout = deadlines[0].__cause__
            assert type(original_timeout) is TimeoutError
            assert len(owned_timeouts) == 1
            actual_scope, scoped_original = owned_timeouts[0]
            assert actual_scope is not None and actual_scope.expired() is True
            assert scoped_original is original_timeout
            turn_tickets = tuple(
                ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.REQUIRED_CONTINUATION_PRODUCER
            )
            assert len(turn_tickets) == 1 and turn_tickets[0].complete
            assert any(error is original_timeout for error in turn_tickets[0].errors)
            assert terminal.failure_code == "deadline_expired"
            assert terminal.result_json is not None
            error = ComposerOperationError.model_validate_json(terminal.result_json, strict=True)
            assert error.http_status == 504 and error.error_type == "composer_operation_deadline_expired"
            return
        raise AssertionError("No owned timeout crossed the real SQLite fractional deadline boundary in three attempts")
    finally:
        if active_task is not None and not active_task.done():
            assert active_held is not None
            active_held.release.set()
            await asyncio.wait_for(active_task, timeout=5)
        app.state.plugin_snapshot_factory = canonical_snapshot_factory
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
        assert current.result_json is not None
        error = ComposerOperationError.model_validate_json(current.result_json, strict=True)
        assert error.http_status == 504 and error.error_type == "composer_operation_deadline_expired"
        assert error.body == {
            "error_type": "composer_operation_deadline_expired",
            "detail": "The composer request timed out. Reload to review the current session state before resubmitting.",
            "timeout_seconds": 0.01,
            "request_id": "accepted-request",
        }
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
