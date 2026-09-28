"""Owned, value-free classification of SDK failures at Composer dispatches."""

from __future__ import annotations

import asyncio
from dataclasses import asdict

import pytest
from litellm.exceptions import APIError as LiteLLMAPIError
from litellm.exceptions import AuthenticationError, BadGatewayError, BadRequestError, ServiceUnavailableError
from litellm.exceptions import Timeout as LiteLLMTimeout
from openai import OpenAIError

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.provider_errors import classify_provider_failure

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
