"""Physical Composer dispatch through real fenced quota admission and settlement."""

from __future__ import annotations

import asyncio
import dataclasses
import sqlite3
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import structlog
from litellm.exceptions import APIError, ServiceUnavailableError
from sqlalchemy import select
from sqlalchemy.engine import Engine

from elspeth.contracts.chargeable_admission import AdmissionRefusalReason, ChargeableAdmissionPolicy, ChargeableAdmissionRefused
from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.llm_response_parsing import build_llm_call_record
from elspeth.web.composer.pipeline_proposal import PlannerSurface
from elspeth.web.composer.provider_quota import admit_provider_attempt, composer_quota_scope, quota_provider_calls
from elspeth.web.composer.service import _litellm_acompletion
from elspeth.web.coordination import chargeable_admission_authority, quota_authority
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.quota_authority import QuotaExceeded
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.secrets.wiring_policy import EMPTY_SECRET_WIRING_POLICY
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import chat_messages_table, quota_provider_attempts_table, token_usage_ledger_table
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.fenced_session import IDENTITY_TOKENS_PER_DAY, seed_token_policies
from tests.unit.web.composer.test_pipeline_planner import _custody, _origin, _plan, _response, _ScriptedCompletion


@pytest.fixture
def quota_service(tmp_path: Path) -> Iterator[tuple[Engine, SessionServiceImpl, list[QuotaExceeded]]]:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'quota-dispatch.db'}")
    initialize_session_schema(engine)
    with engine.begin() as connection:
        ensure_test_identity(connection, identity_id="alice")
        seed_token_policies(connection, identity_id="alice")
    refusals: list[QuotaExceeded] = []
    service = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.quota_dispatch"),
        session_operation_authority=SQLiteLocalSessionOperationAuthority(engine),
        chargeable_admission_policy=ChargeableAdmissionPolicy(
            identity_token_quota_configured=True, secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash
        ),
        quota_exceeded_recorder=refusals.append,
    )
    yield engine, service, refusals
    engine.dispose()


async def _lease(service: SessionServiceImpl, session_id) -> SessionOperationLease:
    return await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    )


