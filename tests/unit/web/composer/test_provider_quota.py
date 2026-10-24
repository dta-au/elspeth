"""Legacy quota lifecycle units and required-guard physical persisted-cap integration."""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

import httpx
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
from elspeth.web.composer.prompt_guard import PromptGuardMount, prompt_guard_user_scope
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.provider_gateway import _litellm_acompletion
from elspeth.web.composer.provider_quota import (
    ProviderInvocationFamily,
    ProviderInvocationOwner,
    admit_provider_attempt,
    composer_quota_scope,
    provider_call_scope,
    quota_provider_calls,
)
from elspeth.web.coordination.quota_authority import ProviderAttempt, TokenUsageSource
from elspeth.web.prompt_guard_config import PromptGuardSettings
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
)
from elspeth.web.sessions.protocol import SessionServiceProtocol
from tests.unit.web.composer.test_prompt_guard_admission import _checks
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


async def _legacy_dispatch(provider: Callable[[], Awaitable[None]]) -> None:
    """Exercise legacy span admission; the guarded SDK integration is tested separately."""
    await admit_provider_attempt(model="test-model")
    await provider()


async def _wait_for_legacy_phase(event: asyncio.Event, child: asyncio.Task[None]) -> None:
    waiter = asyncio.create_task(event.wait())
    try:
        done, _pending = await asyncio.wait({waiter, child}, return_when=asyncio.FIRST_COMPLETED)
        if child in done:
            await child
            raise AssertionError("legacy quota child completed before its expected phase")
        await waiter
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_repeated_physical_dispatch_settles_before_next_admission() -> None:
    """Legacy span unit: settle the preceding dispatch before re-admission."""
    service = AttemptService(limit=1)
    recorder = BufferingRecorder()

    async def provider(**kwargs: Any) -> None:
        service.events.append("provider")

    @quota_provider_calls
    async def repeat() -> None:
        await asyncio.wait_for(_legacy_dispatch(provider), timeout=1)
        terminal(recorder)
        await asyncio.wait_for(_legacy_dispatch(provider), timeout=1)

    with (
        composer_quota_scope(cast(SessionServiceProtocol, service), CONTEXT),
        pytest.raises(ChargeableAdmissionRefused),
    ):
        await repeat()
    assert service.events == ["admit", "provider", "settle", "admit"]
    assert len(recorder.llm_calls) == 1
    assert recorder.llm_calls[0].call_id == "call-0"
    assert service.calls == list(recorder.llm_calls)


@pytest.mark.asyncio
async def test_timeout_preserves_unknown_usage_and_real_timeout_audit() -> None:
    """Legacy span unit: retain an actual timeout and unknown token usage."""
    service = AttemptService()
    recorder = BufferingRecorder()

    async def provider(**kwargs: Any) -> None:
        await asyncio.Event().wait()

    @quota_provider_calls
    async def timed_call() -> None:
        try:
            await asyncio.wait_for(_legacy_dispatch(provider), timeout=0.01)
        finally:
            terminal(recorder, ComposerLLMCallStatus.TIMEOUT)

    with (
        composer_quota_scope(cast(SessionServiceProtocol, service), CONTEXT),
        pytest.raises(TimeoutError),
    ):
        await timed_call()
    assert service.calls[0].status is ComposerLLMCallStatus.TIMEOUT
    assert service.calls[0].prompt_tokens is None
    assert service.calls[0].completion_tokens is None


@pytest.mark.asyncio
async def test_missing_terminal_evidence_retains_pending_attempt() -> None:
    """Legacy span unit: refuse settlement without terminal evidence."""
    service = AttemptService()

    async def provider(**kwargs: Any) -> None:
        return None

    @quota_provider_calls
    async def unfinished() -> None:
        await _legacy_dispatch(provider)

    with (
        composer_quota_scope(cast(SessionServiceProtocol, service), CONTEXT),
        pytest.raises(AuditIntegrityError, match="no terminal audit"),
    ):
        await unfinished()
    assert service.pending is not None
    assert service.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("settlement_failure", [RuntimeError("write failed"), AuditIntegrityError("audit write failed")])
