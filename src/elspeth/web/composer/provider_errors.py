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


def _admit_request_rejection(exc: OpenAIBadRequestError) -> tuple[SafeRequestRejection, bool]:
    """Admit only closed codes from nominal SDK fields or buffered HTTP JSON.

    No message parsing, headers, parameter names, stream reads or network I/O.
    This projector discards foreign values after checking their exact safe code.
    An unread, malformed or oversized response cannot supply a reason.
    """
    sdk_body_reason = _body_reason(exc.body, nested=False)
    sdk_code_reason = _reason_code(exc.code)
    sdk_reason = _merge_reason_observations(sdk_body_reason, sdk_code_reason)
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
    observations = (sdk_body_reason, sdk_code_reason, response_reason)
    specific_code_seen = any(
        observation.disposition == "retained" and observation.code != "invalid_request" for observation in observations
    )
    source: Literal["sdk_body", "http_response", "unavailable"] = "unavailable"
    if combined.disposition != "unavailable":
        if sdk_reason.disposition not in {"absent", "unavailable"} and combined == sdk_reason:
            source = "sdk_body"
        elif combined == response_reason:
            source = "http_response"
        elif combined == sdk_reason:
            source = "sdk_body"
    return SafeRequestRejection(
        reason_code=combined.code, reason_disposition=combined.disposition, reason_source=source
    ), specific_code_seen


# Wordings by which a provider or gateway says a request was over the model's context limit, lower case.
# Tried only on an HTTP 400 without a specific retained code, and only as a yes/no: no part of the
# message is kept. "prompt is too long" is Anthropic's; it is unverified against any particular gateway.
_CONTEXT_LENGTH_MESSAGE_PATTERNS: tuple[str, ...] = (
    "prompt is too long",
    "maximum context length",
    "context_length_exceeded",
    "context length exceeded",
    "exceeds the context window",
    "input is too long for requested model",
)


def _admit_context_length_evidence(
    exc: OpenAIBadRequestError, admitted: SafeRequestRejection, *, specific_code_seen: bool
) -> SafeRequestRejection:
    """Recognise a context-limit rejection the structured fields did not name.

    Any retained specific code takes precedence over class or message evidence;
    absent, unavailable, foreign and generic codes remain eligible, even when
    observations conflict. The SDK's own class is nominal evidence. The message
    is foreign text, so it is matched against fixed wordings and then discarded.
    """
    if specific_code_seen:
        return admitted
    from litellm.exceptions import ContextWindowExceededError

    source: Literal["sdk_class", "message_pattern"]
    if isinstance(exc, ContextWindowExceededError):
        source = "sdk_class"
    else:
        status_code = exc.status_code
        if type(status_code) is not int or status_code != 400:
            return admitted
        message = exc.message
        if type(message) is not str:
            return admitted
        lowered = message.lower()
        if not any(pattern in lowered for pattern in _CONTEXT_LENGTH_MESSAGE_PATTERNS):
            return admitted
        source = "message_pattern"
    return SafeRequestRejection(reason_code="context_length_exceeded", reason_disposition="retained", reason_source=source)


_REQUEST_TOO_LARGE = SafeRequestRejection(reason_code="request_too_large", reason_disposition="retained", reason_source="http_status")


def request_size_rejection(exc: BaseException | None) -> SafeRequestRejection | None:
    """The rejection, when a provider failure is a recognised refusal of the request as too large.

    Too large for the model's context limit (``context_length_exceeded``), or
    for the provider's or a proxy's request size limit (``request_too_large``).
    Either way the request must shrink; it is never resent unchanged.
    """
    if exc is None:
        return None
    failure = classify_provider_failure(exc)
    if failure is None or failure.request_rejection is None:
        return None
    if failure.request_rejection.reason_code in ("context_length_exceeded", "request_too_large"):
        return failure.request_rejection
    return None


def size_rejection_code(exc: BaseException | None) -> RequestRejectionReason | None:
    """The closed reason a size rejection is recorded by on its call; ``None`` for any other failure."""
    rejection = request_size_rejection(exc)
    return None if rejection is None else rejection.reason_code


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
        admitted, specific_code_seen = _admit_request_rejection(exc)
        rejection = _admit_context_length_evidence(exc, admitted, specific_code_seen=specific_code_seen)
        # A retained specific code takes precedence, as it does over context-limit
        # evidence; then context-limit evidence, the more specific; otherwise an
        # HTTP 413 says the request is too large for the provider or a proxy in
        # front of it. The SDK annotates this status as 400; litellm sets the
        # provider's own (413 for a Bedrock request-size rejection), so it is
        # read as a value.
        status: object = exc.status_code
        if not specific_code_seen and rejection.reason_code != "context_length_exceeded" and status == 413:
            rejection = _REQUEST_TOO_LARGE
        return ProviderFailure(ComposerLLMCallStatus.BAD_REQUEST_ERROR, "bad_request", False, rejection)
    if isinstance(exc, APITimeoutError):
        return ProviderFailure(ComposerLLMCallStatus.TIMEOUT, "timeout", False)
    if isinstance(exc, BadGatewayError):
        return ProviderFailure(ComposerLLMCallStatus.API_ERROR, "unavailable", False)
    if isinstance(exc, ServiceUnavailableError):
        return ProviderFailure(ComposerLLMCallStatus.API_ERROR, "unavailable", True)
    if isinstance(exc, LiteLLMAPIError):
        if exc.status_code == 413:
            # Too large for the provider or a proxy in front of it. Only the
            # status is read. An unchanged resend is refused the same way: a
            # rejected request, not an outage. (The installed litellm keeps
            # 413 as the status for OpenAI-compatible routes and Bedrock; its
            # Anthropic mapping reports 400, an ordinary rejected request.)
            return ProviderFailure(ComposerLLMCallStatus.BAD_REQUEST_ERROR, "bad_request", False, _REQUEST_TOO_LARGE)
        return ProviderFailure(ComposerLLMCallStatus.API_ERROR, "unavailable", True)
    return ProviderFailure(ComposerLLMCallStatus.API_ERROR, "unavailable", False)
