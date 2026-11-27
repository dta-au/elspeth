"""Value-free classification of provider SDK failures at Composer call boundaries."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from typing import TYPE_CHECKING, Literal, NamedTuple

import httpx
from openai import RateLimitError as OpenAIRateLimitError

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.composer_progress import ProviderSizeReason
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.contracts.provider_rejection import RequestRejectionReason, SafeRequestRejection
from elspeth.contracts.trust_boundary import observation_boundary

if TYPE_CHECKING:
    from litellm.exceptions import BadRequestError as LiteLLMBadRequestError
    from litellm.exceptions import RateLimitError as LiteLLMRateLimitError
    from litellm.llms.anthropic.common_utils import AnthropicError
    from litellm.llms.custom_httpx.http_handler import MaskedHTTPStatusError
    from litellm.llms.openai.common_utils import OpenAIError as LiteLLMOpenAIError
    from openai import APIStatusError
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
    retry_after: RetryAfter | None = None
    # A provider 429. Callers whose retries are immediate must not re-send it.
    rate_limited: bool = False


@dataclass(frozen=True, slots=True)
class RetryAfter:
    seconds: float


@dataclass(frozen=True, slots=True)
class _RateLimitObservation:
    budget_exhausted: bool
    retry_after: RetryAfter | None


_DELTA_SECONDS = re.compile(r"[0-9]{1,6}")
_MILLISECONDS = re.compile(r"[0-9]{1,9}")
_IMF_FIXDATE = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), [0-9]{2} "
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"[0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2} GMT"
)


@observation_boundary(
    tier=3,
    source="structured SDK rate-limit fields and HTTP Retry-After headers",
    source_param="exc",
    suppresses=("R1", "R5"),
    invariant="retains only exact budget evidence and a bounded full-match delay; malformed or absent headers yield no delay",
)
def observe_rate_limit(exc: OpenAIRateLimitError, *, now: datetime | None = None) -> _RateLimitObservation:
    """Project foreign 429 evidence once; provider prose is never inspected.

    The SDK classes declare ``response``, ``code`` and (on LiteLLM's subclass)
    ``rate_limit_type``, so they are read as typed fields. Their values are
    still foreign and are checked below before anything is retained.

    The budget evidence is honoured only when an error arrives typed: raised
    directly by the SDK or typed by a proxy. LiteLLM's exception mapper, which
    every ``acompletion`` failure passes through, rebuilds a provider 429
    without its body, code or rate-limit type, so a quota or credit refusal
    from that path is indistinguishable from a transient 429 and is retried
    within the same bound. The same rebuild drops the response headers on the
    OpenAI, Anthropic and OpenRouter routes, so a Retry-After is seen only
    where LiteLLM keeps them (Bedrock).
    """
    from litellm.exceptions import RateLimitError as LiteLLMRateLimitError
    from litellm.exceptions import RateLimitType

    rate_limit_type = exc.rate_limit_type if isinstance(exc, LiteLLMRateLimitError) else None
    code = exc.code
    # LiteLLM stores the enum's exact value as str; a foreign __eq__ must not
    # impersonate either representation of the structured budget marker.
    budget_exhausted = (
        rate_limit_type is RateLimitType.BUDGET
        or (type(rate_limit_type) is str and rate_limit_type == RateLimitType.BUDGET.value)
        or (type(code) is str and code == "insufficient_quota")
    )
    response = exc.response
    milliseconds = response.headers.get("retry-after-ms")
    if type(milliseconds) is str and _MILLISECONDS.fullmatch(milliseconds) is not None:
        return _RateLimitObservation(budget_exhausted, RetryAfter(int(milliseconds) / 1_000))
    seconds = response.headers.get("retry-after")
    if type(seconds) is not str:
        return _RateLimitObservation(budget_exhausted, None)
    if _DELTA_SECONDS.fullmatch(seconds) is not None:
        return _RateLimitObservation(budget_exhausted, RetryAfter(float(seconds)))
    if _IMF_FIXDATE.fullmatch(seconds) is None:
        return _RateLimitObservation(budget_exhausted, None)
    try:
        date = datetime.strptime(seconds, "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=UTC)
    except ValueError:
        return _RateLimitObservation(budget_exhausted, None)
    if date.strftime("%a") != seconds[:3]:
        return _RateLimitObservation(budget_exhausted, None)
    reference = datetime.now(UTC) if now is None else now
    return _RateLimitObservation(budget_exhausted, RetryAfter(max(0.0, (date - reference).total_seconds())))


_Disposition = Literal["auth", "bad_request", "timeout", "bad_gateway", "retry", "rate_limit", "generic"]


@lru_cache(maxsize=1)
def _provider_error_arms() -> tuple[tuple[type[BaseException], _Disposition], ...]:
    """Ordered SDK policies; specific subclasses precede their parent policy."""
    import openai
    from litellm import exceptions as le

    return (
        (le.AuthenticationError, "auth"),
        (openai.BadRequestError, "bad_request"),
        (le.Timeout, "timeout"),
        (openai.APITimeoutError, "timeout"),
        # 04c11a713: upstream_response_invalid 502 is deterministic. A class-only
        # policy cannot distinguish it from a transient gateway response.
        (le.BadGatewayError, "bad_gateway"),
        (le.MidStreamFallbackError, "retry"),
        (le.ServiceUnavailableError, "retry"),
        # LiteLLM's OpenAI and Anthropic handlers rewrap a transport failure, or
        # a billed response they could not read, as a status-500 error. A class
        # policy cannot tell that from a provider 500, so it is not retried.
        (openai.InternalServerError, "generic"),
        # LiteLLM wraps every exception it cannot map, including a failure to
        # read a response the provider already billed, as APIConnectionError.
        # The class carries no typed transport evidence, so it is not retried.
        (openai.APIConnectionError, "generic"),
        (openai.PermissionDeniedError, "auth"),
        (openai.NotFoundError, "generic"),
        (openai.UnprocessableEntityError, "generic"),
        (openai.APIResponseValidationError, "generic"),
        (le.APIError, "retry"),
        (le.RateLimitError, "rate_limit"),
        (openai.RateLimitError, "rate_limit"),
    )


@lru_cache(maxsize=1)
def _deliberately_generic() -> dict[type[BaseException], str]:
    """Known SDK classes for which the generic unavailable disposition is intentional."""
    import openai
    from litellm import exceptions as le
    from openai._exceptions import SubjectTokenProviderError
    from openai._extras._common import MissingDependencyError
    from openai._response import StreamAlreadyConsumed
    from openai.lib._old_api import APIRemovedInV1
    from openai.lib.azure import MutuallyExclusiveAuthError

    return {
        openai.OpenAIError: "abstract SDK root",
        openai.APIError: "abstract API root",
        openai.APIStatusError: "status without a specific policy",
        openai.AuthenticationError: "base SDK authentication class kept at existing generic disposition",
        openai.OAuthError: "OAuth subclass kept at existing generic disposition",
        openai.ConflictError: "conflict is not an availability signal",
        openai.ContentFilterFinishReasonError: "content refusal is not transient",
        openai.LengthFinishReasonError: "output length is not transient",
        openai.WebSocketConnectionClosedError: "stream closure has no replay policy",
        openai.WebSocketQueueFullError: "stream queue has no replay policy",
        openai._AmbiguousModuleClientUsageError: "client configuration defect",
        SubjectTokenProviderError: "identity provider configuration defect",
        MissingDependencyError: "missing optional SDK dependency is a client installation defect",
        StreamAlreadyConsumed: "stream lifecycle defect",
        APIRemovedInV1: "removed API use is a client defect",
        MutuallyExclusiveAuthError: "conflicting authentication configuration",
        le.OpenAIError: "LiteLLM root without a specific policy",
        le.MockException: "test-only simulated failure has no replay policy",
    }


def _provider_error_disposition(cls: type[BaseException]) -> _Disposition | None:
    """Resolve the same policy used by classification and the SDK tree pin."""
    if cls in _deliberately_generic():
        return "generic"
    return next((policy for arm, policy in _provider_error_arms() if issubclass(cls, arm)), None)


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
    observations conflict. litellm's ``ContextWindowExceededError`` is recorded
    as ``sdk_class``; it is not typed evidence, since on some routes litellm
    assigns that class by matching the provider's wording. The message is
    foreign text, so it is matched against fixed wordings and then discarded.
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
_REQUEST_TOO_LARGE_FAILURE = ProviderFailure(ComposerLLMCallStatus.BAD_REQUEST_ERROR, "bad_request", False, _REQUEST_TOO_LARGE)


class _ChainedStatusTypes(NamedTuple):
    """The LiteLLM and SDK exception types a relabelled provider status is read through."""

    litellm_bad_request: type[LiteLLMBadRequestError]
    litellm_rate_limit: type[LiteLLMRateLimitError]
    anthropic_wrapper: type[AnthropicError]
    masked_http_status: type[MaskedHTTPStatusError]
    openai_wrapper: type[LiteLLMOpenAIError]
    sdk_status: type[APIStatusError]


def _chained_status_types() -> _ChainedStatusTypes:
    """Import the chain types on first use; LiteLLM stays out of startup (see classify_provider_failure).

    Three of these live in LiteLLM-internal modules. The dependency contract
    test calls this loader, so a LiteLLM release that moves one fails by name.
    """
    from litellm.exceptions import BadRequestError as LiteLLMBadRequestError
    from litellm.exceptions import RateLimitError as LiteLLMRateLimitError
    from litellm.llms.anthropic.common_utils import AnthropicError
    from litellm.llms.custom_httpx.http_handler import MaskedHTTPStatusError
    from litellm.llms.openai.common_utils import OpenAIError as LiteLLMOpenAIError
    from openai import APIStatusError

    return _ChainedStatusTypes(
        litellm_bad_request=LiteLLMBadRequestError,
        litellm_rate_limit=LiteLLMRateLimitError,
        anthropic_wrapper=AnthropicError,
        masked_http_status=MaskedHTTPStatusError,
        openai_wrapper=LiteLLMOpenAIError,
        sdk_status=APIStatusError,
    )


def chained_provider_status(exc: BaseException) -> int | None:
    """The provider's own status when LiteLLM relabelled it and the typed chain agrees; otherwise ``None``.

    Only a relabelled 413 is recognised, so the result is 413 or ``None``.
    """
    chain_types = _chained_status_types()

    # LiteLLM's provider wrapper is authoritative: it holds the HTTP status
    # LiteLLM read before relabelling. The lower SDK/HTTP error must agree.
    # LiteLLM does not promise this implicit __context__ chain; the
    # MockTransport route tests pin it so an upstream change is detected.
    wrapper = exc.__context__
    if isinstance(exc, chain_types.litellm_rate_limit):
        if not isinstance(wrapper, chain_types.openai_wrapper):
            return None
        wrapper_status: object = wrapper.status_code
        if type(wrapper_status) is not int or wrapper_status != 413:
            return None
        lower = wrapper.__context__
        if not isinstance(lower, chain_types.sdk_status):
            return None
        lower_status: object = lower.status_code
        return 413 if type(lower_status) is int and lower_status == 413 else None
    if isinstance(exc, chain_types.litellm_bad_request):
        if not isinstance(wrapper, chain_types.anthropic_wrapper):
            return None
        wrapper_status = wrapper.status_code
        if type(wrapper_status) is not int or wrapper_status != 413:
            return None
        lower = wrapper.__context__
        if not isinstance(lower, chain_types.masked_http_status):
            return None
        lower_status = lower.status_code
        return 413 if type(lower_status) is int and lower_status == 413 else None
    return None


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


def provider_size_reason(exc: BaseException | None) -> ProviderSizeReason | None:
    """The typed reason a provider failure is a size rejection; ``None`` for any other failure."""
    rejection = request_size_rejection(exc)
    if rejection is None:
        return None
    if rejection.reason_code == "context_length_exceeded":
        return "context_length_exceeded"
    if rejection.reason_code == "request_too_large":
        return "request_too_large"
    raise AuditIntegrityError(f"size rejection with a reason that is not a size reason: {rejection.reason_code!r}")


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
    from openai import OpenAIError

    if not isinstance(exc, OpenAIError):
        return None
    disposition = _provider_error_disposition(type(exc))
    if disposition == "auth":
        return ProviderFailure(ComposerLLMCallStatus.AUTH_ERROR, "auth", False)
    if disposition == "bad_request":
        from openai import BadRequestError as OpenAIBadRequestError

        assert isinstance(exc, OpenAIBadRequestError)
        admitted, specific_code_seen = _admit_request_rejection(exc)
        rejection = _admit_context_length_evidence(exc, admitted, specific_code_seen=specific_code_seen)
        # A retained specific code takes precedence, as it does over context-limit
        # evidence; then context-limit evidence, the more specific; otherwise an
        # HTTP 413 says the request is too large for the provider or a proxy in
        # front of it. The SDK annotates this status as 400; litellm sets the
        # provider's own (413 for a Bedrock request-size rejection), so it is
        # read as a value.
        status: object = exc.status_code
        if (
            not specific_code_seen
            and rejection.reason_code != "context_length_exceeded"
            and (status == 413 or chained_provider_status(exc) == 413)
        ):
            rejection = _REQUEST_TOO_LARGE
        return ProviderFailure(ComposerLLMCallStatus.BAD_REQUEST_ERROR, "bad_request", False, rejection)
    if disposition == "timeout":
        return ProviderFailure(ComposerLLMCallStatus.TIMEOUT, "timeout", False)
    if disposition == "bad_gateway":
        return ProviderFailure(ComposerLLMCallStatus.API_ERROR, "unavailable", False)
    if disposition == "retry":
        from litellm.exceptions import APIError as LiteLLMAPIError

        if isinstance(exc, LiteLLMAPIError) and exc.status_code == 413:
            # Too large for the provider or a proxy in front of it. Only the
            # status is read. An unchanged resend is refused the same way: a
            # rejected request, not an outage. The installed LiteLLM keeps
            # 413 for OpenAI-compatible routes and Bedrock; Anthropic reports
            # 400 but is recognised through its typed provider chain above.
            return _REQUEST_TOO_LARGE_FAILURE
        return ProviderFailure(ComposerLLMCallStatus.API_ERROR, "unavailable", True)
    if disposition == "rate_limit":
        assert isinstance(exc, OpenAIRateLimitError)
        if chained_provider_status(exc) == 413:
            return _REQUEST_TOO_LARGE_FAILURE
        observed = observe_rate_limit(exc)
        return ProviderFailure(
            ComposerLLMCallStatus.API_ERROR,
            "unavailable",
            not observed.budget_exhausted,
            retry_after=observed.retry_after,
            rate_limited=True,
        )
    return ProviderFailure(ComposerLLMCallStatus.API_ERROR, "unavailable", False)
