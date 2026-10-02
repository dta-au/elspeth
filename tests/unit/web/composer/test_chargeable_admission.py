"""Public provider entries refuse before either planner or text model work."""

import asyncio
from datetime import UTC, datetime
from typing import cast
from unittest.mock import patch
from uuid import UUID

import pytest

from elspeth.contracts.chargeable_admission import (
    AdmissionPolicyEvidence,
    AdmissionRefusalReason,
    ChargeableAdmissionDecision,
    ChargeableAdmissionRefused,
    ChargeableOperation,
    QuotaDisposition,
)
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.composer import provider_gateway, provider_quota
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.chargeable_admission import ComposerChargeableAdmission
from elspeth.web.composer.protocol import ComposerAdmissionRefused
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.sessions import _auto_title
from elspeth.web.sessions.protocol import ComposerSessionPreferencesRecord, SessionServiceProtocol

_SESSION_ID = "00000000-0000-0000-0000-000000000001"
_CONTEXT = SessionOperationContext(
    fence=SessionOperationFence(session_id=_SESSION_ID, operation_id="operation", lease_token="token", operation_epoch=1),
    operation_kind=SessionOperationKind.COMPOSE,
)


class _AdmissionService:
    def __init__(self, reason: AdmissionRefusalReason | None) -> None:
        self.operations: list[ChargeableOperation] = []
        disposition = QuotaDisposition.NOT_ASSESSED
        if reason is None:
            disposition = QuotaDisposition.NOT_CONFIGURED
        elif reason is AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE:
            disposition = QuotaDisposition.ACCOUNTING_UNAVAILABLE
        self.decision = ChargeableAdmissionDecision(
            refusal_reason=reason,
            evidence=AdmissionPolicyEvidence(
                quota_disposition=disposition,
                identity_policy_id="identity-token-policy" if disposition is QuotaDisposition.ACCOUNTING_UNAVAILABLE else None,
                secret_wiring_hash="a" * 64,
            ),
        )

    async def assess_chargeable_operation(
        self, *, session_operation_context: SessionOperationContext, operation: ChargeableOperation
    ) -> ChargeableAdmissionDecision:
        assert session_operation_context == _CONTEXT
        self.operations.append(operation)
        return self.decision

    async def begin_provider_attempt(self, *, session_operation_context: SessionOperationContext, source: str) -> None:
        assert session_operation_context == _CONTEXT
        assert source == "auto_title"
        self.operations.append(ChargeableOperation.AUTO_TITLE)
        raise ChargeableAdmissionRefused(self.decision)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["compose", "diagnostics", "signoff"])
@pytest.mark.parametrize("reason", [AdmissionRefusalReason.IDENTITY_DISABLED, AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE])
async def test_public_entry_refuses_before_provider_work(
    composer_service_without_sessions_service: ComposerServiceImpl, entry: str, reason: AdmissionRefusalReason
) -> None:
    service = composer_service_without_sessions_service
    authority = _AdmissionService(reason)
    service._sessions_service = cast(SessionServiceProtocol, authority)
    service._chargeable_admission = ComposerChargeableAdmission(cast(SessionServiceProtocol, authority))
    service._planning_application._sessions_service_optional = service._sessions_service
    service._planning_application._chargeable_admission = service._chargeable_admission
    service._advisor_checkpoint._sessions_service = service._sessions_service
    service._advisor_checkpoint._chargeable_admission = service._chargeable_admission
    # These later-stage dependencies deliberately fail if reached. The admission
    # boundary must precede planner preparation as well as outbound model calls.
    state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    with (
        patch.object(service._provider_gateway, "_call_llm", autospec=True) as tool_provider,
        patch.object(service._provider_gateway, "_call_text_llm", autospec=True) as text_provider,
        patch("elspeth.web.composer.planning_application.plan_pipeline", autospec=True) as planner,
        patch.object(service._advisor_checkpoint, "_run_advisor_checkpoint", autospec=True) as advisor,
        pytest.raises(ComposerAdmissionRefused, match=reason.value),
    ):
        if entry == "compose":
            await service.compose(
                "Build a pipeline",
                [],
                state,
                session_id=_SESSION_ID,
                user_id="owner",
                user_message_id="00000000-0000-0000-0000-000000000002",
                session_operation_context=_CONTEXT,
            )
        elif entry == "diagnostics":
            await service.explain_run_diagnostics({}, session_operation_context=_CONTEXT)
        else:
            await service._advisor_checkpoint.run_signoff_checkpoint(
                state=state,
                session_id=_SESSION_ID,
                recorder=BufferingRecorder(),
                session_operation_context=_CONTEXT,
            )
    assert authority.operations == [ChargeableOperation.COMPOSER]
    tool_provider.assert_not_called()
    text_provider.assert_not_called()
    planner.assert_not_called()
    advisor.assert_not_called()


