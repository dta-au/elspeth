"""run_composer_turn: the detached freeform turn, driven with a hand-built running job (plan N11b).

No worker exists yet (N12). ``_running_job`` builds exactly the state the worker
will hand the turn, through the real authorities:

1. ``ComposerAsyncOperationAuthority.admit`` + ``claim_next`` (N05), binding the
   session's current head as the request's base (what the SPA sends, ruling 5);
2. the start composite ``start_composer_async_operation`` (N06, E5), then
   ``SessionOperationLease.adopt`` (R3);
3. the composer request lifecycle (N08, E1).

The turn never settles a failure: every failure test asserts the row is still
``running``. N12 owns the failure terminal.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import threading
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from structlog.testing import capture_logs

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.composer_progress import ComposerProgressEvent, ComposerProgressSink
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.composer.availability import ComposerAvailability
from elspeth.web.composer.pipeline_planner import PipelinePlannerError
from elspeth.web.composer.progress import ComposerProgressRegistry, ComposerRequestLease
from elspeth.web.composer.protocol import (
    ComposerConvergenceError,
    ComposerResult,
    ComposerRuntimePreflightError,
    PipelineCommitIntent,
    RetainedOperationContext,
)
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.composer_progress_authority import ComposerRequestLeaseLost
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions import composer_turn
from elspeth.web.sessions.composer_app_services import composer_app_services
from elspeth.web.sessions.composer_operation_errors import request_cancelled_error
from elspeth.web.sessions.composer_operations import (
    ComposerOperationCancelledDuringTurn,
    ComposerOperationKind,
    ComposerOperationRecord,
    ComposerOperationRunning,
    ComposerTurnDeadlineExpired,
    composer_operation_request_hash,
)
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.routes import _helpers
from elspeth.web.sessions.routes._helpers import ComposerRequestLifecycle, composer_request_lifecycle
from elspeth.web.sessions.routes.composer import pipeline_settlement
from elspeth.web.sessions.schemas import MessageWithStateResponse, RecomposeRequest, SendMessageRequest
from elspeth.web.sessions.service import SessionServiceImpl
from tests.helpers.session_fences import make_compose_context
from tests.unit.web.sessions.test_routes import (
    _EMPTY_STATE,
    _cancelled_error_with_llm_call,
    _create_canonical_pipeline_route_proposal,
    _create_running_pipeline_route_proposal,
    _llm_call,
    _llm_call_audit_rows,
    _make_app,
    _make_composer_mock,
    _prepare_canonical_pipeline_route_proposal,
)
from tests.unit.web.sessions.test_unwind_persist_forensics import _tool_invocation


@dataclass(frozen=True)
class _Job:
    turn: composer_turn.ComposerTurnInput
    lease: SessionOperationLease
    running: ComposerOperationRunning
    lifecycle: ComposerRequestLifecycle
    authority: ComposerAsyncOperationAuthority
    observation: composer_turn.ComposerTurnObservation
    budget_anchor: composer_turn.ComposerBudgetAnchor


class _TestSnapshotFactory:
    """Model the production factory's request-free principal lookup seam."""

    def __init__(self, factory: Any) -> None:
        self._factory = factory

    def for_user_id(self, user_id: str) -> PluginAvailabilitySnapshot:
        return self._factory(user_id)


@contextlib.asynccontextmanager
async def _running_job(
    app: FastAPI,
    service: SessionServiceImpl,
    session_id: uuid.UUID,
    *,
    kind: ComposerOperationKind,
    content: str | None = None,
    expected_user_message_id: uuid.UUID | None = None,
    budget_seconds: float = 85.0,
    request_id: str | None = None,
) -> AsyncIterator[_Job]:
    """Admit, claim, start and adopt one job exactly as the N12 worker will, then open its request lifecycle."""
    app.state.plugin_snapshot_factory = _TestSnapshotFactory(app.state.plugin_snapshot_factory)
    services = composer_app_services(app)
    head = await service.get_current_state(session_id)
    base_state_id = head.id if head is not None else None
    operation_id = str(uuid.uuid4())
    request: SendMessageRequest | RecomposeRequest
    if kind == "compose_message":
        assert content is not None
        request = SendMessageRequest(operation_id=operation_id, content=content, state_id=base_state_id)
    else:
        assert expected_user_message_id is not None
        request = RecomposeRequest(
            operation_id=operation_id,
            expected_user_message_id=expected_user_message_id,
            state_id=base_state_id,
        )
    authority = ComposerAsyncOperationAuthority(
        app.state.session_engine,
        owner_instance_id=service.session_operation_owner_instance_id,
        claim_lease_seconds=30,
    )
    await run_sync_in_worker(
        authority.admit,
        quota=app.state.rate_limiter.composer_admission,
        session_id=session_id,
        operation_id=operation_id,
        kind=kind,
        request_hash=composer_operation_request_hash(session_id=session_id, kind=kind, request=request),
        actor_user_id="alice",
        auth_provider_type="local",
        request_id=request_id,
        base_state_id=base_state_id,
        request_json=request.model_dump_json(),
        deadline_seconds=85.0,
        max_nonterminal=64,
    )
    (claim,) = await run_sync_in_worker(authority.claim_next, limit=1)
    context = await run_sync_in_worker(
        service.session_operation_authority.start_composer_async_operation,
        claim,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
        auth_provider_type="local",
    )
    # E14/F-C5: the worker pins the budget when the start composite returns;
    # ``budget_seconds`` stands in for deadline_at - started_at on the DB clock.
    budget_anchor = composer_turn.ComposerBudgetAnchor(
        remaining_at_running_seconds=budget_seconds,
        monotonic_at_running=time.monotonic(),
    )
    lease = await SessionOperationLease.adopt(
        service.session_operation_authority,
        context,
        lease_seconds=service.session_operation_lease_seconds,
    )
    owner_task = asyncio.current_task()
    assert owner_task is not None
    async with (
        lease,
        composer_request_lifecycle(
            services.progress_registry,
            session_id=str(session_id),
            user_id="alice",
            owner_task=owner_task,
        ) as lifecycle,
    ):
        yield _Job(
            turn=composer_turn.ComposerTurnInput(
                session_id=session_id,
                operation_id=operation_id,
                kind=kind,
                actor_user_id="alice",
                request=request,
                request_id=request_id,
                budget_seconds=budget_seconds,
            ),
            lease=lease,
            running=ComposerOperationRunning(claim=claim, session_operation_context=context),
            lifecycle=lifecycle,
            authority=authority,
            observation=composer_turn.ComposerTurnObservation(),
            budget_anchor=budget_anchor,
        )


async def _run(
    app: FastAPI,
    job: _Job,
    *,
    budget_anchor: composer_turn.ComposerBudgetAnchor | None = None,
) -> ComposerOperationRecord:
    return await composer_turn.run_composer_turn(
        composer_app_services(app),
        job.turn,
        lease=job.lease,
        running=job.running,
        request_lifecycle=job.lifecycle,
        observation=job.observation,
        budget_anchor=job.budget_anchor if budget_anchor is None else budget_anchor,
    )


async def _row(job: _Job) -> ComposerOperationRecord:
    record = await run_sync_in_worker(job.authority.get, session_id=job.turn.session_id, operation_id=job.turn.operation_id)
    assert record is not None
    return record


def _user_and_assistant_roles(messages: Any) -> list[str]:
    return [message.role for message in messages if message.role in {"user", "assistant"}]


@pytest.mark.asyncio
async def test_required_retained_history_reads_existing_authority_and_reaches_compose(tmp_path: Path) -> None:
    from elspeth.web.required_work import RequiredWorkSource

    app, sessions = _make_app(tmp_path)
    composer = _make_composer_mock("Explanation")
    app.state.composer_service = composer
    session = await sessions.create_session("alice", "Existing title", "local")
    authority = app.state.composer_async_operation_authority
    assert type(authority) is ComposerAsyncOperationAuthority
    assert composer_app_services(app).composer_operation_authority is authority
    async with _running_job(app, sessions, session.id, kind="compose_message", content="Explain") as job:
        record = await _run(app, job)
        assert record.status == "completed"
        context = composer.compose.await_args.kwargs["retained_operation_context"]
        assert type(context) is RetainedOperationContext
        assert context.session_id == session.id and context.actor_user_id == "alice"
        assert context.availability == "available" and context.outcomes == () and not context.has_more
        tickets = [
            ticket
            for ticket in job.observation.required_work.tickets
            if ticket.key.source in (RequiredWorkSource.RETAINED_OPERATION_READ_SQL, RequiredWorkSource.RETAINED_OPERATION_READ_PROJECTION)
        ]
        assert len(tickets) == 2
        assert all(ticket.complete and not ticket.errors for ticket in tickets)


@pytest.mark.asyncio
async def test_required_retained_history_is_bounded_and_actor_session_scoped(tmp_path: Path) -> None:
    app, sessions = _make_app(tmp_path)
    composer = _make_composer_mock("Prior failures do not establish the current request's effects.")
    app.state.composer_service = composer
    session = await sessions.create_session("alice", "Existing title", "local")
    other = await sessions.create_session("alice", "Other session", "local")
    authority = app.state.composer_async_operation_authority
    operation_ids = set()
    for _ in range(4):
        operation_id = str(uuid.uuid4())
        request = SendMessageRequest(operation_id=operation_id, content="PRIVATE_HISTORY_CANARY")
        await run_sync_in_worker(
            authority.admit,
            quota=app.state.rate_limiter.composer_admission,
            session_id=session.id,
            operation_id=operation_id,
            kind="compose_message",
            request_hash=composer_operation_request_hash(session_id=session.id, kind="compose_message", request=request),
            actor_user_id="alice",
            auth_provider_type="local",
            request_id=None,
            base_state_id=None,
            request_json=request.model_dump_json(),
            deadline_seconds=85.0,
            max_nonterminal=64,
        )
        stopped = await run_sync_in_worker(
            authority.request_cancel, session_id=session.id, operation_id=operation_id, cancelled_failure=request_cancelled_error
        )
        assert stopped.status == "failed"
        operation_ids.add(operation_id)
    for read_session, actor in ((other.id, "alice"), (session.id, "another-actor")):
        outcomes, cursor = await run_sync_in_worker(authority.list_failure_outcomes, session_id=read_session, actor_user_id=actor, limit=3)
        assert outcomes == () and cursor is None
    async with _running_job(app, sessions, session.id, kind="compose_message", content="Explain earlier failures") as job:
        await _run(app, job)
        context = composer.compose.await_args.kwargs["retained_operation_context"]
        assert len(context.outcomes) == 3 and context.has_more
        assert {outcome.operation_id for outcome in context.outcomes} < operation_ids
        assert all(outcome.failure_code == "request_cancelled" and outcome.saved_revisions.count == 0 for outcome in context.outcomes)
        assert "PRIVATE_HISTORY_CANARY" not in context.provider_message()
        assert job.turn.operation_id not in context.provider_message()


@pytest.mark.asyncio
async def test_retained_history_read_consumes_original_deadline_before_dispatch(tmp_path: Path, monkeypatch) -> None:
    app, sessions = _make_app(tmp_path)
    composer = _make_composer_mock("Must not dispatch")
    app.state.composer_service = composer
    session = await sessions.create_session("alice", "Existing title", "local")
    authority = app.state.composer_async_operation_authority
    original = authority.list_failure_outcomes
    remaining_at_read = []
    async with _running_job(app, sessions, session.id, kind="compose_message", content="Explain", budget_seconds=0.3) as job:

        def delayed_read(**kwargs):
            remaining_at_read.append(job.budget_anchor.remaining_seconds(monotonic_now=time.monotonic()))
            time.sleep(0.35)
            return original(**kwargs)

        monkeypatch.setattr(authority, "list_failure_outcomes", delayed_read)
        with pytest.raises(ComposerTurnDeadlineExpired):
            await _run(app, job)
        assert len(remaining_at_read) == 1 and remaining_at_read[0] > 0
        composer.compose.assert_not_awaited()
        assert job.observation.required_work.all_completed


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["sql", "projection"])
async def test_required_retained_history_failure_is_not_harmless_unavailability(tmp_path: Path, monkeypatch, phase: str) -> None:
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    from elspeth.web.required_work import RequiredWorkSource, reduce_composer_failures

    app, sessions = _make_app(tmp_path)
    composer = _make_composer_mock("Must not dispatch")
    app.state.composer_service = composer
    session = await sessions.create_session("alice", "Existing title", "local")
    failure = (
        OperationalError("synthetic retained history SELECT", {}, RuntimeError("synthetic storage failure"))
        if phase == "sql"
        else AuditIntegrityError("synthetic history projection corruption")
    )
    async with _running_job(app, sessions, session.id, kind="compose_message", content="Explain") as job:
        if phase == "sql":

            def fail_select(conn, cursor, statement, parameters, context, executemany):
                if statement.lstrip().upper().startswith("SELECT") and "composer_async_operations" in statement and "failed" in parameters:
                    raise failure

            event.listen(app.state.session_engine, "before_cursor_execute", fail_select)
        else:
            original = RetainedOperationContext.__post_init__

            def fail_projection(context):
                original(context)
                raise failure

            monkeypatch.setattr(RetainedOperationContext, "__post_init__", fail_projection)
        try:
            with pytest.raises(type(failure)) as raised:
                await _run(app, job)
        finally:
            if phase == "sql":
                event.remove(app.state.session_engine, "before_cursor_execute", fail_select)
        assert raised.value is failure
        composer.compose.assert_not_awaited()
        required = job.observation.required_work
        source = RequiredWorkSource.RETAINED_OPERATION_READ_SQL if phase == "sql" else RequiredWorkSource.RETAINED_OPERATION_READ_PROJECTION
        (ticket,) = [ticket for ticket in required.tickets if ticket.key.source is source]
        assert ticket.complete and ticket.errors == (failure,)
        assert reduce_composer_failures(required.failure_receipts()).winner.original_root is failure


