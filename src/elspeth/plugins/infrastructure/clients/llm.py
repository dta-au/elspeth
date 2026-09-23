"""Audited LLM client with automatic call recording."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, cast, get_args

import structlog

import elspeth.contracts.errors as contract_errors
from elspeth.contracts import CallStatus, CallType, RunMode
from elspeth.contracts.call_data import CallPayload, LLMCallError, LLMCallRequest, LLMCallResponse, LLMErrorCategory, RawCallPayload
from elspeth.contracts.call_governance import LLMCallGovernance
from elspeth.contracts.chat_parts import ChatMessage, audit_messages, wire_messages
from elspeth.contracts.composer_llm_audit import ComposerLLMProviderCostSource
from elspeth.contracts.coordination import CoordinationToken, WorkerMembershipToken
from elspeth.contracts.errors import PluginRetryableError
from elspeth.contracts.events import ExternalCallCompleted
from elspeth.contracts.freeze import deep_freeze
from elspeth.contracts.scheduler import TokenWorkItem
from elspeth.contracts.token_usage import UNKNOWN_TOKEN_USAGE, TokenUsage
from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.core.canonical import stable_hash
from elspeth.core.llm_pricing import provider_cost_from_captured_usage
from elspeth.plugins.infrastructure.clients.base import AuditedClientBase, TelemetryEmitCallback

if TYPE_CHECKING:
    from elspeth.contracts import Call
    from elspeth.contracts.audit_protocols import CallRecorder
    from elspeth.contracts.call_mode import CallModeSession, ReplayCallEvidence
    from elspeth.contracts.contexts import LimiterProtocol

logger = structlog.get_logger(__name__)

_AUDIT_SAFE_PROVIDER_ERROR = "LLM provider request failed"


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Response from an LLM call.

    Frozen: LLM responses are immutable evidence — the content, model,
    usage, and raw response must not be modified after construction.

    Attributes:
        content: The generated text response
        model: The actual model that processed the request
        usage: Token counts (prompt_tokens, completion_tokens)
        latency_ms: Round-trip time in milliseconds
        raw_response: Full response object for debugging (optional)
    """

    content: str
    model: str
    usage: TokenUsage = field(default_factory=TokenUsage.unknown)
    latency_ms: float = 0.0
    raw_response: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.latency_ms < 0 or not math.isfinite(self.latency_ms):
            raise ValueError(f"LLMResponse.latency_ms must be non-negative and finite, got {self.latency_ms}")
        if self.raw_response is not None and not isinstance(self.raw_response, MappingProxyType):
            object.__setattr__(self, "raw_response", deep_freeze(self.raw_response))

    @property
    def total_tokens(self) -> int | None:
        """Total tokens used (prompt + completion), or None if unknown."""
        return self.usage.total_tokens


class LLMClientError(PluginRetryableError):
    """Error from LLM client.

    Base exception for all LLM client errors. Includes retryable
    flag to indicate if the operation might succeed on retry.

    Attributes:
        retryable: Whether the error is likely transient and retryable
    """

    category: LLMErrorCategory = "client"

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message, retryable=retryable)


class RateLimitError(LLMClientError):
    """Rate limit exceeded - retryable.

    Raised when the LLM provider returns a rate limit error (HTTP 429).
    Always marked as retryable since rate limits are transient.
    """

    category: LLMErrorCategory = "rate_limit"

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class NetworkError(LLMClientError):
    """Network/connection error - retryable.

    Raised for transient network issues like timeouts, connection refused,
    DNS failures, etc. These errors are typically transient and should be retried.
    """

    category: LLMErrorCategory = "network"

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class ServerError(LLMClientError):
    """Server error (5xx) - retryable.

    Raised for server-side errors that are typically transient:
    - 500 Internal Server Error
    - 502 Bad Gateway
    - 503 Service Unavailable
    - 504 Gateway Timeout
    - 529 Model Overloaded (Azure-specific)

    These errors indicate temporary infrastructure issues that may
    resolve on retry.
    """

    category: LLMErrorCategory = "server"

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class ContentPolicyError(LLMClientError):
    """Content policy violation - not retryable.

    Raised when the LLM provider rejects the request due to content
    policy violations. Retrying with the same prompt will always fail.
    """

    category: LLMErrorCategory = "content_policy"

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class ContextLengthError(LLMClientError):
    """Context length exceeded - not retryable.

    Raised when the prompt exceeds the model's maximum context length.
    Retrying with the same prompt will always fail.
    """

    category: LLMErrorCategory = "context_length"

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


def public_llm_error_category(error: LLMClientError) -> LLMErrorCategory:
    """Name the public exception behavior retained in an LLM audit error."""
    return error.category


