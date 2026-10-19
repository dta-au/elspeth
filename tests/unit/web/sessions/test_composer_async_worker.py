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
@pytest.mark.parametrize("cancel_turn_caller", (False, True))
@pytest.mark.parametrize("failure_kind", ("settlement", "deadline", "sql-deadline", "settlement-with-deadline"))
async def test_worker_retains_late_original_cause_after_durable_call_cohort(
    tmp_path, monkeypatch: pytest.MonkeyPatch, cancel_turn_caller, failure_kind
):
    import json
    from dataclasses import replace
    from uuid import UUID

    from sqlalchemy import event

    from elspeth.contracts.errors import ComposerOwnedSettlementFailure
    from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence
    from elspeth.web.coordination import composer_operation_authority
    from elspeth.web.sessions import composer_turn
    from elspeth.web.sessions.composer_operations import ComposerTurnDeadlineExpired
    from elspeth.web.sessions.routes._helpers import _persist_turn_audit_cohort
    from tests.unit.web.sessions.test_freeform_route_custody import _ActualCustodyGate

    app, service, engine, composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    actual_compose = composer.compose
    captured = []
    turn_tasks = []
    actual_capture = worker_module.capture_failure_facts
    original_failure = RuntimeError("B7_LATE_ORIGINAL_CANARY")
    deadline_only = failure_kind in ("deadline", "sql-deadline")
    projection_sql = threading.local()
    observed_sql_deadlines = []

    def capture_projection_connection(conn, *_args):
        projection_sql.connection = conn

    event.listen(engine, "before_cursor_execute", capture_projection_connection)
    caller_marker = "B7_CALLER_CANCEL_CANARY"
    gate = _ActualCustodyGate(asyncio.get_running_loop())
    worker = _worker(app, authority)
    app.state.composer_async_worker = worker
    gate.install(app, service, engine, monkeypatch, "projection")

    async def compose(*args, **kwargs):
        # This existing composer fake returns one synthetic audited call. Settle
        # it once before the actual post-provider transaction can fail.
        turn_tasks.append(asyncio.current_task())
        result = await actual_compose(*args, **kwargs)
        await _persist_turn_audit_cohort(
            service,
            UUID(kwargs["session_id"]),
            (),
            result.llm_calls,
            tool_composition_state_id=None,
            llm_composition_state_id=None,
            plugin_crash_pending=False,
            required_audit=True,
            required_work=kwargs["required_work"].coordinator,
            session_operation_context=kwargs["session_operation_context"],
        )
        return replace(result, llm_calls=())

    def fail_projection(*args, **kwargs):
        gate.park_sql()
        if failure_kind == "sql-deadline":
            connection = projection_sql.connection
            actual_now = composer_operation_authority.database_now

            def expired_on_projection_connection(conn):
                return record.deadline_at if conn is connection else actual_now(conn)

            with monkeypatch.context() as cutoff:
                cutoff.setattr(composer_operation_authority, "database_now", expired_on_projection_connection)
                try:
                    composer_operation_authority.require_composer_operation_mutation_on_connection(connection, gate.leases[0].context)
                except ComposerTurnDeadlineExpired as expired:
                    observed_sql_deadlines.append(expired)
                    raise
            raise AssertionError("The actual SQL mutation boundary admitted an expired job")
        raise original_failure

    def capture(root):
        captured.append(root)
        return actual_capture(root)

    monkeypatch.setattr(composer, "compose", compose)
    monkeypatch.setattr(composer_turn, "_message_with_state_response", fail_projection)
    monkeypatch.setattr(worker_module, "capture_failure_facts", capture)
    drive = None
    try:
        record = await _admit(app, service, authority, operation_id=str(uuid4()))
        if failure_kind not in ("settlement", "sql-deadline"):
            deadline = ComposerTurnDeadlineExpired(
                session_id=record.session_id,
                operation_id=record.operation_id,
                remaining_seconds=-0.01,
                budget_seconds_at_running=85.0,
            )
            if failure_kind == "deadline":
                original_failure = deadline
            else:
                original_failure.__cause__ = deadline
        drive = asyncio.create_task(worker.run_until_idle())
        await asyncio.wait_for(gate.entered.wait(), timeout=10)
        if cancel_turn_caller:
            assert turn_tasks[0] is gate.owners[0], "Composer runs inline in the actual operation/turn caller"
            turn_tasks[0].cancel(caller_marker)
            await asyncio.sleep(0)
        assert not drive.done()
        gate.release.set()
        await asyncio.wait_for(drive, timeout=10)
        terminal = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert terminal.status == "failed"
        assert terminal.failure_code == ("deadline_expired" if deadline_only else "operation_failed")
        assert composer.calls == 1
        assert len(captured) == 1
        originals = []
        pending = captured.copy()
        while pending:
            root = pending.pop()
            if any(root is earlier for earlier in originals):
                continue
            originals.append(root)
            if isinstance(root, BaseExceptionGroup):
                pending.extend(root.exceptions)
            if root.__cause__ is not None:
                pending.append(root.__cause__)
            if root.__context__ is not None:
                pending.append(root.__context__)
        expected_original = observed_sql_deadlines[0] if failure_kind == "sql-deadline" else original_failure
        assert any(root is expected_original for root in originals)
        if failure_kind == "sql-deadline":
            assert len(observed_sql_deadlines) == 1
            assert expected_original.session_id == record.session_id and expected_original.operation_id == record.operation_id
            assert expected_original.remaining_seconds == 0
        assert any(isinstance(root, asyncio.CancelledError) and root.args == (caller_marker,) for root in originals) is cancel_turn_caller
        owned = [root for root in originals if type(root) is ComposerOwnedSettlementFailure]
        if deadline_only:
            assert owned == []
        else:
            (owned_primary,) = owned
            if cancel_turn_caller:
                assert isinstance(owned_primary.__cause__, BaseExceptionGroup)
                original_cause, joined_cancellation = owned_primary.__cause__.exceptions
                assert original_cause is original_failure
                assert joined_cancellation in originals and joined_cancellation.args == (caller_marker,)
            else:
                assert owned_primary.__cause__ is original_failure
        rows = await service.get_messages(record.session_id, limit=None)
        assert not [row for row in rows if row.role == "assistant"]
        calls = [item for row in rows for item in row.tool_calls or () if item.get("_kind") == "llm_call_audit"]
        assert len(calls) == 1
        private = [item for row in rows for item in row.tool_calls or () if item.get("_kind") == "composer_diagnostic_evidence"]
        evidence = [ComposerDiagnosticEvidence.from_envelope(item, expected_session_id=str(record.session_id)) for item in private]
        (failure_evidence,) = [item for item in evidence if item.status == "operation_failure"]
        selected = ComposerOperationError.model_validate_json(terminal.result_json, strict=True)
        assert selected.http_status == (504 if deadline_only else 500)
        assert json.loads(failure_evidence.arguments.content)["diagnostic_id"] == selected.diagnostic_id
        assert ("B7_LATE_ORIGINAL_CANARY" in failure_evidence.result.content) is not deadline_only
        assert ("Composer job budget expired before provider dispatch" in failure_evidence.result.content) is (failure_kind != "settlement")
        assert (caller_marker in failure_evidence.result.content) is cancel_turn_caller
        assert "B7_LATE_ORIGINAL_CANARY" not in terminal.result_json and caller_marker not in terminal.result_json
    finally:
        gate.release.set()
        if drive is not None and not drive.done():
            await asyncio.wait_for(drive, timeout=10)
        engine.dispose()
        event.remove(engine, "before_cursor_execute", capture_projection_connection)


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
@pytest.mark.parametrize("deadline_source", ["timeout", "watcher"])
async def test_owned_running_timeout_classifies_retained_preexpiry_sql_snapshot(tmp_path, monkeypatch, deadline_source) -> None:
    app, service, engine, _composer = _file_app(tmp_path)
    canonical_snapshot_factory = app.state.plugin_snapshot_factory
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    active_task = None
    active_held = None
    try:
        # Keep the original real-clock setup. A retained pre-await SQL snapshot
        # controls classification independently of the actual later cancellation
        # and settlement clocks; it never enters live terminal settlement.
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
        early_snapshots = []
        body_cancellations = []
        body_cancel_clocks = []
        handoff_clocks = []
        owned_timeouts = []
        watcher_deadlines = []
        post_read_remaining = []
        original_select = worker._failure_for
        original_owned_failure = worker_module._owned_deadline_failure
        original_watch = worker._watch
        original_read = worker_module.ComposerOperationWatchReader.read
        original_cadence = worker_module.ComposerOperationWatchReader.wait_watch_cadence
        original_timeout = asyncio.timeout
        original_compose = held.compose
        admitted_operation_id = record.operation_id

        async def observe_body_cancellation(*args, **kwargs):
            early_snapshots.append(authority.get_with_database_now(session_id=record.session_id, operation_id=record.operation_id))
            try:
                return await original_compose(*args, **kwargs)
            except asyncio.CancelledError as original:
                body_cancellations.append(original)
                body_cancel_clocks.append(
                    authority.get_with_database_now(session_id=record.session_id, operation_id=record.operation_id)[1]
                )
                raise

        def observe_selection(current, now, exc, **kwargs):
            if current.operation_id == admitted_operation_id:
                selections.append((now, exc))
            return original_select(current, now, exc, **kwargs)

        def observe_owned_failure(original, *, timeout_scope, record, anchor):
            converted = original_owned_failure(original, timeout_scope=timeout_scope, record=record, anchor=anchor)
            if record.operation_id == admitted_operation_id:
                owned_timeouts.append((timeout_scope, original))
                handoff_clocks.append(authority.get_with_database_now(session_id=record.session_id, operation_id=record.operation_id)[1])
            return converted

        async def observe_watch(*args, **kwargs):
            try:
                return await original_watch(*args, **kwargs)
            except ComposerTurnDeadlineExpired as watcher_failure:
                watcher_deadlines.append(watcher_failure)
                raise

        async def wait_for_owned_timeout(reader):
            # Retain the existing real cadence/private-cleanup provenance while
            # letting owned timeout win this independent control.
            while True:
                await original_cadence(reader)

        async def resume_read_after_deadline(reader):
            current, now = await original_read(reader)
            remaining = reader.anchor.remaining_seconds(monotonic_now=worker_module.time.monotonic())
            assert remaining > 0
            post_read_remaining.append(remaining)
            # The existing watcher control retains its actual SQL result across
            # scheduling delay to exercise post-read expiry, not submission cutoff.
            await asyncio.sleep(remaining + 0.02)
            return current, now

        def later_owned_timeout(delay):
            return original_timeout(None if delay is None else delay + 2.0)

        with monkeypatch.context() as patcher:
            patcher.setattr(held, "compose", observe_body_cancellation)
            patcher.setattr(worker, "_failure_for", observe_selection)
            patcher.setattr(worker_module, "_owned_deadline_failure", observe_owned_failure)
            patcher.setattr(worker, "_watch", observe_watch)
            if deadline_source == "timeout":
                patcher.setattr(worker_module.ComposerOperationWatchReader, "wait_watch_cadence", wait_for_owned_timeout)
            else:
                patcher.setattr(worker_module.ComposerOperationWatchReader, "read", resume_read_after_deadline)
                patcher.setattr(asyncio, "timeout", later_owned_timeout)
            active_task = asyncio.create_task(worker.run_until_idle())
            await asyncio.wait_for(held.entered.wait(), timeout=5)
            coordinator = worker._required_coordinators[(record.session_id, record.operation_id)]
            await asyncio.wait_for(active_task, timeout=8)
        terminal = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert terminal is not None and terminal.status == "failed"
        assert held.dispatches == 1 and not worker._jobs
        assert len(selections) == len(early_snapshots) == len(body_cancellations) == len(body_cancel_clocks) == len(handoff_clocks) == 1
        observed_now, failure = selections[0]
        early_current, early_now = early_snapshots[0]
        assert early_current is not None and early_current.status == "running"
        print(
            "deadline_control_clocks",
            deadline_source,
            early_now.isoformat(),
            record.deadline_at.isoformat(),
            body_cancel_clocks[0].isoformat(),
            handoff_clocks[0].isoformat(),
            observed_now.isoformat(),
        )
        assert early_now < record.deadline_at
        assert early_now <= body_cancel_clocks[0] <= handoff_clocks[0] <= observed_now
        deadlines = tuple(leaf for leaf in required_failure_leaves(failure) if isinstance(leaf, ComposerTurnDeadlineExpired))
        assert len(deadlines) == 1, tuple(required_failure_leaves(failure))
        assert len(owned_timeouts) == 1
        actual_scope, scoped_original = owned_timeouts[0]
        assert actual_scope is not None
        turn_tickets = tuple(
            ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.REQUIRED_CONTINUATION_PRODUCER
        )
        assert len(turn_tickets) == 1 and turn_tickets[0].complete
        assert any(error is scoped_original for error in turn_tickets[0].errors)
        if deadline_source == "timeout":
            original_timeout_error = deadlines[0].__cause__
            assert type(original_timeout_error) is TimeoutError
            assert actual_scope.expired() is True
            assert scoped_original is original_timeout_error
            assert original_timeout_error.__cause__ is body_cancellations[0]
            assert not watcher_deadlines and not post_read_remaining
        else:
            assert actual_scope.expired() is False
            assert isinstance(scoped_original, asyncio.CancelledError)
            assert scoped_original is body_cancellations[0]
            assert len(watcher_deadlines) == 1 and deadlines[0] is watcher_deadlines[0]
            assert len(post_read_remaining) == 1 and post_read_remaining[0] > 0
            assert deadlines[0].session_id == record.session_id
            assert deadlines[0].operation_id == record.operation_id
            assert deadlines[0].remaining_seconds <= 0
            assert deadlines[0].budget_seconds_at_running > 0
        # Exercise retained-preexpiry-snapshot classification independently after
        # physical cleanup, with actual joined failure objects. This is not a
        # fresh SQL sample at cancellation; no live coordinator is altered.
        assert (record.session_id, record.operation_id) not in worker._required_coordinators
        assert original_select(early_current, early_now, failure).failure_code == "deadline_expired"
        assert original_select(early_current, early_now, scoped_original).failure_code != "deadline_expired"
        assert terminal.failure_code == "deadline_expired"
        assert terminal.result_json is not None
        error = ComposerOperationError.model_validate_json(terminal.result_json, strict=True)
        assert error.http_status == 504 and error.error_type == "composer_operation_deadline_expired"
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