@pytest.mark.asyncio
async def test_retained_history_repeated_cancellation_waits_for_actual_sql(tmp_path: Path, monkeypatch) -> None:
    app, sessions = _make_app(tmp_path)
    composer = _make_composer_mock("Must not dispatch")
    app.state.composer_service = composer
    session = await sessions.create_session("alice", "Existing title", "local")
    authority = app.state.composer_async_operation_authority
    original = authority.list_failure_outcomes
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def blocked_read(**kwargs):
        entered.set()
        assert release.wait(5), "synthetic retained read was not released"
        try:
            return original(**kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(authority, "list_failure_outcomes", blocked_read)
    async with _running_job(app, sessions, session.id, kind="compose_message", content="Explain") as job:
        task = asyncio.create_task(_run(app, job))
        try:
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            first, second = object(), object()
            task.cancel(first)
            await asyncio.sleep(0.02)
            assert not task.done() and not finished.is_set()
            task.cancel(second)
            await asyncio.sleep(0.02)
            assert not task.done() and not finished.is_set()
        finally:
            release.set()
        with pytest.raises(BaseExceptionGroup) as raised:
            await task
        assert finished.is_set()
        assert [leaf.args[0] for leaf in raised.value.exceptions] == [first, second]
        composer.compose.assert_not_awaited()
        assert job.observation.required_work.all_completed


@pytest.mark.asyncio
@pytest.mark.parametrize("sql_fails", [False, True])
async def test_retained_history_registered_child_cancel_hands_original_after_actual_sql(
    tmp_path: Path, monkeypatch, sql_fails: bool
) -> None:
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    app, sessions = _make_app(tmp_path)
    composer = _make_composer_mock("Must not dispatch")
    app.state.composer_service = composer
    session = await sessions.create_session("alice", "Existing title", "local")
    authority = app.state.composer_async_operation_authority
    original_finish = composer_turn.run_required_sql_finish_once
    original_join = composer_turn._join_freeform_owned_task
    child_cancellations = []
    outer_cancellations = []

    async def capture_finish(*args, **kwargs):
        outcome = await original_finish(*args, **kwargs)
        child_cancellations.extend(outcome.deferred_cancellations)
        return outcome

    async def capture_join(task, **kwargs):
        result = await original_join(task, **kwargs)
        if task.get_name() == "composer-retained-operation-history":
            outer_cancellations.extend(kwargs["cancellation_observations"])
        return result

    monkeypatch.setattr(composer_turn, "run_required_sql_finish_once", capture_finish)
    monkeypatch.setattr(composer_turn, "_join_freeform_owned_task", capture_join)
    read = authority.list_failure_outcomes
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    sql_failure = OperationalError("synthetic retained SELECT", {}, RuntimeError("synthetic SQL failure"))

    def blocked_read(**kwargs):
        entered.set()
        assert release.wait(5)
        try:
            return read(**kwargs)
        finally:
            finished.set()

    def fail_select(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and "composer_async_operations" in statement and "failed" in parameters:
            raise sql_failure

    monkeypatch.setattr(authority, "list_failure_outcomes", blocked_read)
    children = []
    create_task = SessionOperationLease.create_task

    def register(lease, coroutine, *, name):
        child = create_task(lease, coroutine, name=name)
        if name == "composer-retained-operation-history":
            children.append(child)
        return child

    monkeypatch.setattr(SessionOperationLease, "create_task", register)
    child_marker, first_outer, second_outer = object(), object(), object()
    observed = None
    close_error = None
    try:
        async with _running_job(app, sessions, session.id, kind="compose_message", content="Explain") as job:
            if sql_fails:
                event.listen(app.state.session_engine, "before_cursor_execute", fail_select)
            task = asyncio.create_task(_run(app, job))
            try:
                for _ in range(200):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.01)
                assert entered.is_set() and len(children) == 1
                children[0].cancel(child_marker)
                await asyncio.sleep(0.02)
                task.cancel(first_outer)
                await asyncio.sleep(0.02)
                task.cancel(second_outer)
                await asyncio.sleep(0.02)
                assert not finished.is_set() and not task.done()
            finally:
                release.set()
            try:
                await task
            except BaseException as error:
                observed = error
            finally:
                if sql_fails:
                    event.remove(app.state.session_engine, "before_cursor_execute", fail_select)
            assert finished.is_set()
            assert children[0].done() and not children[0].cancelled()
            assert children[0].exception() is None
            assert job.observation.required_work.all_completed
    except BaseException as error:
        close_error = error
    finally:
        release.set()
    assert close_error is None, "History child failures must not replay during lease close"
    assert isinstance(observed, BaseExceptionGroup)

    def leaves(error):
        if isinstance(error, BaseExceptionGroup):
            return [leaf for nested in error.exceptions for leaf in leaves(nested)]
        return [error]

    originals = leaves(observed)
    cancellations = [leaf for leaf in originals if isinstance(leaf, asyncio.CancelledError)]
    assert [leaf.args[0] for leaf in cancellations] == [child_marker, first_outer, second_outer]
    assert len(child_cancellations) == 1 and len(outer_cancellations) == 2
    assert all(actual is original for actual, original in zip(cancellations, [*child_cancellations, *outer_cancellations], strict=True))
    assert [leaf for leaf in originals if isinstance(leaf, OperationalError)] == ([sql_failure] if sql_fails else [])
    composer.compose.assert_not_awaited()


def test_turn_input_refuses_a_kind_request_mismatch() -> None:
    session_id = uuid.uuid4()
    operation_id = str(uuid.uuid4())
    send = SendMessageRequest(operation_id=operation_id, content="x")
    recompose = RecomposeRequest(operation_id=operation_id, expected_user_message_id=uuid.uuid4())
    with pytest.raises(TypeError):
        composer_turn.ComposerTurnInput(
            session_id=session_id,
            operation_id=operation_id,
            kind="compose_recompose",
            actor_user_id="alice",
            request=send,
            request_id=None,
            budget_seconds=85.0,
        )
    with pytest.raises(TypeError):
        composer_turn.ComposerTurnInput(
            session_id=session_id,
            operation_id=operation_id,
            kind="compose_message",
            actor_user_id="alice",
            request=recompose,
            request_id=None,
            budget_seconds=85.0,
        )
    with pytest.raises(ValueError, match="operation_id"):
        composer_turn.ComposerTurnInput(
            session_id=session_id,
            operation_id=str(uuid.uuid4()),
            kind="compose_message",
            actor_user_id="alice",
            request=send,
            request_id=None,
            budget_seconds=85.0,
        )
    with pytest.raises(ValueError):
        composer_turn.ComposerTurnInput(
            session_id=session_id,
            operation_id=operation_id,
            kind="compose_recompose",
            actor_user_id="alice",
            request=recompose,
            request_id=None,
            budget_seconds=0.0,
        )
    integer_budget: Any = 85
    with pytest.raises(ValueError):
        composer_turn.ComposerTurnInput(
            session_id=session_id,
            operation_id=operation_id,
            kind="compose_message",
            actor_user_id="alice",
            request=send,
            request_id=None,
            budget_seconds=integer_budget,
        )


def test_budget_anchor_measures_the_remaining_budget_and_refuses_inexact_input() -> None:
    anchor = composer_turn.ComposerBudgetAnchor(remaining_at_running_seconds=85.0, monotonic_at_running=1000.0)
    assert anchor.remaining_seconds(monotonic_now=1010.5) == 74.5
    assert anchor.remaining_seconds(monotonic_now=1085.0) == 0.0
    assert anchor.remaining_seconds(monotonic_now=1100.0) == -15.0
    integer_budget: Any = 85
    with pytest.raises(ValueError):
        composer_turn.ComposerBudgetAnchor(remaining_at_running_seconds=integer_budget, monotonic_at_running=1000.0)
    with pytest.raises(ValueError):
        composer_turn.ComposerBudgetAnchor(remaining_at_running_seconds=float("inf"), monotonic_at_running=1000.0)
    with pytest.raises(ValueError):
        composer_turn.ComposerBudgetAnchor(remaining_at_running_seconds=85.0, monotonic_at_running=float("nan"))


def test_per_kind_labels_are_the_route_strings() -> None:
    """The turn emits, per kind, exactly the strings each route emits today (measured at 6cb338f2f).

    send: messages.py:279/1019-1020 endpoint, :399/:452 telemetry, :654 provider route,
    :639 convergence prefix, :482/:729/:797 handler prefix, :664 slog, :939 site,
    :609/:975/:996 task names, :271-275 starting copy. recompose: compose.py:188/749-750,
    :228/:281, :454, :439, :307/:506/:556, :464, :684, :410/:717/:735, :181-185.
    """
    send = composer_turn._SEND_LABELS
    recompose = composer_turn._RECOMPOSE_LABELS
    assert (send.endpoint, recompose.endpoint) == ("send_message", "recompose")
    assert (send.telemetry_source, recompose.telemetry_source) == ("compose", "recompose")
    assert (send.provider_route, recompose.provider_route) == ("messages", "recompose")
    assert (send.convergence_log_prefix, recompose.convergence_log_prefix) == ("convergence", "recompose_convergence")
    assert (send.handler_log_prefix, recompose.handler_log_prefix) == ("compose", "recompose")
    assert (send.llm_bad_request_event, recompose.llm_bad_request_event) == ("compose_llm_bad_request", "recompose_llm_bad_request")
    assert (send.site, recompose.site) == ("send_message", "recompose")
    assert (send.noun, recompose.noun) == ("request", "retry")
    assert (send.settlement_task_name, recompose.settlement_task_name) == (
        "send-message-post-provider-settlement",
        "recompose-post-provider-settlement",
    )
    assert (send.cancelled_llm_task_name, recompose.cancelled_llm_task_name) == (
        "send-message-cancelled-llm-call-persist",
        "recompose-cancelled-llm-call-persist",
    )
    assert (send.cancelled_progress_task_name, recompose.cancelled_progress_task_name) == (
        "send-message-cancelled-progress-publish",
        "recompose-cancelled-progress-publish",
    )
    assert (send.starting_headline, send.starting_evidence) == (
        "I'm reading your request and current pipeline.",
        "The request was accepted for this session.",
    )
    assert (recompose.starting_headline, recompose.starting_evidence) == (
        "I'm rereading your request and current pipeline.",
        "The retry was accepted for this session.",
    )


def test_turn_audit_cohort_drafts_bind_tool_rows_to_the_named_parent() -> None:
    """The pure half of ``_persist_turn_audit_cohort``: a named parent makes ``tool`` rows, none makes ``audit`` rows."""
    invocation = _tool_invocation("get_pipeline_state")
    parent = uuid.uuid4()
    tool_state, llm_state = uuid.uuid4(), uuid.uuid4()
    context = make_compose_context(uuid.uuid4())
    drafts, bindings = composer_turn._turn_audit_cohort_drafts(
        (invocation,),
        (_llm_call(),),
        tool_composition_state_id=tool_state,
        llm_composition_state_id=llm_state,
        parent_assistant_id=parent,
        session_operation_context=context,
    )
    assert [draft.role for draft in drafts] == ["audit", "tool", "audit"]
    assert drafts[0].tool_call_id is None
    assert drafts[1].parent_assistant_id == str(parent)
    assert drafts[1].tool_call_id == "call_get_pipeline_state_1"
    assert (drafts[0].composition_state_id, drafts[1].composition_state_id, drafts[2].composition_state_id) == (
        str(tool_state),
        str(tool_state),
        str(llm_state),
    )
    assert bindings == ()
    orphaned, _ = composer_turn._turn_audit_cohort_drafts(
        (invocation,),
        (),
        tool_composition_state_id=None,
        llm_composition_state_id=None,
        parent_assistant_id=None,
        session_operation_context=context,
    )
    assert [(draft.role, draft.parent_assistant_id, draft.tool_call_id) for draft in orphaned] == [
        ("audit", None, None),
        ("audit", None, None),
    ]


def test_settlement_timeout_detail_is_the_settlement_route_string() -> None:
    """E14: a budget spent before auto-commit answers the settlement's own TIMEOUT 504, byte for byte (PS:283-287)."""
    source = inspect.getsource(pipeline_settlement._settle_pipeline_proposal_under_compose_lock)
    assert f'detail="{composer_turn._SETTLEMENT_TIMEOUT_DETAIL}"' in source


class _CancelCommittingComposer:
    """``compose()`` commits the job's cancel exactly as the cancel route will (N13), then ends as told.

    No worker exists, so nothing delivers a cancel marker: the committed
    ``cancel_requested_at`` is what the rest of the turn meets (N07's positive
    predicate on non-audit writes, and the terminal CAS). ``raise_after`` is
    any exception ``compose()`` can end with.
    """

    def __init__(self, job_holder: list[_Job], *, result: ComposerResult | None = None, raise_after: BaseException | None = None) -> None:
        self._job_holder = job_holder
        self._result = result
        self._raise_after = raise_after

    async def compose(self, *args: Any, **kwargs: Any) -> ComposerResult:
        del args, kwargs
        job = self._job_holder[0]
        cancelled = await run_sync_in_worker(
            job.authority.request_cancel,
            session_id=job.turn.session_id,
            operation_id=job.turn.operation_id,
            cancelled_failure=request_cancelled_error,
        )
        assert cancelled is not None
        assert cancelled.status == "running"
        assert cancelled.cancel_requested_at is not None
        if self._raise_after is not None:
            raise self._raise_after
        assert self._result is not None
        return self._result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call_limit", "malformed_first", "expected_end_calls", "admitted", "cost_cap", "end_cost", "handoff"),
    [
        pytest.param(1, False, 0, False, "5", 0.001, "valid", id="no_call_headroom"),
        pytest.param(2, False, 1, True, "5", 0.001, "valid", id="one_end_admit"),
        pytest.param(2, True, 1, False, "5", 0.001, "valid", id="one_end_malformed"),
        pytest.param(3, True, 2, True, "5", 0.001, "valid", id="two_end_format_retry"),
        pytest.param(3, False, 0, False, "0.001", 0.001, "valid", id="cost_exhausted_before_end"),
        pytest.param(3, False, 1, False, "0.01", 0.02, "valid", id="cost_overflow_end"),
        pytest.param(3, False, 1, False, "0.01", None, "valid", id="cost_unknown_end"),
        pytest.param(3, True, 1, False, "0.0015", 0.001, "valid", id="cost_malformed_consumed"),
        pytest.param(3, False, 1, True, "0.002", 0.001, "valid", id="cost_exact_cap_admit"),
        pytest.param(3, False, 0, False, "5", 0.001, "missing", id="missing_handoff_no_fresh_allowance"),
        pytest.param(3, False, 0, False, "5", 0.001, "expired", id="expired_original_deadline"),
        pytest.param(3, False, 0, False, "5", 0.001, "prior_unknown", id="prior_retry_cost_not_silently_zero"),
    ],
)
async def test_actual_planner_decline_review_shares_call_budget_and_saved_terminal(
    tmp_path: Path,
    monkeypatch,
    call_limit: int,
    malformed_first: bool,
    expected_end_calls: int,
    admitted: bool,
    cost_cap: str,
    end_cost: float | None,
    handoff: str,
) -> None:
    import litellm

    from elspeth.web.composer import planning_application
    from elspeth.web.composer.advisor_audit import ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
    from elspeth.web.composer.pipeline_planner import PlannerDeclined
    from elspeth.web.composer.withheld_replies import COMPOSER_WITHHELD_REPLY_KIND
    from tests.unit.web.composer._helpers import _mock_catalog

    app, sessions = _make_app(tmp_path)
    app.state.settings = app.state.settings.model_copy(
        update={"composer_planner_max_provider_calls": call_limit, "composer_planner_max_cumulative_provider_cost": Decimal(cost_cap)}
    )
    monkeypatch.setattr(
        "elspeth.web.composer.service.compute_availability",
        lambda **kwargs: ComposerAvailability(available=True, model=kwargs["model"], provider="test"),
    )
    # A small synthetic catalog has no omitted LLM catalog facts. It makes
    # this ordinary DECLINE arm reachable without stubbing planner or END.
    catalog = _mock_catalog()
    catalog.list_transforms.return_value = []
    composer = ComposerServiceImpl.for_trained_operator(
        catalog=catalog, settings=app.state.settings, sessions_service=sessions, session_engine=app.state.session_engine
    )
    app.state.composer_service = composer
    candidate = "REQUEST_DECLINE_CANARY: I cannot satisfy this request with the available components."
    phases = []
    actual_plan = planning_application.plan_pipeline

    async def plan_with_handoff(**kwargs):
        try:
            return await actual_plan(**kwargs)
        except PlannerDeclined as declined:
            assert declined.continuation_budget is not None
            if handoff == "missing":
                declined.continuation_budget = None
            elif handoff == "expired":
                declined.continuation_budget.deadline = asyncio.get_running_loop().time() - 1.0
            raise

    if handoff != "valid":
        monkeypatch.setattr(planning_application, "plan_pipeline", plan_with_handoff)

    async def completion(**kwargs):
        phase = "end" if "response_format" in kwargs else "planner"
        phases.append(phase)
        assert len(phases) <= call_limit, "Continuation dispatched beyond its original conservative attempt limit"
        if phase == "planner":
            if handoff == "prior_unknown" and len(phases) == 1:
                raise litellm.ServiceUnavailableError("Synthetic provider retry", model=kwargs["model"], llm_provider="test")
            assert phases == ["planner"] * (2 if handoff == "prior_unknown" else 1), "No new primary turn may repair declined final prose"
            content = "DECLINE: " + candidate
        else:
            assert candidate in json.dumps(kwargs["messages"])
            assert kwargs["max_tokens"] <= app.state.settings.composer_planner_max_completion_tokens
            content = (
                "not JSON"
                if malformed_first and phases.count("end") == 1
                else json.dumps(
                    {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "", "note": None, "publication_verdict": "ADMIT"}
                )
            )
        response = litellm.ModelResponse(
            choices=[{"message": {"role": "assistant", "content": content, "tool_calls": None}, "index": 0, "finish_reason": "stop"}],
            model=kwargs["model"],
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20, "cost": 0.001 if phase == "planner" else end_cost},
        )
        if phase == "end" and end_cost is None:
            # The SDK constructor normalizes null counters to zero. Model an
            # actually absent usage field; null cost with usable counters may
            # legitimately be priced from the bundled catalog.
            response.usage = None
        return response

    # This one explicit SDK replacement handles every planner AND END attempt.
    monkeypatch.setattr(litellm, "acompletion", completion)
    session = await sessions.create_session("alice", "Existing title", "local")
    async with _running_job(app, sessions, session.id, kind="compose_message", content="Build the requested pipeline.") as job:
        record = await _run(app, job)
    assert record.status == "completed"
    response = MessageWithStateResponse.model_validate_json(record.result_json)
    assert phases == [*(["planner"] * (2 if handoff == "prior_unknown" else 1)), *(["end"] * expected_end_calls)]
    assert (candidate in response.message.content) is admitted
    rows = await sessions.get_messages(session.id, limit=None)
    calls = [envelope["call"] for _, envelope in _llm_call_audit_rows(rows)]
    if end_cost is None:
        assert calls[-1]["provider_cost"] is None
        assert calls[-1]["provider_cost_source"] == "not_available"
    public_rows = [row for row in rows if row.role == "assistant"]
    assert any(candidate in row.content for row in public_rows) is admitted
    private = [
        envelope for row in rows if row.tool_calls for envelope in row.tool_calls if envelope.get("_kind") == COMPOSER_WITHHELD_REPLY_KIND
    ]
    assert bool(private) is (not admitted)
    if private:
        assert private[-1]["origin"] == "advisor_publication_withheld"
        assert candidate not in json.dumps(_helpers._composer_chat_history(rows))
    passes = [
        envelope
        for row in rows
        if row.tool_calls
        for envelope in row.tool_calls
        if envelope.get("_kind") == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
    ]
    assert len(passes) == int(admitted)
    assert not (await sessions.get_state_versions(session.id))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route,admit,read_all", (("primary", False, False), ("primary", True, False), ("planner", False, False), ("planner", False, True))
)
async def test_actual_inspection_review_controls_public_and_saved_equivalence_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str, admit: bool, read_all: bool
) -> None:
    import litellm

    from elspeth.web.blobs.service import BlobServiceImpl
    from elspeth.web.composer.turn_effects import TurnEvidence
    from tests.helpers.session_fences import fenced_operation_context
    from tests.unit.web.composer._helpers import _mock_catalog

    app, sessions = _make_app(tmp_path)
    monkeypatch.setattr(
        "elspeth.web.composer.service.compute_availability",
        lambda **kwargs: ComposerAvailability(available=True, model=kwargs["model"], provider="test"),
    )
    catalog = _mock_catalog()
    catalog.list_transforms.return_value = []
    composer = ComposerServiceImpl.for_trained_operator(
        catalog=catalog, settings=app.state.settings, sessions_service=sessions, session_engine=app.state.session_engine
    )
    app.state.composer_service = composer
    session = await sessions.create_session("alice", "Existing title", "local")
    content = "name,score\n" + "ada,42\n" * 8_000
    with fenced_operation_context(app.state.session_engine, str(session.id)) as context:
        blob = await BlobServiceImpl(app.state.session_engine, app.state.settings.data_dir).create_blob(
            session.id, "coverage.csv", content.encode(), "text/csv", session_operation_context=context
        )
    candidate = (
        "PREFIX_SCOPE_CANARY: I inspected only the supplied prefix; the remainder is unreviewed."
        if admit
        else "UNSUPPORTED_EQUIVALENCE_CANARY: I inspected the entire file and verified that no rebuild is needed."
    )
    calls = []
    selected_pages = []
    reviews = []
    actual_review = TurnEvidence.review_candidate

    def capture_review(owner, **kwargs):
        review = actual_review(owner, **kwargs)
        reviews.append((kwargs.get("scope", "primary"), json.loads(review.payload_json)))
        return review

    async def complete(**kwargs):
        calls.append(kwargs)
        if "response_format" in kwargs:
            assert candidate in json.dumps(kwargs["messages"])
            body = json.dumps(
                {
                    "verdict": "FLAGGED",
                    "category": "other",
                    "steps": [],
                    "findings": "Empty graph; inspection coverage alone cannot establish semantic whole-file equivalence.",
                    "note": None,
                    "publication_verdict": "ADMIT" if admit else "WITHHOLD",
                }
            )
            tool_calls = None
        else:
            pages = [
                json.loads(message["content"])["data"]
                for message in kwargs["messages"]
                if message["role"] == "tool" and message["tool_call_id"].startswith("inspect-page-")
            ]
            if not pages:
                assert not selected_pages, "A later context lost its selected page; do not silently reread/replay"
                body = None
                tool_calls = [
                    SimpleNamespace(
                        id="inspect-page-1",
                        function=SimpleNamespace(name="get_blob_content", arguments=json.dumps({"blob_id": str(blob.id)})),
                    )
                ]
            else:
                selected_pages.append(pages[-1])
                if read_all and pages[-1]["next_offset_chars"] is not None:
                    page = pages[-1]
                    body = None
                    tool_calls = [
                        SimpleNamespace(
                            id="inspect-page-2",
                            function=SimpleNamespace(
                                name="get_blob_content",
                                arguments=json.dumps(
                                    {
                                        "blob_id": str(blob.id),
                                        "offset_chars": page["next_offset_chars"],
                                        "expected_content_hash": page["content_hash"],
                                    }
                                ),
                            ),
                        )
                    ]
                else:
                    body = ("DECLINE: " if route == "planner" else "") + candidate
                    tool_calls = None
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=body, tool_calls=tool_calls), finish_reason="tool_calls" if tool_calls else "stop"
                )
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20, "cost": 0.001},
            model=kwargs["model"],
        )

    monkeypatch.setattr(TurnEvidence, "review_candidate", capture_review)
    # Covers every reachable primary/planner/END SDK attempt in this test.
    monkeypatch.setattr(litellm, "acompletion", complete)
    request = "Build the requested pipeline." if route == "planner" else f"Explain the inspection limits of blob {blob.id}."
    async with _running_job(app, sessions, session.id, kind="compose_message", content=request) as job:
        record = await _run(app, job)
    assert record.status == "completed"
    response = MessageWithStateResponse.model_validate_json(record.result_json)
    rows = await sessions.get_messages(session.id, limit=None)
    assert (candidate in response.message.content) is admit
    assert any(candidate in row.content for row in rows if row.role == "assistant") is admit
    # Checkpoints and gate facts can save revisions without an authored edit.
    assert all(not row.sources and not row.nodes and not row.outputs for row in await sessions.get_state_versions(session.id))
    assert selected_pages and reviews
    prefix = selected_pages[0]
    assert 0 < prefix["end_char"] < prefix["total_chars"] == len(content)
    assert prefix["content"] == content[: prefix["end_char"]]
    for scope, review in reviews:
        inspection = review["inspection"]
        assert scope == route
        (coverage,) = inspection["currently_available"]
        assert coverage["content_hash"] == prefix["content_hash"]
        assert coverage["ranges"] == [[0, len(content) if read_all else prefix["end_char"]]]
        assert coverage["complete"] is read_all
        assert inspection["semantic_equivalence_established"] is False
        assert review["saved_effects"]["net_changed"] is False
        assert review["saved_effects"]["initial_authored_state_hash"] == review["saved_effects"]["latest_verified_authored_state_hash"]
        assert review["saved_effects"]["changed_fields"] == []
    assert len([call for call in calls if "response_format" in call]) == (1 if admit or route == "planner" else 2)
    assert len([call for call in calls if "response_format" not in call]) == (3 if read_all or (not admit and route == "primary") else 2)
    if read_all:
        assert selected_pages[-1]["start_char"] == prefix["end_char"]
        assert selected_pages[-1]["end_char"] == len(content)
    if route == "primary":
        canonical_results = [row.content for row in rows if row.role == "tool" and row.tool_call_id == "inspect-page-1"]
    else:
        canonical_results = [
            envelope["invocation"]["result_canonical"]
            for row in rows
            for envelope in row.tool_calls or ()
            if envelope.get("_kind") == "audit" and envelope["invocation"]["tool_name"] == "get_blob_content"
        ]
    assert len(canonical_results) == (2 if read_all else 1)
    assert all("name,score" not in result for result in canonical_results)
    retained_calls = [envelope["call"] for _, envelope in _llm_call_audit_rows(rows)]
    for _, review in reviews:
        call_id, messages_hash = review["inspection"]["current_candidate_call"]
        matching_calls = [call for call in retained_calls if call["call_id"] == call_id]
        assert len(matching_calls) == 1
        assert matching_calls[0]["messages_hash"] == messages_hash
        assert matching_calls[0]["status"] == "success"
    if not admit:
        assert "withheld" in response.message.content.lower()
        assert candidate not in json.dumps(_helpers._composer_chat_history(rows))