_RATE_LIMIT_PATTERNS = (
    re.compile(r"\brate[\s_-]*limit(?:ed|ing)?\b"),
    re.compile(r"\brate(?:\s+has\s+been)?\s+exceeded\b"),
    re.compile(r"\btoo many requests\b"),
    re.compile(r"\bthrottl(?:e|ed|ing)\b"),
)
_SERVER_ERROR_CODE_PATTERN = re.compile(r"\b(?:500|502|503|504|529)\b")
_CLIENT_ERROR_CODE_PATTERN = re.compile(r"\b(?:400|401|403|404|422)\b")
_NETWORK_ERROR_PATTERNS = (
    "timeout",
    "timed out",
    "connection refused",
    "connection reset",
    "connection aborted",
    "network unreachable",
    "host unreachable",
    "dns",
    "getaddrinfo failed",
)
_CONTENT_POLICY_PATTERNS = (
    "content_policy_violation",
    "content_filter",
    "content policy",
    "safety system",
)
CONTEXT_LENGTH_PATTERNS = (
    "context_length_exceeded",  # OpenAI / Azure OpenAI canonical code
    "context length",  # OpenAI / Azure verbose wording ("maximum context length is X")
    "context window",  # LiteLLM Bedrock wrapper ("Context Window Error")
    "maximum context",  # Catches "maximum context length", "exceeds maximum context"
    "prompt is too long",  # Anthropic via OpenRouter ("prompt is too long: N tokens > M maximum")
)


@trust_boundary(
    tier=3,
    source="provider/SDK exception objects raised by the LLM client library (OpenAI, Azure, LiteLLM, httpx)",
    source_param="exception",
    suppresses=("R1",),
    invariant=(
        "classifies by message text and by an optional instance-level status_code; "
        "returns 'unknown' rather than raising when the exception carries neither"
    ),
    non_raising=True,
)
def _classify_llm_error(exception: Exception) -> LLMErrorCategory:
    """Classify an LLM error into a canonical category.

    Tier 3 boundary: the exception is constructed by the provider SDK, so its
    attribute set is not ours to assume. ``status_code`` is read from the
    instance dict only — a class-level or property ``status_code`` on an SDK
    exception type is not a per-response fact.
    """
    error_str = str(exception).lower()

    if any(pattern in error_str for pattern in _CONTENT_POLICY_PATTERNS):
        return "content_policy"
    if any(pattern in error_str for pattern in CONTEXT_LENGTH_PATTERNS):
        return "context_length"

    status_code = exception.__dict__.get("status_code")
    if type(status_code) is int:
        if status_code == 429:
            return "rate_limit"
        if status_code in (500, 502, 503, 504, 529):
            return "server"
        if status_code in (400, 401, 403, 404, 422):
            return "client"

    # Text fallback is for exceptions without a recognized HTTP status. A
    # request reference in an authentication error must not turn it retryable.
    if re.search(r"\b429\b", error_str) or any(pattern.search(error_str) for pattern in _RATE_LIMIT_PATTERNS):
        return "rate_limit"
    if _SERVER_ERROR_CODE_PATTERN.search(error_str):
        return "server"
    if any(pattern in error_str for pattern in _NETWORK_ERROR_PATTERNS):
        return "network"
    if _CLIENT_ERROR_CODE_PATTERN.search(error_str):
        return "client"
    return "unknown"


@trust_boundary(
    tier=3,
    source="provider token-usage payloads returned by the LLM client library (OpenAI, Azure, LiteLLM) — an SDK object, a mapping, or a partial aggregate-only payload",
    source_param="usage",
    suppresses=("R1", "R5"),
    invariant=(
        "normalizes mapping- and attribute-shaped usage payloads through TokenUsage.from_dict; "
        "an absent payload becomes TokenUsage.unknown() and absent or non-int counters become "
        "explicit None — never a fabricated zero, never a raise"
    ),
    non_raising=True,
)
def _extract_usage_from_provider_response(usage: Any) -> TokenUsage:
    """Normalize provider usage objects at the Tier 3 boundary.

    Providers may return usage as an SDK object with attributes, a mapping, or
    a partial aggregate-only payload. Reconstruct through ``TokenUsage.from_dict``
    so missing and non-int fields become explicit ``None`` rather than raising.
    """
    if usage is None:
        return TokenUsage.unknown()

    if isinstance(usage, Mapping):
        return TokenUsage.from_dict(usage)
    else:
        usage_data = {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
            "cached_prompt_tokens": getattr(usage, "cached_prompt_tokens", None),
            "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", None),
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
            "reasoning_tokens": getattr(usage, "reasoning_tokens", None),
        }
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        completion_details = getattr(usage, "completion_tokens_details", None)
        usage_data["prompt_tokens_details"] = (
            prompt_details if isinstance(prompt_details, Mapping) else {"cached_tokens": getattr(prompt_details, "cached_tokens", None)}
        )
        usage_data["completion_tokens_details"] = (
            completion_details
            if isinstance(completion_details, Mapping)
            else {"reasoning_tokens": getattr(completion_details, "reasoning_tokens", None)}
        )

    return TokenUsage.from_dict(usage_data)


def _validate_provider_response_model(model: Any) -> str:
    """Require provider model metadata to be a non-empty string."""
    if not isinstance(model, str):
        raise ValueError(f"LLM response model is {type(model).__name__}, expected non-empty str")
    if not model.strip():
        raise ValueError("LLM response model must be non-empty")
    return model


def build_llm_call_request(
    *,
    model: str,
    messages: Sequence[ChatMessage],
    temperature: float | None,
    provider: str,
    max_tokens: int | None,
    max_tokens_param: str = "max_tokens",
    **kwargs: Any,
) -> LLMCallRequest:
    """Build the exact semantic request used by admission and audit."""
    return LLMCallRequest(
        model=model,
        messages=audit_messages(messages),
        temperature=temperature,
        provider=provider,
        max_tokens=max_tokens,
        max_tokens_param=max_tokens_param,
        extra_kwargs=kwargs,
    )