async def test_cancelled_provider_waits_for_settlement_without_losing_error_priority(settlement_failure: Exception) -> None:
    """Exercise legacy quota-span settlement, independently of the guarded SDK gateway."""
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

    async def wait_for_phase(event: asyncio.Event, child: asyncio.Task[None]) -> None:
        waiter = asyncio.create_task(event.wait())
        try:
            done, _pending = await asyncio.wait({waiter, child}, return_when=asyncio.FIRST_COMPLETED)
            if child in done:
                await child
                raise AssertionError("quota child completed before its expected phase")
            await waiter
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    @quota_provider_calls
    async def cancelled_call() -> None:
        try:
            await admit_provider_attempt(model="test-model")
            await provider()
        finally:
            terminal(recorder, ComposerLLMCallStatus.CANCELLED)

    with composer_quota_scope(cast(SessionServiceProtocol, service), CONTEXT):
        task = asyncio.create_task(cancelled_call())
        try:
            await wait_for_phase(provider_started, task)
            task.cancel("original provider cancellation")
            await wait_for_phase(settlement_started, task)
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
        finally:
            # Failure before a rendezvous must not strand an owned settlement.
            settlement_release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert service.pending is not None
    assert service.calls == []
    assert recorder.llm_calls[0].status is ComposerLLMCallStatus.CANCELLED


@pytest.mark.asyncio
@pytest.mark.parametrize("settlement_failure", [RuntimeError("write failed"), AuditIntegrityError("audit write failed")])
async def test_cancellation_during_settlement_observes_child_failure(settlement_failure: Exception) -> None:
    """Legacy span unit: observe settlement after cancellation without changing error priority."""
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
        await _legacy_dispatch(provider)
        terminal(recorder)

    with composer_quota_scope(cast(SessionServiceProtocol, service), CONTEXT):
        task = asyncio.create_task(completed_call())
        try:
            await _wait_for_legacy_phase(settlement_started, task)
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
        finally:
            settlement_release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert service.pending is not None
    assert service.calls == []


@pytest.mark.asyncio
async def test_settlement_child_self_cancellation_keeps_attempt_pending() -> None:
    """Legacy span unit: a self-cancelled settlement retains pending evidence."""

    class SelfCancellingSettlementService(AttemptService):
        async def finish_provider_attempt(self, *, session_operation_context: SessionOperationContext, call: ComposerLLMCall) -> None:
            raise asyncio.CancelledError("settlement stopped itself")

    service = SelfCancellingSettlementService()
    recorder = BufferingRecorder()

    async def provider(**kwargs: Any) -> None:
        return None

    @quota_provider_calls
    async def completed_call() -> None:
        await _legacy_dispatch(provider)
        terminal(recorder)

    with (
        composer_quota_scope(cast(SessionServiceProtocol, service), CONTEXT),
        pytest.raises(AuditIntegrityError, match="settlement was cancelled"),
    ):
        await completed_call()
    assert service.pending is not None
    assert service.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_disposition", [False, True])
async def test_cancelled_admission_is_joined_and_disposed_before_provider_dispatch(fail_disposition: bool) -> None:
    """Legacy span unit: join and dispose admitted work before any provider callback."""
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
        await _legacy_dispatch(provider)

    with composer_quota_scope(cast(SessionServiceProtocol, service), CONTEXT):
        task = asyncio.create_task(cancelled_before_dispatch())
        try:
            await _wait_for_legacy_phase(admission_entered, task)
            task.cancel("cancel during admission")
            admission_release.set()
            await asyncio.wait_for(_wait_for_legacy_phase(disposition_entered, task), timeout=1)
            task.cancel("repeat cancellation during disposition")
            disposition_release.set()
            if fail_disposition:
                with pytest.raises(AuditIntegrityError, match="could not be settled"):
                    await task
            else:
                with pytest.raises(asyncio.CancelledError) as caught:
                    await task
                assert caught.value.args == ("cancel during admission",)
        finally:
            admission_release.set()
            disposition_release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert provider_dispatches == []
    assert (service.pending is not None) is fail_disposition
    assert service.calls == []
    assert service.events == (["admit"] if fail_disposition else ["admit", "undispatched_cancel"])


