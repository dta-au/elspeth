"""Gateway length rejections retain only closed evidence and reach the compose loop."""

from __future__ import annotations

import json
from collections.abc import Mapping

import httpx
import openai
import pytest
from litellm.exceptions import BadRequestError, ContextWindowExceededError
from litellm.litellm_core_utils.exception_mapping_utils import exception_type
from litellm.llms.bedrock.common_utils import BedrockError
from openai import BadRequestError as OpenAIBadRequestError

from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.protocol import ComposerConvergenceError
from elspeth.web.composer.provider_errors import classify_provider_failure, request_size_rejection
from tests.unit.web.composer._helpers import _clean_advisor_checkpoint
from tests.unit.web.composer.test_gateway_capacity_rejection import _GENEROUS_SETTING, _compose, _ScriptedProvider, _service

_LENGTH_MESSAGE = "Input is too long for requested model."


def is_context_length_rejection(exc: BaseException) -> bool:
    """A size rejection for the model's context limit (not a request-size limit)."""
    rejection = request_size_rejection(exc)
    return rejection is not None and rejection.reason_code == "context_length_exceeded"


_REQUEST = httpx.Request("POST", "https://gateway.invalid/model/invoke")


def _bedrock_error(status: int, message: str, body: Mapping[str, object] | None = None) -> BaseException:
    response = httpx.Response(status, request=_REQUEST, json=body if body is not None else {"message": message})
    original = BedrockError(status_code=status, message=message, request=_REQUEST, response=response)
    try:
        exception_type(model="bedrock/anthropic.claude-opus-4-8", original_exception=original, custom_llm_provider="bedrock")
    except BaseException as exc:
        return exc
    raise AssertionError("LiteLLM did not raise")


def _rejection(exc: BaseException) -> tuple[str | None, str, str]:
    failure = classify_provider_failure(exc)
    assert failure is not None and failure.request_rejection is not None
    detail = failure.request_rejection
    return detail.reason_code, detail.reason_disposition, detail.reason_source


def _sdk_error(code: str) -> BaseException:
    body = {"error": {"code": code, "message": _LENGTH_MESSAGE, "type": "invalid_request_error"}}
    response = httpx.Response(400, request=_REQUEST, content=json.dumps(body).encode())
    client = openai.OpenAI(api_key="x", base_url="https://gateway.invalid/v1")
    return client._make_status_error(_LENGTH_MESSAGE, body=body["error"], response=response)


@pytest.mark.parametrize(
    ("code", "expected_reason", "expected_source", "expected_length"),
    [
        ("ValidationException", "context_length_exceeded", "message_pattern", True),
        ("invalid_request", "context_length_exceeded", "message_pattern", True),
        ("unsupported_parameter", "unsupported_parameter", "sdk_body", False),
        ("context_length_exceeded", "context_length_exceeded", "sdk_body", True),
    ],
)
def test_openai_sdk_shape_obeys_specific_code_precedence(
    code: str, expected_reason: str, expected_source: str, expected_length: bool
) -> None:
    exc = _sdk_error(code)

    assert isinstance(exc, OpenAIBadRequestError)
    assert exc.body == {"code": code, "message": _LENGTH_MESSAGE, "type": "invalid_request_error"}
    assert exc.code == code
    assert exc.response.json()["error"]["code"] == code
    assert _rejection(exc) == (expected_reason, "retained", expected_source)
    assert is_context_length_rejection(exc) is expected_length


@pytest.mark.parametrize("code", ["ValidationException", "invalid_request"])
def test_bedrock_length_class_upgrades_generic_gateway_code(code: str) -> None:
    exc = _bedrock_error(400, _LENGTH_MESSAGE, {"error": {"code": code, "message": _LENGTH_MESSAGE}})

    assert isinstance(exc, ContextWindowExceededError)
    assert _rejection(exc) == ("context_length_exceeded", "retained", "sdk_class")
    assert is_context_length_rejection(exc)


@pytest.mark.parametrize("code", ["ValidationException", "invalid_request"])
def test_plain_400_pattern_upgrades_generic_gateway_code(code: str) -> None:
    response = httpx.Response(400, request=_REQUEST, json={"error": {"code": code, "message": _LENGTH_MESSAGE}})
    exc = BadRequestError(message=_LENGTH_MESSAGE, model="m", llm_provider="openai", response=response)

    assert _rejection(exc) == ("context_length_exceeded", "retained", "message_pattern")
    assert is_context_length_rejection(exc)


