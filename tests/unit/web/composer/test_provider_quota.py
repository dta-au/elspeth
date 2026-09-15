"""Quota spans enclose physical dispatch and retain actual terminal evidence."""

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from elspeth.contracts.chargeable_admission import (
    AdmissionPolicyEvidence,
    AdmissionRefusalReason,
    ChargeableAdmissionDecision,
    ChargeableAdmissionRefused,
    QuotaDisposition,
)
from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.llm_response_parsing import build_llm_call_record
from elspeth.web.composer.provider_quota import composer_quota_scope, quota_provider_calls
from elspeth.web.composer.service import _litellm_acompletion
from elspeth.web.coordination.quota_authority import ProviderAttempt, TokenUsageSource
from elspeth.web.sessions.protocol import SessionServiceProtocol
from tests.unit.web.sessions.test_token_usage_adapters import _ledger, _quota_service

CONTEXT = SessionOperationContext(
    fence=SessionOperationFence(session_id="session", operation_id="operation", lease_token="token", operation_epoch=1),
    operation_kind=SessionOperationKind.COMPOSE,
)


class AttemptService:
    def __init__(self, limit: int = 100) -> None:
        self.limit = limit
        self.calls: list[ComposerLLMCall] = []
        self.events: list[str] = []
        self.pending: ProviderAttempt | None = None

    async def begin_provider_attempt(
        self, *, session_operation_context: SessionOperationContext, source: TokenUsageSource
    ) -> ProviderAttempt:
        assert session_operation_context is CONTEXT
        assert source == "composer"
        assert self.pending is None
        self.events.append("admit")
        if len(self.calls) >= self.limit:
            raise ChargeableAdmissionRefused(
                ChargeableAdmissionDecision(
                    refusal_reason=AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE,
                    evidence=AdmissionPolicyEvidence(
                        identity_policy_id="identity-policy",
                        quota_disposition=QuotaDisposition.ACCOUNTING_UNAVAILABLE,
                        secret_wiring_hash="a" * 64,
                    ),
                )
            )
        self.pending = ProviderAttempt(attempt_id=f"call-{len(self.calls)}", started_at=datetime.now(UTC))
        return self.pending

    async def finish_provider_attempt(self, *, session_operation_context: SessionOperationContext, call: ComposerLLMCall) -> None:
        assert session_operation_context is CONTEXT
        assert self.pending is not None
        assert call.call_id == self.pending.attempt_id
        assert call.started_at == self.pending.started_at
        self.calls.append(call)
        self.pending = None
        self.events.append("settle")


def terminal(recorder: BufferingRecorder, status: ComposerLLMCallStatus = ComposerLLMCallStatus.SUCCESS) -> None:
    recorder.record_llm_call(
        build_llm_call_record(
            model_requested="test-model",
            messages=[],
            tools=None,
            status=status,
            started_at=datetime.now(UTC),
            started_ns=time.monotonic_ns(),
            temperature=None,
            seed=None,
            error_class=None if status is ComposerLLMCallStatus.SUCCESS else "TimeoutError",
            error_message=None if status is ComposerLLMCallStatus.SUCCESS else "TimeoutError",
        )
    )


@pytest.mark.asyncio
async def test_repeated_physical_dispatch_settles_before_next_admission() -> None:
    service = AttemptService(limit=1)
    recorder = BufferingRecorder()

    async def provider(**kwargs: Any) -> None:
        service.events.append("provider")

    @quota_provider_calls
    async def repeat() -> None:
        await asyncio.wait_for(_litellm_acompletion(model="test-model", messages=[]), timeout=1)
        terminal(recorder)
        await asyncio.wait_for(_litellm_acompletion(model="test-model", messages=[]), timeout=1)

    with (
        composer_quota_scope(cast(SessionServiceProtocol, service), CONTEXT),
        patch("litellm.acompletion", new=provider),
        pytest.raises(ChargeableAdmissionRefused),
    ):
        await repeat()
    assert service.events == ["admit", "provider", "settle", "admit"]
    assert len(recorder.llm_calls) == 1
    assert recorder.llm_calls[0].call_id == "call-0"
    assert service.calls == list(recorder.llm_calls)


