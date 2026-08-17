"""Post-commit Composer provider telemetry settlement seams."""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.web.composer import provider_telemetry
from elspeth.web.composer.audit import llm_call_audit_envelope, llm_call_audit_summary
from elspeth.web.sessions import service as service_module
from elspeth.web.sessions.models import chat_messages_table
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry


def _call() -> ComposerLLMCall:
    now = datetime(2026, 8, 4, tzinfo=UTC)
    return ComposerLLMCall(
        model_requested="secret-model",
        model_returned="secret-returned-model",
        status=ComposerLLMCallStatus.SUCCESS,
        prompt_tokens=1,
        completion_tokens=2,
        total_tokens=3,
        latency_ms=41,
        provider_request_id="secret-request-id",
        messages_hash="a" * 64,
        tools_spec_hash=None,
        declared_tool_names=(),
        started_at=now,
        finished_at=now,
        error_class=None,
        error_message=None,
        temperature=None,
        seed=None,
    )


def _service(engine) -> SessionServiceImpl:
    return SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.composer-provider-telemetry"),
    )


class _Instrument:
    def __init__(self) -> None:
        self.points: list[tuple[int | float, dict[str, str]]] = []

    def record(self, value: int | float, attributes: dict[str, str]) -> None:
        self.points.append((value, dict(attributes)))


@pytest.mark.asyncio
async def test_freeform_call_projects_only_after_audit_row_commit(engine, monkeypatch) -> None:
    service = _service(engine)
    session_id = (await service.create_session("alice", "telemetry", "local")).id
    call = _call()
    observed: list[tuple[str, str, object]] = []

    def capture(*, role: str, writer_principal: str, tool_calls: object) -> None:
        with engine.connect() as conn:
            durable_count = conn.execute(
                select(func.count()).select_from(chat_messages_table).where(chat_messages_table.c.session_id == str(session_id))
            ).scalar_one()
        observed.append((role, writer_principal, (tool_calls, durable_count)))

    monkeypatch.setattr(service_module, "record_settled_composer_audit_message", capture, raising=False)

    await service.add_message(
        session_id,
        "audit",
        llm_call_audit_summary(call),
        writer_principal="compose_loop",
        tool_calls=[llm_call_audit_envelope(call)],
    )

    assert len(observed) == 1
    role, writer_principal, payload = observed[0]
    tool_calls, durable_count = payload
    assert role == "audit"
    assert writer_principal == "compose_loop"
    assert tool_calls == [llm_call_audit_envelope(call)]
    assert durable_count == 1


@pytest.mark.asyncio
async def test_freeform_rollback_projects_nothing(engine, monkeypatch) -> None:
    service = _service(engine)
    observed: list[object] = []
    monkeypatch.setattr(
        service_module,
        "record_settled_composer_audit_message",
        lambda **kwargs: observed.append(kwargs),
        raising=False,
    )
    call = _call()

    with pytest.raises(IntegrityError):
        await service.add_message(
            uuid4(),
            "audit",
            llm_call_audit_summary(call),
            writer_principal="compose_loop",
            tool_calls=[llm_call_audit_envelope(call)],
        )

    assert observed == []


@pytest.mark.asyncio
async def test_freeform_cancellation_projects_worker_commit_before_reraising(engine, monkeypatch) -> None:
    service = _service(engine)
    session_id = (await service.create_session("alice", "cancelled telemetry", "local")).id
    call = _call()
    started = threading.Event()
    release = threading.Event()
    worker_done = threading.Event()
    original_run_sync = service._run_sync

    async def blocked_run_sync(func, *args, **kwargs):
        def blocked() -> object:
            started.set()
            assert release.wait(timeout=5)
            try:
                return func(*args, **kwargs)
            finally:
                worker_done.set()

        return await original_run_sync(blocked)

    projected: list[object] = []
    request_calls = _Instrument()
    monkeypatch.setattr(service, "_run_sync", blocked_run_sync)
    original_projector = service_module.record_settled_composer_audit_message

    def capture_projection(**kwargs: object) -> None:
        original_projector(**kwargs)
        projected.append(kwargs)

    monkeypatch.setattr(
        service_module,
        "record_settled_composer_audit_message",
        capture_projection,
    )
    monkeypatch.setattr(provider_telemetry, "_REQUEST_DURATION", _Instrument())
    monkeypatch.setattr(provider_telemetry, "_REQUEST_PROVIDER_CALLS", request_calls)
    metrics_token = provider_telemetry.begin_composer_request_metrics(surface="freeform")

    task = asyncio.create_task(
        service.add_message(
            session_id,
            "audit",
            llm_call_audit_summary(call),
            writer_principal="compose_loop",
            tool_calls=[llm_call_audit_envelope(call)],
        )
    )
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    provider_telemetry.finish_composer_request_metrics(metrics_token, status="cancelled")
    assert await asyncio.to_thread(worker_done.wait, 5)
    with engine.connect() as conn:
        durable_count = conn.execute(
            select(func.count()).select_from(chat_messages_table).where(chat_messages_table.c.session_id == str(session_id))
        ).scalar_one()
    assert durable_count == 1
    assert len(projected) == 1
    assert request_calls.points == [(1, {"surface": "freeform", "status": "cancelled"})]