@pytest.mark.asyncio
async def test_cancelled_refused_admission_creates_no_attempt_or_provider_call() -> None:
    """Legacy span unit: refusal after cancellation creates neither attempt nor dispatch."""
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
        await _legacy_dispatch(provider)

    with composer_quota_scope(cast(SessionServiceProtocol, service), CONTEXT):
        task = asyncio.create_task(refused_call())
        try:
            await _wait_for_legacy_phase(admission_entered, task)
            task.cancel("cancel during refused admission")
            admission_release.set()
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
        finally:
            admission_release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert caught.value.args == ("cancel during refused admission",)
    assert isinstance(caught.value.__cause__, ChargeableAdmissionRefused)
    assert service.pending is None
    assert service.calls == []
    assert provider_dispatches == []


@pytest.mark.asyncio
async def test_scoped_unaudited_dispatch_fails_before_provider_and_scope_resets() -> None:
    """Legacy admission refuses a scoped operation without its audited span, then resets."""
    service = AttemptService()
    sent: list[str] = []

    async def provider(**kwargs: Any) -> None:
        sent.append("sent")

    with (
        composer_quota_scope(cast(SessionServiceProtocol, service), CONTEXT),
        pytest.raises(AuditIntegrityError, match="lacks an audited quota span"),
    ):
        await _legacy_dispatch(provider)
    assert sent == []
    await _legacy_dispatch(provider)
    assert sent == ["sent"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("first_usage", "expected_calls"), [(999, 2), (1000, 1), (1001, 1)])
