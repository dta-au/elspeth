"""Value-free classification of provider SDK failures at Composer call boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    """The public disposition of a provider failure, without provider values."""

    audit_status: ComposerLLMCallStatus
    kind: Literal["auth", "bad_request", "unavailable", "timeout"]
    retryable: bool


def classify_provider_failure(exc: BaseException) -> ProviderFailure | None:
    """Classify only SDK failures; leave lifecycle and first-party errors alone."""
    if isinstance(exc, (asyncio.CancelledError, TimeoutError)):
        return None
    # LiteLLM initializes its model cost map on import. Keep that side effect
    # out of Composer startup; this function is reached only after a failure
    # at an actual provider dispatch boundary.
    from openai import APITimeoutError, OpenAIError

    if not isinstance(exc, OpenAIError):
        return None
    from litellm.exceptions import APIError as LiteLLMAPIError
    from litellm.exceptions import AuthenticationError, BadGatewayError, BadRequestError, ServiceUnavailableError

    if isinstance(exc, AuthenticationError):
        return ProviderFailure(ComposerLLMCallStatus.AUTH_ERROR, "auth", False)
    if isinstance(exc, BadRequestError):
        return ProviderFailure(ComposerLLMCallStatus.BAD_REQUEST_ERROR, "bad_request", False)
    if isinstance(exc, APITimeoutError):
        return ProviderFailure(ComposerLLMCallStatus.TIMEOUT, "timeout", False)
    if isinstance(exc, BadGatewayError):
        return ProviderFailure(ComposerLLMCallStatus.API_ERROR, "unavailable", False)
    if isinstance(exc, ServiceUnavailableError):
        return ProviderFailure(ComposerLLMCallStatus.API_ERROR, "unavailable", True)
    if isinstance(exc, LiteLLMAPIError):
        return ProviderFailure(ComposerLLMCallStatus.API_ERROR, "unavailable", True)
    return ProviderFailure(ComposerLLMCallStatus.API_ERROR, "unavailable", False)