@pytest.mark.asyncio
async def test_guided_cancellation_projects_worker_commit_before_reraising(engine, monkeypatch) -> None:
    service = _service(engine)
    call = _call()
    started = threading.Event()
    release = threading.Event()
    worker_done = threading.Event()
    original_run_sync = service._run_sync

    async def blocked_run_sync(func, *args, **kwargs):
        def blocked() -> object:
            started.set()
            assert release.wait(timeout=5)
            try:
                return func(*args, **kwargs)
            finally:
                worker_done.set()

        return await original_run_sync(blocked)

    committed: list[bool] = []
    projected: list[tuple[ComposerLLMCall, ...]] = []
    monkeypatch.setattr(service, "_run_sync", blocked_run_sync)
    monkeypatch.setattr(
        service_module,
        "record_settled_composer_provider_calls",
        lambda calls, *, surface: projected.append(calls),
    )

    task = asyncio.create_task(
        service._run_guided_sync_with_provider_projection(
            lambda: committed.append(True),
            llm_calls=(call,),
        )
    )
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(worker_done.wait, 5)
    assert committed == [True]
    assert projected == [(call,)]


@pytest.mark.asyncio
async def test_guided_cancellation_projects_nothing_when_worker_rolls_back(engine, monkeypatch) -> None:
    service = _service(engine)
    call = _call()
    started = threading.Event()
    release = threading.Event()
    original_run_sync = service._run_sync

    async def blocked_run_sync(func, *args, **kwargs):
        def blocked() -> object:
            started.set()
            assert release.wait(timeout=5)
            return func(*args, **kwargs)

        return await original_run_sync(blocked)

    projected: list[tuple[ComposerLLMCall, ...]] = []
    monkeypatch.setattr(service, "_run_sync", blocked_run_sync)
    monkeypatch.setattr(
        service_module,
        "record_settled_composer_provider_calls",
        lambda calls, *, surface: projected.append(calls),
    )

    def roll_back() -> None:
        raise IntegrityError("rollback", {}, RuntimeError("database rejected transaction"))

    task = asyncio.create_task(
        service._run_guided_sync_with_provider_projection(
            roll_back,
            llm_calls=(call,),
        )
    )
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError) as cancelled:
        await task
    assert isinstance(cancelled.value.__cause__, IntegrityError)
    assert projected == []


@pytest.mark.parametrize(
    "method",
    (
        pytest.param(SessionServiceImpl.fail_guided_operation_with_audit, id="fail_guided_operation_with_audit"),
        pytest.param(SessionServiceImpl.save_state_for_guided_operation, id="save_state_for_guided_operation"),
        pytest.param(SessionServiceImpl.settle_guided_state_operation, id="settle_guided_state_operation"),
        pytest.param(SessionServiceImpl.stage_guided_full_pipeline_proposal, id="stage_guided_full_pipeline_proposal"),
        pytest.param(SessionServiceImpl.decline_guided_full_pipeline_proposal, id="decline_guided_full_pipeline_proposal"),
        pytest.param(SessionServiceImpl.stage_guided_pipeline_proposal, id="stage_guided_pipeline_proposal"),
        pytest.param(SessionServiceImpl.back_edit_guided_pipeline_proposal, id="back_edit_guided_pipeline_proposal"),
        pytest.param(SessionServiceImpl.accept_guided_pipeline_proposal, id="accept_guided_pipeline_proposal"),
    ),
)
def test_every_unconditional_guided_audit_settlement_uses_post_commit_projection(method: Callable[..., object]) -> None:
    source = inspect.getsource(method)

    assert "_run_guided_sync_with_provider_projection" in source


def test_convergent_guided_start_projects_only_on_the_audit_inserting_branch() -> None:
    source = inspect.getsource(SessionServiceImpl.seed_or_complete_guided_start_operation)

    assert "GuidedStartStateSeeded" in source
    assert "record_settled_composer_provider_calls" in source