@pytest.mark.asyncio
async def test_missing_session_authority_is_not_an_allowance(composer_service_without_sessions_service: ComposerServiceImpl) -> None:
    with pytest.raises(ComposerAdmissionRefused, match="requires session authority"):
        await composer_service_without_sessions_service.explain_run_diagnostics({}, session_operation_context=_CONTEXT)


@pytest.mark.asyncio
async def test_allowed_diagnostics_reaches_provider(composer_service_without_sessions_service: ComposerServiceImpl) -> None:
    service = composer_service_without_sessions_service
    authority = _AdmissionService(None)
    service._sessions_service = cast(SessionServiceProtocol, authority)
    service._chargeable_admission = ComposerChargeableAdmission(cast(SessionServiceProtocol, authority))
    service._planning_application._sessions_service_optional = service._sessions_service
    service._planning_application._chargeable_admission = service._chargeable_admission
    service._advisor_checkpoint._sessions_service = service._sessions_service
    service._advisor_checkpoint._chargeable_admission = service._chargeable_admission

    async def explain(*args: object, **kwargs: object) -> str:
        scope = provider_quota._SCOPE.get()
        assert scope is not None
        assert scope.service is authority
        assert scope.context is _CONTEXT
        return "Explanation"

    with patch.object(service._provider_gateway, "_call_text_llm_with_audit", autospec=True, side_effect=explain) as provider:
        assert await service.explain_run_diagnostics({}, session_operation_context=_CONTEXT) == "Explanation"
    provider.assert_awaited_once()
    assert authority.operations == [ChargeableOperation.COMPOSER]
    assert provider_quota._SCOPE.get() is None


@pytest.mark.asyncio
async def test_auto_title_checks_admission_before_provider() -> None:
    authority = _AdmissionService(AdmissionRefusalReason.IDENTITY_DISABLED)
    with patch.object(provider_gateway, "_litellm_acompletion", autospec=True) as provider:
        await _auto_title.maybe_auto_title_session(
            service=cast(SessionServiceProtocol, authority),
            session_id=UUID(_SESSION_ID),
            user_message="Build a pipeline",
            model="test",
            temperature=None,
            seed=None,
            session_operation_context=_CONTEXT,
        )
    provider.assert_not_called()
    assert authority.operations == [ChargeableOperation.AUTO_TITLE]


class _PlannerReached(RuntimeError):
    """Stop at the real planner entry without invoking a network provider."""


class _ProviderReached(BaseException):
    """Stop exactly at the provider boundary without activating provider retries."""


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False])
async def test_rootless_admission_controls_actual_provider_transition(
    composer_service_with_real_sessions: ComposerServiceImpl, allowed: bool
) -> None:
    service = composer_service_with_real_sessions
    sessions = service._sessions_service
    assert sessions is not None
    authority = _AdmissionService(None if allowed else AdmissionRefusalReason.IDENTITY_DISABLED)
    state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    preferences = ComposerSessionPreferencesRecord(
        session_id=UUID(_SESSION_ID),
        trust_mode="explicit_approve",
        density_default="medium",
        interpretation_review_disabled=False,
        updated_at=datetime.now(UTC),
    )
    with (
        patch.object(sessions, "assess_chargeable_operation", new=authority.assess_chargeable_operation),
        patch.object(sessions, "get_composer_preferences", autospec=True, return_value=preferences),
        patch("elspeth.web.composer.provider_gateway._litellm_acompletion", autospec=True, side_effect=_ProviderReached) as provider,
        pytest.raises(_ProviderReached if allowed else ComposerAdmissionRefused),
    ):
        await service.compose(
            "Build a CSV pipeline",
            [],
            state,
            session_id=_SESSION_ID,
            user_id="owner",
            user_message_id="00000000-0000-0000-0000-000000000002",
            session_operation_context=_CONTEXT,
        )
    assert authority.operations == [ChargeableOperation.COMPOSER]
    assert provider.await_count == int(allowed)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["rootless", "signoff"])
