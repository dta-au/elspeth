"""A gateway or provider rejection for capacity reaches the user as that.

The compose loop is driven against a ``litellm.acompletion`` fake that
raises the SDK's own exceptions. A rejection recognised as capacity is the
size refusal, with the gateway named as the ceiling; one that is not
recognised is never called size, and the gateway's own reason is shown
bounded and scrubbed.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from litellm.exceptions import BadRequestError, ContextWindowExceededError

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.protocol import ComposerConvergenceError, ComposerHistoryMessage
from elspeth.web.composer.provider_errors import classify_provider_failure
from elspeth.web.composer.provider_gateway import _BadRequestLLMError
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.sessions.routes._helpers import _litellm_error_detail, _sent_over_estimate_guidance
from tests.unit.web.composer._helpers import _clean_advisor_checkpoint, _composer_service_with_session
from tests.unit.web.composer.test_compose_loop_context_budget import _assistant, _user
from tests.unit.web.composer.test_compose_loop_llm_audit import (
    _empty_state,
    _make_llm_response,
    _make_settings,
    _mock_catalog,
    _passing_preflight,
)

_GENEROUS_SETTING = 10_000_000
_SMALL_SETTING = 20_000


@pytest.fixture(autouse=True)
def _advisor_end_gate_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """The END advisor gate is not under test; it returns CLEAN without a provider call."""
    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint, raising=True)


class _ScriptedProvider:
    """A ``litellm.acompletion`` fake: records each request, then returns or raises the next scripted item."""

    def __init__(self, *items: Any) -> None:
        self._items = iter(items)
        self.requests: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> Any:
        self.requests.append(deepcopy(kwargs))
        item = next(self._items)
        if isinstance(item, BaseException):
            raise item
        return item


def _too_long() -> ContextWindowExceededError:
    return ContextWindowExceededError(
        message="AnthropicError - prompt is too long: 251000 tokens > 200000 maximum", model="test-model", llm_provider="anthropic"
    )


def _other_rejection(message: str = "GatewayError - unsupported value for field 'temperature'") -> BadRequestError:
    return BadRequestError(message=message, model="test-model", llm_provider="openai")


def _service(max_prompt_tokens: int) -> tuple[ComposerServiceImpl, str]:
    return _composer_service_with_session(
        catalog=_mock_catalog(), settings=_make_settings(composer_context_max_prompt_tokens=max_prompt_tokens)
    )


async def _compose(
    service: ComposerServiceImpl, session_id: str, provider: _ScriptedProvider, history: list[ComposerHistoryMessage] | None = None
) -> Any:
    with (
        patch("litellm.acompletion", new_callable=AsyncMock, side_effect=provider.complete),
        patch.object(service._preflight, "cached_runtime_preflight", new_callable=AsyncMock, return_value=_passing_preflight()),
    ):
        return await service.compose("Build a CSV pipeline", history or [], _empty_state(), session_id=session_id)


def _history() -> list[ComposerHistoryMessage]:
    return [_user(0, "Please read the CSV file."), _assistant("I have set up the CSV source.")]


def _history_texts(request: dict[str, Any]) -> list[str]:
    wanted = {row["content"] for row in _history()}
    return [message["content"] for message in request["messages"] if message["content"] in wanted]


def test_the_context_window_class_is_admitted_as_context_length() -> None:
    failure = classify_provider_failure(_too_long())

    assert failure is not None
    assert failure.kind == "bad_request"
    assert failure.retryable is False
    assert failure.request_rejection is not None
    assert failure.request_rejection.reason_code == "context_length_exceeded"
    assert failure.request_rejection.reason_disposition == "retained"
    assert failure.request_rejection.reason_source == "sdk_class"


def test_control_an_ordinary_bad_request_is_not_admitted_as_context_length() -> None:
    failure = classify_provider_failure(_other_rejection())

    assert failure is not None
    assert failure.kind == "bad_request"
    assert failure.request_rejection is not None
    assert failure.request_rejection.reason_code is None


def test_a_raw_400_whose_message_is_the_providers_too_long_wording_is_admitted_by_pattern() -> None:
    """The planner hands the classifier the raw SDK exception; a gateway may map the rejection to a plain 400."""
    failure = classify_provider_failure(
        _other_rejection("GatewayError - AnthropicException - prompt is too long: 251000 tokens > 200000 maximum")
    )

    assert failure is not None and failure.request_rejection is not None
    assert failure.request_rejection.reason_code == "context_length_exceeded"
    assert failure.request_rejection.reason_source == "message_pattern"


def test_control_a_raw_400_whose_body_carries_the_code_is_admitted_from_the_body() -> None:
    import httpx
    from openai import BadRequestError as OpenAIBadRequestError

    response = httpx.Response(400, request=httpx.Request("POST", "https://example.invalid"))
    failure = classify_provider_failure(OpenAIBadRequestError("rejected", response=response, body={"code": "context_length_exceeded"}))

    assert failure is not None and failure.request_rejection is not None
    assert failure.request_rejection.reason_code == "context_length_exceeded"
    assert failure.request_rejection.reason_source == "sdk_body"


def test_the_compose_loops_carrier_form_classifies_through_its_cause() -> None:
    try:
        try:
            raise _too_long()
        except BadRequestError as exc:
            raise _BadRequestLLMError("LLM request rejected (ContextWindowExceededError)", provider_status_code=400) from exc
    except _BadRequestLLMError as carrier:
        cause = carrier.__cause__

    assert cause is not None
    failure = classify_provider_failure(cause)
    assert failure is not None and failure.request_rejection is not None
    assert failure.request_rejection.reason_code == "context_length_exceeded"
    assert failure.request_rejection.reason_source == "sdk_class"


def test_control_a_failure_that_is_not_a_400_is_not_capacity_whatever_its_text() -> None:
    from litellm.exceptions import ServiceUnavailableError

    failure = classify_provider_failure(
        ServiceUnavailableError(message="prompt is too long: 251000 tokens > 200000 maximum", model="test-model", llm_provider="openai")
    )

    assert failure is not None
    assert failure.request_rejection is None


@pytest.mark.asyncio
async def test_a_capacity_rejection_within_the_estimate_with_nothing_to_leave_out_is_the_size_refusal() -> None:
    """No history is attached, so there is nothing to retry without: one request, then the refusal naming the gateway."""
    service, session_id = _service(_GENEROUS_SETTING)
    provider = _ScriptedProvider(_too_long(), _make_llm_response(content="Done."))

    with pytest.raises(ComposerConvergenceError) as exc_info:
        await _compose(service, session_id, provider)

    assert len(provider.requests) == 1
    error = exc_info.value
    assert error.reason == "context_budget_exceeded"
    assert error.evidence["ceiling"] == "provider"
    assert error.evidence["largest_component"] in {"system", "catalog", "tools", "state", "current_message", "latest_tool_round", "other"}
    assert error.evidence["request_bytes"] > 0
    assert "gateway" in str(error)
    (call,) = error.llm_calls
    assert call.status is ComposerLLMCallStatus.BAD_REQUEST_ERROR
    assert call.error_class == "ContextWindowExceededError"
    assert call.error_message == "context_length_exceeded"
    assert call.rejection_reason_source == "sdk_class"


@pytest.mark.asyncio
async def test_a_rejection_recognised_by_its_wording_says_so_on_its_call_record() -> None:
    service, session_id = _service(_GENEROUS_SETTING)
    provider = _ScriptedProvider(_other_rejection("GatewayError - AnthropicException - prompt is too long: 251000 tokens > 200000 maximum"))

    with pytest.raises(ComposerConvergenceError) as exc_info:
        await _compose(service, session_id, provider)

    (call,) = exc_info.value.llm_calls
    assert call.error_message == "context_length_exceeded"
    # Every wording-driven size rejection is visible in the audit as one.
    assert call.rejection_reason_source == "message_pattern"
    assert call.to_dict()["rejection_reason_source"] == "message_pattern"


@pytest.mark.asyncio
async def test_control_an_unrecognised_rejection_is_not_called_size_and_is_not_retried() -> None:
    service, session_id = _service(_GENEROUS_SETTING)
    provider = _ScriptedProvider(_other_rejection(), _make_llm_response(content="Done."))

    with pytest.raises(_BadRequestLLMError):
        await _compose(service, session_id, provider, _history())

    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_control_an_unrecognised_rejection_within_the_estimate_makes_no_size_claim() -> None:
    service, session_id = _service(_GENEROUS_SETTING)
    provider = _ScriptedProvider(_other_rejection())

    with pytest.raises(_BadRequestLLMError) as exc_info:
        await _compose(service, session_id, provider)

    detail = _litellm_error_detail("llm_unavailable", exc_info.value, expose_provider_error=False)
    assert detail == {"error_type": "llm_unavailable", "detail": "_BadRequestLLMError"}
    assert _sent_over_estimate_guidance(exc_info.value) is None


def _rejection_carrier(**extra: Any) -> _BadRequestLLMError:
    secret = "sk-" + "a" * 40
    return _BadRequestLLMError(
        "LLM request rejected (BadRequestError)",
        provider_detail=f"GatewayError - request rejected by policy; api_key={secret} " + "detail " * 400,
        provider_status_code=400,
        **extra,
    )


def test_a_rejection_of_a_request_sent_over_the_estimate_says_so_whatever_the_flag() -> None:
    """The user learns size is a likely cause; the gateway's own words stay behind the operator's flag."""
    detail = _litellm_error_detail("llm_unavailable", _rejection_carrier(sent_over_estimate=True), expose_provider_error=False)

    carrier = _rejection_carrier(sent_over_estimate=True)
    guidance = _sent_over_estimate_guidance(carrier)
    assert guidance is not None and "size" in guidance
    assert "provider_detail" not in detail
    assert "provider_status_code" not in detail


def test_control_the_gateways_reason_is_shown_bounded_and_scrubbed_only_with_the_flag_on() -> None:
    exc = _rejection_carrier()

    off = _litellm_error_detail("llm_unavailable", exc, expose_provider_error=False)
    on = _litellm_error_detail("llm_unavailable", exc, expose_provider_error=True)

    assert "provider_detail" not in off
    shown = on["provider_detail"]
    assert isinstance(shown, str) and shown
    assert len(shown) <= 1_000
    assert "sk-" + "a" * 40 not in shown
    assert on["provider_status_code"] == 400


def test_control_the_operator_flag_still_governs_other_provider_failures() -> None:
    from litellm.exceptions import ServiceUnavailableError

    exc = ServiceUnavailableError(message="upstream overloaded", model="test-model", llm_provider="openai")

    detail = _litellm_error_detail("llm_unavailable", exc, expose_provider_error=False)

    assert "provider_detail" not in detail