@pytest.mark.asyncio
async def test_timeout_preserves_unknown_usage_and_real_timeout_audit() -> None:
    service = AttemptService()
    recorder = BufferingRecorder()

    async def provider(**kwargs: Any) -> None:
        await asyncio.Event().wait()

    @quota_provider_calls
    async def timed_call() -> None:
        try:
            await asyncio.wait_for(_litellm_acompletion(model="test-model", messages=[]), timeout=0.01)
        finally:
            terminal(recorder, ComposerLLMCallStatus.TIMEOUT)

    with (
        composer_quota_scope(cast(SessionServiceProtocol, service), CONTEXT),
        patch("litellm.acompletion", new=provider),
        pytest.raises(TimeoutError),
    ):
        await timed_call()
    assert service.calls[0].status is ComposerLLMCallStatus.TIMEOUT
    assert service.calls[0].prompt_tokens is None
    assert service.calls[0].completion_tokens is None


@pytest.mark.asyncio
async def test_missing_terminal_evidence_retains_pending_attempt() -> None:
    service = AttemptService()

    async def provider(**kwargs: Any) -> None:
        return None

    @quota_provider_calls
    async def unfinished() -> None:
        await _litellm_acompletion(model="test-model", messages=[])

    with (
        composer_quota_scope(cast(SessionServiceProtocol, service), CONTEXT),
        patch("litellm.acompletion", new=provider),
        pytest.raises(AuditIntegrityError, match="no terminal audit"),
    ):
        await unfinished()
    assert service.pending is not None
    assert service.calls == []


@pytest.mark.asyncio
async def test_scoped_unaudited_dispatch_fails_before_provider_and_scope_resets() -> None:
    service = AttemptService()
    sent: list[str] = []

    async def provider(**kwargs: Any) -> None:
        sent.append("sent")

    with patch("litellm.acompletion", new=provider):
        with (
            composer_quota_scope(cast(SessionServiceProtocol, service), CONTEXT),
            pytest.raises(AuditIntegrityError, match="lacks an audited quota span"),
        ):
            await _litellm_acompletion(model="test-model", messages=[])
        assert sent == []
        await _litellm_acompletion(model="test-model", messages=[])
    assert sent == ["sent"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("first_usage", "expected_calls"), [(999, 2), (1000, 1), (1001, 1)])
async def test_one_turn_obeys_actual_persisted_daily_cap(tmp_path: Path, first_usage: int, expected_calls: int) -> None:
    refusals = []
    engine, authority, service = _quota_service(tmp_path, quota_exceeded_recorder=refusals.append)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="quota", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    recorder = BufferingRecorder()
    physical_calls = []

    async def provider(**kwargs: Any) -> SimpleNamespace:
        physical_calls.append("call")
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=first_usage, completion_tokens=0))

    @quota_provider_calls
    async def repeated_turn() -> None:
        for _ in range(3):
            response = await _litellm_acompletion(model="test-model", messages=[])
            recorder.record_llm_call(
                build_llm_call_record(
                    model_requested="test-model",
                    messages=[],
                    tools=None,
                    status=ComposerLLMCallStatus.SUCCESS,
                    started_at=datetime.now(UTC),
                    started_ns=time.monotonic_ns(),
                    temperature=None,
                    seed=None,
                    response=response,
                )
            )

    with (
        composer_quota_scope(service, context),
        patch("litellm.acompletion", new=provider),
        pytest.raises(ChargeableAdmissionRefused) as refused,
    ):
        await repeated_turn()
    assert refused.value.decision.refusal_reason is AdmissionRefusalReason.QUOTA_EXCEEDED
    assert len(physical_calls) == expected_calls
    assert len(_ledger(engine)) == expected_calls
    assert len(refusals) == 1