@pytest.mark.parametrize("allowed", [True, False])
async def test_same_valid_request_reaches_planner_only_when_admitted(
    composer_service_with_real_sessions: ComposerServiceImpl, entry: str, allowed: bool
) -> None:
    service = composer_service_with_real_sessions
    sessions = service._sessions_service
    assert sessions is not None

    async def assert_scoped_planner(*args: object, **kwargs: object) -> None:
        scope = provider_quota._SCOPE.get()
        assert scope is not None
        assert scope.service is sessions
        assert scope.context is _CONTEXT
        raise _PlannerReached

    authority = _AdmissionService(None if allowed else AdmissionRefusalReason.IDENTITY_DISABLED)
    state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    message_id = "00000000-0000-0000-0000-000000000002"
    preferences = ComposerSessionPreferencesRecord(
        session_id=UUID(_SESSION_ID),
        trust_mode="explicit_approve",
        density_default="medium",
        interpretation_review_disabled=False,
        updated_at=datetime.now(UTC),
    )
    with (
        patch.object(sessions, "assess_chargeable_operation", new=authority.assess_chargeable_operation),
        patch.object(sessions, "get_composer_preferences", autospec=True, return_value=preferences),
        patch("elspeth.web.composer.planning_application.plan_pipeline", autospec=True, side_effect=assert_scoped_planner) as planner,
        patch.object(service._advisor_checkpoint, "_run_advisor_checkpoint", autospec=True, side_effect=assert_scoped_planner) as advisor,
        pytest.raises(_PlannerReached if allowed else ComposerAdmissionRefused),
    ):
        if entry == "rootless":
            await service.compose(
                "Build a CSV pipeline",
                [],
                state,
                session_id=_SESSION_ID,
                user_id="owner",
                user_message_id=message_id,
                session_operation_context=_CONTEXT,
            )
        else:
            await service._advisor_checkpoint.run_signoff_checkpoint(
                state=state,
                session_id=_SESSION_ID,
                recorder=BufferingRecorder(),
                session_operation_context=_CONTEXT,
            )
    assert authority.operations == [ChargeableOperation.COMPOSER]
    assert planner.await_count == int(allowed and entry != "signoff")
    assert advisor.await_count == int(allowed and entry == "signoff")
    assert provider_quota._SCOPE.get() is None


@pytest.mark.asyncio
async def test_concurrent_diagnostics_restore_separate_session_scopes(
    composer_service_without_sessions_service: ComposerServiceImpl,
) -> None:
    service = composer_service_without_sessions_service
    authority = _AdmissionService(None)
    service._sessions_service = cast(SessionServiceProtocol, authority)
    service._chargeable_admission = ComposerChargeableAdmission(cast(SessionServiceProtocol, authority))
    service._planning_application._sessions_service_optional = service._sessions_service
    service._planning_application._chargeable_admission = service._chargeable_admission
    service._advisor_checkpoint._sessions_service = service._sessions_service
    service._advisor_checkpoint._chargeable_admission = service._chargeable_admission
    second_context = SessionOperationContext(
        fence=SessionOperationFence(session_id="second-session", operation_id="second-operation", lease_token="token", operation_epoch=1),
        operation_kind=SessionOperationKind.COMPOSE,
    )
    reached = asyncio.Event()
    contexts: list[SessionOperationContext] = []

    async def admit(*, session_operation_context: SessionOperationContext, operation: ChargeableOperation) -> ChargeableAdmissionDecision:
        return authority.decision

    async def explain(*args: object, **kwargs: object) -> str:
        initial = provider_quota._SCOPE.get()
        assert initial is not None
        assert initial.service is authority
        contexts.append(initial.context)
        if len(contexts) == 2:
            reached.set()
        await asyncio.wait_for(reached.wait(), timeout=2)
        await asyncio.sleep(0)
        assert provider_quota._SCOPE.get() is initial
        return initial.context.fence.session_id

    with (
        patch.object(authority, "assess_chargeable_operation", new=admit),
        patch.object(service._provider_gateway, "_call_text_llm_with_audit", autospec=True, side_effect=explain),
    ):
        results = await asyncio.gather(
            service.explain_run_diagnostics({}, session_operation_context=_CONTEXT),
            service.explain_run_diagnostics({}, session_operation_context=second_context),
        )
    assert results == [_SESSION_ID, "second-session"]
    assert contexts == [_CONTEXT, second_context]
    assert provider_quota._SCOPE.get() is None
