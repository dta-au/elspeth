"""Value-free classification of provider SDK failures at Composer call boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import httpx

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.contracts.provider_rejection import RequestRejectionReason, SafeRequestRejection

if TYPE_CHECKING:
    from openai import BadRequestError as OpenAIBadRequestError


@dataclass(frozen=True, slots=True)
class ProviderRequestFailure:
    """Internal cause identity and its separately serializable safe evidence.

    Serialize ``details.to_wire()`` only. The original exception deliberately
    remains process-local and must never enter generic dataclass serialization.
    """

    original: BaseException = field(repr=False, compare=False)
    details: SafeRequestRejection


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    """The public disposition of a provider failure, without provider values."""

    audit_status: ComposerLLMCallStatus
    kind: Literal["auth", "bad_request", "unavailable", "timeout"]
    retryable: bool
    request_rejection: SafeRequestRejection | None = None


@dataclass(frozen=True, slots=True)
class _ReasonObservation:
    code: RequestRejectionReason | None
    disposition: Literal["retained", "absent", "unrecognized", "unavailable"]


def _reason_code(value: object) -> _ReasonObservation:
    if value is None:
        return _ReasonObservation(None, "absent")
    if type(value) is str:
        if value == "invalid_request":
            return _ReasonObservation("invalid_request", "retained")
        if value == "context_length_exceeded":
            return _ReasonObservation("context_length_exceeded", "retained")
        if value == "unsupported_parameter":
            return _ReasonObservation("unsupported_parameter", "retained")
    return _ReasonObservation(None, "unrecognized")


def _body_reason(value: object, *, nested: bool) -> _ReasonObservation:
    """Read only the code slot of the SDK's documented error object."""
    if value is None:
        return _ReasonObservation(None, "absent")
    if not isinstance(value, dict):
        return _ReasonObservation(None, "unavailable")
    if nested:
        if "error" not in value:
            return _ReasonObservation(None, "unavailable")
        value = value["error"]
        if not isinstance(value, dict):
            return _ReasonObservation(None, "unavailable")
    return _reason_code(value["code"] if "code" in value else None)


def _merge_reason_observations(first: _ReasonObservation, second: _ReasonObservation) -> _ReasonObservation:
    """Conflicting explicit observations never choose an actionable reason."""
    if first.disposition in {"absent", "unavailable"}:
        return second if second.disposition != "absent" else first
    if second.disposition in {"absent", "unavailable"}:
        return first
    if first != second:
        return _ReasonObservation(None, "unrecognized")
    return first


def _admit_request_rejection(exc: OpenAIBadRequestError) -> SafeRequestRejection:
    """Admit only closed codes from nominal SDK fields or buffered HTTP JSON.

    No message parsing, headers, parameter names, stream reads or network I/O.
    This projector discards foreign values after checking their exact safe code.
    An unread, malformed or oversized response cannot supply a reason.
    """
    sdk_reason = _merge_reason_observations(_body_reason(exc.body, nested=False), _reason_code(exc.code))
    response_reason = _ReasonObservation(None, "unavailable")
    if isinstance(exc.response, httpx.Response):
        try:
            content = exc.response.content
        except httpx.ResponseNotRead:
            content = None
        if content is not None and len(content) <= 65_536:
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            if text is not None:
                parsed, error = parse_json_strict(text)
                if error is None:
                    response_reason = _body_reason(parsed, nested=True)
    combined = _merge_reason_observations(sdk_reason, response_reason)
    source: Literal["sdk_body", "http_response", "unavailable"] = "unavailable"
    if combined.disposition != "unavailable":
        if sdk_reason.disposition not in {"absent", "unavailable"} and combined == sdk_reason:
            source = "sdk_body"
        elif combined == response_reason:
            source = "http_response"
        elif combined == sdk_reason:
            source = "sdk_body"
    return SafeRequestRejection(reason_code=combined.code, reason_disposition=combined.disposition, reason_source=source)


def classify_provider_failure(exc: BaseException) -> ProviderFailure | None:
    """Classify only SDK failures; leave lifecycle and first-party errors alone."""
    if isinstance(exc, (asyncio.CancelledError, TimeoutError)):
        return None
    # LiteLLM initializes its model cost map on import. Keep that side effect
    # out of Composer startup; this function is reached only after a failure
    # at an actual provider dispatch boundary.
    from openai import APITimeoutError, OpenAIError
    from openai import BadRequestError as OpenAIBadRequestError

    if not isinstance(exc, OpenAIError):
        return None
    from litellm.exceptions import APIError as LiteLLMAPIError
    from litellm.exceptions import AuthenticationError, BadGatewayError, ServiceUnavailableError

    if isinstance(exc, AuthenticationError):
        return ProviderFailure(ComposerLLMCallStatus.AUTH_ERROR, "auth", False)
    if isinstance(exc, OpenAIBadRequestError):
        return ProviderFailure(
            ComposerLLMCallStatus.BAD_REQUEST_ERROR,
            "bad_request",
            False,
            _admit_request_rejection(exc),
        )
    if isinstance(exc, APITimeoutError):
        return ProviderFailure(ComposerLLMCallStatus.TIMEOUT, "timeout", False)
    if isinstance(exc, BadGatewayError):
        return ProviderFailure(ComposerLLMCallStatus.API_ERROR, "unavailable", False)
    if isinstance(exc, ServiceUnavailableError):
        return ProviderFailure(ComposerLLMCallStatus.API_ERROR, "unavailable", True)
    if isinstance(exc, LiteLLMAPIError):
        return ProviderFailure(ComposerLLMCallStatus.API_ERROR, "unavailable", True)
    return ProviderFailure(ComposerLLMCallStatus.API_ERROR, "unavailable", False)
