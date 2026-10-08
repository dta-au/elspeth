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
from elspeth.web.composer.provider_gateway import _litellm_acompletion
from elspeth.web.composer.provider_quota import composer_quota_scope, quota_provider_calls
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
@pytest.mark.parametrize("settlement_failure", [RuntimeError("write failed"), AuditIntegrityError("audit write failed")])
async def test_cancelled_provider_waits_for_settlement_without_losing_error_priority(settlement_failure: Exception) -> None:
    provider_started = asyncio.Event()
    settlement_started = asyncio.Event()
    settlement_release = asyncio.Event()

    class FailingSettlementService(AttemptService):
        async def finish_provider_attempt(self, *, session_operation_context: SessionOperationContext, call: ComposerLLMCall) -> None:
            assert session_operation_context is CONTEXT
            assert self.pending is not None
            assert call.call_id == self.pending.attempt_id
            settlement_started.set()
            await settlement_release.wait()
            raise settlement_failure

    service = FailingSettlementService()
    recorder = BufferingRecorder()

    async def provider(**kwargs: Any) -> None:
        provider_started.set()
        await asyncio.Event().wait()

    @quota_provider_calls
    async def cancelled_call() -> None:
        try:
            await _litellm_acompletion(model="test-model", messages=[])
        finally:
            terminal(recorder, ComposerLLMCallStatus.CANCELLED)

    with composer_quota_scope(cast(SessionServiceProtocol, service), CONTEXT), patch("litellm.acompletion", new=provider):
        task = asyncio.create_task(cancelled_call())
        await provider_started.wait()
        task.cancel("original provider cancellation")
        await settlement_started.wait()
        task.cancel("repeat cancellation during settlement")
        settlement_release.set()
        if isinstance(settlement_failure, AuditIntegrityError):
            with pytest.raises(AuditIntegrityError) as caught:
                await task
            assert caught.value is settlement_failure
            assert isinstance(caught.value.__cause__, asyncio.CancelledError)
        else:
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert caught.value.args == ("original provider cancellation",)
            assert caught.value.__cause__ is settlement_failure
    assert service.pending is not None
    assert service.calls == []
    assert recorder.llm_calls[0].status is ComposerLLMCallStatus.CANCELLED


@pytest.mark.asyncio
@pytest.mark.parametrize("settlement_failure", [RuntimeError("write failed"), AuditIntegrityError("audit write failed")])
async def test_cancellation_during_settlement_observes_child_failure(settlement_failure: Exception) -> None:
    settlement_started = asyncio.Event()
    settlement_release = asyncio.Event()

    class FailingSettlementService(AttemptService):
        async def finish_provider_attempt(self, *, session_operation_context: SessionOperationContext, call: ComposerLLMCall) -> None:
            settlement_started.set()
            await settlement_release.wait()
            raise settlement_failure

    service = FailingSettlementService()
    recorder = BufferingRecorder()

    async def provider(**kwargs: Any) -> None:
        return None

    @quota_provider_calls
    async def completed_call() -> None:
        await _litellm_acompletion(model="test-model", messages=[])
        terminal(recorder)

    with composer_quota_scope(cast(SessionServiceProtocol, service), CONTEXT), patch("litellm.acompletion", new=provider):
        task = asyncio.create_task(completed_call())
        await settlement_started.wait()
        task.cancel("cancel during settlement")
        settlement_release.set()
        if isinstance(settlement_failure, AuditIntegrityError):
            with pytest.raises(AuditIntegrityError) as caught:
                await task
            assert caught.value is settlement_failure
            assert isinstance(caught.value.__cause__, asyncio.CancelledError)
        else:
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert caught.value.args == ("cancel during settlement",)
            assert caught.value.__cause__ is settlement_failure
    assert service.pending is not None
    assert service.calls == []


