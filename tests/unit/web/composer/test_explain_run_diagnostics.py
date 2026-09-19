"""Tests for ``ComposerServiceImpl.explain_run_diagnostics`` error handling.

The exception-catching surface is widened from ``LiteLLMAPIError`` alone to
include LiteLLM's policy-gate exceptions (``BudgetExceededError``,
``BlockedPiiEntityError``, ``GuardrailRaisedException``). These do not subclass
``LiteLLMAPIError`` at the pinned LiteLLM version — without the wider catch,
they would escape this method as raw ``litellm.exceptions`` types and break
the route-layer contract that diagnostics failures surface as
``ComposerServiceError``.

Ticket: elspeth-ab3ad30e87.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec, patch

import pytest
from litellm.exceptions import (
    BlockedPiiEntityError,
    BudgetExceededError,
    GuardrailRaisedException,
)

from elspeth.contracts.chargeable_admission import AdmissionPolicyEvidence, ChargeableAdmissionDecision, QuotaDisposition
from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.protocol import ComposerServiceError
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.coordination.quota_authority import ProviderAttempt
from elspeth.web.sessions.protocol import SessionServiceProtocol


@pytest.fixture
def admission_context(composer_service_without_sessions_service: ComposerServiceImpl) -> Iterator[SessionOperationContext]:
    """Provider error tests begin after an explicit no-quota admission."""
    sessions = create_autospec(SessionServiceProtocol, instance=True)
    sessions.assess_chargeable_operation.return_value = ChargeableAdmissionDecision(
        refusal_reason=None,
        evidence=AdmissionPolicyEvidence(quota_disposition=QuotaDisposition.NOT_CONFIGURED, secret_wiring_hash="a" * 64),
    )
    sessions.begin_provider_attempt.return_value = ProviderAttempt(attempt_id="diagnostics-attempt", started_at=datetime.now(UTC))
    composer_service_without_sessions_service._sessions_service = sessions
    context = SessionOperationContext(
        fence=SessionOperationFence(session_id="diagnostics-session", operation_id="operation", lease_token="token", operation_epoch=1),
        operation_kind=SessionOperationKind.COMPOSE,
    )
    yield context
    sessions.begin_provider_attempt.assert_awaited_once_with(session_operation_context=context, source="composer")
    sessions.finish_provider_attempt.assert_awaited_once()
    settlement = sessions.finish_provider_attempt.await_args.kwargs
    assert settlement["session_operation_context"] == context
    assert settlement["call"].call_id == "diagnostics-attempt"


@pytest.mark.parametrize(
    ("outcome", "expected_status"),
    [
        pytest.param("success", ComposerLLMCallStatus.SUCCESS, id="success"),
        pytest.param("timeout", ComposerLLMCallStatus.TIMEOUT, id="timeout"),
        pytest.param("malformed", ComposerLLMCallStatus.MALFORMED_RESPONSE, id="malformed"),
        pytest.param("provider_error", ComposerLLMCallStatus.API_ERROR, id="provider-error"),
    ],
)
@pytest.mark.asyncio
async def test_explain_run_diagnostics_records_each_outbound_call_once(
    composer_service_without_sessions_service: ComposerServiceImpl,
    outcome: str,
    expected_status: ComposerLLMCallStatus,
    admission_context: SessionOperationContext,
) -> None:
    service = composer_service_without_sessions_service
    recorder = BufferingRecorder()
    snapshot: dict[str, object] = {
        "run_id": "SECRET_DIAGNOSTICS_RUN_ID",
        "status": "failed",
        "rows": [],
    }

    async def fake_call_text_llm(**_kwargs: object) -> object:
        if outcome == "timeout":
            raise TimeoutError
        if outcome == "provider_error":
            raise BudgetExceededError(current_cost=10.0, max_budget=1.0, message="provider secret must not persist")
        if outcome == "malformed":
            return SimpleNamespace(model="test/diagnostics-model", choices=[])
        return SimpleNamespace(
            model="test/diagnostics-model",
            choices=[SimpleNamespace(message=SimpleNamespace(content="The run is processing one row."))],
        )

    with patch("litellm.acompletion", new=fake_call_text_llm):
        if outcome == "success":
            assert (
                await service.explain_run_diagnostics(snapshot, recorder=recorder, session_operation_context=admission_context)
                == "The run is processing one row."
            )
        else:
            with pytest.raises(ComposerServiceError):
                await service.explain_run_diagnostics(snapshot, recorder=recorder, session_operation_context=admission_context)

    assert len(recorder.llm_calls) == 1
    call = recorder.llm_calls[0]
    assert call.status is expected_status
    assert call.tools_spec_hash is None
    assert call.declared_tool_names == ()
    assert "SECRET_DIAGNOSTICS_RUN_ID" not in repr(call)
    assert "provider secret must not persist" not in repr(call)


@pytest.mark.asyncio
async def test_explain_run_diagnostics_without_recorder_settles_success_and_usage(
    composer_service_without_sessions_service: ComposerServiceImpl,
    admission_context: SessionOperationContext,
) -> None:
    service = composer_service_without_sessions_service
    sessions = service._sessions_service
    assert sessions is not None
    response = SimpleNamespace(
        model="test/diagnostics-model",
        choices=[SimpleNamespace(message=SimpleNamespace(content="The run completed."))],
        usage={"prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22},
    )
    with (
        patch("litellm.acompletion", autospec=True, return_value=response),
        patch.object(
            sessions, "finish_provider_attempt", spec=SessionServiceProtocol.finish_provider_attempt, wraps=sessions.finish_provider_attempt
        ) as finish,
    ):
        result = await service.explain_run_diagnostics(
            {"run_id": "run-default-recorder", "status": "completed", "rows": []},
            session_operation_context=admission_context,
        )

    assert result == "The run completed."
    finish.assert_awaited_once()
    call = finish.await_args.kwargs["call"]
    assert call.status is ComposerLLMCallStatus.SUCCESS
    assert call.prompt_tokens == 17
    assert call.completion_tokens == 5


@pytest.mark.parametrize(
    "exc_factory",
    [
        pytest.param(
            lambda: BudgetExceededError(current_cost=10.0, max_budget=1.0, message="budget exceeded"),
            id="BudgetExceededError",
        ),
        pytest.param(
            lambda: BlockedPiiEntityError(entity_type="EMAIL_ADDRESS", guardrail_name="presidio"),
            id="BlockedPiiEntityError",
        ),
        pytest.param(
            lambda: GuardrailRaisedException(
                guardrail_name="bedrock-guardrails",
                message="content blocked",
            ),
            id="GuardrailRaisedException",
        ),
    ],
)
@pytest.mark.asyncio
async def test_explain_run_diagnostics_wraps_litellm_policy_exceptions(
    composer_service_without_sessions_service: ComposerServiceImpl,
    exc_factory: Any,
    admission_context: SessionOperationContext,
) -> None:
    """Policy-gate LiteLLM exceptions surface as ``ComposerServiceError``.

    Without the widened catch, the raw ``litellm.exceptions`` class would
    escape ``explain_run_diagnostics`` and the route handler would translate
    it into a 500 with provider-specific messaging instead of a 502 with the
    composer's normalised error envelope.
    """
    service = composer_service_without_sessions_service
    litellm_exception = exc_factory()
    snapshot: dict[str, object] = {
        "run_id": "run-test-1",
        "status": "failed",
        "row_count": 0,
        "rows": [],
    }

    async def fake_call_text_llm(**_kwargs: object) -> object:
        raise litellm_exception

    with (
        patch(
            "litellm.acompletion",
            new=fake_call_text_llm,
        ),
        pytest.raises(ComposerServiceError) as exc_info,
    ):
        await service.explain_run_diagnostics(snapshot, session_operation_context=admission_context)

    # Wrap message mirrors the existing ``LLM unavailable ({type})`` pattern.
    assert "LLM unavailable" in str(exc_info.value)
    assert type(litellm_exception).__name__ in str(exc_info.value)


@pytest.mark.parametrize("exc_cls", [RuntimeError, ValueError, asyncio.CancelledError])
@pytest.mark.asyncio
async def test_unrelated_exceptions_propagate(
    composer_service_without_sessions_service: ComposerServiceImpl,
    exc_cls: type[BaseException],
    admission_context: SessionOperationContext,
) -> None:
    """Widened catch must NOT swallow unrelated exception types.

    Pins the negative side of commit 4dacddfda's widening — RuntimeError,
    ValueError, asyncio.CancelledError, etc. MUST escape unwrapped so the
    route layer's normalised error envelope only covers the documented
    LiteLLM policy-gate set.
    """
    service = composer_service_without_sessions_service
    unrelated_exception = exc_cls("boom")
    sessions = service._sessions_service
    assert sessions is not None
    snapshot: dict[str, object] = {"run_id": "run-x", "status": "failed", "row_count": 0, "rows": []}

    async def fake_call_text_llm(**_kwargs: object) -> object:
        raise unrelated_exception

    with (
        patch("litellm.acompletion", new=fake_call_text_llm),
        patch.object(
            sessions, "finish_provider_attempt", spec=SessionServiceProtocol.finish_provider_attempt, wraps=sessions.finish_provider_attempt
        ) as finish,
        pytest.raises(exc_cls) as exc_info,
    ):
        await service.explain_run_diagnostics(snapshot, session_operation_context=admission_context)

    assert exc_info.value is unrelated_exception
    finish.assert_awaited_once()
    expected_status = ComposerLLMCallStatus.CANCELLED if exc_cls is asyncio.CancelledError else ComposerLLMCallStatus.API_ERROR
    assert finish.await_args.kwargs["call"].status is expected_status