class AuditedLLMClient(AuditedClientBase):
    """LLM client that automatically records all calls to audit trail.

    Wraps an OpenAI-compatible client to ensure every LLM call is
    recorded to the Landscape audit trail. Supports:
    - Automatic request/response recording
    - Latency measurement
    - Error recording with retry classification
    - Token usage tracking
    - Telemetry emission after successful audit recording
    - Rate limiting (when limiter provided)

    Example:
        client = AuditedLLMClient(
            execution=execution_repo,
            state_id=state_id,
            run_id=run_id,
            telemetry_emit=telemetry_emit,
            underlying_client=openai.OpenAI(api_key="..."),
            provider="openai",
            limiter=registry.get_limiter("openai"),
        )

        response = client.chat_completion(
            model="gpt-4",
            messages=[ChatMessage(role="user", content="Hello")],
        )
        print(response.content)
    """

    def __init__(
        self,
        execution: CallRecorder,
        state_id: str | None,
        run_id: str,
        telemetry_emit: TelemetryEmitCallback,
        underlying_client: Any,  # openai.OpenAI or openai.AzureOpenAI
        *,
        provider: str = "openai",
        pricing_model: str | None = None,
        limiter: LimiterProtocol | None = None,
        token_id: str | None = None,
        operation_id: str | None = None,
        coordination_token: CoordinationToken | None = None,
        member_token: WorkerMembershipToken | None = None,
        work_item: TokenWorkItem | None = None,
        llm_call_governance: LLMCallGovernance | None = None,
        max_tokens_param: Literal["max_tokens", "max_completion_tokens"] = "max_tokens",
        call_mode_session: CallModeSession | None = None,
    ) -> None:
        """Initialize audited LLM client.

        Args:
            execution: CallRecorder for audit trail storage
            state_id: Node state ID to associate calls with
            run_id: Pipeline run ID for telemetry correlation
            telemetry_emit: Callback to emit telemetry events
            underlying_client: OpenAI-compatible client instance
            provider: Provider name for audit trail (default: "openai")
            limiter: Optional rate limiter for throttling requests
            token_id: Optional token identity for telemetry correlation
            operation_id: Optional operation parent for runtime preflight calls
            max_tokens_param: Wire name the output budget is sent and recorded
                under. Reasoning deployments reject ``max_tokens`` and require
                ``max_completion_tokens``; the choice is the provider's, never
                inferred from the model name.
        """
        super().__init__(
            execution,
            state_id,
            run_id,
            telemetry_emit,
            operation_id=operation_id,
            limiter=limiter,
            token_id=token_id,
            coordination_token=coordination_token,
            member_token=member_token,
            work_item=work_item,
            llm_call_governance=llm_call_governance,
        )
        self._client = underlying_client
        self._provider = provider
        self._pricing_model = pricing_model
        self._max_tokens_param = max_tokens_param
        self._call_mode_session = call_mode_session

    def _replay_completion(
        self,
        *,
        request_dto: LLMCallRequest,
        call_index: int,
        approved_prompt_artifact_hash: str | None,
    ) -> LLMResponse:
        """Rebuild the exact audited response before any provider dispatch."""
        session = self._call_mode_session
        if session is None or session.mode is not RunMode.REPLAY:
            raise RuntimeError("LLM replay requested without a replay session")
        evidence: ReplayCallEvidence = session.replay_call(
            call_type=CallType.LLM,
            request_data=request_dto.to_dict(),
            current_state_id=self._state_id,
            current_operation_id=self._operation_id,
            current_call_index=call_index,
        )
        if evidence.status is CallStatus.ERROR:
            error = evidence.error_data
            if error is None:
                raise ValueError("Recorded LLM error has no error payload")
            if evidence.response_data is not None:
                raise ValueError("Recorded LLM response-processing error cannot be reproduced exactly")
            if set(error) != {"type", "message", "retryable", "pricing_model", "provider_cost", "provider_cost_source", "category"}:
                raise ValueError("Recorded LLM error lacks a complete public classification")
            retryable = error["retryable"]
            error_type = error["type"]
            error_message = error["message"]
            pricing_model = error["pricing_model"]
            if type(retryable) is not bool or type(error_type) is not str or type(error_message) is not str:
                raise ValueError("Recorded LLM error has invalid fields")
            raw_cost_source = error["provider_cost_source"]
            if raw_cost_source not in get_args(ComposerLLMProviderCostSource):
                raise ValueError("Recorded LLM error has invalid provider cost source")
            raw_category = error["category"]
            if raw_category not in get_args(LLMErrorCategory) or raw_category == "response_processing":
                raise ValueError("Recorded LLM error has no replayable public classification")
            category = cast(LLMErrorCategory, raw_category)
            if retryable is not (category in {"rate_limit", "server", "network"}):
                raise ValueError("Recorded LLM error classification disagrees with retryability")
            error_dto = LLMCallError(
                type=error_type,
                message=error_message,
                retryable=retryable,
                pricing_model=pricing_model,
                provider_cost=error["provider_cost"],
                provider_cost_source=cast(ComposerLLMProviderCostSource, raw_cost_source),
                category=category,
            )
            self._record_call(
                call_index=call_index,
                call_type=CallType.LLM,
                status=CallStatus.ERROR,
                request_data=request_dto,
                error=error_dto,
                latency_ms=evidence.latency_ms,
                approved_prompt_artifact_hash=approved_prompt_artifact_hash,
                source_call_id=evidence.source_call_id,
            )
            self._emit_telemetry_after_audit(
                call_status=CallStatus.ERROR,
                latency_ms=0.0 if evidence.latency_ms is None else evidence.latency_ms,
                request_data=request_dto.to_dict(),
                request_payload=request_dto,
                response_data=None,
                response_payload=None,
                token_usage=None,
            )
            if category == "rate_limit":
                raise RateLimitError(error_dto.message)
            if category == "content_policy":
                raise ContentPolicyError(error_dto.message)
            if category == "context_length":
                raise ContextLengthError(error_dto.message)
            if category == "server":
                raise ServerError(error_dto.message)
            if category == "network":
                raise NetworkError(error_dto.message)
            raise LLMClientError(error_dto.message, retryable=False)
        if evidence.status is not CallStatus.SUCCESS or evidence.error_data is not None:
            raise ValueError("Recorded LLM call has an unsupported status or error payload")
        response_data = evidence.response_data
        if response_data is None:
            raise ValueError("Recorded LLM success has no response payload")
        required_fields = {
            "content",
            "model",
            "usage",
            "raw_response",
            "pricing_model",
            "provider_cost",
            "provider_cost_source",
        }
        if set(response_data) != required_fields:
            raise ValueError("Recorded LLM response has incomplete fields")
        usage_data = response_data["usage"]
        usage = TokenUsage.from_dict(usage_data)
        if usage.to_dict() != usage_data:
            raise ValueError("Recorded LLM usage is malformed")
        response_dto = LLMCallResponse(
            content=response_data["content"],
            model=response_data["model"],
            usage=usage,
            raw_response=response_data["raw_response"],
            pricing_model=response_data["pricing_model"],
            provider_cost=response_data["provider_cost"],
            provider_cost_source=response_data["provider_cost_source"],
        )
        response = LLMResponse(
            content=response_dto.content,
            model=response_dto.model,
            usage=usage,
            latency_ms=0.0 if evidence.latency_ms is None else evidence.latency_ms,
            raw_response=response_dto.raw_response,
        )
        self._record_call(
            call_index=call_index,
            call_type=CallType.LLM,
            status=CallStatus.SUCCESS,
            request_data=request_dto,
            response_data=response_dto,
            latency_ms=evidence.latency_ms,
            approved_prompt_artifact_hash=approved_prompt_artifact_hash,
            token_usage=usage,
            source_call_id=evidence.source_call_id,
        )
        self._emit_telemetry_after_audit(
            call_status=CallStatus.SUCCESS,
            latency_ms=response.latency_ms,
            request_data=request_dto.to_dict(),
            request_payload=request_dto,
            response_data=response_dto.to_dict(),
            response_payload=response_dto,
            token_usage=usage if usage.has_data else None,
        )
        return response

    def _record_call(
        self,
        *,
        call_index: int,
        call_type: CallType,
        status: CallStatus,
        request_data: CallPayload,
        response_data: CallPayload | None = None,
        error: CallPayload | None = None,
        latency_ms: float | None = None,
        approved_prompt_artifact_hash: str | None = None,
        token_usage: TokenUsage = UNKNOWN_TOKEN_USAGE,
        llm_call_attempt: str | None = None,
        source_call_id: str | None = None,
    ) -> Call:
        call = super()._record_call(
            call_index=call_index,
            call_type=call_type,
            status=status,
            request_data=request_data,
            response_data=response_data,
            error=error,
            latency_ms=latency_ms,
            approved_prompt_artifact_hash=approved_prompt_artifact_hash,
            token_usage=token_usage,
            llm_call_attempt=llm_call_attempt,
            source_call_id=source_call_id,
        )
        session = self._call_mode_session
        if session is not None and session.mode is RunMode.VERIFY:
            session.verify_call(
                call_type=call_type,
                request_data=request_data.to_dict(),
                current_state_id=self._state_id,
                current_operation_id=self._operation_id,
                current_call_index=call_index,
                current_call_id=call.call_id,
                live_status=status,
                live_response_data=None if response_data is None else response_data.to_dict(),
                live_error_data=None if error is None else error.to_dict(),
            )
        return call

    def _emit_telemetry_after_audit(
        self,
        *,
        call_status: CallStatus,
        latency_ms: float,
        request_data: Mapping[str, Any],
        request_payload: CallPayload,
        response_data: Mapping[str, Any] | None,
        response_payload: CallPayload | None,
        token_usage: TokenUsage | None,
    ) -> None:
        """Emit LLM telemetry after audit recording, crashing on programmer bugs.

        Telemetry is best-effort operational visibility emitted *after* the
        authoritative Landscape record already succeeded (telemetry primacy
        order). This is the single named best-effort path for the LLM client:
        Tier-1 audit-integrity violations and programming errors re-raise (they
        are bugs in our code and must crash). Other callback failures are
        acknowledged through the last-resort logger without changing the
        already-audited call outcome.
        The telemetry callback is a bare ``Callable`` supplied by the caller, so
        the residual catch cannot be narrowed to a typed telemetry error. Event
        construction — hashing included — happens BEFORE the try: a failure
        there is a first-party bug and crashes without ever entering the
        best-effort containment below, which wraps only the callback delivery.
        """
        event = ExternalCallCompleted(
            timestamp=datetime.now(UTC),
            run_id=self._run_id,
            call_type=CallType.LLM,
            provider=self._provider,
            status=call_status,
            latency_ms=latency_ms,
            state_id=self._telemetry_state_id(),
            operation_id=self._telemetry_operation_id(),
            token_id=self._telemetry_token_id(),
            request_hash=stable_hash(request_data),
            response_hash=stable_hash(response_data) if response_data is not None else None,
            request_payload=request_payload,
            response_payload=response_payload,
            token_usage=token_usage,
        )
        try:
            self._telemetry_emit(event)
        except contract_errors.TIER_1_ERRORS:
            raise  # System bugs and audit integrity violations must crash
        except (TypeError, AttributeError, KeyError, NameError):
            raise  # Programming errors must crash
        except Exception as tel_err:
            # Telemetry failure must not corrupt the audited call flow — Landscape
            # already holds the authoritative record; telemetry is best-effort.
            logger.warning(
                "telemetry_emit_failed",
                error_type=type(tel_err).__name__,
                run_id=self._run_id,
                state_id=self._telemetry_state_id(),
                operation_id=self._telemetry_operation_id(),
                call_type="llm",
            )

    def chat_completion(
        self,
        model: str,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = 0.0,
        max_tokens: int | None = None,
        approved_prompt_artifact_hash: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Make chat completion call with automatic audit recording.

        Args:
            model: Model identifier (e.g., "gpt-4", "gpt-3.5-turbo")
            messages: Ordered chat messages. The SDK sees the wire projection
                (base64 image data URIs); the audit trail sees the bytes-free
                projection.
            temperature: Sampling temperature (default: 0.0 for determinism).
                None omits the parameter so the provider default applies —
                reasoning deployments reject any explicit value.
            max_tokens: Maximum tokens to generate (optional), sent under
                this client's ``max_tokens_param`` wire name
            approved_prompt_artifact_hash: Phase 5b Task 9 cross-DB anchor.
                When the LLM transform is downstream of a resolved
                interpretation event, the runtime reads the SHA-256 from
                ``options.approved_prompt_artifact_hash`` on the node config
                and forwards it here. Persisted to
                ``calls.approved_prompt_artifact_hash`` on every call this
                method records (SUCCESS or ERROR), making the cross-DB
                hash join discoverable from any LLM-call audit row.
                ``None`` for non-interpretation LLM transforms.
            **kwargs: Additional arguments passed to the underlying client

        Returns:
            LLMResponse with content, model, usage, and latency

        Raises:
            RateLimitError: If rate limited (retryable)
            LLMClientError: For other errors (check retryable flag)
        """
        call_index = self._next_call_index()

        # Build request DTO - frozen dataclass ensures construction-time type safety;
        # to_dict() conditionally omits temperature and max_tokens when None (hash-stable).
        # DTO stays alive for typed telemetry payload; dict form used for Landscape hashing.
        request_dto = build_llm_call_request(
            model=model,
            messages=messages,
            temperature=temperature,
            provider=self._provider,
            max_tokens=max_tokens,
            max_tokens_param=self._max_tokens_param,
            **kwargs,
        )
        request_data = request_dto.to_dict()

        if self._call_mode_session is not None and self._call_mode_session.mode is RunMode.REPLAY:
            return self._replay_completion(
                request_dto=request_dto,
                call_index=call_index,
                approved_prompt_artifact_hash=approved_prompt_artifact_hash,
            )

        if self._call_mode_session is not None and self._call_mode_session.mode is RunMode.VERIFY:
            self._call_mode_session.admit_verify_call(
                call_type=CallType.LLM,
                request_data=request_data,
                current_state_id=self._state_id,
                current_operation_id=self._operation_id,
                current_call_index=call_index,
            )

        # A rate limiter is part of live dispatch and must not run during replay.
        self._acquire_rate_limit()

        # Build SDK call kwargs - omit temperature and max_tokens when None to
        # avoid serializing as JSON null (which can trigger provider validation errors)
        sdk_kwargs: dict[str, Any] = {
            "model": model,
            "messages": wire_messages(messages),  # wire form to the SDK only
            **kwargs,
        }
        if temperature is not None:
            sdk_kwargs["temperature"] = temperature
        if max_tokens is not None:
            sdk_kwargs[self._max_tokens_param] = max_tokens

        llm_call_attempt = self._before_llm_call()
        start = time.perf_counter()
        usage = TokenUsage.unknown()

        try:
            response = self._client.chat.completions.create(**sdk_kwargs)
        except Exception as e:
            latency_ms = (time.perf_counter() - start) * 1000
            error_type = type(e).__name__
            error_class = _classify_llm_error(e)

            # Classify error for retry decision
            is_retryable = error_class in {"rate_limit", "server", "network"}

            self._record_call(
                llm_call_attempt=llm_call_attempt,
                call_index=call_index,
                call_type=CallType.LLM,
                status=CallStatus.ERROR,
                request_data=request_dto,
                error=LLMCallError(
                    type=error_type,
                    pricing_model=self._pricing_model or model,
                    message=_AUDIT_SAFE_PROVIDER_ERROR,
                    retryable=is_retryable,
                    category=error_class,
                ),
                latency_ms=latency_ms,
                approved_prompt_artifact_hash=approved_prompt_artifact_hash,
                token_usage=usage,
            )

            # Telemetry emitted AFTER successful Landscape recording (even for call errors)
            self._emit_telemetry_after_audit(
                call_status=CallStatus.ERROR,
                latency_ms=latency_ms,
                request_data=request_data,
                request_payload=request_dto,
                response_data=None,  # No response on error
                response_payload=None,  # No response on error
                token_usage=None,
            )

            # Raise specific exception type based on error classification.
            # Message is the audit-safe constant, not str(e): callers persist
            # caught exception text into transform_errors.error_details_json,
            # so raw SDK error text (endpoints, ARNs, presigned URLs) must not
            # leave this boundary. The original exception stays reachable via
            # __cause__ for in-process diagnostics (elspeth-5d17bcff15).
            if error_class == "rate_limit":
                raise RateLimitError(_AUDIT_SAFE_PROVIDER_ERROR) from e
            elif error_class == "content_policy":
                raise ContentPolicyError(_AUDIT_SAFE_PROVIDER_ERROR) from e
            elif error_class == "context_length":
                raise ContextLengthError(_AUDIT_SAFE_PROVIDER_ERROR) from e
            elif error_class == "server":
                raise ServerError(_AUDIT_SAFE_PROVIDER_ERROR) from e
            elif error_class == "network":
                raise NetworkError(_AUDIT_SAFE_PROVIDER_ERROR) from e
            else:
                # Client error or unknown - not retryable
                raise LLMClientError(_AUDIT_SAFE_PROVIDER_ERROR, retryable=False) from e

        # Success path — OUTSIDE the SDK-call try/except so genuine internal logic
        # bugs crash instead of being misclassified as LLM errors. Tier-3 boundary
        # reads taken from the response snapshot below (usage, model_dump, content,
        # finish_reason) are each individually guarded and RECORDED on failure, so a
        # malformed provider response is audited rather than crashing (B4.1).
        latency_ms = (time.perf_counter() - start) * 1000

        # Capture the provider response once, then validate/normalize the Tier 3
        # fields from that snapshot. This keeps malformed responses on the
        # audited path instead of letting them leak into success handling.
        # Both the usage read and model_dump() are Tier-3 attribute accesses: an
        # OpenAI-compatible provider whose response omits .usage would raise
        # AttributeError here, so they share the guard. usage defaults to
        # unknown() so the error handler can still run if the usage read is what
        # failed (the LLM call happened — it must be recorded, not vanish).
        pricing_model = self._pricing_model or model
        provider_cost: float | None = None
        provider_cost_source: ComposerLLMProviderCostSource = "not_available"
        try:
            provider_usage = response.usage
            usage = _extract_usage_from_provider_response(provider_usage)
            provider_cost, provider_cost_source = provider_cost_from_captured_usage(response, provider_usage, pricing_model=pricing_model)
            raw_response = response.model_dump()
        except (TypeError, ValueError, RecursionError, AttributeError) as dump_exc:
            # The LLM call happened — record it before re-raising so the
            # audit trail reflects the consumed tokens even though we can't
            # fully read/serialize the response.
            self._record_call(
                llm_call_attempt=llm_call_attempt,
                call_index=call_index,
                call_type=CallType.LLM,
                status=CallStatus.ERROR,
                request_data=request_dto,
                error=LLMCallError(
                    type="ResponseProcessingError",
                    pricing_model=pricing_model,
                    provider_cost=provider_cost,
                    provider_cost_source=provider_cost_source,
                    message=f"Failed to read LLM response: {dump_exc}",
                    retryable=False,
                    category="response_processing",
                ),
                latency_ms=latency_ms,
                approved_prompt_artifact_hash=approved_prompt_artifact_hash,
                token_usage=usage,
            )
            # Telemetry emitted AFTER successful Landscape recording — without
            # this, serialization failures undercount in dashboards relative to
            # the SDK-error/null-content branches (elspeth-a960d22540). No
            # response payload: model_dump() failed, so there is nothing to hash.
            self._emit_telemetry_after_audit(
                call_status=CallStatus.ERROR,
                latency_ms=latency_ms,
                request_data=request_data,
                request_payload=request_dto,
                response_data=None,
                response_payload=None,
                token_usage=usage if usage.has_data else None,
            )
            raise LLMClientError(
                f"Failed to serialize LLM response: {dump_exc}",
                retryable=False,
            ) from dump_exc

        try:
            response_model = _validate_provider_response_model(response.model)
            # The SDK constructs responses without strict field validation.
            # Admit the array before indexing: malformed Azure envelopes must
            # retain their call record, just like malformed model metadata.
            if not isinstance(response.choices, list):
                raise ValueError("LLM response choices must be an array")
        except (ValueError, AttributeError) as model_exc:
            error_msg = f"{model_exc}. Provider returned malformed data at Tier 3 boundary."
            response_payload = RawCallPayload(raw_response)
            self._record_call(
                llm_call_attempt=llm_call_attempt,
                call_index=call_index,
                call_type=CallType.LLM,
                status=CallStatus.ERROR,
                request_data=request_dto,
                response_data=response_payload,
                error=LLMCallError(
                    pricing_model=pricing_model,
                    provider_cost=provider_cost,
                    provider_cost_source=provider_cost_source,
                    type="MalformedResponseError",
                    message=error_msg,
                    retryable=False,
                    category="response_processing",
                ),
                latency_ms=latency_ms,
                approved_prompt_artifact_hash=approved_prompt_artifact_hash,
                token_usage=usage,
            )

            response_data = response_payload.to_dict()
            self._emit_telemetry_after_audit(
                call_status=CallStatus.ERROR,
                latency_ms=latency_ms,
                request_data=request_data,
                request_payload=request_dto,
                response_data=response_data,
                response_payload=response_payload,
                token_usage=usage if usage.has_data else None,
            )

            raise LLMClientError(error_msg, retryable=False) from model_exc

        # Tier 3 boundary: validate LLM response structure immediately.
        if not response.choices:
            error_msg = "LLM returned empty choices array — abnormal response"
            response_dto = LLMCallResponse(
                content="",  # No content available
                model=response_model,
                pricing_model=pricing_model,
                provider_cost=provider_cost,
                provider_cost_source=provider_cost_source,
                usage=usage,
                raw_response=raw_response,
            )
            self._record_call(
                llm_call_attempt=llm_call_attempt,
                call_index=call_index,
                call_type=CallType.LLM,
                status=CallStatus.ERROR,
                request_data=request_dto,
                response_data=response_dto,
                error=LLMCallError(
                    pricing_model=pricing_model,
                    provider_cost=provider_cost,
                    provider_cost_source=provider_cost_source,
                    type="EmptyChoicesError",
                    message=error_msg,
                    retryable=False,
                    category="response_processing",
                ),
                latency_ms=latency_ms,
                approved_prompt_artifact_hash=approved_prompt_artifact_hash,
                token_usage=usage,
            )
            # Telemetry emitted AFTER successful Landscape recording — keeps
            # empty-choices failures counted alongside the other error branches
            # (elspeth-a960d22540).
            self._emit_telemetry_after_audit(
                call_status=CallStatus.ERROR,
                latency_ms=latency_ms,
                request_data=request_data,
                request_payload=request_dto,
                response_data=response_dto.to_dict(),
                response_payload=response_dto,
                token_usage=usage if usage.has_data else None,
            )
            raise LLMClientError(error_msg, retryable=False)

        # B4.1 (operator decision 2026-06-14): wrap the Tier-3 content/finish_reason
        # reads so that a malformed response (e.g. missing .message/.content due to
        # SDK drift) is RECORDED in the audit trail before re-raising. The LLM call
        # consumed a call_index and provider tokens, so it must appear in the audit
        # trail even if the response shape is unexpected. Supersedes the prior
        # direct-crash doctrine (Bug-4.6 / TestBug4_6_SuccessPathOutsideTryExcept).
        # Mirror: the malformed-model branch (lines ~512-543) which uses RawCallPayload
        # and raises LLMClientError(retryable=False) from the original exception.
        try:
            content = response.choices[0].message.content
            finish_reason = response.choices[0].finish_reason
        except AttributeError as attr_exc:
            error_msg = f"LLM response missing expected attribute at Tier-3 boundary: {attr_exc}"
            response_payload = RawCallPayload(raw_response)
            self._record_call(
                llm_call_attempt=llm_call_attempt,
                call_index=call_index,
                call_type=CallType.LLM,
                status=CallStatus.ERROR,
                request_data=request_dto,
                response_data=response_payload,
                error=LLMCallError(
                    type="MalformedResponseError",
                    pricing_model=pricing_model,
                    provider_cost=provider_cost,
                    provider_cost_source=provider_cost_source,
                    message=error_msg,
                    retryable=False,
                    category="response_processing",
                ),
                latency_ms=latency_ms,
                approved_prompt_artifact_hash=approved_prompt_artifact_hash,
                token_usage=usage,
            )
            # Telemetry emitted AFTER successful Landscape recording -- keeps
            # content-extraction failures counted alongside the other error
            # branches (elspeth-a960d22540).
            self._emit_telemetry_after_audit(
                call_status=CallStatus.ERROR,
                latency_ms=latency_ms,
                request_data=request_data,
                request_payload=request_dto,
                response_data=response_payload.to_dict(),
                response_payload=response_payload,
                token_usage=usage if usage.has_data else None,
            )
            raise LLMClientError(error_msg, retryable=False) from attr_exc

        if content is None:
            # Tool call responses have no text content — ELSPETH does not support
            # tool_calls, so this is an error (not a fabrication opportunity).
            # Record the call as ERROR with raw response preserved, then raise.
            if finish_reason == "tool_calls":
                error_msg = "LLM returned tool_calls response (not supported by ELSPETH)"
                response_dto = LLMCallResponse(
                    content="",  # No text content available
                    model=response_model,
                    pricing_model=pricing_model,
                    provider_cost=provider_cost,
                    provider_cost_source=provider_cost_source,
                    usage=usage,
                    raw_response=raw_response,
                )
                self._record_call(
                    llm_call_attempt=llm_call_attempt,
                    call_index=call_index,
                    call_type=CallType.LLM,
                    status=CallStatus.ERROR,
                    request_data=request_dto,
                    response_data=response_dto,
                    error=LLMCallError(
                        pricing_model=pricing_model,
                        provider_cost=provider_cost,
                        provider_cost_source=provider_cost_source,
                        type="UnsupportedResponseError",
                        message=error_msg,
                        retryable=False,
                        category="response_processing",
                    ),
                    latency_ms=latency_ms,
                    approved_prompt_artifact_hash=approved_prompt_artifact_hash,
                    token_usage=usage,
                )
                # Telemetry emitted AFTER successful Landscape recording — keeps
                # unsupported tool_calls failures counted alongside the other
                # error branches (elspeth-a960d22540).
                self._emit_telemetry_after_audit(
                    call_status=CallStatus.ERROR,
                    latency_ms=latency_ms,
                    request_data=request_data,
                    request_payload=request_dto,
                    response_data=response_dto.to_dict(),
                    response_payload=response_dto,
                    token_usage=usage if usage.has_data else None,
                )
                raise LLMClientError(error_msg, retryable=False)

            # Record the call BEFORE raising — the LLM call happened and must
            # appear in the audit trail even though the response is unusable.
            # Without this, content-filtered calls vanish from the audit trail
            # and create unexplained call-index gaps.
            error_msg = "LLM returned null content (likely content-filtered by provider)"
            response_dto = LLMCallResponse(
                content="",  # Null content normalized for DTO
                model=response_model,
                pricing_model=pricing_model,
                provider_cost=provider_cost,
                provider_cost_source=provider_cost_source,
                usage=usage,
                raw_response=raw_response,
            )
            self._record_call(
                llm_call_attempt=llm_call_attempt,
                call_index=call_index,
                call_type=CallType.LLM,
                status=CallStatus.ERROR,
                request_data=request_dto,
                response_data=response_dto,
                error=LLMCallError(
                    pricing_model=pricing_model,
                    provider_cost=provider_cost,
                    provider_cost_source=provider_cost_source,
                    type="ContentPolicyError",
                    message=error_msg,
                    retryable=False,
                    category="content_policy",
                ),
                latency_ms=latency_ms,
                approved_prompt_artifact_hash=approved_prompt_artifact_hash,
                token_usage=usage,
            )

            # Telemetry emitted AFTER successful Landscape recording (even for null-content errors)
            # Unlike SDK errors, we have response data here — the HTTP call succeeded
            response_data = response_dto.to_dict()
            self._emit_telemetry_after_audit(
                call_status=CallStatus.ERROR,
                latency_ms=latency_ms,
                request_data=request_data,
                request_payload=request_dto,
                response_data=response_data,
                response_payload=response_dto,
                token_usage=usage if usage.has_data else None,
            )

            raise ContentPolicyError(error_msg)

        # Tier 3 boundary: validate content is actually str.
        # Provider bugs or SDK schema drift could return non-str content
        # (e.g., list for multi-part, int, dict). Recording non-str as SUCCESS
        # would violate the response contract and crash downstream .strip() calls.
        if not isinstance(content, str):
            error_msg = (
                f"LLM response content is {type(content).__name__}, expected str. Provider returned malformed data at Tier 3 boundary."
            )
            response_payload = RawCallPayload(raw_response)
            self._record_call(
                llm_call_attempt=llm_call_attempt,
                call_index=call_index,
                call_type=CallType.LLM,
                status=CallStatus.ERROR,
                request_data=request_dto,
                response_data=response_payload,
                error=LLMCallError(
                    type="MalformedResponseError",
                    message=error_msg,
                    pricing_model=pricing_model,
                    provider_cost=provider_cost,
                    provider_cost_source=provider_cost_source,
                    retryable=False,
                    category="response_processing",
                ),
                latency_ms=latency_ms,
                approved_prompt_artifact_hash=approved_prompt_artifact_hash,
                token_usage=usage,
            )
            # Telemetry emitted AFTER successful Landscape recording — keeps
            # non-str content failures counted alongside the other error
            # branches (elspeth-a960d22540). Mirrors the malformed-model branch:
            # response payload is the raw provider response.
            self._emit_telemetry_after_audit(
                call_status=CallStatus.ERROR,
                latency_ms=latency_ms,
                request_data=request_data,
                request_payload=request_dto,
                response_data=response_payload.to_dict(),
                response_payload=response_payload,
                token_usage=usage if usage.has_data else None,
            )
            raise LLMClientError(error_msg, retryable=False)

        response_dto = LLMCallResponse(
            content=content,
            model=response_model,
            pricing_model=pricing_model,
            provider_cost=provider_cost,
            provider_cost_source=provider_cost_source,
            usage=usage,
            raw_response=raw_response,
        )
        response_data = response_dto.to_dict()

        self._record_call(
            llm_call_attempt=llm_call_attempt,
            call_index=call_index,
            call_type=CallType.LLM,
            status=CallStatus.SUCCESS,
            request_data=request_dto,
            response_data=response_dto,
            latency_ms=latency_ms,
            approved_prompt_artifact_hash=approved_prompt_artifact_hash,
            token_usage=usage,
        )

        # Telemetry emitted AFTER successful Landscape recording
        usage_snapshot = usage if usage.has_data else None
        self._emit_telemetry_after_audit(
            call_status=CallStatus.SUCCESS,
            latency_ms=latency_ms,
            request_data=request_data,
            request_payload=request_dto,
            response_data=response_data,
            response_payload=response_dto,
            token_usage=usage_snapshot,
        )

        return LLMResponse(
            content=content,
            model=response_model,
            usage=usage,
            latency_ms=latency_ms,
            raw_response=raw_response,  # Reuse captured response from audit recording
        )