@pytest.mark.asyncio
@pytest.mark.parametrize("review_mode", ("clean_admit", "flagged_admit", "unavailable", "input_budget"))
async def test_actual_proof_exhaustion_reviews_or_withholds_public_and_saved_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, review_mode: str
) -> None:
    import litellm

    from elspeth.web.blobs.service import BlobServiceImpl
    from elspeth.web.catalog.policy_view import PolicyCatalogView
    from elspeth.web.composer.advisor_decision import AdvisorGatePassed
    from elspeth.web.composer.tools import execute_tool
    from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
    from tests.helpers.session_fences import fenced_operation_context
    from tests.unit.web.composer._helpers import _mock_catalog

    app, sessions = _make_app(tmp_path)
    if review_mode == "input_budget":
        app.state.settings = app.state.settings.model_copy(update={"composer_advisor_max_prompt_tokens": 1})
    monkeypatch.setattr(
        "elspeth.web.composer.service.compute_availability",
        lambda **kwargs: ComposerAvailability(available=True, model=kwargs["model"], provider="test"),
    )
    catalog = _mock_catalog()
    catalog.list_transforms.return_value = []
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    policy_catalog = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    session = await sessions.create_session("alice", "Existing title", "local")
    with fenced_operation_context(app.state.session_engine, str(session.id)) as context:
        blob = await BlobServiceImpl(app.state.session_engine, app.state.settings.data_dir).create_blob(
            session.id, "proof.csv", b"order_id,customer,price\nO-1,Alice,49.95\n", "text/csv", session_operation_context=context
        )
        state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
        for name, arguments in (
            (
                "set_source_from_blob",
                {
                    "blob_id": str(blob.id),
                    "on_success": "out",
                    "on_validation_failure": "discard",
                    "options": {"schema": {"mode": "fixed", "fields": ["order_id: str"]}},
                },
            ),
            (
                "set_output",
                {
                    "sink_name": "out",
                    "plugin": "json",
                    "on_write_failure": "discard",
                    "options": {
                        "path": "outputs/proof.json",
                        "schema": {"mode": "observed"},
                        "mode": "write",
                        "collision_policy": "auto_increment",
                    },
                },
            ),
        ):
            outcome = execute_tool(
                name,
                arguments,
                state,
                policy_catalog,
                plugin_snapshot=snapshot,
                data_dir=str(app.state.settings.data_dir),
                session_engine=app.state.session_engine,
                session_id=str(session.id),
                session_operation_context=context,
                session_operation_authority=SQLiteLocalSessionOperationAuthority(app.state.session_engine),
            )
            assert outcome.success
            state = outcome.updated_state
        payload = state.to_dict()
        await sessions.save_composition_state(
            session.id,
            CompositionStateData(
                sources=payload["sources"],
                nodes=payload["nodes"],
                edges=payload["edges"],
                outputs=payload["outputs"],
                metadata_=payload["metadata"],
                is_valid=True,
            ),
            provenance="session_seed",
            session_operation_context=context,
        )
    composer = ComposerServiceImpl.for_trained_operator(
        catalog=catalog, settings=app.state.settings, sessions_service=sessions, session_engine=app.state.session_engine
    )
    app.state.composer_service = composer
    admitted = review_mode in ("clean_admit", "flagged_admit")
    candidate = (
        "PROOF_EXHAUSTED_EXPLANATION_CANARY: The name is saved, but the fixed schema still omits observed columns and execution remains blocked."
        if admitted
        else "PROOF_EXHAUSTED_UNREVIEWED_CANARY: Every requested change is complete and the pipeline is ready to run."
    )
    primary_calls = []
    end_calls = []

    async def complete(**kwargs):
        if "response_format" in kwargs:
            end_calls.append(kwargs)
            assert kwargs["response_format"]["json_schema"]["name"] == "advisor_end_response"
            assert candidate in json.dumps(kwargs["messages"])
            if review_mode == "unavailable":
                raise ConnectionError("Synthetic unavailable proof END review")
            assert review_mode != "input_budget", "No physical review fits the original configured input budget"
            content = json.dumps(
                {
                    "verdict": "FLAGGED" if review_mode == "flagged_admit" else "CLEAN",
                    "category": "other",
                    "steps": [],
                    "findings": "The proof remains blocked; the candidate explains that truthfully."
                    if review_mode == "flagged_admit"
                    else "",
                    "note": None,
                    "publication_verdict": "ADMIT",
                }
            )
            calls = None
        else:
            primary_calls.append(kwargs)
            assert len(primary_calls) <= 4, "Proof exhaustion must grant no extra repair allowance"
            if len(primary_calls) == 1:
                content = None
                calls = [
                    SimpleNamespace(
                        id="saved-proof-name",
                        function=SimpleNamespace(name="set_metadata", arguments=json.dumps({"patch": {"name": "Saved proof name"}})),
                    )
                ]
            else:
                content, calls = candidate, None
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls), finish_reason="tool_calls" if calls else "stop")
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20, "cost": 0.001},
            model=kwargs["model"],
        )

    # This finite seam covers every newly reachable primary and END SDK call.
    monkeypatch.setattr(litellm, "acompletion", complete)
    async with _running_job(
        app, sessions, session.id, kind="compose_message", content="Change the pipeline name and explain remaining blockers."
    ) as job:
        record = await _run(app, job)
        result = job.observation.compose_result
    assert record.status == "completed"
    response = MessageWithStateResponse.model_validate_json(record.result_json)
    assert result is not None and result.repair_turns_used == 2
    assert not isinstance(result.advisor_gate_decision, AdvisorGatePassed)
    assert result.state.metadata.name == "Saved proof name"
    proof = result.runtime_preflight
    assert proof is not None and not proof.is_valid
    assert not proof.readiness.authoring_valid and not proof.readiness.completion_ready and not proof.readiness.execution_ready
    assert {error.error_code for error in proof.errors} == {"proof_repair_exhausted"}
    assert {blocker.code for blocker in proof.readiness.blockers} == {"proof_repair_exhausted"}
    assert (candidate in response.message.content) is admitted
    rows = await sessions.get_messages(session.id, limit=None)
    assert any(candidate in row.content for row in rows if row.role == "assistant") is admitted
    assert len(primary_calls) == 4
    assert len(end_calls) == (1 if admitted else 4 if review_mode == "unavailable" else 0)
    withheld = [
        row
        for row in rows
        if row.role == "audit" and any(envelope.get("origin") == "advisor_publication_withheld" for envelope in row.tool_calls or ())
    ]
    assert bool(withheld) is (not admitted)
    if not admitted:
        assert "Saved" in response.message.content or "saved" in response.message.content
        assert candidate not in json.dumps(_helpers._composer_chat_history(rows))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_site", ("end_transport", "end_cancel", "pass_audit"))
