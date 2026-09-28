"""Post-commit Composer provider telemetry settlement seams."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.composer import provider_telemetry
from elspeth.web.composer.audit import llm_call_audit_envelope, llm_call_audit_summary
from elspeth.web.coordination.contracts import SessionOperationKind
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions import service as service_module
from elspeth.web.sessions._persist_payload import AuditMessageDraft
from elspeth.web.sessions.models import chat_messages_table, quota_provider_attempts_table
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web.sessions.session_test_authority import FencedSessionServiceHarness


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
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    return FencedSessionServiceHarness(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.composer-provider-telemetry"),
    )


class _Instrument:
    def __init__(self) -> None:
        self.points: list[tuple[int | float, dict[str, str]]] = []

    def record(self, value: int | float, attributes: dict[str, str]) -> None:
        self.points.append((value, dict(attributes)))

    def add(self, value: int | float, attributes: dict[str, str]) -> None:
        self.record(value, attributes)


@pytest.mark.asyncio
async def test_replayed_provider_checkpoint_projects_only_durable_call(engine, monkeypatch) -> None:
    service = _service(engine)
    session_id = (await service.create_session("alice", "checkpoint replay", "local")).id
    provider_calls = _Instrument()
    provider_durations = _Instrument()
    request_calls = _Instrument()
    monkeypatch.setattr(provider_telemetry, "_PROVIDER_CALL_COUNTER", provider_calls)
    monkeypatch.setattr(provider_telemetry, "_PROVIDER_CALL_DURATION", provider_durations)
    monkeypatch.setattr(provider_telemetry, "_REQUEST_PROVIDER_CALLS", request_calls)

    token = provider_telemetry.begin_composer_request_metrics(surface="freeform")
    try:
        async with await SessionOperationLease.acquire(
            service.session_operation_authority,
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        ) as lease:
            attempt = await service.begin_provider_attempt(session_operation_context=lease.context, source="composer")
            call = replace(_call(), call_id=attempt.attempt_id, started_at=attempt.started_at, finished_at=attempt.started_at)
            await service.finish_provider_attempt(session_operation_context=lease.context, call=call)
            with engine.connect() as conn:
                initial_rows = conn.execute(
                    select(func.count()).select_from(chat_messages_table).where(chat_messages_table.c.session_id == str(session_id))
                ).scalar_one()
            await service.finish_provider_attempt(session_operation_context=lease.context, call=call)
            with engine.connect() as conn:
                replay_rows = conn.execute(
                    select(func.count()).select_from(chat_messages_table).where(chat_messages_table.c.session_id == str(session_id))
                ).scalar_one()
    finally:
        provider_telemetry.finish_composer_request_metrics(token, status="completed")

    assert (initial_rows, replay_rows) == (1, 1)
    assert provider_calls.points == [(1, {"surface": "freeform", "status": "success"})]
    assert provider_durations.points == [(0.041, {"surface": "freeform", "status": "success"})]
    assert request_calls.points == [(1, {"surface": "freeform", "status": "completed"})]


@pytest.mark.asyncio
async def test_partially_replayed_cohort_projects_only_its_new_envelope(engine, monkeypatch) -> None:
    service = _service(engine)
    session_id = (await service.create_session("alice", "partial replay", "local")).id
    provider_calls = _Instrument()
    monkeypatch.setattr(provider_telemetry, "_PROVIDER_CALL_COUNTER", provider_calls)

    async with await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    ) as lease:
        first_attempt = await service.begin_provider_attempt(session_operation_context=lease.context, source="composer")
        first = replace(
            _call(), call_id=first_attempt.attempt_id, started_at=first_attempt.started_at, finished_at=first_attempt.started_at
        )
        await service.finish_provider_attempt(session_operation_context=lease.context, call=first)
        second_attempt = await service.begin_provider_attempt(session_operation_context=lease.context, source="composer")
        second = replace(
            _call(), call_id=second_attempt.attempt_id, started_at=second_attempt.started_at, finished_at=second_attempt.started_at
        )
        third_attempt = await service.begin_provider_attempt(session_operation_context=lease.context, source="composer")
        third = replace(
            _call(), call_id=third_attempt.attempt_id, started_at=third_attempt.started_at, finished_at=third_attempt.started_at
        )
        await service.add_messages_atomic(
            session_id,
            (
                AuditMessageDraft(role="audit", content="Already recorded.", tool_calls=(llm_call_audit_envelope(first),)),
                AuditMessageDraft(
                    role="audit",
                    content="Partially recorded.",
                    tool_calls=(llm_call_audit_envelope(first), llm_call_audit_envelope(second)),
                ),
                AuditMessageDraft(role="audit", content="Fresh call.", tool_calls=(llm_call_audit_envelope(third),)),
                AuditMessageDraft(role="audit", content="Ordinary breadcrumb."),
            ),
            writer_principal="compose_loop",
            session_operation_context=lease.context,
        )

    with engine.connect() as conn:
        rows = conn.execute(
            select(chat_messages_table.c.content, chat_messages_table.c.tool_calls)
            .where(chat_messages_table.c.session_id == str(session_id))
            .order_by(chat_messages_table.c.sequence_no)
        ).all()
    assert len(rows) == 4
    assert [row.content for row in rows[1:]] == ["Partially recorded.", "Fresh call.", "Ordinary breadcrumb."]
    assert len(rows[1].tool_calls) == 1
    assert rows[1].tool_calls[0]["call"]["call_id"] == second.call_id
    assert rows[2].tool_calls[0]["call"]["call_id"] == third.call_id
    assert rows[3].tool_calls is None
    assert provider_calls.points == [
        (1, {"surface": "freeform", "status": "success"}),
        (1, {"surface": "freeform", "status": "success"}),
        (1, {"surface": "freeform", "status": "success"}),
    ]


@pytest.mark.asyncio
async def test_atomic_audit_cohort_rollback_projects_nothing(engine, monkeypatch) -> None:
    service = _service(engine)
    session_id = (await service.create_session("alice", "cohort rollback", "local")).id
    provider_calls = _Instrument()
    monkeypatch.setattr(provider_telemetry, "_PROVIDER_CALL_COUNTER", provider_calls)

    async with await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    ) as lease:
        attempt = await service.begin_provider_attempt(session_operation_context=lease.context, source="composer")
        call = replace(_call(), call_id=attempt.attempt_id, started_at=attempt.started_at, finished_at=attempt.started_at)
        with pytest.raises(IntegrityError):
            await service.add_messages_atomic(
                session_id,
                (
                    AuditMessageDraft(role="audit", content="Provider call recorded.", tool_calls=(llm_call_audit_envelope(call),)),
                    AuditMessageDraft(role="audit", content="Invalid parent.", parent_assistant_id=str(uuid4())),
                ),
                writer_principal="compose_loop",
                session_operation_context=lease.context,
            )

    with engine.connect() as conn:
        durable_rows = conn.execute(
            select(func.count()).select_from(chat_messages_table).where(chat_messages_table.c.session_id == str(session_id))
        ).scalar_one()
        pending_attempt = conn.execute(
            select(quota_provider_attempts_table.c.settled_at).where(quota_provider_attempts_table.c.attempt_id == attempt.attempt_id)
        ).scalar_one()
    assert durable_rows == 0
    assert pending_attempt is None
    assert provider_calls.points == []


@pytest.mark.asyncio
async def test_cancelled_atomic_audit_cohort_projects_committed_call_before_reraising(engine, monkeypatch) -> None:
    service = _service(engine)
    session_id = (await service.create_session("alice", "cancelled cohort", "local")).id
    provider_calls = _Instrument()
    monkeypatch.setattr(provider_telemetry, "_PROVIDER_CALL_COUNTER", provider_calls)

    async with await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    ) as lease:
        attempt = await service.begin_provider_attempt(session_operation_context=lease.context, source="composer")
        call = replace(_call(), call_id=attempt.attempt_id, started_at=attempt.started_at, finished_at=attempt.started_at)
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

        monkeypatch.setattr(service, "_run_sync", blocked_run_sync)
        task = asyncio.create_task(
            service.add_messages_atomic(
                session_id,
                (AuditMessageDraft(role="audit", content="Provider call recorded.", tool_calls=(llm_call_audit_envelope(call),)),),
                writer_principal="compose_loop",
                session_operation_context=lease.context,
            )
        )
        assert await run_sync_in_worker(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert worker_done.is_set()

    with engine.connect() as conn:
        durable_rows = conn.execute(
            select(func.count()).select_from(chat_messages_table).where(chat_messages_table.c.session_id == str(session_id))
        ).scalar_one()
    assert durable_rows == 1
    assert provider_calls.points == [(1, {"surface": "freeform", "status": "success"})]


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
    # P4-D6 family A2b: the write is fenced, so an absent session is now
    # refused at the FENCE, before a transaction is ever opened — which no
    # longer exercises the rollback this test measures. Use a real session so
    # the fence passes, and let the row fail at the DATABASE instead: an
    # ``audit`` row carrying a parent violates the ``ck_chat_messages_parent_role``
    # CHECK constraint, so the INSERT raises inside the transaction, the
    # transaction unwinds, and the post-commit projection must not fire.
    session_id = (await service.create_session("alice", "rollback telemetry", "local")).id
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
            session_id,
            "audit",
            llm_call_audit_summary(call),
            writer_principal="compose_loop",
            tool_calls=[llm_call_audit_envelope(call)],
            parent_assistant_id=uuid4(),
        )

    assert observed == []
    with engine.connect() as conn:
        assert (
            conn.execute(
                select(func.count()).select_from(chat_messages_table).where(chat_messages_table.c.session_id == str(session_id))
            ).scalar_one()
            == 0
        )


@pytest.mark.asyncio
async def test_freeform_cancellation_projects_worker_commit_before_reraising(engine, monkeypatch) -> None:
    service = _service(engine)
    session_id = (await service.create_session("alice", "cancelled telemetry", "local")).id
    call = _call()
    # P4-D6 family A2b: acquire the turn's COMPOSE operation BEFORE
    # ``_run_sync`` is blocked below and pass it explicitly. The harness would
    # otherwise acquire it through the same ``_run_sync`` this test suspends,
    # so ``started`` would fire on the fence acquisition instead of on the
    # message write the cancellation is aimed at.
    compose_context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
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
            session_operation_context=compose_context,
        )
    )
    assert await run_sync_in_worker(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    provider_telemetry.finish_composer_request_metrics(metrics_token, status="cancelled")
    assert await run_sync_in_worker(worker_done.wait, 5)
    with engine.connect() as conn:
        durable_count = conn.execute(
            select(func.count()).select_from(chat_messages_table).where(chat_messages_table.c.session_id == str(session_id))
        ).scalar_one()
    assert durable_count == 1
    assert len(projected) == 1
    assert request_calls.points == [(1, {"surface": "freeform", "status": "cancelled"})]