async def test_one_turn_obeys_actual_persisted_daily_cap(tmp_path: Path, first_usage: int, expected_calls: int) -> None:
    """Keep the real guarded SDK boundary and fenced SQL cap/accounting integration."""
    refusals = []
    engine, authority, service = _quota_service(tmp_path, quota_exceeded_recorder=refusals.append)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="quota", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    recorder = BufferingRecorder()
    physical_calls = []
    guard_requests: list[httpx.Request] = []
    user_prompt = "Build a small ordinary pipeline."
    messages = [{"role": "user", "content": user_prompt}]

    def serve_guard(request: httpx.Request) -> httpx.Response:
        guard_requests.append(request)
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _ in payload["documents"]],
            },
        )

    mount = PromptGuardMount(
        AzurePromptGuardClient(
            PromptGuardSettings(endpoint="https://guard.example.test", subscription_key="fixture-guard-key"),
            http_transport=httpx.MockTransport(serve_guard),
        )
    )

    async def provider(**kwargs: Any) -> SimpleNamespace:
        # Real guard audit and admission precede SDK entry; prior settlement
        # precedes the next physical call and quota decision.
        checks = _checks(engine)
        assert len(checks) == len(physical_calls) + 1
        check = checks[-1]
        assert check["operation_id"] == context.fence.operation_id
        assert check["operation_epoch"] == context.fence.operation_epoch
        assert (check["adapter"], check["mode"], check["role"]) == ("azure_prompt_shields", "required", "composer")
        assert (check["disposition"], check["reason"]) == ("allow", "clean")
        assert check["policy_fingerprint"] == mount.client.policy_fingerprint
        assert json.loads(guard_requests[-1].content)["userPrompt"] == user_prompt
        assert kwargs["messages"] == messages
        assert custody.attempt is not None
        assert custody.attempt.attempt_id not in {call.call_id for call in recorder.llm_calls}
        assert len(_ledger(engine)) == len(physical_calls)
        physical_calls.append("call")
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=first_usage, completion_tokens=0))

    async def repeated_turn() -> None:
        for _ in range(3):
            async with provider_call_scope(custody):
                response = await _litellm_acompletion(
                    prompt_guard_mount=mount,
                    prompt_guard_mode="required",
                    prompt_guard_role="composer",
                    provider_custody=custody,
                    model="test-model",
                    messages=messages,
                )
                recorder.record_llm_call(
                    build_llm_call_record(
                        model_requested="test-model",
                        messages=messages,
                        tools=None,
                        status=ComposerLLMCallStatus.SUCCESS,
                        started_at=datetime.now(UTC),
                        started_ns=time.monotonic_ns(),
                        temperature=None,
                        seed=None,
                        response=response,
                        provider_custody=custody,
                    ),
                    provider_custody=custody,
                )

    try:
        with (
            prompt_guard_user_scope(user_prompt),
            patch("litellm.acompletion", new=provider),
            pytest.raises(ChargeableAdmissionRefused) as refused,
        ):
            await repeated_turn()
        assert refused.value.decision.refusal_reason is AdmissionRefusalReason.QUOTA_EXCEEDED
        assert len(physical_calls) == expected_calls
        assert len(_ledger(engine)) == expected_calls
        assert len(refusals) == 1
        assert len(recorder.llm_calls) == expected_calls
        assert len(_checks(engine)) == expected_calls + 1
        assert len(guard_requests) == expected_calls + 1
        assert custody.attempt is None and custody.call is None
        coordinator.assert_completed()
    finally:
        authority.release(context)
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_index", [0, 1, 2])
@pytest.mark.parametrize("bad_cost", [None, 6.0])
async def test_actual_common_roles_share_one_settled_cost_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_index: int,
    bad_cost: float | None,
) -> None:
    from decimal import Decimal

    from elspeth.web.composer.provider_cost import (
        CommonProviderCostBudget,
        CommonProviderCostPolicy,
        CommonProviderCostRefused,
        CommonProviderCostSnapshot,
    )

    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="cost", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    budget = CommonProviderCostBudget(
        CommonProviderCostSnapshot(
            str(session.id), "original-user-message", 1, "legacy_empty_v1", CommonProviderCostPolicy(75, 2097152, 16384, Decimal("5.00"), 2)
        )
    )
    owner.bind_cost_budget(budget)
    recorder = BufferingRecorder()
    calls: list[dict[str, Any]] = []

    async def sdk(**kwargs: Any) -> SimpleNamespace:
        assert len(_ledger(engine)) == len(calls)
        calls.append(kwargs)
        assert budget.snapshot().physical_calls == len(calls)
        assert kwargs["max_tokens"] == 16384
        index = len(calls) - 1
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        usage["cost"] = 0.01 if index != bad_index else "malformed-explicit-cost" if bad_cost is None else bad_cost
        return SimpleNamespace(usage=usage)

    monkeypatch.setattr("litellm.acompletion", sdk)
    families = (ProviderInvocationFamily.PRIMARY, ProviderInvocationFamily.ADVISOR, ProviderInvocationFamily.ADVISOR)
    with pytest.raises(CommonProviderCostRefused) as refused:
        for family in families:
            custody = owner.mint(family)
            async with provider_call_scope(custody):
                started_at, started_ns = datetime.now(UTC), time.monotonic_ns()
                response = await _litellm_acompletion(
                    prompt_guard_mount=None,
                    prompt_guard_mode="debug-when-unconfigured",
                    prompt_guard_role="composer" if family is ProviderInvocationFamily.PRIMARY else "advisor",
                    prompt_guard_user_prompt="Actual policy boundary fixture.",
                    provider_custody=custody,
                    model="test/unknown-cost-model",
                    messages=[{"role": "user", "content": "Actual policy boundary fixture."}],
                )
                custody.mark_cost_response_accountable(admitted_metadata=True)
                recorder.record_llm_call(
                    build_llm_call_record(
                        model_requested="test/unknown-cost-model",
                        messages=[],
                        tools=None,
                        status=ComposerLLMCallStatus.SUCCESS,
                        started_at=started_at,
                        started_ns=started_ns,
                        temperature=None,
                        seed=None,
                        response=response,
                        provider_custody=custody,
                    ),
                    provider_custody=custody,
                )
    assert refused.value.code == ("COST_UNAVAILABLE" if bad_cost is None else "COST_CAP_EXCEEDED")
    assert len(calls) == len(_ledger(engine)) == len(recorder.llm_calls) == bad_index + 1
    assert budget.snapshot().physical_calls == bad_index + 1
    assert tuple(c.call_id for c in budget.snapshot().charges) == tuple(c.call_id for c in recorder.llm_calls)
    assert recorder.llm_calls[-1].provider_cost == bad_cost
    assert budget.snapshot().cost_known is (bad_cost is not None)
    with pytest.raises(CommonProviderCostRefused):
        budget.require_dispatch()
    with pytest.raises(AuditIntegrityError, match="cannot replace"):
        owner.bind_cost_budget(CommonProviderCostBudget(budget.snapshot()))