async def test_actual_planner_decline_end_failure_preserves_audit_and_never_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_site: str
) -> None:
    import litellm

    from elspeth.web.composer.advisor_audit import ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
    from tests.unit.web.composer._helpers import _mock_catalog

    app, sessions = _make_app(tmp_path)
    app.state.settings = app.state.settings.model_copy(update={"composer_planner_max_provider_calls": 3})
    monkeypatch.setattr(
        "elspeth.web.composer.service.compute_availability",
        lambda **kwargs: ComposerAvailability(available=True, model=kwargs["model"], provider="test"),
    )
    catalog = _mock_catalog()
    catalog.list_transforms.return_value = []
    composer = ComposerServiceImpl.for_trained_operator(
        catalog=catalog, settings=app.state.settings, sessions_service=sessions, session_engine=app.state.session_engine
    )
    app.state.composer_service = composer
    candidate = "DECLINE_FAILURE_CANARY: I cannot satisfy this request."
    original = (
        asyncio.CancelledError("Synthetic END cancellation")
        if failure_site == "end_cancel"
        else AuditIntegrityError("Synthetic pass audit failure")
        if failure_site == "pass_audit"
        else ConnectionError("Synthetic END transport failure")
    )
    phases = []

    async def completion(**kwargs):
        phase = "end" if "response_format" in kwargs else "planner"
        phases.append(phase)
        assert phases in (["planner"], ["planner", "end"]), "Unknown failed-call cost must not grant a retry"
        if phase == "end" and failure_site != "pass_audit":
            raise original
        content = (
            "DECLINE: " + candidate
            if phase == "planner"
            else json.dumps(
                {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "", "note": None, "publication_verdict": "ADMIT"}
            )
        )
        return litellm.ModelResponse(
            choices=[{"message": {"role": "assistant", "content": content, "tool_calls": None}, "index": 0, "finish_reason": "stop"}],
            model=kwargs["model"],
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20, "cost": 0.001},
        )

    actual_add = sessions.add_message

    async def add_message(*args, **kwargs):
        if failure_site == "pass_audit" and any(
            envelope.get("_kind") == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND for envelope in kwargs.get("tool_calls") or ()
        ):
            raise original
        return await actual_add(*args, **kwargs)

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(sessions, "add_message", add_message)
    session = await sessions.create_session("alice", "Existing title", "local")
    async with _running_job(app, sessions, session.id, kind="compose_message", content="Build the requested pipeline.") as job:
        if failure_site == "end_transport":
            record = await _run(app, job)
            assert record.status == "completed"
            assert candidate not in MessageWithStateResponse.model_validate_json(record.result_json).message.content
        else:
            with pytest.raises(asyncio.CancelledError if failure_site == "end_cancel" else AuditIntegrityError) as caught:
                await _run(app, job)
            assert caught.value is original
            assert (await _row(job)).status == "running"
            attached = composer_turn._llm_calls_from_exception(caught.value)
            assert len(attached) == 1
            assert attached[0].status.value == ("cancelled" if failure_site == "end_cancel" else "success")
    assert phases == ["planner", "end"]
    rows = await sessions.get_messages(session.id, limit=None)
    assert all(candidate not in row.content for row in rows if row.role == "assistant")
    assert all(envelope.get("_kind") != ADVISOR_CHECKPOINT_PASS_AUDIT_KIND for row in rows for envelope in row.tool_calls or ())
    calls = [envelope["call"] for _, envelope in _llm_call_audit_rows(rows)]
    assert len(calls) == 2
    assert len({call["call_id"] for call in calls}) == len(calls)
    if failure_site == "end_transport":
        assert calls[-1]["provider_cost"] is None
    assert not (await sessions.get_state_versions(session.id))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("graph_verdict", "publication_verdict", "stale_evidence"),
    (
        ("CLEAN", "ADMIT", False),
        ("FLAGGED", "ADMIT", False),
        ("CLEAN", "WITHHOLD", False),
        ("FLAGGED", "WITHHOLD", False),
        ("CLEAN", "ADMIT", True),
    ),
)
@pytest.mark.parametrize("revoke_build", (False, True))
async def test_actual_end_permission_controls_public_terminal_and_saved_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    graph_verdict: str,
    publication_verdict: str,
    stale_evidence: bool,
    revoke_build: bool,
) -> None:
    import litellm

    from elspeth.web.composer.advisor_audit import ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
    from elspeth.web.composer.advisor_decision import AdvisorGatePassed
    from elspeth.web.composer.turn_effects import TurnEvidence
    from elspeth.web.composer.withheld_replies import COMPOSER_WITHHELD_REPLY_KIND

    app, sessions = _make_app(tmp_path)
    if stale_evidence:
        app.state.settings = app.state.settings.model_copy(update={"composer_advisor_checkpoint_max_passes": 1})
    monkeypatch.setattr(
        "elspeth.web.composer.service.compute_availability",
        lambda **kwargs: ComposerAvailability(available=True, model=kwargs["model"], provider="test"),
    )
    composer = ComposerServiceImpl.for_trained_operator(
        catalog=app.state.catalog_service,
        settings=app.state.settings,
        sessions_service=sessions,
        session_engine=app.state.session_engine,
    )
    app.state.composer_service = composer
    candidate = "EXACT_CANDIDATE_CANARY: ELSPETH transforms rows; this explanation makes no pipeline changes."
    calls = []
    reviews = []
    owners = []
    actual_review = TurnEvidence.review_candidate

    def capture_review(owner, **kwargs):
        review = actual_review(owner, **kwargs)
        owners.append(owner)
        reviews.append(review)
        return review

    monkeypatch.setattr(TurnEvidence, "review_candidate", capture_review)

    async def complete(**kwargs):
        calls.append(kwargs)
        if "response_format" in kwargs:
            schema = kwargs["response_format"]["json_schema"]
            assert schema["name"] == "advisor_end_response"
            assert "publication_verdict" in schema["schema"]["required"]
            content = json.dumps(
                {
                    "verdict": graph_verdict,
                    "category": "other",
                    "steps": [],
                    "findings": "Visible empty graph or unsupported prose"
                    if graph_verdict == "FLAGGED" or publication_verdict == "WITHHOLD"
                    else "",
                    "note": None,
                    "publication_verdict": publication_verdict,
                }
            )
            assert candidate in json.dumps(kwargs["messages"])
            if stale_evidence:
                owners[-1].mark_verification_unavailable()
        else:
            content = candidate
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None), finish_reason="stop")],
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20, "cost": 0.001},
            model=kwargs["model"],
        )

    monkeypatch.setattr(litellm, "acompletion", complete)
    session = await sessions.create_session("alice", "Existing session title", "local")
    request = "Explain what ELSPETH does." + (" Do not build anything." if revoke_build else "")
    async with _running_job(app, sessions, session.id, kind="compose_message", content=request) as job:
        record = await _run(app, job)
        result = job.observation.compose_result
    assert record.status == "completed"
    assert record.result_json is not None
    response = MessageWithStateResponse.model_validate_json(record.result_json)
    assert result is not None
    assert not isinstance(result.advisor_gate_decision, AdvisorGatePassed)
    assert not result.state.sources and not result.state.nodes
    admitted = publication_verdict == "ADMIT" and not stale_evidence
    assert (candidate in response.message.content) is admitted
    messages = await sessions.get_messages(session.id, limit=None)
    visible = [row for row in messages if row.role != "audit"]
    assert (candidate in json.dumps([row.content for row in visible])) is admitted
    passes = [
        entry["pass"]
        for row in messages
        if row.role == "audit"
        for entry in (row.tool_calls or ())
        if entry["_kind"] == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
    ]
    assert passes
    assert all(item["publication_verdict"] == publication_verdict and len(item["publication_binding"]) == 64 for item in passes)
    expected_review_calls = 1 if admitted or stale_evidence else 2
    assert len([call for call in calls if "response_format" in call]) == expected_review_calls
    primary_calls = [call for call in calls if "response_format" not in call]
    assert len(primary_calls) == expected_review_calls + int(revoke_build)
    primary_audits = [call for call in result.llm_calls if call.model_requested == app.state.settings.composer_model]
    assert all(call.call_id is not None for call in primary_audits)
    assert json.loads(reviews[-1].payload_json)["inspection"]["current_candidate_call"][0] == primary_audits[-1].call_id
    if revoke_build:
        assert any(
            message["role"] == "user" and message["content"].startswith("[composer-system] No tool has run this turn")
            for message in primary_calls[1]["messages"]
        )
    withheld = [
        entry
        for row in messages
        if row.role == "audit"
        for entry in (row.tool_calls or ())
        if entry["_kind"] == COMPOSER_WITHHELD_REPLY_KIND and entry["origin"] == "advisor_publication_withheld"
    ]
    assert bool(withheld) is not admitted
    if not admitted:
        assert "withheld" in response.message.content.lower()
        assert candidate not in record.result_json