def test_explicit_context_length_code_keeps_its_structured_source() -> None:
    exc = _bedrock_error(400, _LENGTH_MESSAGE, {"error": {"code": "context_length_exceeded"}})

    assert _rejection(exc) == ("context_length_exceeded", "retained", "http_response")
    assert is_context_length_rejection(exc)


def test_specific_non_length_code_takes_precedence_over_length_class() -> None:
    exc = _bedrock_error(400, _LENGTH_MESSAGE, {"error": {"code": "unsupported_parameter"}})

    assert isinstance(exc, ContextWindowExceededError)
    assert _rejection(exc) == ("unsupported_parameter", "retained", "http_response")
    assert not is_context_length_rejection(exc)


@pytest.mark.parametrize(
    ("body_code", "expected_length"),
    [("invalid_request", True), ("unsupported_parameter", False)],
)
def test_conflicting_body_and_response_codes_follow_specific_precedence(body_code: str, expected_length: bool) -> None:
    response = httpx.Response(400, request=_REQUEST, json={"error": {"code": "OtherForeignCode"}})
    exc = OpenAIBadRequestError(_LENGTH_MESSAGE, response=response, body={"code": body_code})

    expected = ("context_length_exceeded", "retained", "message_pattern") if expected_length else (None, "unrecognized", "http_response")
    assert _rejection(exc) == expected
    assert is_context_length_rejection(exc) is expected_length


def test_two_foreign_code_observations_can_gain_a_length_reason() -> None:
    response = httpx.Response(400, request=_REQUEST, json={"error": {"code": "OtherForeignCode"}})
    exc = OpenAIBadRequestError(_LENGTH_MESSAGE, response=response, body={"code": "ValidationException"})

    assert _rejection(exc) == ("context_length_exceeded", "retained", "message_pattern")
    assert is_context_length_rejection(exc)


def test_unrelated_invalid_request_stays_generic() -> None:
    response = httpx.Response(400, request=_REQUEST, json={"error": {"code": "invalid_request"}})
    exc = BadRequestError(message="Unsupported input value", model="m", llm_provider="openai", response=response)

    assert _rejection(exc) == ("invalid_request", "retained", "http_response")
    assert not is_context_length_rejection(exc)


def test_short_input_too_long_wording_is_not_a_pattern() -> None:
    exc = BadRequestError(message="GatewayError - Input is too long", model="m", llm_provider="openai")

    assert _rejection(exc)[0] is None
    assert not is_context_length_rejection(exc)


def test_413_unrelated_to_context_is_not_upgraded() -> None:
    exc = _bedrock_error(413, "Request entity too large")

    assert not isinstance(exc, ContextWindowExceededError)
    assert not is_context_length_rejection(exc)


@pytest.mark.parametrize(
    "message",
    [_LENGTH_MESSAGE, "ValidationException: Input is too long for requested model."],
)
def test_direct_bedrock_400_length_rejection_is_recognised(message: str) -> None:
    """Direct Bedrock on a ``bedrock/`` route: the body is ``{"message": ...}`` with no error code."""
    exc = _bedrock_error(400, message)

    assert isinstance(exc, ContextWindowExceededError)
    assert _rejection(exc) == ("context_length_exceeded", "retained", "sdk_class")
    assert is_context_length_rejection(exc)


@pytest.mark.parametrize("status", [413, 500])
def test_bedrock_non_400_length_class_keeps_class_evidence(status: int) -> None:
    exc = _bedrock_error(status, _LENGTH_MESSAGE)

    assert isinstance(exc, ContextWindowExceededError)
    assert _rejection(exc) == ("context_length_exceeded", "retained", "sdk_class")
    assert is_context_length_rejection(exc)


@pytest.mark.asyncio
async def test_gateway_length_code_reaches_compose_size_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint, raising=True)
    service, session_id = _service(_GENEROUS_SETTING)
    provider = _ScriptedProvider(_bedrock_error(400, _LENGTH_MESSAGE, {"error": {"code": "ValidationException"}}))

    with pytest.raises(ComposerConvergenceError) as caught:
        await _compose(service, session_id, provider)

    assert len(provider.requests) == 1
    assert caught.value.reason == "context_budget_exceeded"
    assert caught.value.evidence["ceiling"] == "provider"
    (call,) = caught.value.llm_calls
    assert call.error_message == "context_length_exceeded"
    # litellm maps this Bedrock wording to its context-window class itself, and the record says so.
    assert call.rejection_reason_source == "sdk_class"