@pytest.mark.parametrize(
    ("cost", "tokens", "expected"),
    [
        (5.0, 5, None),
        (5.01, 5, "COST_CAP_EXCEEDED"),
        (0.01, 16384, None),
        (0.01, 16385, "COMPLETION_TOKENS_EXCEEDED"),
        (None, 5, "COST_UNAVAILABLE"),
        (0.01, None, "MALFORMED_RESPONSE"),
    ],
)
def test_common_settled_response_exact_boundaries(cost, tokens, expected):
    from dataclasses import replace
    from decimal import Decimal

    from elspeth.web.composer.provider_cost import (
        CommonProviderCostBudget,
        CommonProviderCostPolicy,
        CommonProviderCostRefused,
        CommonProviderCostSnapshot,
    )

    budget = CommonProviderCostBudget(
        CommonProviderCostSnapshot(
            "session", "request", 1, "legacy_empty_v1", CommonProviderCostPolicy(1, 2097152, 16384, Decimal("5.00"), 2)
        )
    )
    budget.dispatched()
    usage = {"prompt_tokens": 10}
    if tokens is not None:
        usage["completion_tokens"] = tokens
    if cost is not None:
        usage["cost"] = cost
    call = build_llm_call_record(
        model_requested="test/unknown-cost-model",
        messages=[],
        tools=None,
        status=ComposerLLMCallStatus.SUCCESS,
        started_at=datetime.now(UTC),
        started_ns=time.monotonic_ns(),
        temperature=None,
        seed=None,
        response=SimpleNamespace(usage=usage),
    )
    call = replace(call, call_id="unit-physical-call")
    budget.consume_settled(call, response_accountable=True)
    if expected is None:
        budget.require_output()
        with pytest.raises(CommonProviderCostRefused, match="PROVIDER_CALLS_EXHAUSTED"):
            budget.require_dispatch()
    else:
        with pytest.raises(CommonProviderCostRefused, match=expected):
            budget.require_output()
    with pytest.raises(AuditIntegrityError, match="charged twice"):
        budget.consume_settled(call, response_accountable=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["timeout", "cancelled"])