@pytest.mark.asyncio
@pytest.mark.parametrize("self_corrects", (False, True))
async def test_actual_repeated_reply_review_stops_stalled_repair_without_reusing_permission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    self_corrects: bool,
) -> None:
    import litellm

    from elspeth.web.composer.turn_effects import TurnEvidence

    app, sessions = _make_app(tmp_path)
    app.state.settings = app.state.settings.model_copy(update={"composer_advisor_checkpoint_max_passes": 3})
    monkeypatch.setattr(
        "elspeth.web.composer.service.compute_availability",
        lambda **kwargs: ComposerAvailability(available=True, model=kwargs["model"], provider="test"),
    )
    composer = ComposerServiceImpl.for_trained_operator(
        catalog=app.state.catalog_service,
        settings=app.state.settings,
        sessions_service=sessions,
        session_engine=app.state.session_engine,
    )
    app.state.composer_service = composer
    candidate = "REPEATED_CANDIDATE: ELSPETH transforms data."
    calls = []
    reviews = []
    actual_review = TurnEvidence.review_candidate

    def capture_review(owner, **kwargs):
        review = actual_review(owner, **kwargs)
        if not reviews or reviews[-1].binding != review.binding:
            reviews.append(review)
        return review

    async def complete(**kwargs):
        calls.append(kwargs)
        if "response_format" in kwargs:
            pass_number = len([call for call in calls if "response_format" in call])
            admitted = self_corrects and pass_number == 2
            content = json.dumps(
                {
                    "verdict": "CLEAN" if admitted else "FLAGGED",
                    "category": "other",
                    "steps": [],
                    "findings": "" if admitted else "The candidate has an unsupported claim.",
                    "note": None,
                    "publication_verdict": "ADMIT" if admitted else "WITHHOLD",
                }
            )
        else:
            content = candidate
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None), finish_reason="stop")],
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20, "cost": 0.001},
            model=kwargs["model"],
        )

    monkeypatch.setattr(TurnEvidence, "review_candidate", capture_review)
    monkeypatch.setattr(litellm, "acompletion", complete)
    session = await sessions.create_session("alice", "Existing title", "local")
    async with _running_job(app, sessions, session.id, kind="compose_message", content="Explain ELSPETH.") as job:
        record = await _run(app, job)
    assert record.status == "completed"
    assert len([call for call in calls if "response_format" in call]) == 2
    assert len([call for call in calls if "response_format" not in call]) == 2
    assert len(reviews) == 2
    assert reviews[0].binding != reviews[1].binding
    assert reviews[0].semantic_repair_key == reviews[1].semantic_repair_key
    assert (
        json.loads(reviews[0].payload_json)["inspection"]["current_candidate_call"][0]
        != json.loads(reviews[1].payload_json)["inspection"]["current_candidate_call"][0]
    )
    assert (candidate in record.result_json) is self_corrects
    visible = [row.content for row in await sessions.get_messages(session.id, limit=None) if row.role != "audit"]
    assert (candidate in json.dumps(visible)) is self_corrects


@pytest.mark.asyncio
@pytest.mark.parametrize("verification_fails", (False, True))
@pytest.mark.parametrize("mixed_group", (False, True))
async def test_actual_early_failure_preserves_committed_checkpoint_and_owned_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verification_fails: bool,
    mixed_group: bool,
) -> None:
    import litellm

    from elspeth.web.composer.turn_effects import TurnEvidence
    from elspeth.web.required_work import RequiredWorkSource, reduce_composer_failures

    app, sessions = _make_app(tmp_path)
    monkeypatch.setattr(
        "elspeth.web.composer.service.compute_availability",
        lambda **kwargs: ComposerAvailability(available=True, model=kwargs["model"], provider="test"),
    )
    composer = ComposerServiceImpl.for_trained_operator(
        catalog=app.state.catalog_service,
        settings=app.state.settings,
        sessions_service=sessions,
        session_engine=app.state.session_engine,
    )
    app.state.composer_service = composer
    original_leaves = (asyncio.CancelledError("Synthetic owned cancellation"), AuditIntegrityError("Synthetic owned integrity failure"))
    original_failure = (
        BaseExceptionGroup("Synthetic owned EARLY mixed failure", original_leaves)
        if mixed_group
        else ValueError("Synthetic owned EARLY failure")
    )
    comparison_failure = RuntimeError("Synthetic private comparison failure")
    actual_accept = TurnEvidence.accept_checkpoint
    actual_persist = sessions.persist_compose_turn_async
    persisted = []
    calls = []

    async def persist(**kwargs):
        result = await actual_persist(**kwargs)
        persisted.append(result)
        return result

    def accept(owner, *args, **kwargs):
        actual_accept(owner, *args, **kwargs)
        if verification_fails:
            raise comparison_failure

    async def early(**kwargs):
        raise original_failure

    async def complete(**kwargs):
        calls.append(kwargs)
        assert "response_format" not in kwargs
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id="owned-saved-metadata",
                                function=SimpleNamespace(
                                    name="set_metadata",
                                    arguments=json.dumps({"patch": {"name": "Saved owned change"}}),
                                ),
                            ),
                        ],
                    ),
                    finish_reason="tool_calls",
                )
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20, "cost": 0.001},
            model=kwargs["model"],
        )

    monkeypatch.setattr(sessions, "persist_compose_turn_async", persist)
    monkeypatch.setattr(TurnEvidence, "accept_checkpoint", accept)
    monkeypatch.setattr(composer._advisor_checkpoint, "_maybe_run_early_checkpoint", early)
    monkeypatch.setattr(litellm, "acompletion", complete)
    session = await sessions.create_session("alice", "Existing title", "local")
    async with _running_job(app, sessions, session.id, kind="compose_message", content="Change the pipeline name.") as job:
        with pytest.raises(BaseExceptionGroup if mixed_group else ValueError) as caught:
            await _run(app, job)
        assert caught.value is original_failure
        if mixed_group:
            assert caught.value.exceptions == original_leaves
        assert (await _row(job)).status == "running"
        assert len(calls) == 1
        assert len(persisted) == 1
        assert persisted[0].checkpoint_disposition == "committed"
        required_work = job.observation.required_work
        assert required_work is not None
        (projection,) = tuple(
            ticket for ticket in required_work.tickets if ticket.key.source is RequiredWorkSource.COMPOSE_CHECKPOINT_PROJECTION
        )
        assert projection.complete
        if verification_fails:
            (verification_error,) = projection.errors
            assert isinstance(verification_error, AuditIntegrityError)
            assert verification_error.__cause__ is comparison_failure
            assert verification_error.failed_turn.assistant_message_id == persisted[0].assistant_id
            assert isinstance(caught.value.__cause__, BaseExceptionGroup)
            assert caught.value.__cause__.exceptions == (verification_error,)
            reduction = reduce_composer_failures(required_work.failure_receipts())
            assert reduction.winner.original_root is verification_error
        else:
            assert projection.errors == ()
            assert caught.value.__cause__ is None
        rows = await sessions.get_messages(session.id, limit=None)
        assert len([row for row in rows if row.role == "tool"]) == 1
        assert len([row for row in rows if row.role == "assistant" and row.tool_calls]) == 1
        versions = await sessions.get_state_versions(session.id)
        assert len(versions) == 1
        assert str(versions[0].id) == persisted[0].current_state_id


@pytest.mark.asyncio
async def test_send_turn_settles_the_route_response_and_binds_the_user_row(tmp_path: Path) -> None:
    app, service = _make_app(tmp_path)
    composer = _make_composer_mock("Hello from the turn.")
    app.state.composer_service = composer
    session = await service.create_session("alice", "Turn", "local")
    async with _running_job(app, service, session.id, kind="compose_message", content="Build a pipeline.") as job:
        assert job.lifecycle.durable_completed is False
        record = await _run(app, job)
        snapshot = await composer_app_services(app).progress_registry.get_latest(str(session.id))
        assert job.lifecycle.durable_completed is True

    assert record.status == "completed"
    assert record.settled_by == "owner_terminal"
    assert record.result_schema == "message_with_state.v1"
    assert record.result_json is not None
    response = MessageWithStateResponse.model_validate_json(record.result_json)
    assert response.message.content == "Hello from the turn."
    assert response.state is None
    assert response.proposals == []
    messages = await service.get_messages(session.id, limit=None)
    assert _user_and_assistant_roles(messages) == ["user", "assistant"]
    user_row = next(message for message in messages if message.role == "user")
    assert record.user_message_id == user_row.id
    kwargs = composer.compose.await_args.kwargs
    assert kwargs["user_message_id"] == str(user_row.id)
    assert kwargs["session_operation_context"] == job.lease.context
    # E14/F-C5: measured from the anchor immediately before compose(), so strictly below
    # the at-running 85.0 (a pass-through of the at-running value fails).
    assert 75.0 < kwargs["budget_seconds"] < 85.0
    # F-B2/C2: the success composite made the cohort durable; the job frame sees it.
    assert job.observation.compose_result is composer.compose.return_value
    assert job.observation.audit_cohort_durable is True
    assert job.observation.compose_base_state_id is None
    # E20: the progress request id is the persisted user-message id, never the operation id.
    assert snapshot.request_id == str(user_row.id)
    assert snapshot.phase == "complete"


@pytest.mark.asyncio
async def test_send_turn_joins_auto_title_under_the_adopted_lease_before_the_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E12: the title child finishes before the terminal composite. Known-negative: joined after, it reads ``completed``."""
    app, service = _make_app(tmp_path)
    app.state.composer_service = _make_composer_mock("Titled.")
    session = await service.create_session("alice", "New session", "local")
    job_holder: list[_Job] = []
    observed: list[tuple[object, str]] = []

    async def _fake_auto_title(**kwargs: Any) -> None:
        await asyncio.sleep(0.2)
        observed.append((kwargs["session_operation_context"], (await _row(job_holder[0])).status))

    monkeypatch.setattr(composer_turn, "maybe_auto_title_session", _fake_auto_title)
    async with _running_job(app, service, session.id, kind="compose_message", content="Title me.") as job:
        job_holder.append(job)
        record = await _run(app, job)

    assert record.status == "completed"
    assert observed == [(job.lease.context, "running")]


@pytest.mark.asyncio
async def test_recompose_turn_binds_the_retried_user_row_without_inserting_one(tmp_path: Path) -> None:
    """E9: the start composite bound ``user_message_id`` to the retried row; the turn inserts no user row."""
    app, service = _make_app(tmp_path)
    composer = _make_composer_mock("Recomposed.")
    app.state.composer_service = composer
    session = await service.create_session("alice", "Recompose", "local")
    user_row = await service.add_message(session.id, "user", "Build it again.", writer_principal="route_user_message")
    async with _running_job(app, service, session.id, kind="compose_recompose", expected_user_message_id=user_row.id) as job:
        record = await _run(app, job)
        snapshot = await composer_app_services(app).progress_registry.get_latest(str(session.id))

    assert record.status == "completed"
    assert record.user_message_id == user_row.id
    assert composer.compose.await_args.args[0] == "Build it again."
    assert composer.compose.await_args.kwargs["user_message_id"] == str(user_row.id)
    users = [message for message in await service.get_messages(session.id, limit=None) if message.role == "user"]
    assert [message.id for message in users] == [user_row.id]
    assert snapshot.request_id == str(user_row.id)


@pytest.mark.asyncio
async def test_turn_refuses_a_head_that_moved_under_its_own_lease(tmp_path: Path) -> None:
    """E7: the start composite proved head == bound base; a different head under the same lease is Tier 1.

    Known-positive for the invariant. Seeding from the head is only honest while
    the head is the base the operator saw; the turn refuses before any write.
    """
    app, service = _make_app(tmp_path)
    composer = _make_composer_mock()
    app.state.composer_service = composer
    session = await service.create_session("alice", "Moved", "local")
    async with _running_job(app, service, session.id, kind="compose_message", content="Go.") as job:
        await service.save_composition_state(
            session.id,
            CompositionStateData(is_valid=True),
            provenance="session_seed",
            session_operation_context=job.lease.context,
        )
        with pytest.raises(AuditIntegrityError, match="bound base"):
            await _run(app, job)
        assert (await _row(job)).status == "running"

    composer.compose.assert_not_awaited()
    assert [message for message in await service.get_messages(session.id, limit=None) if message.role == "user"] == []


@pytest.mark.asyncio
async def test_recompose_turn_refuses_a_transcript_that_moved_under_its_own_lease(tmp_path: Path) -> None:
    """E5: the start composite verified the transcript; a changed one under the same lease is Tier 1, not a 409."""
    app, service = _make_app(tmp_path)
    composer = _make_composer_mock()
    app.state.composer_service = composer
    session = await service.create_session("alice", "Transcript", "local")
    user_row = await service.add_message(session.id, "user", "Question.", writer_principal="route_user_message")
    async with _running_job(app, service, session.id, kind="compose_recompose", expected_user_message_id=user_row.id) as job:
        await service.add_message(
            session.id,
            "assistant",
            "Answer.",
            writer_principal="compose_loop",
            session_operation_context=job.lease.context,
        )
        with pytest.raises(AuditIntegrityError, match="Recompose saved response"):
            await _run(app, job)
        assert (await _row(job)).status == "running"

    composer.compose.assert_not_awaited()


@pytest.mark.asyncio
async def test_marker_delivered_while_the_settlement_child_runs_is_deferred_and_completed_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owned-child custody (`_join_freeform_owned_task`): the marker is deferred past the composite, then re-raised.

    The route analogue is test_freeform_route_custody.py:220-221 (caller cancel
    after provider return still settles). N12 reads ``durable_completed`` and
    writes no terminal after a completed one.
    """
    app, service = _make_app(tmp_path)
    app.state.composer_service = _make_composer_mock("Settled despite the marker.")
    session = await service.create_session("alice", "Deferred", "local")
    composite_entered = asyncio.Event()
    release = asyncio.Event()
    real_complete = service.complete_composer_async_operation

    async def _gated_complete(*args: Any, **kwargs: Any) -> ComposerOperationRecord:
        composite_entered.set()
        await release.wait()
        return await real_complete(*args, **kwargs)

    monkeypatch.setattr(service, "complete_composer_async_operation", _gated_complete)
    async with _running_job(app, service, session.id, kind="compose_message", content="Go.") as job:
        task = asyncio.create_task(_run(app, job))
        await asyncio.wait_for(composite_entered.wait(), timeout=5.0)
        task.cancel(composer_turn._COMPOSER_OPERATION_SHUTDOWN)
        await asyncio.sleep(0.05)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert job.lifecycle.durable_completed is True
        assert job.observation.audit_cohort_durable is True
        record = await _row(job)

    assert record.status == "completed"
    assert record.settled_by == "owner_terminal"
    assert _user_and_assistant_roles(await service.get_messages(session.id, limit=None)) == ["user", "assistant"]


