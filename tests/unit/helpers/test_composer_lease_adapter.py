"""Adapter forwarding and real exhausted-budget controls, without a remote provider."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import litellm
import pytest
from tests.helpers.composer_lease import install_fenced_compose_adapter
from tests.helpers.session_fences import fenced_operation_context
from tests.integration.web.composer.test_freeform_proposal_prevalidation import _harness

from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.composer.advisor_transcript import AdvisorTranscriptHistory
from elspeth.web.composer.graph_recovery import GraphRecoveryContext
from elspeth.web.composer.protocol import ComposerConvergenceError, ComposerResult, RetainedOperationContext
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
)


@pytest.mark.asyncio
async def test_adapter_forwards_exact_remaining_budget_and_owned_provider_pair(tmp_path, monkeypatch):
    harness = _harness(tmp_path)
    state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    history = []
    calls = []
    recovery = GraphRecoveryContext(None, 0, None, 0, False)
    advisor_history = AdvisorTranscriptHistory.from_records(
        session_id=harness.session_id,
        actor_user_id="proposal-prevalidation-user",
        current_request_id=harness.user_message_id,
        request="Build a reviewed pipeline.",
        records=await harness.sessions.get_messages(UUID(harness.session_id), limit=None, include_private_evidence=False),
    )

    async def capture(
        self,
        message,
        messages,
        state,
        session_id=None,
        current_state_id=None,
        user_id=None,
        progress=None,
        user_message_id=None,
        session_operation_context=None,
        completion_gates=None,
        budget_seconds=None,
        required_work=None,
        provider_owner=None,
        recovery_context=None,
        retained_operation_context: RetainedOperationContext | None = None,
        advisor_history: AdvisorTranscriptHistory | None = None,
    ):
        calls.append(
            (self, message, messages, state, session_operation_context, budget_seconds, required_work, provider_owner, advisor_history)
        )
        assert recovery_context is recovery
        assert retained_operation_context is None
        return ComposerResult(message="Captured adapter forwarding.", state=state)

    monkeypatch.setattr(ComposerServiceImpl, "compose", capture)
    install_fenced_compose_adapter(monkeypatch)
    try:
        with fenced_operation_context(harness.engine, harness.session_id) as context:
            work = RequiredWorkBinding(
                RequiredWorkCoordinator(RequiredWorkAuthority(RequiredAuthorityKind.DURABLE_COMPOSE, context, str(uuid4()), 1)),
                0,
                0,
                RequiredWorkRole.TURN,
            )
            owner = ProviderInvocationOwner(service=harness.sessions, required_work=work)
            result = await harness.service.compose(
                "Forward the remaining budget.",
                history,
                state,
                session_id=harness.session_id,
                session_operation_context=context,
                budget_seconds=0.125,
                required_work=work,
                provider_owner=owner,
                recovery_context=recovery,
                advisor_history=advisor_history,
            )
            assert result.state is state
            assert len(calls) == 1
            supplied = calls[0]
            assert supplied[0] is harness.service
            assert supplied[2] is history
            assert supplied[3] is state
            assert supplied[4] is context
            assert supplied[5] == 0.125
            assert supplied[6] is work
            assert supplied[7] is owner
            assert supplied[7].required_work is supplied[6]
            assert supplied[8] is advisor_history
            # A borrowed live authority is forwarded, never replaced/closed.
            harness.sessions.session_operation_authority.compare_and_swap(context)
            with pytest.raises(TypeError, match="budget_seconds_typo"):
                await harness.service.compose("Bad keyword", history, state, budget_seconds_typo=0.125)
            assert len(calls) == 1
    finally:
        harness.engine.dispose()


@pytest.mark.asyncio
async def test_adapter_legacy_call_preserves_budget_and_closes_actual_owned_lease(tmp_path, monkeypatch):
    harness = _harness(tmp_path)
    contexts = []
    budgets = []
    bindings = []
    settled_before_close = []
    real_close = SessionOperationLease.close

    async def close_after_settlement(lease):
        assert lease.required_work is bindings[0].coordinator
        lease.required_work.assert_completed()
        settled_before_close.append(lease.context)
        await real_close(lease)

    async def capture(
        self,
        message,
        messages,
        state,
        session_id=None,
        current_state_id=None,
        user_id=None,
        progress=None,
        user_message_id=None,
        session_operation_context=None,
        completion_gates=None,
        budget_seconds=None,
        required_work=None,
        provider_owner=None,
        recovery_context=None,
        retained_operation_context: RetainedOperationContext | None = None,
        advisor_history: AdvisorTranscriptHistory | None = None,
    ):
        contexts.append(session_operation_context)
        budgets.append(budget_seconds)
        assert type(required_work) is RequiredWorkBinding
        assert type(provider_owner) is ProviderInvocationOwner
        assert provider_owner.required_work is required_work
        assert provider_owner.service is self._sessions_service
        assert required_work.coordinator.authority.context is session_operation_context
        assert required_work.coordinator.authority.authority_kind is RequiredAuthorityKind.SYNCHRONOUS_COMPOSE
        bindings.append(required_work)
        assert recovery_context is None
        assert retained_operation_context is None
        assert advisor_history is None
        self._sessions_service.session_operation_authority.compare_and_swap(session_operation_context)
        return ComposerResult(message="Captured legacy forwarding.", state=state)

    monkeypatch.setattr(ComposerServiceImpl, "compose", capture)
    monkeypatch.setattr(SessionOperationLease, "close", close_after_settlement)
    install_fenced_compose_adapter(monkeypatch)
    try:
        await harness.service.compose(
            "Forward legacy call.",
            [],
            CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1),
            session_id=harness.session_id,
            budget_seconds=0.125,
        )
        assert budgets == [0.125]
        assert len(contexts) == 1
        context = contexts[0]
        assert type(context) is SessionOperationContext
        assert context.operation_kind is SessionOperationKind.COMPOSE
        assert context.fence.session_id == harness.session_id
        assert settled_before_close == [context]
        bindings[0].coordinator.assert_completed()
        with pytest.raises(SessionOperationFenceLost):
            harness.sessions.session_operation_authority.compare_and_swap(context)
    finally:
        harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_body", [False, True], ids=["body-error", "body-cancellation"])
@pytest.mark.parametrize("fail_close", [False, True], ids=["close-success", "close-error"])
async def test_adapter_preserves_original_body_and_close_outcomes(tmp_path, monkeypatch, cancel_body, fail_close):
    harness = _harness(tmp_path)
    body = asyncio.CancelledError("test body cancelled") if cancel_body else RuntimeError("test body failed")
    closing = RuntimeError("test close outcome failed")
    contexts = []
    real_close = SessionOperationLease.close

    async def capture(
        self,
        message,
        messages,
        state,
        session_id=None,
        current_state_id=None,
        user_id=None,
        progress=None,
        user_message_id=None,
        session_operation_context=None,
        completion_gates=None,
        budget_seconds=None,
        required_work=None,
        provider_owner=None,
        recovery_context=None,
        retained_operation_context=None,
        advisor_history: AdvisorTranscriptHistory | None = None,
    ):
        assert type(required_work) is RequiredWorkBinding
        assert advisor_history is None
        assert type(provider_owner) is ProviderInvocationOwner
        assert provider_owner.required_work is required_work
        assert provider_owner.service is self._sessions_service
        assert required_work.coordinator.authority.context is session_operation_context
        self._sessions_service.session_operation_authority.compare_and_swap(session_operation_context)
        contexts.append(session_operation_context)
        raise body

    async def close_after_settlement(lease):
        lease.required_work.assert_completed()
        await real_close(lease)
        if fail_close:
            raise closing

    monkeypatch.setattr(ComposerServiceImpl, "compose", capture)
    monkeypatch.setattr(SessionOperationLease, "close", close_after_settlement)
    install_fenced_compose_adapter(monkeypatch)
    try:
        with pytest.raises(BaseException) as raised:
            await harness.service.compose(
                "Exercise the test fixture lifecycle.",
                [],
                CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1),
                session_id=harness.session_id,
            )
        if fail_close:
            assert isinstance(raised.value, BaseExceptionGroup)
            assert raised.value.exceptions == (body, closing)
        else:
            assert raised.value is body
        assert len(contexts) == 1
        with pytest.raises(SessionOperationFenceLost):
            harness.sessions.session_operation_authority.compare_and_swap(contexts[0])
    finally:
        harness.engine.dispose()


@pytest.mark.asyncio
async def test_adapter_zero_remaining_budget_reaches_real_composer_timeout_before_sdk(tmp_path, monkeypatch):
    harness = _harness(tmp_path)
    completion = AsyncMock(
        spec=litellm.acompletion, side_effect=AssertionError("SDK must not be called after the remaining budget expires")
    )
    monkeypatch.setattr("litellm.acompletion", completion)
    install_fenced_compose_adapter(monkeypatch)
    try:
        with pytest.raises(ComposerConvergenceError) as raised:
            await harness.service.compose(
                "Do not build or run a pipeline",
                [],
                CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1),
                session_id=harness.session_id,
                user_id="proposal-prevalidation-user",
                user_message_id=harness.user_message_id,
                budget_seconds=0.0,
            )
        assert raised.value.budget_exhausted == "timeout"
        completion.assert_not_awaited()
        assert await harness.sessions.list_composition_proposals(UUID(harness.session_id)) == []
    finally:
        harness.engine.dispose()
