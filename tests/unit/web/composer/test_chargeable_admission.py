"""Public provider entries refuse before either planner or text model work."""

from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import patch
from uuid import UUID

import pytest

from elspeth.contracts.chargeable_admission import (
    AdmissionPolicyEvidence,
    AdmissionRefusalReason,
    ChargeableAdmissionDecision,
    ChargeableOperation,
    QuotaDisposition,
)
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.guided.profile import EMPTY_PROFILE
from elspeth.web.composer.guided.protocol import GuidedStep
from elspeth.web.composer.guided.resolved import SinkOutputResolved, SourceResolved
from elspeth.web.composer.guided.state_machine import GuidedSession
from elspeth.web.composer.pipeline_planner import PlannerOriginatingMessage
from elspeth.web.composer.pipeline_proposal import PresentBase, composition_content_hash
from elspeth.web.composer.service import ComposerAdmissionRefused, ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.sessions import _auto_title
from elspeth.web.sessions.protocol import ComposerSessionPreferencesRecord, GuidedOperationFence, SessionServiceProtocol

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


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["compose", "guided_full", "guided_delta", "diagnostics", "signoff"])
@pytest.mark.parametrize("reason", [AdmissionRefusalReason.IDENTITY_DISABLED, AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE])
async def test_public_entry_refuses_before_provider_work(
    composer_service_without_sessions_service: ComposerServiceImpl, entry: str, reason: AdmissionRefusalReason
) -> None:
    service = composer_service_without_sessions_service
    authority = _AdmissionService(reason)
    service._sessions_service = cast(SessionServiceProtocol, authority)
    origin = PlannerOriginatingMessage(_SESSION_ID, None, "Build a pipeline", "owner")
    # These later-stage dependencies deliberately fail if reached. The admission
    # boundary must precede planner preparation as well as outbound model calls.
    unused: Any = None
    state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    with (
        patch.object(service, "_call_llm", autospec=True) as tool_provider,
        patch.object(service, "_call_text_llm", autospec=True) as text_provider,
        patch("elspeth.web.composer.service.plan_pipeline", autospec=True) as planner,
        patch.object(service, "_run_advisor_checkpoint", autospec=True) as advisor,
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
        elif entry == "guided_full":
            await service.plan_guided_full_pipeline(
                intent="Build a pipeline",
                current_state=state,
                originating_message=origin,
                base=unused,
                policy_catalog=unused,
                plugin_snapshot=unused,
                recorder=BufferingRecorder(),
                operation_fence=unused,
                session_operation_context=_CONTEXT,
            )
        elif entry == "guided_delta":
            await service.plan_guided_pipeline(
                intent="Build a pipeline",
                current_state=state,
                guided=unused,
                originating_message=origin,
                base=unused,
                user_id="owner",
                supersedes_draft_hash=None,
                recorder=BufferingRecorder(),
                operation_fence=unused,
                session_operation_context=_CONTEXT,
            )
        elif entry == "diagnostics":
            await service.explain_run_diagnostics({}, session_operation_context=_CONTEXT)
        else:
            await service.run_signoff_checkpoint(
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
    with patch.object(service, "_call_text_llm_with_audit", autospec=True, return_value="Explanation") as provider:
        assert await service.explain_run_diagnostics({}, session_operation_context=_CONTEXT) == "Explanation"
    provider.assert_awaited_once()
    assert authority.operations == [ChargeableOperation.COMPOSER]


@pytest.mark.asyncio
async def test_auto_title_checks_admission_before_provider() -> None:
    authority = _AdmissionService(AdmissionRefusalReason.IDENTITY_DISABLED)
    with patch.object(_auto_title, "_litellm_acompletion", autospec=True) as provider:
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
        patch("elspeth.web.composer.service._litellm_acompletion", autospec=True, side_effect=_ProviderReached) as provider,
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
@pytest.mark.parametrize("entry", ["rootless", "guided_full", "guided_delta", "signoff"])
@pytest.mark.parametrize("allowed", [True, False])
async def test_same_valid_request_reaches_planner_only_when_admitted(
    composer_service_with_real_sessions: ComposerServiceImpl, entry: str, allowed: bool
) -> None:
    service = composer_service_with_real_sessions
    sessions = service._sessions_service
    assert sessions is not None
    authority = _AdmissionService(None if allowed else AdmissionRefusalReason.IDENTITY_DISABLED)
    state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    message_id = "00000000-0000-0000-0000-000000000002"
    origin = PlannerOriginatingMessage(_SESSION_ID, message_id, "Build a CSV pipeline", "owner")
    base = PresentBase(state_id=UUID(message_id), composition_content_hash=composition_content_hash(state))
    fence = GuidedOperationFence(session_id=UUID(_SESSION_ID), operation_id="guided-operation", lease_token="token", attempt=1)
    snapshot, catalog = service._plugin_policy_context("owner")
    source_id = "11111111-1111-4111-8111-111111111111"
    output_id = "22222222-2222-4222-8222-222222222222"
    guided = GuidedSession(
        step=GuidedStep.STEP_3_TRANSFORMS,
        profile=EMPTY_PROFILE,
        source_order=(source_id,),
        reviewed_sources={
            source_id: SourceResolved(
                name="input",
                plugin="csv",
                options={"path": "/data/input.csv"},
                observed_columns=("id",),
                sample_rows=(),
                on_validation_failure="discard",
            )
        },
        output_order=(output_id,),
        reviewed_outputs={
            output_id: SinkOutputResolved(
                name="results",
                plugin="json",
                options={"path": "/data/results.jsonl"},
                required_fields=("id",),
                schema_mode="observed",
                on_write_failure="discard",
            )
        },
    )
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
        patch("elspeth.web.composer.service.plan_pipeline", autospec=True, side_effect=_PlannerReached) as planner,
        patch.object(service, "_run_advisor_checkpoint", autospec=True, side_effect=_PlannerReached) as advisor,
        pytest.raises(_PlannerReached if allowed else ComposerAdmissionRefused),
    ):
        if entry == "rootless":
            await service.compose(
                origin.content,
                [],
                state,
                session_id=_SESSION_ID,
                user_id="owner",
                user_message_id=message_id,
                session_operation_context=_CONTEXT,
            )
        elif entry == "guided_full":
            await service.plan_guided_full_pipeline(
                intent=origin.content,
                current_state=state,
                originating_message=origin,
                base=base,
                policy_catalog=catalog,
                plugin_snapshot=snapshot,
                recorder=BufferingRecorder(),
                operation_fence=fence,
                session_operation_context=_CONTEXT,
            )
        elif entry == "guided_delta":
            await service.plan_guided_pipeline(
                intent=origin.content,
                current_state=state,
                guided=guided,
                originating_message=origin,
                base=base,
                user_id="owner",
                supersedes_draft_hash=None,
                recorder=BufferingRecorder(),
                operation_fence=fence,
                session_operation_context=_CONTEXT,
            )
        else:
            await service.run_signoff_checkpoint(
                state=state,
                session_id=_SESSION_ID,
                recorder=BufferingRecorder(),
                session_operation_context=_CONTEXT,
            )
    assert authority.operations == [ChargeableOperation.COMPOSER]
    assert planner.await_count == int(allowed and entry != "signoff")
    assert advisor.await_count == int(allowed and entry == "signoff")