@pytest.mark.parametrize("kind", ["compose_message", "compose_recompose"])
@pytest.mark.asyncio
async def test_cancelled_turn_joins_its_llm_sidecar_before_reraising(tmp_path: Path, kind: ComposerOperationKind) -> None:
    """Both kinds join the shielded persist (routes: messages.py:963-978, compose.py:705-720)."""
    app, service = _make_app(tmp_path)
    llm_call = _llm_call(
        status=ComposerLLMCallStatus.CANCELLED,
        model_returned=None,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        provider_request_id=None,
        error_class="CancelledError",
        error_message="CancelledError",
    )

    class _CancellingComposer:
        async def compose(self, *args: Any, **kwargs: Any) -> ComposerResult:
            del args, kwargs
            raise _cancelled_error_with_llm_call(llm_call)

    app.state.composer_service = _CancellingComposer()
    session = await service.create_session("alice", "Cancelled", "local")
    expected: uuid.UUID | None = None
    if kind == "compose_recompose":
        expected = (await service.add_message(session.id, "user", "Build it.", writer_principal="route_user_message")).id
    async with _running_job(
        app,
        service,
        session.id,
        kind=kind,
        content="Build it." if kind == "compose_message" else None,
        expected_user_message_id=expected,
    ) as job:
        with pytest.raises(asyncio.CancelledError):
            await _run(app, job)
        snapshot = await composer_app_services(app).progress_registry.get_latest(str(session.id))
        assert (await _row(job)).status == "running"
        # Cancellation retains the evidence observation. The durable witness makes
        # the frame persist a no-op after the joined exception-attached sidecar.
        assert job.observation.compose_result is None
        assert job.observation.pending_exception_llm_calls == (llm_call,)
        await composer_turn.persist_cancelled_turn_audit(composer_app_services(app), job.running, job.observation)

    rows = _llm_call_audit_rows(await service.get_messages(session.id, limit=None))
    assert len(rows) == 1
    assert rows[0][1]["call"]["status"] == "cancelled"
    # E22: only the cancel endpoint's marker is a user Stop; an unmarked cancel is a server fault.
    assert (snapshot.phase, snapshot.reason) == ("failed", "service_setup_failed")