async def test_failed_sdk_unknown_keeps_original_outcome_and_end_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    from decimal import Decimal

    from elspeth.web.composer.provider_cost import (
        CommonProviderCostBudget,
        CommonProviderCostPolicy,
        CommonProviderCostRefused,
        CommonProviderCostSnapshot,
    )

    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="failed cost", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    budget = CommonProviderCostBudget(
        CommonProviderCostSnapshot(
            str(session.id), "original-user-message", 1, "legacy_empty_v1", CommonProviderCostPolicy(75, 2097152, 16384, Decimal("5.00"), 2)
        )
    )
    owner.bind_cost_budget(budget)
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    recorder = BufferingRecorder()
    failure = TimeoutError("actual transport timeout") if failure_kind == "timeout" else asyncio.CancelledError("actual cancel")
    status = ComposerLLMCallStatus.TIMEOUT if failure_kind == "timeout" else ComposerLLMCallStatus.CANCELLED
    physical = 0

    async def sdk(**kwargs: Any):
        nonlocal physical
        physical += 1
        raise failure

    monkeypatch.setattr("litellm.acompletion", sdk)
    with pytest.raises(type(failure)) as caught:
        async with provider_call_scope(custody):
            started_at, started_ns = datetime.now(UTC), time.monotonic_ns()
            try:
                await _litellm_acompletion(
                    prompt_guard_mount=None,
                    prompt_guard_mode="debug-when-unconfigured",
                    prompt_guard_role="composer",
                    prompt_guard_user_prompt="Actual failed call fixture.",
                    provider_custody=custody,
                    model="test/unknown-cost-model",
                    messages=[{"role": "user", "content": "Actual failed call fixture."}],
                )
            finally:
                custody.mark_cost_response_accountable(admitted_metadata=False)
                recorder.record_llm_call(
                    build_llm_call_record(
                        model_requested="test/unknown-cost-model",
                        messages=[],
                        tools=None,
                        status=status,
                        started_at=started_at,
                        started_ns=started_ns,
                        temperature=None,
                        seed=None,
                        error_class=type(failure).__name__,
                        error_message=type(failure).__name__,
                        provider_custody=custody,
                    ),
                    provider_custody=custody,
                )
    assert caught.value is failure
    assert physical == len(_ledger(engine)) == len(recorder.llm_calls) == 1
    assert recorder.llm_calls[0].status is status
    assert recorder.llm_calls[0].provider_cost is None
    assert recorder.llm_calls[0].prompt_tokens is None and recorder.llm_calls[0].completion_tokens is None
    assert budget.snapshot().cost_known is False
    assert budget.snapshot().validated_cost == Decimal("0")
    budget.require_output()
    budget.require_dispatch()
    with pytest.raises(CommonProviderCostRefused, match="COST_UNAVAILABLE"):
        budget.require_end_headroom()
    # This fixture has a real identity token quota. Unlike the no-quota-policy
    # HTTP retry case, an actual next admission must refuse unknown token usage.
    next_custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    with pytest.raises(ChargeableAdmissionRefused) as refused:
        async with provider_call_scope(next_custody):
            await _litellm_acompletion(
                prompt_guard_mount=None,
                prompt_guard_mode="debug-when-unconfigured",
                prompt_guard_role="composer",
                prompt_guard_user_prompt="Actual failed call fixture.",
                provider_custody=next_custody,
                model="test/unknown-cost-model",
                messages=[{"role": "user", "content": "Actual failed call fixture."}],
            )
    assert refused.value.decision.refusal_reason is AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE
    assert physical == len(_ledger(engine)) == 1
    coordinator.assert_completed()


@pytest.mark.asyncio
async def test_cost_charge_does_not_precede_failed_required_settlement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from decimal import Decimal

    from elspeth.web.composer.provider_cost import CommonProviderCostBudget, CommonProviderCostPolicy, CommonProviderCostSnapshot

    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="settlement failure", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    budget = CommonProviderCostBudget(
        CommonProviderCostSnapshot(
            str(session.id), "request", 1, "legacy_empty_v1", CommonProviderCostPolicy(75, 2097152, 16384, Decimal("5.00"), 2)
        )
    )
    owner.bind_cost_budget(budget)
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    recorder = BufferingRecorder()
    failure = AuditIntegrityError("injected required settlement failure")

    async def fail_settlement(**kwargs):
        assert kwargs["call"] is custody.call
        raise failure

    async def sdk(**kwargs):
        return SimpleNamespace(usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01})

    monkeypatch.setattr(service, "finish_provider_attempt", fail_settlement)
    monkeypatch.setattr("litellm.acompletion", sdk)
    with pytest.raises(AuditIntegrityError) as caught:
        async with provider_call_scope(custody):
            started_at, started_ns = datetime.now(UTC), time.monotonic_ns()
            response = await _litellm_acompletion(
                prompt_guard_mount=None,
                prompt_guard_mode="debug-when-unconfigured",
                prompt_guard_role="composer",
                prompt_guard_user_prompt="Settlement failure control.",
                provider_custody=custody,
                model="test/unknown-cost-model",
                messages=[{"role": "user", "content": "Settlement failure control."}],
            )
            custody.mark_cost_response_accountable(admitted_metadata=True)
            recorder.record_llm_call(
                build_llm_call_record(
                    model_requested="test/unknown-cost-model",
                    messages=[],
                    tools=None,
                    status=ComposerLLMCallStatus.SUCCESS,
                    started_at=started_at,
                    started_ns=started_ns,
                    temperature=None,
                    seed=None,
                    response=response,
                    provider_custody=custody,
                ),
                provider_custody=custody,
            )
    assert caught.value is failure
    assert custody.attempt is not None and custody.call is recorder.llm_calls[0]
    assert _ledger(engine) == []
    assert budget.snapshot().physical_calls == 1
    assert budget.snapshot().charges == ()
    assert budget.snapshot().cost_known is False
