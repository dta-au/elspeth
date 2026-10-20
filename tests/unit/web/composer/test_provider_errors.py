"""Owned, value-free classification of SDK failures at Composer dispatches."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import asdict

import httpx
import pytest
from litellm.exceptions import APIError as LiteLLMAPIError
from litellm.exceptions import AuthenticationError, BadGatewayError, BadRequestError, ServiceUnavailableError
from litellm.exceptions import Timeout as LiteLLMTimeout
from openai import BadRequestError as OpenAIBadRequestError
from openai import OpenAIError
from pydantic import ValidationError

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.provider_rejection import SafeRequestRejection
from elspeth.web.composer.provider_errors import ProviderRequestFailure, classify_provider_failure

_SECRET = "UPSTREAM-RESPONSE-BODY-SECRET"
_PROVIDER = "test-provider"
_MODEL = "test/model"


@pytest.mark.parametrize(
    ("failure", "status", "kind", "retryable"),
    [
        pytest.param(
            AuthenticationError(message=_SECRET, llm_provider=_PROVIDER, model=_MODEL),
            ComposerLLMCallStatus.AUTH_ERROR,
            "auth",
            False,
            id="authentication",
        ),
        pytest.param(
            BadRequestError(message=_SECRET, llm_provider=_PROVIDER, model=_MODEL),
            ComposerLLMCallStatus.BAD_REQUEST_ERROR,
            "bad_request",
            False,
            id="bad-request",
        ),
        pytest.param(
            BadGatewayError(message=_SECRET, llm_provider=_PROVIDER, model=_MODEL),
            ComposerLLMCallStatus.API_ERROR,
            "unavailable",
            False,
            id="bad-gateway",
        ),
        pytest.param(
            ServiceUnavailableError(message=_SECRET, llm_provider=_PROVIDER, model=_MODEL),
            ComposerLLMCallStatus.API_ERROR,
            "unavailable",
            True,
            id="service-unavailable",
        ),
        pytest.param(
            LiteLLMAPIError(status_code=503, message=_SECRET, llm_provider=_PROVIDER, model=_MODEL),
            ComposerLLMCallStatus.API_ERROR,
            "unavailable",
            True,
            id="generic-api-error",
        ),
        pytest.param(
            OpenAIError(_SECRET),
            ComposerLLMCallStatus.API_ERROR,
            "unavailable",
            False,
            id="other-openai-error",
        ),
        pytest.param(
            LiteLLMTimeout(message=_SECRET, llm_provider=_PROVIDER, model=_MODEL),
            ComposerLLMCallStatus.TIMEOUT,
            "timeout",
            False,
            id="sdk-timeout",
        ),
    ],
)
def test_sdk_failure_is_classified_without_provider_values(
    failure: BaseException,
    status: ComposerLLMCallStatus,
    kind: str,
    retryable: bool,
) -> None:
    result = classify_provider_failure(failure)

    assert result is not None
    assert (result.audit_status, result.kind, result.retryable) == (status, kind, retryable)
    assert _SECRET not in repr(result)
    assert _SECRET not in repr(asdict(result))


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(ValueError(_SECRET), id="first-party-value-error"),
        pytest.param(AuditIntegrityError(_SECRET), id="audit-integrity"),
        pytest.param(TimeoutError(_SECRET), id="timeout-lifecycle"),
        pytest.param(asyncio.CancelledError(_SECRET), id="cancelled-lifecycle"),
    ],
)
def test_non_provider_failures_are_not_classified(failure: BaseException) -> None:
    assert classify_provider_failure(failure) is None


@pytest.mark.parametrize("code", ["invalid_request", "context_length_exceeded", "unsupported_parameter"])
def test_rejection_retains_closed_sdk_reason_and_original_identity(code: str) -> None:
    response = httpx.Response(400, request=httpx.Request("POST", "https://example.invalid"), json={"error": {"code": code}})
    failure = OpenAIBadRequestError(_SECRET, response=response, body={"code": code, "message": _SECRET, "param": _SECRET})
    result = classify_provider_failure(failure)

    assert result is not None and result.request_rejection is not None
    detail = result.request_rejection
    assert detail.reason_code == code and detail.reason_disposition == "retained"
    assert detail.reason_source == "sdk_body"
    assert not result.retryable and result.kind == "bad_request"
    assert result.audit_status == ComposerLLMCallStatus.BAD_REQUEST_ERROR
    internal = ProviderRequestFailure(original=failure, details=detail)
    assert internal.original is failure
    assert _SECRET not in repr(internal)
    assert _SECRET not in repr(asdict(result))
    assert _SECRET not in detail.model_dump_json()
    assert SafeRequestRejection.from_wire(detail.to_wire()) == detail


@pytest.mark.parametrize(
    ("code", "expected_code", "disposition"),
    [
        ("context_length_exceeded", "context_length_exceeded", "retained"),
        ("unsupported_parameter", "unsupported_parameter", "retained"),
        (_SECRET, None, "unrecognized"),
        (42, None, "unrecognized"),
        (None, None, "absent"),
    ],
)
def test_rejection_recovers_only_safe_reason_from_retained_http_response(code: object, expected_code: str | None, disposition: str) -> None:
    """LiteLLM may discard SDK body/code while preserving the buffered response."""
    response = httpx.Response(
        400,
        request=httpx.Request("POST", "https://example.invalid"),
        json={"error": {"code": code, "message": _SECRET, "param": _SECRET}},
    )
    failure = BadRequestError(_SECRET, model=_MODEL, llm_provider=_PROVIDER, response=response)
    assert failure.body is None and failure.code is None
    result = classify_provider_failure(failure)

    assert result is not None and result.request_rejection is not None
    detail = result.request_rejection
    assert detail.reason_code == expected_code and detail.reason_disposition == disposition
    assert detail.reason_source == "http_response"
    assert not result.retryable
    assert _SECRET not in repr(asdict(result)) + detail.model_dump_json() + detail.explanation


def test_conflicting_sdk_and_response_reasons_are_not_rendered_as_either_reason() -> None:
    response = httpx.Response(
        400,
        request=httpx.Request("POST", "https://example.invalid"),
        json={"error": {"code": "unsupported_parameter"}},
    )
    failure = OpenAIBadRequestError(_SECRET, response=response, body={"code": "context_length_exceeded"})
    result = classify_provider_failure(failure)

    assert result is not None and result.request_rejection is not None
    detail = result.request_rejection
    assert detail.reason_code is None and detail.reason_disposition == "unrecognized"
    assert detail.reason_source == "unavailable"
    assert "context limit" not in detail.explanation and "unsupported parameter" not in detail.explanation


@pytest.mark.parametrize(
    "content",
    [
        b"not JSON",
        b'{"error":{"code":"invalid_request","code":"context_length_exceeded"}}',
        b'{"error":{"code":"invalid_request"}}' + b" " * 65_536,
        b'{"error":false}',
        b'"invalid_request"',
        b"\xff",
    ],
)
def test_unavailable_response_reason_is_not_invented(content: bytes) -> None:
    response = httpx.Response(400, request=httpx.Request("POST", "https://example.invalid"), content=content)
    failure = OpenAIBadRequestError(_SECRET, response=response, body=None)
    result = classify_provider_failure(failure)

    assert result is not None and result.request_rejection is not None
    assert result.request_rejection.reason_code is None
    assert result.request_rejection.reason_disposition == "unavailable"
    assert result.request_rejection.reason_source == "unavailable"


def test_unread_response_is_never_consumed_to_recover_a_reason() -> None:
    class ForbiddenStream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            raise AssertionError("The classifier must not read a response stream")

    response = httpx.Response(400, request=httpx.Request("POST", "https://example.invalid"), stream=ForbiddenStream())
    failure = OpenAIBadRequestError(_SECRET, response=response, body=None)
    result = classify_provider_failure(failure)

    assert result is not None and result.request_rejection is not None
    assert result.request_rejection.reason_disposition == "unavailable"
    assert not response.is_stream_consumed


def test_safe_rejection_contract_rejects_extra_prose_inconsistent_evidence_and_reference() -> None:
    detail = SafeRequestRejection(reason_code=None, reason_disposition="absent", reason_source="http_response")
    with pytest.raises(ValueError):
        SafeRequestRejection.from_wire({**detail.to_wire(), "message": _SECRET})
    with pytest.raises(ValidationError):
        SafeRequestRejection(reason_code="invalid_request", reason_disposition="absent", reason_source="sdk_body")
    with pytest.raises(ValidationError):
        SafeRequestRejection(reason_code=None, reason_disposition="retained", reason_source="sdk_body")
    with pytest.raises(ValidationError):
        SafeRequestRejection(reason_code="invalid_request", reason_disposition="retained", reason_source="unavailable")
    with pytest.raises(ValidationError):
        SafeRequestRejection.from_wire({**detail.to_wire(), "diagnostic_reference": _SECRET})
    with pytest.raises(ValidationError):
        SafeRequestRejection.from_wire({**detail.to_wire(), "retryable": True})
    with pytest.raises(ValidationError):
        SafeRequestRejection.from_wire({**detail.to_wire(), "retryable": 0})
    with pytest.raises(ValidationError):
        detail.__setattr__("reason_code", "invalid_request")


def test_safe_rejection_wire_requires_each_field_in_mutable_and_frozen_metadata() -> None:
    from elspeth.contracts.freeze import deep_freeze

    detail = SafeRequestRejection(reason_code=None, reason_disposition="absent", reason_source="http_response")
    wire = detail.to_wire()
    assert SafeRequestRejection.from_wire(wire) == detail
    assert SafeRequestRejection.from_wire(deep_freeze(wire)) == detail
    for key in wire:
        missing = {name: value for name, value in wire.items() if name != key}
        with pytest.raises(ValueError):
            SafeRequestRejection.from_wire(missing)
        with pytest.raises(ValueError):
            SafeRequestRejection.from_wire(deep_freeze(missing))
    assert detail.to_wire() == wire
