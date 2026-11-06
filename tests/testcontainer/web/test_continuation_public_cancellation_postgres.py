"""Public Continuation cancellation at actual P4 and proposal SQL boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import sys
import threading
import traceback
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from litellm import ModelResponse
from sqlalchemy import event, func, select, update
from sqlalchemy.exc import OperationalError
from tests.helpers.continuation_app import ScriptedSDK, continuation_app, end_admit, install_sdk, settle, tool_call
from tests.integration.web.composer.test_freeform_pipeline_planner import _pipeline
from tests.testcontainer.web.test_composer_stream_replica_postgres import _provision

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web import async_workers as worker_module
from elspeth.web.composer.provider_audit_source import ProviderAuditSource, provider_attempt_audit_drafts_for_source
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.contracts import SessionOperationKind
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions import continuation_driver as driver_module
from elspeth.web.sessions import continuation_p4 as p4_module
from elspeth.web.sessions import service as service_module
from elspeth.web.sessions.composer_operations import COMPOSER_CANCEL_REQUESTED
from elspeth.web.sessions.models import (
    blobs_table,
    chat_messages_table,
    composer_async_operations_table,
    composer_continuation_effects_table,
    composition_proposals_table,
    composition_states_table,
    proposal_events_table,
    quota_provider_attempts_table,
    session_operation_fences_table,
    token_usage_ledger_table,
)

pytestmark = pytest.mark.testcontainer


@pytest.fixture
def pg_continuation_overrides(composer_http_databases: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    _provision(composer_http_databases)
    for directory in (tmp_path, tmp_path / "blobs", tmp_path / "payloads"):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    monkeypatch.setenv("OPENAI_API_KEY", "offline-pg-http-test-only-not-a-provider-credential")
    return {
        "session_db_url": composer_http_databases.session_runtime_url,
        "landscape_url": composer_http_databases.landscape_runtime_url,
        "deployment_target": "docker-compose",
        "deployment_state_mode": "external-postgresql",
        "host": "0.0.0.0",
        "secret_key": "external-deployment-secret-key-with-more-than-32-bytes",
        "shareable_link_signing_key": bytes(range(32)),
        "composer_async_claim_lease_seconds": 5,
    }


def _early_clean() -> dict[str, Any]:
    return {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "The draft is coherent.", "note": None}


class _CostedScriptedSDK(ScriptedSDK):
    """The scripted physical provider reports its actual deterministic cost."""

    async def __call__(self, **kwargs: Any) -> object:
        response = await super().__call__(**kwargs)
        assert isinstance(response, ModelResponse) and response.usage is not None
        response.usage.cost = 0.01
        return response


def _leaves(error: BaseException) -> tuple[BaseException, ...]:
    if isinstance(error, BaseExceptionGroup):
        return tuple(leaf for child in error.exceptions for leaf in _leaves(child))
    return (error,)


async def _seed_pipeline_request(harness: Any, tmp_path: Path, *, trust_mode: str) -> None:
    source = tmp_path / "blobs" / str(harness.session_id) / "input.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("name\nAda\n", encoding="utf-8")
    (tmp_path / "outputs" / str(harness.session_id)).mkdir(parents=True, exist_ok=True)
    await harness.app.state.session_service.update_composer_preferences(
        harness.session_id, trust_mode=trust_mode, density_default="high", actor="test"
    )


def _scripted_pipeline_sdk(tmp_path: Path, app_ref: list[Any], session_ref: list[Any], *, auto: bool) -> ScriptedSDK:
    def author(app: Any) -> tuple[str, list[dict[str, Any]]]:
        pipeline = _pipeline(tmp_path, str(session_ref[0]))
        return "Building the pipeline.", [tool_call(app, "u49-pipeline-1", "set_pipeline", pipeline)]

    steps: list[tuple[str, Any]] = [("primary", author)]
    if auto:
        steps.append(("end", _early_clean()))
    steps.extend((("primary", ("The draft was saved.", None)), ("end", end_admit())))
    return _CostedScriptedSDK(steps, app_ref=app_ref)


def _rows(harness: Any, operation_id: str) -> dict[str, tuple[Any, ...]]:
    sid = str(harness.session_id)
    tables = {
        "states": composition_states_table,
        "proposals": composition_proposals_table,
        "proposal_events": proposal_events_table,
        "messages": chat_messages_table,
        "physical": quota_provider_attempts_table,
        "usage": token_usage_ledger_table,
        "effects": composer_continuation_effects_table,
        "fences": session_operation_fences_table,
        "blobs": blobs_table,
    }
    with harness.app.state.session_engine.connect() as conn:
        result = {name: tuple(conn.execute(select(table).where(table.c.session_id == sid)).all()) for name, table in tables.items()}
        result["operation"] = tuple(
            conn.execute(
                select(composer_async_operations_table).where(
                    composer_async_operations_table.c.session_id == sid,
                    composer_async_operations_table.c.operation_id == operation_id,
                )
            ).all()
        )
    return result


def _assert_provider_cohort(rows: dict[str, tuple[Any, ...]], driver: Any, sdk: ScriptedSDK, *, expected: int) -> None:
    assert len(sdk.requests) == len(rows["physical"]) == len(rows["usage"]) == expected
    calls = driver.phases.runtime.live.recorder.llm_calls
    assert len(calls) == expected
    assert {attempt.attempt_id for attempt in rows["physical"]} == {call.call_id for call in calls}
    assert {usage.entry_id for usage in rows["usage"]} == {attempt.ledger_entry_id for attempt in rows["physical"]}
    audit_rows = [row for row in rows["messages"] if row.role == "audit"]
    matched: set[str] = set()
    for call in calls:
        attempt = next(row for row in rows["physical"] if row.attempt_id == call.call_id)
        source = ProviderAuditSource(attempt.session_id, attempt.operation_id, attempt.operation_epoch)
        for draft in provider_attempt_audit_drafts_for_source(call, source):
            equivalent = [row for row in audit_rows if deep_thaw(row.tool_calls) == deep_thaw(draft.tool_calls)]
            assert len(equivalent) == 1
            matched.add(equivalent[0].id)
    assert len(matched) == 2 * expected
    public = [row for row in audit_rows if row.tool_calls and any(envelope.get("_kind") == "llm_call_audit" for envelope in row.tool_calls)]
    private = [
        row
        for row in audit_rows
        if row.tool_calls
        and any(
            envelope.get("_kind") == "composer_diagnostic_evidence" and envelope.get("evidence", {}).get("status") == "provider_request"
            for envelope in row.tool_calls
        )
    ]
    assert len(public) == len(private) == expected
    assert {row.id for row in (*public, *private)} == matched


def _assert_released_and_joined(harness: Any, rows: dict[str, tuple[Any, ...]], *, diagnostic: dict[str, Any] | None = None) -> None:
    assert not harness.app.state.composer_async_worker._jobs, diagnostic
    assert rows["fences"] and rows["fences"][-1].released_at is not None, diagnostic


def _native_fence_diagnostic(
    harness: Any, driver: Any, outcomes: list[BaseException | None], rows: dict[str, tuple[Any, ...]]
) -> dict[str, Any]:
    def error_detail(error: BaseException | None) -> tuple[tuple[str, str, str | None, str | None], ...]:
        if error is None:
            return ()
        details: list[tuple[str, str, str | None, str | None]] = []
        for leaf in _leaves(error):
            sqlstate = leaf.orig.sqlstate if isinstance(leaf, OperationalError) and isinstance(leaf.orig, psycopg.Error) else None
            statement = leaf.statement if isinstance(leaf, OperationalError) else None
            details.append((type(leaf).__name__, str(leaf).splitlines()[0][:200], sqlstate, None if statement is None else statement[:160]))
        return tuple(details)

    lease = driver.lease
    coordinator = lease.required_work
    close_task = lease._close_task
    close_error = None if close_task is None or not close_task.done() or close_task.cancelled() else close_task.exception()
    fence = lease.context.fence
    with harness.app.state.session_engine.connect() as conn:
        observed_db_now = conn.scalar(select(func.now()))
    return {
        "operation": tuple((row.status, row.failure_code, row.phase, row.cancel_requested_at) for row in rows["operation"]),
        "job_outcomes": tuple(error_detail(error) for error in outcomes),
        "lease": (str(lease.disposition), lease.closed, error_detail(lease.renewal_error)),
        "close_task": (
            close_task is not None,
            None if close_task is None else close_task.done(),
            None if close_task is None else close_task.cancelled(),
            error_detail(close_error),
        ),
        "required_all_completed": None if coordinator is None else coordinator.all_completed,
        "incomplete_tickets": (
            ()
            if coordinator is None
            else tuple((ticket.key.source.name, ticket.key.order) for ticket in coordinator.tickets if not ticket.complete)
        ),
        "fence_owner": (fence.operation_id, fence.lease_token, fence.operation_epoch),
        "fence_db": tuple(
            (row.operation_id, row.lease_token, row.operation_epoch, row.lease_expires_at, row.released_at) for row in rows["fences"]
        ),
        "db_now": observed_db_now,
    }


def _observe_worker_outcomes(harness: Any, monkeypatch: pytest.MonkeyPatch) -> list[BaseException | None]:
    worker = harness.app.state.composer_async_worker
    actual_finished = worker._job_finished
    outcomes: list[BaseException | None] = []

    def observe_finished(key: Any, task: asyncio.Task[None]) -> None:
        outcomes.append(asyncio.CancelledError("worker cancelled before SQL") if task.cancelled() else task.exception())
        actual_finished(key, task)

    monkeypatch.setattr(worker, "_job_finished", observe_finished)
    return outcomes


def _observe_settlement(
    monkeypatch: pytest.MonkeyPatch, *, after_sql: asyncio.Event, stop_committed: asyncio.Event
) -> list[BaseException | None]:
    actual_settle = driver_module._SegmentDriver.settle
    originals: list[BaseException | None] = []

    async def observe_settle(driver: Any, record: Any, original: BaseException | None = None) -> Any:
        originals.append(original)
        if after_sql.is_set() and not stop_committed.is_set():
            await stop_committed.wait()
        return await actual_settle(driver, record, original)

    monkeypatch.setattr(driver_module._SegmentDriver, "settle", observe_settle)
    return originals


def _sample_pre_sql_wait(sdk: ScriptedSDK) -> dict[str, Any]:
    tasks = [
        (task.get_name(), "".join("".join(traceback.format_stack(frame, limit=8)) for frame in task.get_stack(limit=8)))
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    ]
    frames = sys._current_frames()
    worker_threads: list[tuple[str, str]] = []
    classifier_inputs: list[tuple[str, int, str, str]] = []
    for thread in threading.enumerate():
        if thread is threading.main_thread() or thread.ident is None or thread.ident not in frames:
            continue
        frame = frames[thread.ident]
        stack = "".join(traceback.format_stack(frame, limit=16))
        if "elspeth" in stack or "sqlalchemy" in stack:
            worker_threads.append((thread.name, stack))
        while frame is not None:
            if frame.f_code.co_filename.endswith("credential_material.py") and frame.f_code.co_name in (
                "<genexpr>",
                "_classify_string",
            ):
                value = frame.f_locals.get("value")
                if isinstance(value, str):
                    masked_preview = "".join("x" if char.isalnum() else char for char in value[:80])
                    encoded = value.encode("utf-8", errors="replace")
                    classifier_inputs.append(
                        (
                            str(frame.f_locals.get("_name", frame.f_code.co_name)),
                            len(encoded),
                            hashlib.sha256(encoded).hexdigest(),
                            masked_preview,
                        )
                    )
            frame = frame.f_back
    return {"physical_calls": sdk.roles, "tasks": tasks, "worker_threads": worker_threads, "classifier_inputs": classifier_inputs}


async def _wait_for_sql_entry(
    entered: threading.Event,
    outcomes: list[BaseException | None],
    sdk: ScriptedSDK,
    harness: Any,
    operation_id: str,
    settlement_originals: list[BaseException | None],
) -> None:
    loop = asyncio.get_running_loop()
    start = loop.time()
    first_sample: dict[str, Any] | None = None
    while not entered.is_set():
        if outcomes:
            failure = outcomes[0]
            if failure is None:
                async with harness.client() as client:
                    response = await client.get(f"/api/sessions/{harness.session_id}/operations/{operation_id}")
                body = response.json()
                public_state = {
                    "http_status": response.status_code,
                    "status": body.get("status"),
                    "phase": body.get("phase"),
                    "error": body.get("error"),
                    "result_keys": sorted(body["result"]) if isinstance(body.get("result"), dict) else None,
                }
                original_labels = [
                    tuple(f"{type(leaf).__name__}: {(str(leaf).splitlines() or [''])[0][:160]}" for leaf in _leaves(original))
                    if original is not None
                    else ("None",)
                    for original in settlement_originals
                ]
                raise AssertionError(
                    f"Current Continuation worker completed without target SQL; public_state={public_state!r}; "
                    f"sdk_roles={sdk.roles!r}; remaining_roles={[role for role, _payload in sdk.steps]!r}; "
                    f"settlement_originals={original_labels!r}; sample={first_sample!r}"
                )
            raise AssertionError(f"Current Continuation worker failed before target SQL: {failure!r}; sample={first_sample!r}") from failure
        elapsed = loop.time() - start
        if first_sample is None and elapsed >= 8:
            first_sample = _sample_pre_sql_wait(sdk)
            print(f"u49_pre_sql_sample_8s={first_sample!r}")
        if elapsed >= 360:
            raise AssertionError(f"Target SQL not entered by 360s; sample_8s={first_sample!r}; final={_sample_pre_sql_wait(sdk)!r}")
        await asyncio.sleep(0.05)


async def _assert_public_failure(harness: Any, operation_id: str, snapshot: Any, rows: dict[str, tuple[Any, ...]]) -> dict[str, Any]:
    assert snapshot.status == "failed" and snapshot.error is not None
    (operation,) = rows["operation"]
    assert snapshot.operation_id == operation_id and snapshot.kind == operation.kind and snapshot.phase == operation.phase
    assert operation.status == snapshot.status and operation.failure_code == snapshot.error.failure_code
    assert snapshot.cancel_requested is (operation.cancel_requested_at is not None)
    async with harness.client() as client:
        history = await client.get(f"/api/sessions/{harness.session_id}/operations", params={"operation_id": operation_id})
    assert history.status_code == 200 and history.headers["cache-control"] == "no-store"
    (failure,) = history.json()["items"]
    assert failure["operation_id"] == operation_id
    assert failure["failure_code"] == snapshot.error.failure_code
    assert failure["error_type"] == snapshot.error.error_type
    assert failure["request_id"] == operation.request_id
    assert failure["diagnostic_id"] == snapshot.error.diagnostic_id
    service = harness.app.state.session_service
    lease = await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=harness.session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    )
    async with lease:
        await asyncio.wait_for(
            service.add_message(
                harness.session_id,
                "system",
                "post cancellation lock probe",
                writer_principal="route_system_message",
                session_operation_context=lease.context,
            ),
            3,
        )
    return failure


def _hold_next_action_after_sql(
    monkeypatch: pytest.MonkeyPatch, source: RequiredWorkSource, observed: list[Any] | None = None
) -> tuple[asyncio.Event, asyncio.Event]:
    actual_finish = service_module.run_required_sql_finish_once
    actual_record = driver_module._SegmentDriver.record
    after_sql, stop_committed = asyncio.Event(), asyncio.Event()

    async def observe_finish(ticket: Any, func: Any, *args: Any, **kwargs: Any) -> Any:
        outcome = await actual_finish(ticket, func, *args, **kwargs)
        if ticket.key.source is source:
            if observed is not None:
                observed.append(outcome)
            after_sql.set()
        return outcome

    async def hold_record(driver: Any) -> Any:
        if after_sql.is_set() and not stop_committed.is_set():
            await stop_committed.wait()
        return await actual_record(driver)

    monkeypatch.setattr(service_module, "run_required_sql_finish_once", observe_finish)
    monkeypatch.setattr(driver_module._SegmentDriver, "record", hold_record)
    return after_sql, stop_committed


def _hold_proposal_return_after_sql(monkeypatch: pytest.MonkeyPatch) -> tuple[asyncio.Event, asyncio.Event]:
    actual_create = service_module.SessionServiceImpl.create_pipeline_composition_proposal
    after_sql, stop_committed = asyncio.Event(), asyncio.Event()

    async def hold_return(service: Any, **kwargs: Any) -> Any:
        proposal = await actual_create(service, **kwargs)
        after_sql.set()
        await stop_committed.wait()
        return proposal

    monkeypatch.setattr(service_module.SessionServiceImpl, "create_pipeline_composition_proposal", hold_return)
    return after_sql, stop_committed


def _observe_cancel_mutation_entry(monkeypatch: pytest.MonkeyPatch, operation_id: str) -> threading.Event:
    actual_cancel = ComposerAsyncOperationAuthority.request_owner_cancel
    entered = threading.Event()

    def observe_cancel(authority: ComposerAsyncOperationAuthority, **kwargs: Any) -> Any:
        if kwargs["operation_id"] == operation_id:
            entered.set()
        return actual_cancel(authority, **kwargs)

    monkeypatch.setattr(ComposerAsyncOperationAuthority, "request_owner_cancel", observe_cancel)
    return entered


async def _queue_public_cancel(client: Any, harness: Any, operation_id: str, entered: threading.Event) -> asyncio.Task[Any]:
    task = asyncio.create_task(client.post(f"/api/sessions/{harness.session_id}/operations/{operation_id}/cancel"))
    async with asyncio.timeout(3):
        while not entered.is_set():
            await asyncio.sleep(0.01)
    assert not task.done(), "Stop was not queued behind the actual SQL session lock"
    return task


async def _finish_queued_public_cancel(client: Any, harness: Any, operation_id: str, task: asyncio.Task[Any]) -> None:
    async with asyncio.timeout(10):
        first = await task
    assert first.status_code == 202, first.text
    responses = [first]
    for _ in range(2):
        responses.append(await client.post(f"/api/sessions/{harness.session_id}/operations/{operation_id}/cancel"))
    for response in responses:
        assert response.status_code == 202, response.text
        snapshot = response.json()
        assert snapshot["operation_id"] == operation_id and snapshot["cancel_requested"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sql_fault", "native_interrupt"),
    ((False, False), (True, False), (False, True)),
    ids=("queued-commit", "queued-rollback", "native-p4-and-queued-stop"),
)
async def test_public_stop_queued_behind_actual_auto_p4_sql(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pg_continuation_overrides: dict[str, Any],
    sql_fault: bool,
    native_interrupt: bool,
) -> None:
    app_ref: list[Any] = []
    session_ref: list[Any] = []
    sdk = _scripted_pipeline_sdk(tmp_path, app_ref, session_ref, auto=True)
    install_sdk(monkeypatch, sdk)
    stages: list[tuple[Any, asyncio.Task[Any]]] = []
    p4_stages: list[Any] = []
    actual_advance = driver_module._SegmentDriver.advance
    actual_create_stage = SessionOperationLease.create_continuation_stage

    async def observe_advance(driver: Any, stored: Any, selected: Any) -> Any:
        task = asyncio.current_task()
        assert task is not None
        stages.append((driver, task))
        return await actual_advance(driver, stored, selected)

    monkeypatch.setattr(driver_module._SegmentDriver, "advance", observe_advance)

    def observe_create_stage(lease: SessionOperationLease, coroutine: Any, *, name: str) -> Any:
        stage = actual_create_stage(lease, coroutine, name=name)
        if name == "continuation-required-p4":
            p4_stages.append(stage)
        return stage

    monkeypatch.setattr(SessionOperationLease, "create_continuation_stage", observe_create_stage)

    entered, release = threading.Event(), threading.Event()
    fault = OperationalError("u49 actual P4 state insert", {}, RuntimeError("u49 actual DBAPI original"))
    operation_id = str(uuid4())
    physical_outcomes: list[Any] = []
    after_sql, stop_committed = _hold_next_action_after_sql(monkeypatch, RequiredWorkSource.COMPOSE_CHECKPOINT_SQL, physical_outcomes)
    cancel_entered = _observe_cancel_mutation_entry(monkeypatch, operation_id)
    native_originals: list[asyncio.CancelledError] = []
    native_seen = asyncio.Event()
    actual_retain = worker_module._retain_cancellation

    def observe_native(cancellations: list[asyncio.CancelledError], original: asyncio.CancelledError) -> None:
        if native_interrupt and p4_stages and asyncio.current_task() is p4_stages[0].task:
            native_originals.append(original)
            native_seen.set()
        actual_retain(cancellations, original)

    monkeypatch.setattr(worker_module, "_retain_cancellation", observe_native)
    async with continuation_app(tmp_path, monkeypatch, **pg_continuation_overrides) as harness:
        app_ref.append(harness.app)
        session_ref.append(harness.session_id)
        await _seed_pipeline_request(harness, tmp_path, trust_mode="auto_commit")
        early_outcomes = _observe_worker_outcomes(harness, monkeypatch)
        settlement_originals = _observe_settlement(monkeypatch, after_sql=after_sql, stop_committed=stop_committed)
        engine = harness.app.state.session_engine
        state_inserts: list[str] = []

        def hold_state(_conn: Any, _cursor: Any, statement: str, _parameters: Any, _context: Any, _many: Any) -> None:
            if statement.startswith("INSERT INTO composition_states"):
                state_inserts.append(statement)
                entered.set()
                assert release.wait(30), "actual P4 state insert was never released"
                if sql_fault:
                    raise fault

        event.listen(engine, "before_cursor_execute", hold_state)
        try:
            async with harness.client() as client:
                accepted = await client.post(
                    f"/api/sessions/{harness.session_id}/messages",
                    json={"content": "Build a CSV to JSONL pipeline.", "operation_id": operation_id, "state_id": None},
                )
                assert accepted.status_code == 202, accepted.text
                await _wait_for_sql_entry(entered, early_outcomes, sdk, harness, operation_id, settlement_originals)
                assert len(state_inserts) == 1 and stages and len(p4_stages) == 1
                driver, stage = stages[-1]
                assert driver.running.claim.operation_id == operation_id and not stage.done()
                p4_stage = p4_stages[0]
                assert p4_stage.task is not None and not p4_stage.task.done()
                cancel_task = await _queue_public_cancel(client, harness, operation_id, cancel_entered)
                assert not after_sql.is_set()
                if native_interrupt:
                    assert p4_stage.task.cancel("u49-native-p4")
                    async with asyncio.timeout(3):
                        await native_seen.wait()
                    assert len(native_originals) == 1 and native_originals[0].args == ("u49-native-p4",)
                    assert not p4_stage.task.done() and not after_sql.is_set()
                release.set()
                try:
                    async with asyncio.timeout(10):
                        await after_sql.wait()
                    assert len(physical_outcomes) == 1
                    if native_interrupt:
                        physical = physical_outcomes[0]
                        assert type(physical) is RequiredSQLReturned
                        assert physical.deferred_cancellations == (native_originals[0],)
                    await _finish_queued_public_cancel(client, harness, operation_id, cancel_task)
                finally:
                    stop_committed.set()
                snapshot = await settle(harness, operation_id=operation_id, client=client)
            rows = _rows(harness, operation_id)
            _assert_provider_cohort(rows, driver, sdk, expected=1)
            diagnostic = _native_fence_diagnostic(harness, driver, early_outcomes, rows) if native_interrupt else None
            if diagnostic is not None and rows["fences"] and rows["fences"][-1].released_at is None:
                for name, detail in diagnostic.items():
                    print(f"u49_fence_{name}={detail!r}")
            _assert_released_and_joined(harness, rows, diagnostic=diagnostic)
            failure = await _assert_public_failure(harness, operation_id, snapshot, rows)
            assert len(state_inserts) == 1 and len(sdk.steps) == 3
            assert sdk.roles == ["primary"]
            assert [role for role, _payload in sdk.steps] == ["end", "primary", "end"]
            assert not rows["proposals"]
            phase = driver.phases.p4
            assert phase is not None and phase.physical is not None
            assert phase.physical is physical_outcomes[0]
            if native_interrupt:
                assert phase.physical.deferred_cancellations == (native_originals[0],)
                assert any(
                    native_originals[0] is leaf for original in settlement_originals if original is not None for leaf in _leaves(original)
                )
            else:
                assert not phase.physical.deferred_cancellations
                stop_deliveries = tuple(
                    leaf
                    for original in settlement_originals
                    if original is not None
                    for leaf in _leaves(original)
                    if isinstance(leaf, asyncio.CancelledError)
                )
                assert all(len(delivery.args) == 1 and delivery.args[0] is COMPOSER_CANCEL_REQUESTED for delivery in stop_deliveries)
            if sql_fault:
                assert type(phase.physical) is RequiredSQLRaised
                wrapped = phase.physical.error
                assert isinstance(wrapped, AuditIntegrityError) and wrapped.__cause__ is fault
                assert any(error is fault for error in phase.deferred_failures)
                assert snapshot.error.error_type == "audit_integrity_error"
                assert any(wrapped is leaf for original in settlement_originals if original is not None for leaf in _leaves(original))
                assert not rows["states"]
                assert not [row for row in rows["messages"] if row.role in ("assistant", "tool") and row.composition_state_id]
                p4_effects = [row for row in rows["effects"] if row.effect_key.endswith(":p4")]
                assert len(p4_effects) == 1
                (prepared,) = p4_effects
                assert prepared.owner == "session_sql" and prepared.disposition == "prepared"
                assert prepared.receipt_key is None and prepared.result_chunk_key is None
                assert prepared.committed_state_ids_json is None and prepared.graph_sha256 is None
                assert phase.committed_outcome is None and phase.publication is None
            else:
                assert type(phase.physical) is RequiredSQLReturned
                (state,) = rows["states"]
                tools = [row for row in rows["messages"] if row.role == "tool" and row.composition_state_id == state.id]
                assert len(tools) == 1 and tools[0].tool_call_id == "u49-pipeline-1"
                assistants = [row for row in rows["messages"] if row.role == "assistant" and row.id == tools[0].parent_assistant_id]
                assert len(assistants) == 1 and assistants[0].composition_state_id is None
                assert phase.committed_outcome is not None and phase.committed_outcome.audit_outcome.current_state_id == state.id
                assert len([row for row in rows["effects"] if row.effect_key.endswith(":p4") and row.disposition == "committed"]) == 1
                assert failure["saved_revisions"]["count"] == 1
        finally:
            release.set()
            stop_committed.set()
            event.remove(engine, "before_cursor_execute", hold_state)


@pytest.mark.asyncio
async def test_queued_stop_refuses_changed_p4_sql_after_known_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_continuation_overrides: dict[str, Any]
) -> None:
    """A first physical rollback witness cannot authorize a changed terminal read."""
    app_ref: list[Any] = []
    session_ref: list[Any] = []
    sdk = _scripted_pipeline_sdk(tmp_path, app_ref, session_ref, auto=True)
    install_sdk(monkeypatch, sdk)
    drivers: list[Any] = []
    actual_advance = driver_module._SegmentDriver.advance

    async def observe_advance(driver: Any, stored: Any, selected: Any) -> Any:
        drivers.append(driver)
        return await actual_advance(driver, stored, selected)

    monkeypatch.setattr(driver_module._SegmentDriver, "advance", observe_advance)
    readbacks: list[tuple[Any, Any]] = []
    actual_readback = p4_module.read_p4_transaction_outcome_on_connection

    def observe_readback(conn: Any, observation: Any, *, env_ref_names: frozenset[str]) -> Any:
        try:
            result = actual_readback(conn, observation, env_ref_names=env_ref_names)
        except BaseException as error:
            readbacks.append((observation, error))
            raise
        readbacks.append((observation, result))
        return result

    monkeypatch.setattr(p4_module, "read_p4_transaction_outcome_on_connection", observe_readback)
    entered, release = threading.Event(), threading.Event()
    fault = OperationalError("g9 actual P4 state insert", {}, RuntimeError("g9 original DBAPI fault"))
    operation_id = str(uuid4())
    after_sql, stop_committed = _hold_next_action_after_sql(monkeypatch, RequiredWorkSource.COMPOSE_CHECKPOINT_SQL)
    cancel_entered = _observe_cancel_mutation_entry(monkeypatch, operation_id)
    settlement_originals = _observe_settlement(monkeypatch, after_sql=after_sql, stop_committed=stop_committed)
    actual_settle = driver_module._SegmentDriver.settle
    terminal_waiting, release_terminal = asyncio.Event(), asyncio.Event()

    async def hold_terminal(driver: Any, record: Any, original: BaseException | None = None) -> Any:
        await stop_committed.wait()
        terminal_waiting.set()
        await release_terminal.wait()
        return await actual_settle(driver, record, original)

    monkeypatch.setattr(driver_module._SegmentDriver, "settle", hold_terminal)
    async with continuation_app(tmp_path, monkeypatch, **pg_continuation_overrides) as harness:
        app_ref.append(harness.app)
        session_ref.append(harness.session_id)
        await _seed_pipeline_request(harness, tmp_path, trust_mode="auto_commit")
        worker_outcomes = _observe_worker_outcomes(harness, monkeypatch)
        engine = harness.app.state.session_engine
        state_inserts: list[str] = []
        original_digest: str | None = None
        changed_digest: str | None = None
        effect_key: str | None = None

        def fail_state(_conn: Any, _cursor: Any, statement: str, _parameters: Any, _context: Any, _many: Any) -> None:
            if statement.startswith("INSERT INTO composition_states"):
                state_inserts.append(statement)
                entered.set()
                assert release.wait(30), "actual P4 state insert was never released"
                raise fault

        event.listen(engine, "before_cursor_execute", fail_state)
        try:
            async with harness.client() as client:
                accepted = await client.post(
                    f"/api/sessions/{harness.session_id}/messages",
                    json={"content": "Build a CSV to JSONL pipeline.", "operation_id": operation_id, "state_id": None},
                )
                assert accepted.status_code == 202, accepted.text
                await _wait_for_sql_entry(entered, worker_outcomes, sdk, harness, operation_id, settlement_originals)
                assert len(state_inserts) == 1 and drivers
                driver = drivers[-1]
                cancel_task = await _queue_public_cancel(client, harness, operation_id, cancel_entered)
                release.set()
                async with asyncio.timeout(10):
                    await after_sql.wait()
                phase = driver.phases.p4
                assert phase is not None and type(phase.physical) is RequiredSQLRaised
                assert phase.commit is not None
                observed = phase.writer_observation
                assert observed is not None and observed.known_not_committed and not observed.committed
                assert observed.candidate is None and observed.readback is None
                assert readbacks == [(observed, None)]
                assert isinstance(phase.physical.error, AuditIntegrityError) and phase.physical.error.__cause__ is fault
                assert any(error is fault for error in phase.deferred_failures)
                await _finish_queued_public_cancel(client, harness, operation_id, cancel_task)
                stop_committed.set()
                async with asyncio.timeout(10):
                    await terminal_waiting.wait()
                before = _rows(harness, operation_id)
                (operation,) = before["operation"]
                assert operation.cancel_requested_at is not None and operation.settled_at is None
                p4 = [effect for effect in before["effects"] if effect.effect_key == phase.commit.effect_key]
                assert len(p4) == 1 and p4[0].disposition == "prepared"
                assert p4[0].receipt_key is None and p4[0].result_chunk_key is None
                effect_key, original_digest = p4[0].effect_key, p4[0].input_sha256
                changed_digest = "0" * 64 if original_digest != "0" * 64 else "1" * 64
                with engine.begin() as conn:
                    changed = conn.execute(
                        update(composer_continuation_effects_table)
                        .where(
                            composer_continuation_effects_table.c.session_id == str(harness.session_id),
                            composer_continuation_effects_table.c.operation_id == operation_id,
                            composer_continuation_effects_table.c.effect_key == effect_key,
                            composer_continuation_effects_table.c.disposition == "prepared",
                            composer_continuation_effects_table.c.input_sha256 == original_digest,
                        )
                        .values(input_sha256=changed_digest)
                    )
                    assert changed.rowcount == 1
                release_terminal.set()
                async with asyncio.timeout(90):
                    while not worker_outcomes:
                        await asyncio.sleep(0.05)
                # The terminal's complete structural admission rejects the
                # changed digest before a second P4 readback can grant priority.
                assert readbacks == [(observed, None)]
                assert worker_outcomes[0] is not None and any(
                    isinstance(leaf, AuditIntegrityError) and "structural admission changed the original P4 input/owner" in str(leaf)
                    for leaf in _leaves(worker_outcomes[0])
                )
            rows = _rows(harness, operation_id)
            (operation,) = rows["operation"]
            assert operation.cancel_requested_at is not None and operation.settled_at is None
            assert operation.settled_by is None and operation.result_json is None
            assert len(rows["fences"]) == len(before["fences"])
            assert [(fence.operation_id, fence.lease_token, fence.operation_epoch) for fence in rows["fences"]] == [
                (fence.operation_id, fence.lease_token, fence.operation_epoch) for fence in before["fences"]
            ]
            assert not rows["states"] and not rows["proposals"]
            assert not [row for row in rows["messages"] if row.role in ("assistant", "tool") and row.composition_state_id]
            p4 = [effect for effect in rows["effects"] if effect.effect_key == effect_key]
            assert len(p4) == 1 and p4[0].disposition == "prepared"
            assert p4[0].receipt_key is None and p4[0].result_chunk_key is None
            assert p4[0].input_sha256 == changed_digest
            assert len(sdk.requests) == len(rows["physical"]) == len(rows["usage"]) == 1
            assert sdk.roles == ["primary"]
            assert any(
                phase.physical.error is leaf for original in settlement_originals if original is not None for leaf in _leaves(original)
            )
        finally:
            release.set()
            stop_committed.set()
            release_terminal.set()
            event.remove(engine, "before_cursor_execute", fail_state)
            if effect_key is not None and original_digest is not None and changed_digest is not None:
                with engine.begin() as conn:
                    conn.execute(
                        update(composer_continuation_effects_table)
                        .where(
                            composer_continuation_effects_table.c.session_id == str(harness.session_id),
                            composer_continuation_effects_table.c.operation_id == operation_id,
                            composer_continuation_effects_table.c.effect_key == effect_key,
                            composer_continuation_effects_table.c.input_sha256 == changed_digest,
                        )
                        .values(input_sha256=original_digest)
                    )


@pytest.mark.asyncio
async def test_public_stop_queued_behind_actual_proposal_insert_keeps_pending_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_continuation_overrides: dict[str, Any]
) -> None:
    app_ref: list[Any] = []
    session_ref: list[Any] = []
    sdk = _scripted_pipeline_sdk(tmp_path, app_ref, session_ref, auto=False)
    install_sdk(monkeypatch, sdk)
    stages: list[tuple[Any, asyncio.Task[Any]]] = []
    actual_advance = driver_module._SegmentDriver.advance

    async def observe_advance(driver: Any, stored: Any, selected: Any) -> Any:
        task = asyncio.current_task()
        assert task is not None
        stages.append((driver, task))
        return await actual_advance(driver, stored, selected)

    monkeypatch.setattr(driver_module._SegmentDriver, "advance", observe_advance)
    entered, release = threading.Event(), threading.Event()
    operation_id = str(uuid4())
    after_sql, stop_committed = _hold_proposal_return_after_sql(monkeypatch)
    cancel_entered = _observe_cancel_mutation_entry(monkeypatch, operation_id)
    async with continuation_app(tmp_path, monkeypatch, **pg_continuation_overrides) as harness:
        app_ref.append(harness.app)
        session_ref.append(harness.session_id)
        await _seed_pipeline_request(harness, tmp_path, trust_mode="explicit_approve")
        early_outcomes = _observe_worker_outcomes(harness, monkeypatch)
        settlement_originals = _observe_settlement(monkeypatch, after_sql=after_sql, stop_committed=stop_committed)
        engine = harness.app.state.session_engine
        proposal_inserts: list[str] = []

        def hold_proposal(_conn: Any, _cursor: Any, statement: str, _parameters: Any, _context: Any, _many: Any) -> None:
            if statement.startswith("INSERT INTO composition_proposals"):
                proposal_inserts.append(statement)
                entered.set()
                assert release.wait(30), "actual proposal insert was never released"

        event.listen(engine, "before_cursor_execute", hold_proposal)
        try:
            async with harness.client() as client:
                accepted = await client.post(
                    f"/api/sessions/{harness.session_id}/messages",
                    json={"content": "Build a CSV to JSONL pipeline.", "operation_id": operation_id, "state_id": None},
                )
                assert accepted.status_code == 202, accepted.text
                await _wait_for_sql_entry(entered, early_outcomes, sdk, harness, operation_id, settlement_originals)
                assert len(proposal_inserts) == 1 and stages
                driver, stage = stages[-1]
                assert driver.running.claim.operation_id == operation_id and not stage.done()
                cancel_task = await _queue_public_cancel(client, harness, operation_id, cancel_entered)
                assert not after_sql.is_set()
                release.set()
                try:
                    async with asyncio.timeout(10):
                        await after_sql.wait()
                    await _finish_queued_public_cancel(client, harness, operation_id, cancel_task)
                finally:
                    stop_committed.set()
                snapshot = await settle(harness, operation_id=operation_id, client=client)
            rows = _rows(harness, operation_id)
            _assert_provider_cohort(rows, driver, sdk, expected=1)
            _assert_released_and_joined(harness, rows)
            await _assert_public_failure(harness, operation_id, snapshot, rows)
            assert len(proposal_inserts) == 1 and len(sdk.steps) == 2
            assert not rows["states"] and not rows["blobs"]
            assert len(rows["proposals"]) == 1
            (proposal,) = rows["proposals"]
            # This retained public contract is intentionally RED if current cleanup auto-rejects.
            assert proposal.status == "pending"
            assert not any(
                original is not None and any(isinstance(leaf, asyncio.CancelledError) for leaf in _leaves(original))
                for original in settlement_originals
            )
            assert proposal.tool_call_id == "u49-pipeline-1" and proposal.committed_state_id is None
            created = [row for row in rows["proposal_events"] if row.proposal_id == proposal.id]
            assert len(created) == 1 and created[0].event_type == "proposal.created"
            assert proposal.audit_event_id == created[0].id
            assert not [row for row in rows["messages"] if row.role in ("assistant", "tool") and row.composition_state_id]
        finally:
            release.set()
            stop_committed.set()
            event.remove(engine, "before_cursor_execute", hold_proposal)