@quota_provider_calls
async def _audited_dispatch(*, tokens: int | None, fail: bool = False, finished_at: datetime | None = None) -> None:
    recorder = BufferingRecorder()
    status = ComposerLLMCallStatus.SUCCESS
    try:
        await _litellm_acompletion(model="test/model", messages=[])
    except TimeoutError:
        status = ComposerLLMCallStatus.TIMEOUT
        raise
    finally:
        call = build_llm_call_record(
            model_requested="test/model",
            messages=[],
            tools=None,
            status=status,
            started_at=datetime.now(UTC),
            started_ns=time.monotonic_ns(),
            temperature=None,
            seed=None,
            error_class="TimeoutError" if fail else None,
            error_message="timed out" if fail else None,
        )
        recorder.record_llm_call(
            dataclasses.replace(
                call,
                prompt_tokens=tokens,
                completion_tokens=0 if tokens is not None else None,
                finished_at=finished_at if finished_at is not None else call.finished_at,
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("tokens", [IDENTITY_TOKENS_PER_DAY, IDENTITY_TOKENS_PER_DAY + 1])
async def test_real_admission_settles_spend_before_refusing_next_dispatch(
    quota_service, monkeypatch: pytest.MonkeyPatch, tokens: int
) -> None:
    engine, service, refusals = quota_service
    session = await service.create_session(user_id="alice", title="Quota", auth_provider_type="local")
    dispatches: list[str] = []

    async def provider(**kwargs: Any) -> None:
        with engine.connect() as connection:
            pending = connection.execute(select(quota_provider_attempts_table)).all()
        assert len(pending) == 1 and pending[0].settled_at is None
        dispatches.append("sent")

    monkeypatch.setattr("litellm.acompletion", provider)
    async with await _lease(service, session.id) as lease:
        with composer_quota_scope(service, lease.context):
            await _audited_dispatch(tokens=tokens)
            with engine.connect() as connection:
                usage = connection.execute(select(token_usage_ledger_table)).one()
            assert usage.prompt_tokens == tokens and usage.completion_tokens == 0
            with pytest.raises(ChargeableAdmissionRefused) as caught:
                await _audited_dispatch(tokens=1)
    assert caught.value.decision.refusal_reason is AdmissionRefusalReason.QUOTA_EXCEEDED
    assert dispatches == ["sent"]
    assert len(refusals) == 1
    assert (refusals[0].dimension, refusals[0].cap, refusals[0].ceiling, refusals[0].usage) == ("tokens", 1000, 5000, tokens)


@pytest.mark.asyncio
async def test_timeout_with_unknown_usage_blocks_next_physical_call(quota_service, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, service, refusals = quota_service
    session = await service.create_session(user_id="alice", title="Quota", auth_provider_type="local")
    dispatches: list[str] = []

    async def provider(**kwargs: Any) -> None:
        dispatches.append("sent")
        raise TimeoutError("provider response lost")

    monkeypatch.setattr("litellm.acompletion", provider)
    async with await _lease(service, session.id) as lease:
        with composer_quota_scope(service, lease.context):
            with pytest.raises(TimeoutError):
                await _audited_dispatch(tokens=None, fail=True)
            with pytest.raises(ChargeableAdmissionRefused) as caught:
                await _audited_dispatch(tokens=1)
    with engine.connect() as connection:
        usage = connection.execute(select(token_usage_ledger_table)).one()
    assert usage.prompt_tokens is None and usage.completion_tokens is None
    assert caught.value.decision.refusal_reason is AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE
    assert dispatches == ["sent"]
    assert refusals == []


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", [PlannerSurface.FREEFORM, PlannerSurface.GUIDED_FULL, PlannerSurface.TUTORIAL_PROFILE])
async def test_second_planning_call_503_settles_before_quota_refuses_retry(
    quota_service,
    tmp_path: Path,
    tool_context,
    surface: PlannerSurface,
) -> None:
    engine, service, _ = quota_service
    session = await service.create_session(user_id="alice", title="Planner quota", auth_provider_type="local")
    secret = "UPSTREAM-RESPONSE-BODY-SECRET"

    class AdmittedCompletion(_ScriptedCompletion):
        async def __call__(self, **kwargs: Any) -> Any:
            await admit_provider_attempt(model=kwargs["model"])
            return await super().__call__(**kwargs)

    script = AdmittedCompletion(
        _response(("list_sources", {})),
        ServiceUnavailableError(message=secret, llm_provider="test-provider", model="test/planner"),
    )

    recorder = BufferingRecorder()
    origin = dataclasses.replace(_origin(), session_id=str(session.id), user_id="alice")

    async with await _lease(service, session.id) as lease:
        custody = dataclasses.replace(
            _custody(tmp_path),
            session_engine=engine,
            session_operation_context=lease.context,
            session_operation_authority=service.session_operation_authority,
        )
        with composer_quota_scope(service, lease.context), pytest.raises(ChargeableAdmissionRefused) as caught:
            await _plan(
                tmp_path=tmp_path,
                tool_context=tool_context,
                completion=script,
                recorder=recorder,
                model_overrides={"max_api_attempts": 2},
                originating_message=origin,
                custody_config=custody,
                surface=surface,
            )

    assert caught.value.decision.refusal_reason is AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE
    assert len(script.requests) == 2
    assert [call.status for call in recorder.llm_calls] == [ComposerLLMCallStatus.SUCCESS, ComposerLLMCallStatus.API_ERROR]
    assert [call.planner_call_ordinal for call in recorder.llm_calls] == [1, 2]
    assert recorder.llm_calls[1].error_class == "ServiceUnavailableError"
    assert secret not in repr(recorder.llm_calls)
    with engine.connect() as connection:
        attempts = connection.execute(select(quota_provider_attempts_table).order_by(quota_provider_attempts_table.c.started_at)).all()
        usage = connection.execute(select(token_usage_ledger_table).order_by(token_usage_ledger_table.c.recorded_at)).all()
    assert len(attempts) == len(usage) == 2
    assert all(attempt.settled_at is not None for attempt in attempts)
    assert [call.call_id for call in recorder.llm_calls] == [attempt.attempt_id for attempt in attempts]
    assert usage[1].prompt_tokens is None and usage[1].completion_tokens is None

    # The settled unknown usage remains authoritative for a fresh operation.
    async with await _lease(service, session.id) as fresh:
        fresh_custody = dataclasses.replace(custody, session_operation_context=fresh.context)
        with composer_quota_scope(service, fresh.context), pytest.raises(ChargeableAdmissionRefused) as fresh_caught:
            await _plan(
                tmp_path=tmp_path,
                tool_context=tool_context,
                completion=script,
                recorder=BufferingRecorder(),
                model_overrides={"max_api_attempts": 2},
                originating_message=origin,
                custody_config=fresh_custody,
                surface=surface,
            )
    assert fresh_caught.value.decision.refusal_reason is AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE
    assert len(script.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [sqlite3.OperationalError("settlement write failed"), TimeoutError("settlement timed out")])
@pytest.mark.parametrize("cancel", [False, True])
async def test_retry_settlement_failure_preserves_original_provider_evidence(
    quota_service, tmp_path: Path, tool_context, monkeypatch: pytest.MonkeyPatch, failure: Exception, cancel: bool
) -> None:
    engine, service, _ = quota_service
    session = await service.create_session(user_id="alice", title="Retry settlement", auth_provider_type="local")
    dispatches: list[str] = []

    async def provider(**kwargs: Any) -> None:
        dispatches.append("sent")
        raise APIError(status_code=500, message="provider failed", llm_provider="test", model="test/planner")

    monkeypatch.setattr("litellm.acompletion", provider)
    settlement_calls: list[ComposerLLMCall] = []
    settlement_started = asyncio.Event()
    settlement_release = asyncio.Event()
    finish = service.finish_provider_attempt

    async def fail_first_settlement(*, session_operation_context: SessionOperationContext, call: ComposerLLMCall) -> None:
        settlement_calls.append(call)
        if len(settlement_calls) == 1:
            settlement_started.set()
            if cancel:
                await settlement_release.wait()
            raise failure
        await finish(session_operation_context=session_operation_context, call=call)

    monkeypatch.setattr(service, "finish_provider_attempt", fail_first_settlement)
    recorder = BufferingRecorder()
    origin = dataclasses.replace(_origin(), session_id=str(session.id), user_id="alice")
    async with await _lease(service, session.id) as lease:
        custody = dataclasses.replace(
            _custody(tmp_path),
            session_engine=engine,
            session_operation_context=lease.context,
            session_operation_authority=service.session_operation_authority,
        )
        with composer_quota_scope(service, lease.context):
            task = asyncio.create_task(
                _plan(
                    tmp_path=tmp_path,
                    tool_context=tool_context,
                    completion=_litellm_acompletion,
                    recorder=recorder,
                    model_overrides={"max_api_attempts": 2},
                    originating_message=origin,
                    custody_config=custody,
                )
            )
            if cancel:
                await asyncio.wait_for(settlement_started.wait(), timeout=5)
                task.cancel("cancel during retry settlement")
                settlement_release.set()
            with pytest.raises(asyncio.CancelledError if cancel else type(failure)) as caught:
                await task

    if cancel:
        assert caught.value.args == ("cancel during retry settlement",)
    else:
        assert caught.value is failure
    assert dispatches == ["sent"]
    with engine.connect() as connection:
        attempt = connection.execute(select(quota_provider_attempts_table)).one()
        usage = connection.execute(select(token_usage_ledger_table)).one()
        message = connection.execute(select(chat_messages_table).where(chat_messages_table.c.role == "audit")).one()
    persisted_call = message.tool_calls[0]["call"]
    assert persisted_call["planner_call_ordinal"] == 1
    assert persisted_call["error_class"] == "APIError"
    assert persisted_call["call_id"] == attempt.attempt_id
    assert attempt.settled_at is not None
    assert usage.prompt_tokens is None and usage.completion_tokens is None
    assert len(settlement_calls) == 2
    assert settlement_calls[0] == settlement_calls[1]
    assert recorder.llm_calls == (settlement_calls[0],)


@pytest.mark.asyncio
async def test_unsettled_attempt_blocks_a_fresh_operation(quota_service, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, service, _ = quota_service
    session = await service.create_session(user_id="alice", title="Quota", auth_provider_type="local")
    async with await _lease(service, session.id) as lease:
        attempt = await service.begin_provider_attempt(session_operation_context=lease.context, source="composer")
    # Persisted state left by death after dispatch; a new operation cannot
    # interpret its absent terminal response as an empty day's spend.
    dispatches: list[str] = []

    async def provider(**kwargs: Any) -> None:
        dispatches.append("sent")

    monkeypatch.setattr("litellm.acompletion", provider)
    async with await _lease(service, session.id) as fresh:
        with composer_quota_scope(service, fresh.context), pytest.raises(ChargeableAdmissionRefused) as caught:
            await _audited_dispatch(tokens=1)
    with engine.connect() as connection:
        pending = connection.execute(select(quota_provider_attempts_table)).one()
    assert pending.attempt_id == attempt.attempt_id and pending.settled_at is None
    assert caught.value.decision.refusal_reason is AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE
    assert dispatches == []


@pytest.mark.asyncio
async def test_utc_midnight_reopens_daily_allowance_without_retiming_old_usage(quota_service, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, service, _ = quota_service
    session = await service.create_session(user_id="alice", title="Quota", auth_provider_type="local")
    before_midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(seconds=1)
    clock = [before_midnight]
    monkeypatch.setattr(quota_authority, "database_now", lambda _: clock[0])
    monkeypatch.setattr(chargeable_admission_authority, "database_now", lambda _: clock[0])
    dispatches: list[str] = []

    async def provider(**kwargs: Any) -> None:
        dispatches.append("sent")

    monkeypatch.setattr("litellm.acompletion", provider)
    async with await _lease(service, session.id) as lease:
        with composer_quota_scope(service, lease.context):
            await _audited_dispatch(tokens=IDENTITY_TOKENS_PER_DAY, finished_at=before_midnight)
            with pytest.raises(ChargeableAdmissionRefused):
                await _audited_dispatch(tokens=1)
            clock[0] += timedelta(seconds=1)
            await _audited_dispatch(tokens=1, finished_at=before_midnight + timedelta(seconds=1))
    with engine.connect() as connection:
        usage = connection.execute(select(token_usage_ledger_table).order_by(token_usage_ledger_table.c.recorded_at)).all()
    assert [row.prompt_tokens for row in usage] == [1000, 1]
    assert usage[0].recorded_at.replace(tzinfo=UTC) == before_midnight
    assert usage[1].recorded_at.replace(tzinfo=UTC) == before_midnight + timedelta(seconds=1)
    assert dispatches == ["sent", "sent"]
