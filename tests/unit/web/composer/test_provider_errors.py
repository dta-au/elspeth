"""Owned, value-free classification of SDK failures at Composer dispatches."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import httpx
import litellm
import pytest
from litellm.exceptions import APIError as LiteLLMAPIError
from litellm.exceptions import AuthenticationError, BadGatewayError, BadRequestError, RateLimitType, ServiceUnavailableError
from litellm.exceptions import RateLimitError as LiteLLMRateLimitError
from litellm.exceptions import Timeout as LiteLLMTimeout
from litellm.llms.anthropic.common_utils import AnthropicError
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler, MaskedHTTPStatusError
from litellm.llms.openai.common_utils import OpenAIError as LiteLLMOpenAIError
from openai import APIError, APIStatusError, AsyncOpenAI, OpenAIError
from openai import BadRequestError as OpenAIBadRequestError
from openai import RateLimitError as OpenAIRateLimitError
from pydantic import ValidationError

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.provider_rejection import SafeRequestRejection
from elspeth.web.composer.provider_errors import (
    ProviderRequestFailure,
    RetryAfter,
    _deliberately_generic,
    _provider_error_arms,
    _provider_error_disposition,
    classify_provider_failure,
    observe_rate_limit,
)
from elspeth.web.composer.provider_retry import retry_delay_seconds

_SECRET = "UPSTREAM-RESPONSE-BODY-SECRET"
_PROVIDER = "test-provider"
_MODEL = "test/model"


@pytest.mark.parametrize(
    ("status", "content", "content_type", "mapped_type", "kind", "retryable", "rate_limited", "reason"),
    [
        pytest.param(
            413,
            b'{"error":{"message":"Request too large"}}',
            "application/json",
            LiteLLMRateLimitError,
            "bad_request",
            False,
            False,
            "request_too_large",
            id="untyped-json-413",
        ),
        pytest.param(
            413,
            b"Request too large",
            "text/plain",
            LiteLLMRateLimitError,
            "bad_request",
            False,
            False,
            "request_too_large",
            id="plain-text-413",
        ),
        pytest.param(
            413,
            b'{"error":{"message":"Request too large","type":"invalid_request_error"}}',
            "application/json",
            OpenAIBadRequestError,
            "bad_request",
            False,
            False,
            "request_too_large",
            id="typed-json-413",
        ),
        pytest.param(
            429,
            b'{"error":{"message":"Rate limit reached on tokens per min (TPM)","type":"tokens"}}',
            "application/json",
            LiteLLMRateLimitError,
            "unavailable",
            True,
            True,
            None,
            id="genuine-429",
        ),
    ],
)
def test_openai_route_classifies_http_status_after_litellm_mapping(
    status: int,
    content: bytes,
    content_type: str,
    mapped_type: type[OpenAIError],
    kind: str,
    retryable: bool,
    rate_limited: bool,
    reason: str | None,
) -> None:
    async def mapped_failure() -> OpenAIError:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(status, content=content, headers={"content-type": content_type}))
        ) as http_client:
            client = AsyncOpenAI(api_key="test-key", base_url="https://example.invalid/v1", http_client=http_client, max_retries=0)
            with pytest.raises(OpenAIError) as caught:
                await litellm.acompletion(
                    model="openai/gpt-4o",
                    messages=[{"role": "user", "content": "hello"}],
                    client=client,
                    api_key="test-key",
                    num_retries=0,
                    max_retries=0,
                )
            return caught.value

    mapped = asyncio.run(mapped_failure())
    assert isinstance(mapped, mapped_type)
    result = classify_provider_failure(mapped)
    assert result is not None
    assert (result.kind, result.retryable, result.rate_limited) == (kind, retryable, rate_limited)
    assert result.audit_status == (ComposerLLMCallStatus.BAD_REQUEST_ERROR if kind == "bad_request" else ComposerLLMCallStatus.API_ERROR)
    assert (None if result.request_rejection is None else result.request_rejection.reason_code) == reason


def test_direct_rate_limit_without_a_chain_keeps_rate_limit_policy() -> None:
    result = classify_provider_failure(_rate_limit(headers={"retry-after": "7"}))
    assert result is not None
    assert (result.audit_status, result.kind, result.retryable, result.rate_limited) == (
        ComposerLLMCallStatus.API_ERROR,
        "unavailable",
        True,
        True,
    )
    assert result.request_rejection is None
    assert result.retry_after == RetryAfter(7)


def test_disagreeing_http_status_links_keep_rate_limit_policy() -> None:
    # An SDK response with a different status cannot be produced by one HTTP exchange.
    sdk_response = httpx.Response(429, request=httpx.Request("POST", "https://example.invalid"))
    sdk_error = APIStatusError(_SECRET, response=sdk_response, body=None)
    wrapper = LiteLLMOpenAIError(status_code=413, message=_SECRET)
    wrapper.__context__ = sdk_error
    mapped = _rate_limit(headers={"retry-after": "7"})
    mapped.__context__ = wrapper

    result = classify_provider_failure(mapped)
    assert result is not None
    assert (result.audit_status, result.kind, result.retryable, result.rate_limited) == (
        ComposerLLMCallStatus.API_ERROR,
        "unavailable",
        True,
        True,
    )
    assert result.request_rejection is None
    assert result.retry_after == RetryAfter(7)


@pytest.mark.parametrize(("status", "reason"), [(413, "request_too_large"), (429, None)])
def test_openai_provider_chain_uses_its_typed_status(status: int, reason: str | None) -> None:
    sdk_response = httpx.Response(status, request=httpx.Request("POST", "https://example.invalid"))
    sdk_error = APIStatusError(_SECRET, response=sdk_response, body=None)
    wrapper = LiteLLMOpenAIError(status_code=status, message=_SECRET)
    wrapper.__context__ = sdk_error
    mapped = _rate_limit()
    mapped.__context__ = wrapper

    result = classify_provider_failure(mapped)
    assert result is not None
    assert (None if result.request_rejection is None else result.request_rejection.reason_code) == reason
    assert result.rate_limited is (status == 429)


def test_openai_chain_with_non_413_wrapper_keeps_rate_limit_policy() -> None:
    sdk_response = httpx.Response(413, request=httpx.Request("POST", "https://example.invalid"))
    sdk_error = APIStatusError(_SECRET, response=sdk_response, body=None)
    wrapper = LiteLLMOpenAIError(status_code=429, message=_SECRET)
    wrapper.__context__ = sdk_error
    mapped = _rate_limit()
    mapped.__context__ = wrapper

    result = classify_provider_failure(mapped)
    assert result is not None
    assert result.rate_limited is True
    assert result.request_rejection is None


@pytest.mark.parametrize(("status", "reason"), [(413, "request_too_large"), (400, None)])
def test_direct_claude_provider_chain_uses_its_typed_status(status: int, reason: str | None) -> None:
    request = httpx.Request("POST", "https://example.invalid")
    response = httpx.Response(status, request=request)
    lower = MaskedHTTPStatusError(httpx.HTTPStatusError(_SECRET, request=request, response=response))
    wrapper = AnthropicError(status_code=status, message=_SECRET)
    wrapper.__context__ = lower
    mapped = BadRequestError(_SECRET, model=_MODEL, llm_provider="anthropic")
    mapped.__context__ = wrapper

    result = classify_provider_failure(mapped)
    assert result is not None
    assert (None if result.request_rejection is None else result.request_rejection.reason_code) == reason


def test_direct_claude_chain_with_non_413_wrapper_keeps_bad_request_policy() -> None:
    request = httpx.Request("POST", "https://example.invalid")
    response = httpx.Response(413, request=request)
    lower = MaskedHTTPStatusError(httpx.HTTPStatusError(_SECRET, request=request, response=response))
    wrapper = AnthropicError(status_code=400, message=_SECRET)
    wrapper.__context__ = lower
    mapped = BadRequestError(_SECRET, model=_MODEL, llm_provider="anthropic")
    mapped.__context__ = wrapper

    result = classify_provider_failure(mapped)
    assert result is not None
    assert result.kind == "bad_request"
    assert result.request_rejection is not None
    assert result.request_rejection.reason_code is None


@pytest.mark.parametrize(
    ("status", "body", "expected_type", "kind", "retryable", "reason"),
    [
        pytest.param(
            413,
            b'{"type":"error","error":{"type":"request_too_large","message":"Request too large"}}',
            BadRequestError,
            "bad_request",
            False,
            "request_too_large",
            id="typed-413",
        ),
        pytest.param(
            413, b'{"error":{"message":"Request too large"}}', BadRequestError, "bad_request", False, "request_too_large", id="untyped-413"
        ),
        pytest.param(
            429,
            b'{"type":"error","error":{"type":"rate_limit_error","message":"Slow down"}}',
            LiteLLMRateLimitError,
            "unavailable",
            True,
            None,
            id="rate-limit-429",
        ),
        pytest.param(
            400,
            b'{"type":"error","error":{"type":"invalid_request_error","message":"Invalid input"}}',
            BadRequestError,
            "bad_request",
            False,
            None,
            id="ordinary-400",
        ),
        pytest.param(
            400,
            b'{"type":"error","error":{"type":"invalid_request_error","message":"prompt is too long"}}',
            BadRequestError,
            "bad_request",
            False,
            "context_length_exceeded",
            id="context-length-400",
        ),
    ],
)
def test_direct_claude_route_classifies_http_status_after_litellm_mapping(
    status: int, body: bytes, expected_type: type[OpenAIError], kind: str, retryable: bool, reason: str | None
) -> None:
    async def mapped_failure() -> OpenAIError:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(status, content=body, headers={"content-type": "application/json"})
            )
        ) as http_client:
            handler = AsyncHTTPHandler()
            handler.client = http_client
            with pytest.raises(OpenAIError) as caught:
                await litellm.acompletion(
                    model="anthropic/claude-3-5-sonnet-20240620",
                    messages=[{"role": "user", "content": "hello"}],
                    api_base="https://example.invalid",
                    api_key="test-key",
                    client=handler,
                    num_retries=0,
                    max_retries=0,
                )
            return caught.value

    mapped = asyncio.run(mapped_failure())
    assert isinstance(mapped, expected_type)
    result = classify_provider_failure(mapped)
    assert result is not None
    assert (result.kind, result.retryable, result.rate_limited) == (kind, retryable, status == 429)
    assert (None if result.request_rejection is None else result.request_rejection.reason_code) == reason


@pytest.mark.parametrize(
    ("model", "header"),
    [
        ("bedrock/converse/anthropic.claude-3-5-sonnet-20240620-v1:0", "ValidationException"),
        ("bedrock/invoke/anthropic.claude-3-5-sonnet-20240620-v1:0", "ValidationException"),
    ],
)
def test_bedrock_routes_keep_http_413_as_bad_request(model: str, header: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """LiteLLM 1.102.2 and OpenAI 2.36.0 map both Bedrock routes to BadRequestError 413."""
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    async def mapped_failure() -> OpenAIError:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(413, json={"message": "Request too large"}, headers={"x-amzn-ErrorType": header})
            )
        ) as http_client:
            handler = AsyncHTTPHandler()
            handler.client = http_client
            with pytest.raises(OpenAIError) as caught:
                await litellm.acompletion(
                    model=model,
                    messages=[{"role": "user", "content": "hello"}],
                    api_base="https://example.invalid",
                    client=handler,
                    aws_access_key_id="test-access-key",
                    aws_secret_access_key="test-not-a-secret",
                    aws_region_name="us-east-1",
                    num_retries=0,
                    max_retries=0,
                )
            return caught.value

    mapped = asyncio.run(mapped_failure())
    assert isinstance(mapped, BadRequestError)
    assert mapped.status_code == 413
    result = classify_provider_failure(mapped)
    assert result is not None
    assert (result.kind, result.retryable, result.request_rejection.reason_code if result.request_rejection else None) == (
        "bad_request",
        False,
        "request_too_large",
    )


def test_openrouter_route_keeps_http_413_as_api_error() -> None:
    """LiteLLM 1.102.2 and OpenAI 2.36.0 map OpenRouter 413 to APIError 413."""

    async def mapped_failure() -> OpenAIError:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(413, json={"error": {"message": "Request too large"}}))
        ) as http_client:
            client = AsyncHTTPHandler()
            client.client = http_client
            with pytest.raises(OpenAIError) as caught:
                await litellm.acompletion(
                    model="openrouter/anthropic/claude-3.5-sonnet",
                    messages=[{"role": "user", "content": "hello"}],
                    api_base="https://example.invalid/api/v1",
                    api_key="test-key",
                    client=client,
                    num_retries=0,
                    max_retries=0,
                )
            return caught.value

    mapped = asyncio.run(mapped_failure())
    assert isinstance(mapped, LiteLLMAPIError)
    assert mapped.status_code == 413
    result = classify_provider_failure(mapped)
    assert result is not None
    assert (result.kind, result.retryable, result.request_rejection.reason_code if result.request_rejection else None) == (
        "bad_request",
        False,
        "request_too_large",
    )


def _rate_limit(*, headers: dict[str, str] | None = None, rate_limit_type: RateLimitType | None = None) -> LiteLLMRateLimitError:
    response = httpx.Response(429, request=httpx.Request("POST", "https://example.invalid"), headers=headers)
    return LiteLLMRateLimitError(message=_SECRET, model=_MODEL, llm_provider=_PROVIDER, response=response, rate_limit_type=rate_limit_type)


def test_real_rate_limit_and_non_429_status_have_distinct_dispositions() -> None:
    limited = classify_provider_failure(_rate_limit())
    assert limited is not None
    assert (limited.audit_status, limited.kind, limited.retryable) == (ComposerLLMCallStatus.API_ERROR, "unavailable", True)
    assert limited.rate_limited is True
    response = httpx.Response(409, request=httpx.Request("POST", "https://example.invalid"))
    other = classify_provider_failure(APIStatusError(_SECRET, response=response, body=None))
    assert other is not None and not other.retryable and other.kind == "unavailable"
    assert other.rate_limited is False


def test_rate_limit_budget_uses_exact_typed_evidence() -> None:
    """A 429 that arrives typed (raised directly or typed by a proxy) honours its budget marker.

    LiteLLM's mapper strips this structure; see the mapper-built cases below.
    """
    budget = classify_provider_failure(_rate_limit(rate_limit_type=RateLimitType.BUDGET))
    requests = classify_provider_failure(_rate_limit(rate_limit_type=RateLimitType.REQUESTS))
    no_type = classify_provider_failure(_rate_limit())
    assert budget is not None and not budget.retryable
    assert requests is not None and requests.retryable
    assert no_type is not None and no_type.retryable


def test_structured_insufficient_quota_code_stops_retry_without_message_matching() -> None:
    """A directly raised SDK 429 with the structured quota code is not retried."""
    response = httpx.Response(429, request=httpx.Request("POST", "https://example.invalid"))
    exhausted = classify_provider_failure(OpenAIRateLimitError(_SECRET, response=response, body={"code": "insufficient_quota"}))
    near_miss = classify_provider_failure(OpenAIRateLimitError(_SECRET, response=response, body={"code": "quota_nearly_full"}))
    assert exhausted is not None and not exhausted.retryable
    assert near_miss is not None and near_miss.retryable


@pytest.mark.parametrize(
    ("headers", "seconds"),
    [
        ({"retry-after": "7"}, 7.0),
        ({"retry-after-ms": "1500", "retry-after": "7"}, 1.5),
        ({"retry-after-ms": "bad", "retry-after": "7"}, 7.0),
        ({"retry-after": "Sun, 06 Nov 1994 08:49:37 GMT"}, 37.0),
        ({"retry-after": " 7"}, None),
        ({"retry-after": "7.0"}, None),
        ({"retry-after": "-7"}, None),
        ({"retry-after": "7, 8"}, None),
        ({"retry-after": "Sunday, 06-Nov-94 08:49:37 GMT"}, None),
        ({"retry-after": "Mon, 06 Nov 1994 08:49:37 GMT"}, None),
        ({"retry-after": "1000000"}, None),
        ({"retry-after-ms": "1000000000"}, None),
    ],
    ids=(
        "seconds",
        "ms-wins",
        "malformed-ms",
        "imf-date",
        "whitespace",
        "fraction",
        "negative",
        "multiple",
        "obsolete-date",
        "wrong-weekday",
        "long-seconds",
        "long-ms",
    ),
)
def test_rate_limit_header_boundary_accepts_only_exact_grammars(headers: dict[str, str], seconds: float | None) -> None:
    observed = observe_rate_limit(_rate_limit(headers=headers), now=datetime(1994, 11, 6, 8, 49, tzinfo=UTC))
    assert (observed.retry_after.seconds if observed.retry_after is not None else None) == seconds


def test_rate_limit_boundary_refuses_non_string_header_values() -> None:
    class NonStringHeaders(httpx.Headers):
        # httpx always yields str header values; only a foreign headers
        # object can carry anything else, so stand one in. httpx's own
        # constructor reads its headers, so attach this after construction.
        def get(self, key: str, default: Any = None) -> Any:
            return {"retry-after-ms": 1000, "retry-after": 2}.get(key, default)

    response = httpx.Response(429, request=httpx.Request("POST", "https://example.invalid"))
    response.headers = NonStringHeaders()
    observed = observe_rate_limit(OpenAIRateLimitError(_SECRET, response=response, body=None))
    assert observed.retry_after is None


def test_live_sdk_subclass_tree_has_an_explicit_or_deliberately_generic_disposition() -> None:
    arms = {cls for cls, _ in _provider_error_arms()}
    generic = _deliberately_generic()
    assert not {OpenAIError, APIError, APIStatusError} & arms
    assert LiteLLMRateLimitError in arms
    assert all(generic.values())
    pending = [OpenAIError]
    seen: set[type[BaseException]] = set()
    while pending:
        cls = pending.pop()
        if cls in seen:
            continue
        seen.add(cls)
        pending.extend(cls.__subclasses__())
    sdk_classes = {cls for cls in seen if cls.__module__ in {"openai", "litellm"} or cls.__module__.startswith(("openai.", "litellm."))}
    # Positive control: the walk and the module filter must keep the real SDK
    # classes, or an empty set would pass this test without checking anything.
    assert {LiteLLMRateLimitError, OpenAIRateLimitError, APIStatusError} <= sdk_classes
    unexplained = {f"{cls.__module__}.{cls.__name__}" for cls in sdk_classes if _provider_error_disposition(cls) is None}
    assert not unexplained, sorted(unexplained)


def test_addendum_sdk_policy_and_timeout_order() -> None:
    from litellm.exceptions import APIConnectionError as LiteLLMAPIConnectionError
    from litellm.exceptions import InternalServerError as LiteLLMInternalServerError
    from litellm.exceptions import InvalidRequestError, JSONSchemaValidationError, MidStreamFallbackError, MockException
    from litellm.exceptions import PermissionDeniedError as LiteLLMPermissionDeniedError
    from litellm.exceptions import Timeout as LiteLLMTimeout
    from openai import APIConnectionError, APITimeoutError, InternalServerError, PermissionDeniedError

    # LiteLLM reports transport failures and unreadable billed responses as a 500
    # or as APIConnectionError; without typed transport evidence neither is retried.
    for unmapped in (InternalServerError, LiteLLMInternalServerError, APIConnectionError, LiteLLMAPIConnectionError):
        assert _provider_error_disposition(unmapped) == "generic"
    for timed_out in (APITimeoutError, LiteLLMTimeout):
        assert _provider_error_disposition(timed_out) == "timeout"
    for denied in (PermissionDeniedError, LiteLLMPermissionDeniedError):
        assert _provider_error_disposition(denied) == "auth"
    assert _provider_error_disposition(LiteLLMRateLimitError) == "rate_limit"
    assert _provider_error_disposition(InvalidRequestError) == "bad_request"
    assert _provider_error_disposition(MidStreamFallbackError) == "retry"
    assert _provider_error_disposition(JSONSchemaValidationError) == "generic"
    assert _provider_error_disposition(MockException) == "generic"


def test_addendum_concrete_sdk_failures_keep_value_free_dispositions() -> None:
    from litellm.exceptions import APIConnectionError as LiteLLMAPIConnectionError
    from litellm.exceptions import APIResponseValidationError, InternalServerError, PermissionDeniedError
    from openai import NotFoundError, UnprocessableEntityError

    request = httpx.Request("POST", "https://example.invalid")
    response = httpx.Response(403, request=request)
    cases = (
        (InternalServerError(_SECRET, llm_provider=_PROVIDER, model=_MODEL), "unavailable", False),
        (LiteLLMAPIConnectionError(_SECRET, llm_provider=_PROVIDER, model=_MODEL, request=request), "unavailable", False),
        (PermissionDeniedError(_SECRET, llm_provider=_PROVIDER, model=_MODEL, response=response), "auth", False),
        (APIResponseValidationError(_SECRET, llm_provider=_PROVIDER, model=_MODEL), "unavailable", False),
        (NotFoundError(_SECRET, response=httpx.Response(404, request=request), body=None), "unavailable", False),
        (UnprocessableEntityError(_SECRET, response=httpx.Response(422, request=request), body=None), "unavailable", False),
    )
    for error, kind, retryable in cases:
        result = classify_provider_failure(error)
        assert result is not None
        assert (result.kind, result.retryable) == (kind, retryable)
        assert _SECRET not in repr(result)


def test_shared_retry_delay_respects_provider_minimum_and_call_headroom() -> None:
    assert retry_delay_seconds(attempt=1, retry_after=RetryAfter(3), deadline=100, now=20, base_delay_seconds=1) == 3
    assert retry_delay_seconds(attempt=2, retry_after=None, deadline=100, now=20, base_delay_seconds=1) == 2
    assert retry_delay_seconds(attempt=1, retry_after=RetryAfter(31), deadline=100, now=20, base_delay_seconds=1) is None
    assert retry_delay_seconds(attempt=1, retry_after=RetryAfter(6), deadline=30, now=20, base_delay_seconds=1) is None
    assert retry_delay_seconds(attempt=1, retry_after=None, deadline=24, now=20, base_delay_seconds=1) is None


@pytest.mark.parametrize(
    ("retry_after", "deadline", "expected"),
    [
        # A Retry-After of up to 30 s is waited for; anything longer is not retried.
        pytest.param(30.0, 100.0, 30.0, id="retry-after-at-ceiling"),
        pytest.param(30.001, 100.0, None, id="retry-after-over-ceiling"),
        # A Retry-After shorter than the 1 s step waits the step.
        pytest.param(0.5, 100.0, 1.0, id="retry-after-below-step"),
        # A retry starts only when its delay plus 5 s still fits before the deadline.
        pytest.param(3.0, 8.0, 3.0, id="delay-plus-margin-exactly-fits"),
        pytest.param(3.0, 7.999, None, id="inside-call-margin"),
    ],
)
def test_shared_retry_delay_boundaries(retry_after: float, deadline: float, expected: float | None) -> None:
    assert retry_delay_seconds(attempt=1, retry_after=RetryAfter(retry_after), deadline=deadline, now=0.0, base_delay_seconds=1) == expected


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
