"""A request the provider, or a proxy in front of it, refuses as too large (HTTP 413) is a size rejection.

The status is structured evidence: no provider text is read. The installed
litellm surfaces a 413 differently per route (measured through its own
mapping, ``exception_type``, as ``acompletion`` raises it):

- OpenAI-compatible routes: ``APIError`` with ``status_code`` 413;
- Bedrock: ``BadRequestError`` with ``status_code`` 413;
- Anthropic: ``BadRequestError`` with ``status_code`` 400. The 413 is not on
  the raised exception, so it stays an ordinary rejected request: never
  resent unchanged, but not recognised as a size rejection.

A size rejection is not retried unchanged. While an earlier turn is still in
the request, it is resent once with every earlier turn left out, as a
context-limit rejection is.
"""

from __future__ import annotations

import litellm
import pytest
from litellm.litellm_core_utils.exception_mapping_utils import exception_type
from litellm.llms.anthropic.common_utils import AnthropicError
from litellm.llms.base_llm.chat.transformation import BaseLLMException
from litellm.llms.bedrock.common_utils import BedrockError
from litellm.llms.openai.common_utils import OpenAIError

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.provider_rejection import SafeRequestRejection
from elspeth.web.composer import audit as composer_audit
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.protocol import ComposerConvergenceError
from elspeth.web.composer.provider_errors import classify_provider_failure, request_size_rejection
from tests.unit.web.composer.test_compose_loop_llm_audit import _make_llm_response
from tests.unit.web.composer.test_gateway_capacity_rejection import _GENEROUS_SETTING, _compose, _ScriptedProvider, _service
from tests.unit.web.composer.test_gateway_capacity_rejection import (
    _advisor_end_gate_clean as _advisor_end_gate_clean_fixture,
)
from tests.unit.web.composer.test_provider_rejection_retry import _HISTORY_TEXTS, _budget, _history, _history_in

_advisor_end_gate_clean = _advisor_end_gate_clean_fixture

_PROXY_BODY = "<html><head><title>413 Request Entity Too Large</title></head><body><center>nginx</center></body></html>"
_ROUTES: dict[str, type[BaseLLMException]] = {
    "openai": OpenAIError,
    "anthropic": AnthropicError,
    "bedrock": BedrockError,
    "bedrock_mantle": BedrockError,
}
# The routes whose 413 the installed litellm reports with that status.
SIZE_413_ROUTES = ("bedrock", "bedrock_mantle", "openai")


def mapped_provider_error(route: str, status: int) -> BaseException:
    """The exception the installed litellm raises for an HTTP ``status`` from ``route``."""
    litellm.suppress_debug_info = True
    provider_error = _ROUTES[route]
    try:
        try:
            raise provider_error(status_code=status, message=_PROXY_BODY)
        except BaseLLMException as caught:
            exception_type(model="test-model", original_exception=caught, custom_llm_provider=route)
    except Exception as mapped:
        return mapped
    raise AssertionError("litellm did not map the provider error")


_TOO_LARGE = SafeRequestRejection(reason_code="request_too_large", reason_disposition="retained", reason_source="http_status")


@pytest.mark.parametrize("route", SIZE_413_ROUTES)
def test_a_413_is_a_request_too_large_rejection_that_is_not_retried_unchanged(route: str) -> None:
    failure = classify_provider_failure(mapped_provider_error(route, 413))

    assert failure is not None
    assert (failure.audit_status, failure.kind, failure.retryable) == (ComposerLLMCallStatus.BAD_REQUEST_ERROR, "bad_request", False)
    assert failure.request_rejection == _TOO_LARGE
    assert request_size_rejection(mapped_provider_error(route, 413)) == _TOO_LARGE


def test_an_anthropic_413_is_a_rejected_request_that_is_not_retried_unchanged() -> None:
    # litellm reports it as 400, so it is not a size rejection; it is not an outage either.
    exc = mapped_provider_error("anthropic", 413)
    failure = classify_provider_failure(exc)

    assert failure is not None
    assert (failure.kind, failure.retryable) == ("bad_request", False)
    assert request_size_rejection(exc) is None


@pytest.mark.parametrize("route", sorted(_ROUTES))
@pytest.mark.parametrize("status", [400, 500, 503])
def test_another_status_with_the_same_body_is_not_a_size_rejection(route: str, status: int) -> None:
    # Control: only the status decides; the proxy's 413 page as a body does not.
    assert request_size_rejection(mapped_provider_error(route, status)) is None


def test_the_rejection_explains_itself_apart_from_a_context_limit() -> None:
    context_limit = SafeRequestRejection(reason_code="context_length_exceeded", reason_disposition="retained", reason_source="sdk_class")
    assert _TOO_LARGE.explanation != context_limit.explanation
    assert _TOO_LARGE.to_wire()["reason_code"] == "request_too_large"
    assert _TOO_LARGE.to_wire()["reason_source"] == "http_status"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", SIZE_413_ROUTES)
async def test_a_413_with_history_present_is_resent_once_with_every_earlier_turn_left_out(route: str) -> None:
    service, session_id = _service(_GENEROUS_SETTING)
    provider = _ScriptedProvider(mapped_provider_error(route, 413), _make_llm_response(content="Pipeline built."))

    result = await _compose(service, session_id, provider, _history())

    assert len(provider.requests) == 2
    assert _history_in(provider.requests[0]) == list(_HISTORY_TEXTS)
    assert _history_in(provider.requests[1]) == []
    rejected, retried = result.llm_calls
    assert rejected.status is ComposerLLMCallStatus.BAD_REQUEST_ERROR
    # Recorded by its closed reason, never by provider text.
    assert rejected.error_message == "request_too_large"
    assert rejected.rejection_reason_source == "http_status"
    assert retried.status is ComposerLLMCallStatus.SUCCESS
    assert _budget(retried).retry_after_provider_rejection is True


@pytest.mark.asyncio
async def test_a_413_with_nothing_left_to_leave_out_is_the_size_refusal_naming_the_provider() -> None:
    service, session_id = _service(_GENEROUS_SETTING)
    provider = _ScriptedProvider(mapped_provider_error("openai", 413))

    with pytest.raises(ComposerConvergenceError) as exc_info:
        await _compose(service, session_id, provider)

    assert len(provider.requests) == 1
    assert exc_info.value.reason == "context_budget_exceeded"
    assert exc_info.value.evidence["ceiling"] == "provider"
    (call,) = exc_info.value.llm_calls
    assert call.error_message == "request_too_large"
    assert call.to_dict()["rejection_reason_source"] == "http_status"
    assert _PROXY_BODY not in str(call.to_dict())


@pytest.mark.asyncio
async def test_the_persisted_call_audit_names_what_recognised_the_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    service, session_id = _service(_GENEROUS_SETTING)
    provider = _ScriptedProvider(mapped_provider_error("openai", 413))

    with pytest.raises(ComposerConvergenceError) as exc_info:
        await _compose(service, session_id, provider)

    (call,) = exc_info.value.llm_calls
    persisted = llm_call_audit_envelope(call)["call"]
    assert isinstance(persisted, dict)
    assert persisted["rejection_reason_source"] == "http_status"
    # Control: the instrument goes red when the public whitelist drops the field.
    monkeypatch.setattr(
        composer_audit,
        "_LLM_CALL_PUBLIC_AUDIT_FIELDS",
        tuple(name for name in composer_audit._LLM_CALL_PUBLIC_AUDIT_FIELDS if name != "rejection_reason_source"),
    )
    dropped = llm_call_audit_envelope(call)["call"]
    assert isinstance(dropped, dict)
    assert "rejection_reason_source" not in dropped