@pytest.mark.asyncio
async def test_cancel_committed_during_compose_still_persists_the_attached_sidecar(tmp_path: Path) -> None:
    """F-B2/C2: the cancelled arm's LLM sidecar is audit-only, never fenced by a committed cancel.

    Known-negative: without ``audit_only=True`` in ``_persist_llm_calls`` (N07, E11),
    the positive predicate raises ``ComposerOperationCancelledDuringTurn`` out of the
    joined persist, which replaces the ``CancelledError`` and loses the sidecar.
    """
    app, service = _make_app(tmp_path)
    llm_call = _llm_call(
        status=ComposerLLMCallStatus.CANCELLED,
        model_returned=None,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        provider_request_id=None,
        error_class="CancelledError",
        error_message="CancelledError",
    )
    job_holder: list[_Job] = []
    app.state.composer_service = _CancelCommittingComposer(job_holder, raise_after=_cancelled_error_with_llm_call(llm_call))
    session = await service.create_session("alice", "Committed mid-compose", "local")
    async with _running_job(app, service, session.id, kind="compose_message", content="Stop me.") as job:
        job_holder.append(job)
        with pytest.raises(asyncio.CancelledError):
            await _run(app, job)
        assert (await _row(job)).status == "running"
        assert (await _row(job)).cancel_requested_at is not None

    rows = _llm_call_audit_rows(await service.get_messages(session.id, limit=None))
    assert len(rows) == 1
    assert rows[0][1]["call"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_committed_after_compose_keeps_the_llm_cohort_out_of_the_rollback(tmp_path: Path) -> None:
    """F-B2/C2 (spec §4): the terminal CAS loses to the cancel, the provider evidence does not.

    The composite rolls back the assistant row with its cohort and raises
    ``ComposerOperationCancelledDuringTurn``. The frame then persists the observed
    result's cohort through the audit-only path, once.
    """
    app, service = _make_app(tmp_path)
    result = ComposerResult(message="Too late.", state=_EMPTY_STATE, llm_calls=(_llm_call(),))
    job_holder: list[_Job] = []
    app.state.composer_service = _CancelCommittingComposer(job_holder, result=result)
    session = await service.create_session("alice", "Committed after compose", "local")
    async with _running_job(app, service, session.id, kind="compose_message", content="Stop after.") as job:
        job_holder.append(job)
        with pytest.raises(ComposerOperationCancelledDuringTurn):
            await _run(app, job)
        assert job.observation.compose_result is result
        assert job.observation.audit_cohort_durable is False
        assert job.lifecycle.durable_completed is False
        assert _llm_call_audit_rows(await service.get_messages(session.id, limit=None)) == []

        services = composer_app_services(app)
        await composer_turn.persist_cancelled_turn_audit(services, job.running, job.observation)
        assert job.observation.audit_cohort_durable is True
        await composer_turn.persist_cancelled_turn_audit(services, job.running, job.observation)
        assert (await _row(job)).status == "running"

    messages = await service.get_messages(session.id, limit=None)
    assert _user_and_assistant_roles(messages) == ["user"]
    rows = _llm_call_audit_rows(messages)
    assert len(rows) == 1
    assert rows[0][1]["call"]["status"] == ComposerLLMCallStatus.SUCCESS.value


@pytest.mark.asyncio
async def test_cancelled_turn_audit_is_a_no_op_after_the_success_composite(tmp_path: Path) -> None:
    """F-B2/C2 idempotency: a marker landing after the terminal committed must not duplicate the cohort."""
    app, service = _make_app(tmp_path)
    composer = _make_composer_mock("Settled.")
    composer.compose.return_value = ComposerResult(message="Settled.", state=_EMPTY_STATE, llm_calls=(_llm_call(),))
    app.state.composer_service = composer
    session = await service.create_session("alice", "Settled first", "local")
    async with _running_job(app, service, session.id, kind="compose_message", content="Go.") as job:
        record = await _run(app, job)
        assert record.status == "completed"
        assert job.observation.audit_cohort_durable is True
        await composer_turn.persist_cancelled_turn_audit(composer_app_services(app), job.running, job.observation)

    assert len(_llm_call_audit_rows(await service.get_messages(session.id, limit=None))) == 1


@pytest.mark.asyncio
async def test_post_compose_runtime_preflight_arm_persists_one_audit_row_per_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """B2-closure (a) / VERIFY B2 gap: the handler's cohort is durable, so the frame's persist adds nothing.

    Path 2 rebuilds the error with ``llm_calls=result.llm_calls`` and
    ``_handle_runtime_preflight_failure`` persists that cohort unconditionally
    (``_helpers.py:3488``). Known-negative: without the flag set after the
    handler returns, the frame writes a second ``llm_call_audit`` row.
    """
    app, service = _make_app(tmp_path)
    llm_call = _llm_call()
    changed_state = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=2)
    composer = _make_composer_mock()
    composer.compose.return_value = ComposerResult(message="Validated later.", state=changed_state, llm_calls=(llm_call,))
    app.state.composer_service = composer
    session = await service.create_session("alice", "Preflight path 2", "local")

    async def _refuse_preflight(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise ComposerRuntimePreflightError(original_exc=ValueError("preflight refused the saved state"), partial_state=None)

    # composer_turn imports the helper by name, so this patches only the turn's
    # post-compose call (path 2). partial_state=None skips the handler's own
    # partial-state save; the handler goes straight to its cohort write.
    monkeypatch.setattr(composer_turn, "_state_data_from_composer_state", _refuse_preflight)
    async with _running_job(app, service, session.id, kind="compose_message", content="Save it.") as job:
        with pytest.raises(HTTPException) as refused:
            await _run(app, job)
        assert refused.value.status_code == 500
        assert job.observation.compose_result is composer.compose.return_value
        assert job.observation.pending_exception_llm_calls == (llm_call,)
        assert job.observation.audit_cohort_durable is True
        assert len(_llm_call_audit_rows(await service.get_messages(session.id, limit=None))) == 1
        await composer_turn.persist_cancelled_turn_audit(composer_app_services(app), job.running, job.observation)
        assert (await _row(job)).status == "running"

    rows = _llm_call_audit_rows(await service.get_messages(session.id, limit=None))
    assert len(rows) == 1
    assert rows[0][1]["call"]["status"] == ComposerLLMCallStatus.SUCCESS.value


@pytest.mark.asyncio
async def test_cancel_fenced_convergence_handler_leaves_its_exception_llm_calls_to_the_frame(tmp_path: Path) -> None:
    """B2-closure (b): a committed cancel that fences the handler's partial-state save loses no provider evidence.

    ``_handle_convergence_error`` saves the partial state (a non-audit write,
    ``_helpers.py:3263-3268``) before its cohort write. Under a committed cancel
    the positive predicate raises ``ComposerOperationCancelledDuringTurn`` there;
    the handler's ``except SQLAlchemyError`` does not catch it, so its cohort
    write never runs. Known-negative: without the arm recording the calls first,
    the frame's persist is a no-op and the call leaves no row.
    """
    app, service = _make_app(tmp_path)
    llm_call = _llm_call()
    job_holder: list[_Job] = []
    app.state.composer_service = _CancelCommittingComposer(
        job_holder,
        raise_after=ComposerConvergenceError(3, budget_exhausted="composition", partial_state=_EMPTY_STATE, llm_calls=(llm_call,)),
    )
    session = await service.create_session("alice", "Fenced convergence", "local")
    async with _running_job(app, service, session.id, kind="compose_message", content="Converge.") as job:
        job_holder.append(job)
        with pytest.raises(ComposerOperationCancelledDuringTurn):
            await _run(app, job)
        assert job.observation.compose_result is None
        assert job.observation.pending_exception_llm_calls == (llm_call,)
        assert job.observation.audit_cohort_durable is False
        assert await service.get_current_state(session.id) is None
        assert _llm_call_audit_rows(await service.get_messages(session.id, limit=None)) == []

        services = composer_app_services(app)
        await composer_turn.persist_cancelled_turn_audit(services, job.running, job.observation)
        assert job.observation.audit_cohort_durable is True
        await composer_turn.persist_cancelled_turn_audit(services, job.running, job.observation)
        assert (await _row(job)).status == "running"
        assert (await _row(job)).cancel_requested_at is not None

    rows = _llm_call_audit_rows(await service.get_messages(session.id, limit=None))
    assert len(rows) == 1
    assert rows[0][1]["call"]["status"] == ComposerLLMCallStatus.SUCCESS.value


@pytest.mark.asyncio
async def test_turn_refuses_a_budget_anchor_that_is_not_its_admitted_budget(tmp_path: Path) -> None:
    app, service = _make_app(tmp_path)
    composer = _make_composer_mock()
    app.state.composer_service = composer
    session = await service.create_session("alice", "Mismatch", "local")
    mismatched = composer_turn.ComposerBudgetAnchor(remaining_at_running_seconds=60.0, monotonic_at_running=time.monotonic())
    async with _running_job(app, service, session.id, kind="compose_message", content="Go.") as job:
        with pytest.raises(ValueError, match="budget anchor"):
            await _run(app, job, budget_anchor=mismatched)

    composer.compose.assert_not_awaited()
    assert [message for message in await service.get_messages(session.id, limit=None) if message.role == "user"] == []


@pytest.mark.asyncio
async def test_compose_budget_is_measured_immediately_before_compose(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """E14/F-C5 (Codex C5): setup time after ``running`` is spent from the admitted budget, not added to it.

    Known-negative: a budget taken at turn entry, or the at-running value passed
    through, is above 84.5 here.
    """
    app, service = _make_app(tmp_path)
    composer = _make_composer_mock()
    app.state.composer_service = composer
    session = await service.create_session("alice", "Slow preamble", "local")
    async with _running_job(app, service, session.id, kind="compose_message", content="Go.") as job:
        real_load_context = service.load_graph_recovery_context

        async def _slow_load_context(session_id: uuid.UUID, **kwargs: Any) -> Any:
            await asyncio.sleep(0.5)
            return await real_load_context(session_id, **kwargs)

        monkeypatch.setattr(service, "load_graph_recovery_context", _slow_load_context)
        record = await _run(app, job)

    assert record.status == "completed"
    assert 75.0 < composer.compose.await_args.kwargs["budget_seconds"] <= 84.5


@pytest.mark.asyncio
async def test_expired_budget_raises_before_any_provider_call(tmp_path: Path) -> None:
    """E14/F-C5: a budget already spent when compose() would start never reaches the provider."""
    app, service = _make_app(tmp_path)
    composer = _make_composer_mock()
    app.state.composer_service = composer
    session = await service.create_session("alice", "Expired", "local")
    backdated = composer_turn.ComposerBudgetAnchor(remaining_at_running_seconds=85.0, monotonic_at_running=time.monotonic() - 100.0)
    async with _running_job(app, service, session.id, kind="compose_message", content="Too late.") as job:
        with pytest.raises(ComposerTurnDeadlineExpired) as expired:
            await _run(app, job, budget_anchor=backdated)
        assert (await _row(job)).status == "running"
        snapshot = await composer_app_services(app).progress_registry.get_latest(str(session.id))

    composer.compose.assert_not_awaited()
    assert expired.value.session_id == session.id
    assert expired.value.operation_id == job.turn.operation_id
    assert "expired" in str(expired.value)
    assert expired.value.budget_seconds_at_running == 85.0
    assert job.observation.compose_result is None
    assert (snapshot.phase, snapshot.reason) == ("failed", "convergence_wall_clock_timeout")


def _parked_auto_title_factory(started: asyncio.Event, cancelled: asyncio.Event) -> Any:
    """A stand-in for ``maybe_auto_title_session`` that parks until cancelled and records the cancel."""

    async def _parked_auto_title(**kwargs: Any) -> None:
        del kwargs
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    return _parked_auto_title


@pytest.mark.asyncio
async def test_auto_title_join_timeout_is_logged_and_never_relabels_the_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """E12 / review m3: a join timeout cancels and settles the child, is logged, and the turn still settles."""
    app, service = _make_app(tmp_path)
    app.state.composer_service = _make_composer_mock("Titled later.")
    session = await service.create_session("alice", "New session", "local")
    title_started, title_cancelled = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(composer_turn, "maybe_auto_title_session", _parked_auto_title_factory(title_started, title_cancelled))
    monkeypatch.setattr(composer_turn, "_AUTO_TITLE_JOIN_SECONDS", 0.05)
    async with _running_job(app, service, session.id, kind="compose_message", content="Title me.") as job:
        with capture_logs() as logs:
            record = await _run(app, job)

    assert record.status == "completed"
    assert title_started.is_set()
    assert title_cancelled.is_set()
    timed_out = [entry for entry in logs if entry["event"] == "composer_operation.auto_title_join_timed_out"]
    assert len(timed_out) == 1
    assert timed_out[0]["session_id"] == str(session.id)
    assert timed_out[0]["operation_id"] == job.turn.operation_id
    assert timed_out[0]["child_settled"] is True


@pytest.mark.asyncio
async def test_cancel_during_the_auto_title_join_settles_the_child_and_keeps_the_compose_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review m3 + F-B2/C2: a marker delivered in the pre-terminal title join orphans nothing and loses no evidence.

    Known-negative: dropping the child reference before the join leaves the
    child running past the turn (no ``auto_title_join_cancelled`` event,
    ``title_cancelled`` unset).
    """
    app, service = _make_app(tmp_path)
    composer = _make_composer_mock("Never published.")
    composer.compose.return_value = ComposerResult(message="Never published.", state=_EMPTY_STATE, llm_calls=(_llm_call(),))
    app.state.composer_service = composer
    session = await service.create_session("alice", "New session", "local")
    title_started, title_cancelled = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(composer_turn, "maybe_auto_title_session", _parked_auto_title_factory(title_started, title_cancelled))
    monkeypatch.setattr(composer_turn, "_AUTO_TITLE_JOIN_SECONDS", 30.0)
    async with _running_job(app, service, session.id, kind="compose_message", content="Title me.") as job:
        with capture_logs() as logs:
            task = asyncio.create_task(_run(app, job))
            await asyncio.wait_for(title_started.wait(), timeout=5.0)
            for _ in range(500):
                if job.observation.compose_result is not None:
                    break
                await asyncio.sleep(0.01)
            assert job.observation.compose_result is composer.compose.return_value
            await asyncio.sleep(0.05)
            assert not task.done()
            marker = composer_turn._COMPOSER_OPERATION_CANCEL_REQUESTED
            task.cancel(marker)
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert caught.value.args[0] is marker
        assert title_cancelled.is_set()
        assert job.observation.audit_cohort_durable is False
        assert job.lifecycle.durable_completed is False
        assert (await _row(job)).status == "running"
        await composer_turn.persist_cancelled_turn_audit(composer_app_services(app), job.running, job.observation)

    cancelled_joins = [entry for entry in logs if entry["event"] == "composer_operation.auto_title_join_cancelled"]
    assert len(cancelled_joins) == 1
    assert cancelled_joins[0]["operation_id"] == job.turn.operation_id
    assert cancelled_joins[0]["child_settled"] is True
    messages = await service.get_messages(session.id, limit=None)
    assert _user_and_assistant_roles(messages) == ["user"]
    assert len(_llm_call_audit_rows(messages)) == 1


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        (composer_turn._COMPOSER_OPERATION_LEASE_LOST, ("failed", "service_setup_failed")),
        (composer_turn._COMPOSER_OPERATION_SHUTDOWN, ("failed", "service_setup_failed")),
        (composer_turn._COMPOSER_OPERATION_CANCEL_REQUESTED, ("cancelled", "client_cancelled")),
        (None, ("failed", "service_setup_failed")),
    ],
)
@pytest.mark.asyncio
async def test_worker_cancel_markers_classify_the_cancelled_progress(tmp_path: Path, marker: object, expected: tuple[str, str]) -> None:
    """E22: only the cancel endpoint's marker is a user Stop; lease loss, shutdown and an unmarked cancel are server faults."""
    app, service = _make_app(tmp_path)
    started = asyncio.Event()

    class _ParkedComposer:
        async def compose(self, *args: Any, **kwargs: Any) -> ComposerResult:
            del args, kwargs
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable: the test cancels this call")

    app.state.composer_service = _ParkedComposer()
    session = await service.create_session("alice", "Marker", "local")
    async with _running_job(app, service, session.id, kind="compose_message", content="Hold.") as job:
        task = asyncio.create_task(_run(app, job))
        await asyncio.wait_for(started.wait(), timeout=5.0)
        if marker is None:
            task.cancel()
        else:
            task.cancel(marker)
        with pytest.raises(asyncio.CancelledError):
            await task
        snapshot = await composer_app_services(app).progress_registry.get_latest(str(session.id))

    assert (snapshot.phase, snapshot.reason) == expected


class _FailingPublishRegistry(ComposerProgressRegistry):
    def __init__(self, failure: BaseException) -> None:
        super().__init__()
        self._failure = failure

    async def claim_request(
        self,
        *,
        session_id: str,
        request_id: str | None,
        user_id: str,
        lease: ComposerRequestLease,
        operation_id: str | None = None,
        session_operation_id: str | None = None,
        session_operation_epoch: int | None = None,
    ) -> ComposerProgressSink:
        await super().claim_request(
            session_id=session_id,
            request_id=request_id,
            user_id=user_id,
            lease=lease,
            operation_id=operation_id,
            session_operation_id=session_operation_id,
            session_operation_epoch=session_operation_epoch,
        )
        failure = self._failure

        async def _publish(event: ComposerProgressEvent) -> None:
            del event
            raise failure

        return _publish


class _FailingClaimRegistry(ComposerProgressRegistry):
    async def claim_request(
        self,
        *,
        session_id: str,
        request_id: str | None,
        user_id: str,
        lease: ComposerRequestLease,
        operation_id: str | None = None,
        session_operation_id: str | None = None,
        session_operation_epoch: int | None = None,
    ) -> ComposerProgressSink:
        del session_id, request_id, user_id, lease, operation_id, session_operation_id, session_operation_epoch
        raise ComposerRequestLeaseLost("Composer request lease cannot be renewed")


@pytest.mark.asyncio
async def test_progress_publish_loss_is_advisory_and_never_decides_the_terminal(tmp_path: Path) -> None:
    """E20 known-positive: a lost request lease on publish does not fail the turn."""
    app, service = _make_app(tmp_path)
    app.state.composer_service = _make_composer_mock("Still settled.")
    app.state.composer_progress_registry = _FailingPublishRegistry(ComposerRequestLeaseLost("Composer request lease cannot be renewed"))
    session = await service.create_session("alice", "Advisory", "local")
    async with _running_job(app, service, session.id, kind="compose_message", content="Go.") as job:
        with capture_logs() as logs:
            record = await _run(app, job)
    assert record.status == "completed"
    assert [entry["phase"] for entry in logs if entry["event"] == "composer_operation.progress_publish_failed"] == [
        "starting",
        "complete",
    ]


@pytest.mark.asyncio
async def test_progress_claim_loss_is_advisory_and_never_decides_the_terminal(tmp_path: Path) -> None:
    """E20: a progress claim that fails leaves the turn running on a discarding sink."""
    app, service = _make_app(tmp_path)
    app.state.composer_service = _make_composer_mock("Settled without progress.")
    app.state.composer_progress_registry = _FailingClaimRegistry()
    session = await service.create_session("alice", "Advisory claim", "local")
    async with _running_job(app, service, session.id, kind="compose_message", content="Go.") as job:
        with capture_logs() as logs:
            record = await _run(app, job)
    assert record.status == "completed"
    claims = [entry for entry in logs if entry["event"] == "composer_operation.progress_claim_failed"]
    assert len(claims) == 1
    assert claims[0]["exc_class"] == "ComposerRequestLeaseLost"


@pytest.mark.asyncio
async def test_progress_publish_defect_is_not_swallowed(tmp_path: Path) -> None:
    """E20 known-negative: the advisory catch is the lease/ownership/DB family, not every exception."""
    app, service = _make_app(tmp_path)
    app.state.composer_service = _make_composer_mock()
    app.state.composer_progress_registry = _FailingPublishRegistry(RuntimeError("progress defect"))
    session = await service.create_session("alice", "Defect", "local")
    async with _running_job(app, service, session.id, kind="compose_message", content="Go.") as job:
        with pytest.raises(RuntimeError, match="progress defect"):
            await _run(app, job)
        assert (await _row(job)).status == "running"


@pytest.mark.asyncio
async def test_send_turn_auto_commit_settles_through_the_services_bundle_with_the_remaining_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D6 + E14: the settlement is reached through the bundle and receives what is left of the job budget."""
    app, service, _pipeline, session_id, creation, _endpoint = await _prepare_canonical_pipeline_route_proposal(
        tmp_path, monkeypatch, tool_call_id="turn-auto-pipeline"
    )
    composer = _make_composer_mock()

    async def compose_with_owned_proposal(*args: Any, **kwargs: Any) -> ComposerResult:
        row = await _create_running_pipeline_route_proposal(service, creation, kwargs)
        assert row.pipeline_metadata is not None
        return ComposerResult(
            message="Pipeline prepared.",
            state=_EMPTY_STATE,
            repair_turns_used=2,
            pipeline_commit_intent=PipelineCommitIntent(proposal_id=row.id, draft_hash=row.pipeline_metadata.draft_hash),
        )

    composer.compose.side_effect = compose_with_owned_proposal
    app.state.composer_service = composer
    seen_budgets: list[float] = []
    real_settle = composer_turn.settle_auto_commit_intent

    async def _spy_settle(**kwargs: Any) -> Any:
        seen_budgets.append(kwargs["commit_timeout_seconds"])
        return await real_settle(**kwargs)

    monkeypatch.setattr(composer_turn, "settle_auto_commit_intent", _spy_settle)
    async with _running_job(app, service, session_id, kind="compose_message", content="Build the pipeline.") as job:
        record = await _run(app, job)

    assert record.status == "completed"
    assert record.result_json is not None
    response = MessageWithStateResponse.model_validate_json(record.result_json)
    assert response.state is not None
    assert response.proposals == []
    settled = await service.list_composition_proposals(session_id)
    assert [proposal.status for proposal in settled] == ["committed"]
    assert str(settled[0].committed_state_id) == response.state.id
    assert len(seen_budgets) == 1
    assert 0.0 < seen_budgets[0] < 85.0


@pytest.mark.asyncio
async def test_spent_budget_before_auto_commit_is_the_settlement_timeout_504(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """E14: no settlement starts on a spent budget; the outcome is the settlement's own TIMEOUT 504."""
    app, service, _pipeline, session_id, row, _endpoint = await _create_canonical_pipeline_route_proposal(
        tmp_path, monkeypatch, tool_call_id="turn-spent-budget"
    )
    assert row.pipeline_metadata is not None
    intent = PipelineCommitIntent(proposal_id=row.id, draft_hash=row.pipeline_metadata.draft_hash)

    class _SlowComposer:
        async def compose(self, *args: Any, **kwargs: Any) -> ComposerResult:
            del args, kwargs
            await asyncio.sleep(0.5)
            return ComposerResult(message="Too slow.", state=_EMPTY_STATE, pipeline_commit_intent=intent)

    app.state.composer_service = _SlowComposer()
    async with _running_job(app, service, session_id, kind="compose_message", content="Build.", budget_seconds=0.3) as job:
        with pytest.raises(HTTPException) as timed_out:
            await _run(app, job)
        assert (await _row(job)).status == "running"
        assert job.observation.compose_result is not None
        assert job.observation.audit_cohort_durable is False

    assert (timed_out.value.status_code, timed_out.value.detail) == (504, composer_turn._SETTLEMENT_TIMEOUT_DETAIL)
    assert [proposal.status for proposal in await service.list_composition_proposals(session_id)] == ["pending"]


@pytest.mark.asyncio
async def test_recompose_planner_failure_progress_uses_the_send_copy(tmp_path: Path) -> None:
    """D7 (a recorded visible change): recompose now attributes a planner failure like send (messages.py:819-837)."""
    app, service = _make_app(tmp_path)

    class _PlannerFailingComposer:
        async def compose(self, *args: Any, **kwargs: Any) -> ComposerResult:
            del args, kwargs
            raise PipelinePlannerError("pricing is not configured", code="COST_UNAVAILABLE")

    app.state.composer_service = _PlannerFailingComposer()
    session = await service.create_session("alice", "Planner", "local")
    user_row = await service.add_message(session.id, "user", "Build it.", writer_principal="route_user_message")
    async with _running_job(app, service, session.id, kind="compose_recompose", expected_user_message_id=user_row.id) as job:
        with pytest.raises(HTTPException) as refused:
            await _run(app, job)
        snapshot = await composer_app_services(app).progress_registry.get_latest(str(session.id))

    assert refused.value.status_code == 503
    assert snapshot.phase == "failed"
    assert snapshot.headline == "The composer could not build a pipeline for this retry."
    assert snapshot.evidence == ("Cost accounting could not admit the model response.",)
    assert snapshot.likely_next == "Ask an administrator to configure or correct model pricing before trying again."
    assert snapshot.reason == _helpers.freeform_planner_progress_reason("COST_UNAVAILABLE")


@pytest.mark.asyncio
@pytest.mark.parametrize("narration", ["", "I updated the pipeline name."])
async def test_detached_retry_preserves_partial_tool_prefix_without_replaying_tools(tmp_path: Path, narration: str) -> None:
    from tests.unit.web.sessions.test_routes import _save_test_composition_state

    app, service = _make_app(tmp_path)
    composer = _make_composer_mock("Recovered reply.")
    app.state.composer_service = composer
    session = await service.create_session("alice", "Partial tool retry", "local")
    user = await service.add_message(session.id, "user", "Set the name once", writer_principal="route_user_message")
    state = await _save_test_composition_state(
        service, session.id, CompositionStateData(metadata_={"name": "Already set", "description": ""}), provenance="tool_call"
    )
    assistant = await service.add_message(
        session.id,
        "assistant",
        narration,
        writer_principal="compose_loop",
        tool_calls=[{"id": "call_done", "type": "function", "function": {"name": "set_metadata", "arguments": "{}"}}],
        composition_state_id=state.id,
    )
    tool = await service.add_message(
        session.id,
        "tool",
        '{"success":true}',
        writer_principal="compose_loop",
        tool_call_id="call_done",
        parent_assistant_id=assistant.id,
        composition_state_id=state.id,
    )
    before = await service.get_messages(session.id, limit=None)
    async with _running_job(app, service, session.id, kind="compose_recompose", expected_user_message_id=user.id) as job:
        record = await _run(app, job)
    assert record.status == "completed"
    composer.compose.assert_awaited_once()
    call = composer.compose.await_args
    assert call.args[0] == user.content
    assert call.args[2].metadata.name == "Already set"
    assert call.kwargs["current_state_id"] == str(state.id)
    assert call.kwargs["user_message_id"] == str(user.id)
    assert call.args[1] == [
        {"role": "user", "content": user.content, "_elspeth_user_authored": True, "_elspeth_user_message_id": str(user.id)},
        {"role": "assistant", "content": narration},
    ]
    after = await service.get_messages(session.id, limit=None)
    assert after[: len(before)] == before
    assert [row.id for row in after if row.role == "tool"] == [tool.id]
    assert [row.id for row in after if row.role == "user"] == [user.id]


@pytest.mark.asyncio
async def test_cancelled_turn_required_audit_failure_outranks_cancellation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy.exc import SQLAlchemyError

    from elspeth.web.required_work import RequiredWorkSource, RequiredWorkSubphase

    app, service = _make_app(tmp_path)
    call = _llm_call()

    class CancelledComposer:
        async def compose(self, *args: Any, **kwargs: Any) -> ComposerResult:
            raise _cancelled_error_with_llm_call(call)

    original_insert = service._insert_chat_message
    insert_failure = SQLAlchemyError("required audit insert failed")
    audit_insert_hits = 0

    def fail_insert(conn: Any, **kwargs: Any) -> str:
        nonlocal audit_insert_hits
        if kwargs["role"] == "audit" and kwargs["audit_only"] is True:
            assert kwargs["writer_principal"] == "compose_loop"
            assert kwargs["tool_calls"][0]["_kind"] == "llm_call_audit"
            audit_insert_hits += 1
            raise insert_failure
        return original_insert(conn, **kwargs)

    app.state.composer_service = CancelledComposer()
    session = await service.create_session("alice", "Required evidence", "local")
    async with _running_job(app, service, session.id, kind="compose_message", content="Go.") as job:
        monkeypatch.setattr(service, "_insert_chat_message", fail_insert)
        with pytest.raises(AuditIntegrityError, match="composer_turn_audit_cohort_persist_failed"):
            await _run(app, job)
        assert audit_insert_hits == 1
        assert job.observation.pending_exception_llm_calls == (call,)
        assert job.observation.audit_cohort_durable is False
        assert (await _row(job)).status == "running"
        required_work = job.observation.required_work
        assert required_work is not None
        audit_sql_tickets = tuple(
            ticket for ticket in required_work.tickets if ticket.key.source is RequiredWorkSource.REQUIRED_UNWIND_AUDIT_SQL
        )
        assert len(audit_sql_tickets) == 2
        (audit_producer,) = tuple(ticket for ticket in audit_sql_tickets if ticket.key.subphase is RequiredWorkSubphase.PRODUCER)
        (audit_sql_ticket,) = tuple(ticket for ticket in audit_sql_tickets if ticket.key.subphase is RequiredWorkSubphase.SQL_INITIAL)
        assert audit_producer.key.invocation_ordinal == audit_sql_ticket.key.invocation_ordinal
        assert audit_producer.complete and audit_producer.errors == () and audit_producer.receipts() == ()
        assert audit_sql_ticket.complete
        assert audit_sql_ticket.errors == (insert_failure,)
        (receipt,) = audit_sql_ticket.receipts()
        assert receipt.original_root is insert_failure
        (audit_projection,) = tuple(
            ticket for ticket in required_work.tickets if ticket.key.source is RequiredWorkSource.REQUIRED_UNWIND_AUDIT_PROJECTION
        )
        assert audit_projection.key.subphase is RequiredWorkSubphase.PROJECTION
        assert audit_projection.complete and audit_projection.errors == () and audit_projection.receipts() == ()
    assert required_work.all_completed


@pytest.mark.asyncio
async def test_owned_title_failure_has_nominal_priority_marker() -> None:
    from elspeth.web.sessions.composer_operations import ComposerOwnedSettlementFailure

    async def fail_title() -> None:
        raise RuntimeError("private title failure")

    title = asyncio.create_task(fail_title())
    with pytest.raises(ComposerOwnedSettlementFailure, match="owned settlement did not complete") as raised:
        await composer_turn._join_auto_title(title, session_id=uuid.uuid4(), operation_id=str(uuid.uuid4()), cancellation_observations=[])
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert "private" not in str(raised.value)


@pytest.mark.asyncio
async def test_owned_required_audit_unknown_failure_has_nominal_priority_marker() -> None:
    from elspeth.web.sessions.composer_operations import ComposerOwnedSettlementFailure

    async def fail_audit() -> None:
        raise RuntimeError("private settlement failure")

    observation = composer_turn.ComposerTurnObservation()
    with pytest.raises(ComposerOwnedSettlementFailure):
        await composer_turn._required_audit(observation, fail_audit())
    assert observation.audit_cohort_durable is False


@pytest.mark.asyncio
async def test_unknown_owned_terminal_child_failure_keeps_result_and_priority_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from elspeth.web.sessions.composer_operations import ComposerOwnedSettlementFailure

    app, service = _make_app(tmp_path)
    composer = _make_composer_mock("Provider result exists.")
    app.state.composer_service = composer
    session = await service.create_session("alice", "Owned terminal fault", "local")

    async def fail_terminal(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("private terminal transaction fault")

    async with _running_job(app, service, session.id, kind="compose_message", content="Go.") as job:
        monkeypatch.setattr(service, "complete_composer_async_operation", fail_terminal)
        with pytest.raises(ComposerOwnedSettlementFailure) as raised:
            await _run(app, job)
        assert isinstance(raised.value.__cause__, RuntimeError)
        assert job.observation.compose_result is composer.compose.return_value
        assert job.observation.audit_cohort_durable is False
        assert (await _row(job)).status == "running"


@pytest.mark.asyncio
async def test_cancellation_resistant_title_must_actually_finish_before_join_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def title_accounting() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        finished.set()

    monkeypatch.setattr(composer_turn, "_AUTO_TITLE_JOIN_SECONDS", 0.01)
    child = asyncio.create_task(title_accounting())
    await entered.wait()
    observed: list[asyncio.CancelledError] = []
    join = asyncio.create_task(
        composer_turn._join_auto_title(child, session_id=uuid.uuid4(), operation_id=str(uuid.uuid4()), cancellation_observations=observed)
    )
    await cancelled.wait()
    assert not join.done()
    assert not finished.is_set()
    marker = object()
    join.cancel(marker)
    for _ in range(100):
        if observed:
            break
        await asyncio.sleep(0.01)
    assert len(observed) == 1 and observed[0].args[0] is marker and not finished.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await join
    assert len(observed) == 1 and caught.value is observed[0] and caught.value.args[0] is marker
    assert child.done()
    assert finished.is_set()


@pytest.mark.asyncio
async def test_initial_auto_title_join_caller_cancellation_keeps_exact_original_and_settles_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, cancelled = asyncio.Event(), asyncio.Event()
    observed: list[asyncio.CancelledError] = []

    async def title_accounting() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(composer_turn, "_AUTO_TITLE_JOIN_SECONDS", 30.0)
    child = asyncio.create_task(title_accounting())
    await entered.wait()
    join = asyncio.create_task(
        composer_turn._join_auto_title(child, session_id=uuid.uuid4(), operation_id=str(uuid.uuid4()), cancellation_observations=observed)
    )
    await asyncio.sleep(0)
    assert not join.done()
    marker = object()
    join.cancel(marker)
    with pytest.raises(asyncio.CancelledError) as caught:
        await join
    assert len(observed) == 1 and caught.value is observed[0] and caught.value.args[0] is marker
    assert cancelled.is_set() and child.done()


@pytest.mark.asyncio
async def test_auto_title_timeout_retains_repeated_late_caller_cancellations_after_physical_child_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, service = _make_app(tmp_path)
    composer = _make_composer_mock("Never published after cancellation.")
    app.state.composer_service = composer
    session = await service.create_session("alice", "New session", "local")
    entered, child_cancelled, release, child_finished = (asyncio.Event() for _ in range(4))
    observations: list[asyncio.CancelledError] | None = None
    original_join = composer_turn._join_cancelled_auto_title

    async def resistant_title(**kwargs: Any) -> None:
        del kwargs
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            child_cancelled.set()
            await release.wait()
        child_finished.set()

    async def observe_join(task: asyncio.Task[None], *, cancellation_observations: list[asyncio.CancelledError]) -> None:
        nonlocal observations
        assert observations is None
        observations = cancellation_observations
        await original_join(task, cancellation_observations=cancellation_observations)

    monkeypatch.setattr(composer_turn, "maybe_auto_title_session", resistant_title)
    monkeypatch.setattr(composer_turn, "_join_cancelled_auto_title", observe_join)
    monkeypatch.setattr(composer_turn, "_AUTO_TITLE_JOIN_SECONDS", 0.01)
    async with _running_job(app, service, session.id, kind="compose_message", content="Title me.") as job:
        task = asyncio.create_task(_run(app, job))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            await asyncio.wait_for(child_cancelled.wait(), timeout=5)
            assert job.observation.compose_result is composer.compose.return_value
            assert not task.done() and not child_finished.is_set()
            first_marker = composer_turn._COMPOSER_OPERATION_CANCEL_REQUESTED
            second_marker = object()
            task.cancel(first_marker)
            for _ in range(100):
                if observations is not None and len(observations) == 1:
                    break
                await asyncio.sleep(0.01)
            assert observations is not None and len(observations) == 1 and observations[0].args[0] is first_marker
            task.cancel(second_marker)
            for _ in range(100):
                if len(observations) == 2:
                    break
                await asyncio.sleep(0.01)
            assert len(observations) == 2 and observations[1].args[0] is second_marker
            assert observations[0] is not observations[1] and not task.done() and not child_finished.is_set()
            assert (await _row(job)).status == "running"
            release.set()
            with pytest.raises(BaseExceptionGroup) as raised:
                await task
            assert len(raised.value.exceptions) == 2
            assert all(actual is retained for actual, retained in zip(raised.value.exceptions, observations, strict=True))
            assert child_finished.is_set()
            assert job.lifecycle.durable_completed is False
            assert (await _row(job)).status == "running"
        finally:
            release.set()
            if not task.done():
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_auto_title_late_caller_cancellation_keeps_physical_child_failure_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    entered, child_cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observed: list[asyncio.CancelledError] = []
    child_failure = AuditIntegrityError("Title accounting did not settle")

    async def failing_title() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            child_cancelled.set()
            await release.wait()
        raise child_failure

    monkeypatch.setattr(composer_turn, "_AUTO_TITLE_JOIN_SECONDS", 0.01)
    child = asyncio.create_task(failing_title())
    await entered.wait()
    join = asyncio.create_task(
        composer_turn._join_auto_title(child, session_id=uuid.uuid4(), operation_id=str(uuid.uuid4()), cancellation_observations=observed)
    )
    try:
        await asyncio.wait_for(child_cancelled.wait(), timeout=5)
        marker = object()
        join.cancel(marker)
        for _ in range(100):
            if observed:
                break
            await asyncio.sleep(0.01)
        assert len(observed) == 1 and observed[0].args[0] is marker and not child.done()
        release.set()
        with pytest.raises(BaseExceptionGroup) as raised:
            await join
        assert len(raised.value.exceptions) == 2
        assert raised.value.exceptions[0] is child_failure
        assert raised.value.exceptions[1] is observed[0]
        assert child.done()
    finally:
        release.set()
        if not join.done():
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(join, timeout=5)


@pytest.mark.asyncio
async def test_required_audit_joins_delayed_child_before_setting_durable_witness() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def required_insert() -> None:
        entered.set()
        await release.wait()
        finished.set()

    observation = composer_turn.ComposerTurnObservation()
    join = asyncio.create_task(composer_turn._required_audit(observation, required_insert()))
    await entered.wait()
    join.cancel("first caller cancellation")
    join.cancel("second caller cancellation")
    assert observation.audit_cohort_durable is False
    assert not finished.is_set()
    release.set()
    await join
    assert finished.is_set()
    assert observation.audit_cohort_durable is True
    assert observation.deferred_cancellation is not None