@pytest.mark.asyncio
async def test_settlement_child_self_cancellation_keeps_attempt_pending() -> None:
    class SelfCancellingSettlementService(AttemptService):
        async def finish_provider_attempt(self, *, session_operation_context: SessionOperationContext, call: ComposerLLMCall) -> None:
            raise asyncio.CancelledError("settlement stopped itself")

    service = SelfCancellingSettlementService()
    recorder = BufferingRecorder()

    async def provider(**kwargs: Any) -> None:
        return None

    @quota_provider_calls
    async def completed_call() -> None:
        await _litellm_acompletion(model="test-model", messages=[])
        terminal(recorder)

    with (
        composer_quota_scope(cast(SessionServiceProtocol, service), CONTEXT),
        patch("litellm.acompletion", new=provider),
        pytest.raises(AuditIntegrityError, match="settlement was cancelled"),
    ):
        await completed_call()
    assert service.pending is not None
    assert service.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_disposition", [False, True])
async def test_cancelled_admission_is_joined_and_disposed_before_provider_dispatch(fail_disposition: bool) -> None:
    admission_entered = asyncio.Event()
    admission_release = asyncio.Event()
    disposition_entered = asyncio.Event()
    disposition_release = asyncio.Event()
    provider_dispatches: list[str] = []

    class DelayedAdmissionService(AttemptService):
        async def begin_provider_attempt(
            self, *, session_operation_context: SessionOperationContext, source: TokenUsageSource
        ) -> ProviderAttempt:
            admission_entered.set()
            await admission_release.wait()
            return await super().begin_provider_attempt(session_operation_context=session_operation_context, source=source)

        async def cancel_undispatched_provider_attempt(
            self, *, session_operation_context: SessionOperationContext, attempt_id: str, requested_model: str
        ) -> None:
            assert session_operation_context is CONTEXT
            assert self.pending is not None and self.pending.attempt_id == attempt_id
            assert requested_model == "test-model"
            disposition_entered.set()
            await disposition_release.wait()
            if fail_disposition:
                raise RuntimeError("undispatched disposition write failed")
            self.pending = None
            self.events.append("undispatched_cancel")

    service = DelayedAdmissionService()

    async def provider(**kwargs: Any) -> None:
        provider_dispatches.append("sent")

    @quota_provider_calls
    async def cancelled_before_dispatch() -> None:
        await _litellm_acompletion(model="test-model", messages=[])

    with composer_quota_scope(cast(SessionServiceProtocol, service), CONTEXT), patch("litellm.acompletion", new=provider):
        task = asyncio.create_task(cancelled_before_dispatch())
        await admission_entered.wait()
        task.cancel("cancel during admission")
        admission_release.set()
        await asyncio.wait_for(disposition_entered.wait(), timeout=1)
        task.cancel("repeat cancellation during disposition")
        disposition_release.set()
        if fail_disposition:
            with pytest.raises(AuditIntegrityError, match="could not be settled"):
                await task
        else:
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert caught.value.args == ("cancel during admission",)
    assert provider_dispatches == []
    assert (service.pending is not None) is fail_disposition
    assert service.calls == []
    assert service.events == (["admit"] if fail_disposition else ["admit", "undispatched_cancel"])


@pytest.mark.asyncio
async def test_cancelled_refused_admission_creates_no_attempt_or_provider_call() -> None:
    admission_entered = asyncio.Event()
    admission_release = asyncio.Event()
    provider_dispatches: list[str] = []

    class RefusingAdmissionService(AttemptService):
        async def begin_provider_attempt(
            self, *, session_operation_context: SessionOperationContext, source: TokenUsageSource
        ) -> ProviderAttempt:
            admission_entered.set()
            await admission_release.wait()
            return await super().begin_provider_attempt(session_operation_context=session_operation_context, source=source)

    service = RefusingAdmissionService(limit=0)

    async def provider(**kwargs: Any) -> None:
        provider_dispatches.append("sent")

    @quota_provider_calls
    async def refused_call() -> None:
        await _litellm_acompletion(model="test-model", messages=[])

    with composer_quota_scope(cast(SessionServiceProtocol, service), CONTEXT), patch("litellm.acompletion", new=provider):
        task = asyncio.create_task(refused_call())
        await admission_entered.wait()
        task.cancel("cancel during refused admission")
        admission_release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
    assert caught.value.args == ("cancel during refused admission",)
    assert isinstance(caught.value.__cause__, ChargeableAdmissionRefused)
    assert service.pending is None
    assert service.calls == []
    assert provider_dispatches == []


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
