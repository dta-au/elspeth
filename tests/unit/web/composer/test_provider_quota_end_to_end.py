"""Physical Composer dispatch through real fenced quota admission and settlement."""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.engine import Engine

from elspeth.contracts.chargeable_admission import AdmissionRefusalReason, ChargeableAdmissionPolicy, ChargeableAdmissionRefused
from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.llm_response_parsing import build_llm_call_record
from elspeth.web.composer.provider_quota import composer_quota_scope, quota_provider_calls
from elspeth.web.composer.service import _litellm_acompletion
from elspeth.web.coordination import chargeable_admission_authority, quota_authority
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.quota_authority import QuotaExceeded
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.secrets.wiring_policy import EMPTY_SECRET_WIRING_POLICY
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import quota_provider_attempts_table, token_usage_ledger_table
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.fenced_session import IDENTITY_TOKENS_PER_DAY, seed_token_policies


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
