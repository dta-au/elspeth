"""Adapter forwarding and real exhausted-budget controls, without a remote provider."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import litellm
import pytest
from tests.helpers.composer_lease import install_fenced_compose_adapter
from tests.helpers.session_fences import fenced_operation_context
from tests.integration.web.composer.test_freeform_proposal_prevalidation import _harness

from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.composer.protocol import ComposerConvergenceError, ComposerResult
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.coordination.contracts import SessionOperationFenceLost
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
    ):
        calls.append((self, message, messages, state, session_operation_context, budget_seconds, required_work, provider_owner))
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
    ):
        contexts.append(session_operation_context)
        budgets.append(budget_seconds)
        assert required_work is provider_owner is None
        self._sessions_service.session_operation_authority.compare_and_swap(session_operation_context)
        return ComposerResult(message="Captured legacy forwarding.", state=state)

    monkeypatch.setattr(ComposerServiceImpl, "compose", capture)
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
        with pytest.raises(SessionOperationFenceLost):
            harness.sessions.session_operation_authority.compare_and_swap(context)
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
