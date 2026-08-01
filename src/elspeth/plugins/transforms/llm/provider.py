"""LLM provider protocol and response DTOs.

The LLMProvider protocol defines the narrow interface between LLMTransform
(shared logic) and provider-specific transport (Azure SDK, OpenRouter HTTP).

Providers are responsible for:
1. Client lifecycle (creation, caching per audit parent, cleanup)
2. LLM API calls (transport-specific)
3. Tier 3 boundary validation (response parsing, NaN rejection)
4. Error classification (raising typed exceptions)
5. Audit trail recording (via their Audited*Client)
6. Finish reason normalization (provider-specific → FinishReason enum)

The transform above the provider never sees raw SDK/HTTP responses.
raw_response is NOT on LLMQueryResult — providers record audit data
via their Audited*Client (D2 from architecture remediation).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, TypedDict, runtime_checkable

from elspeth.contracts import Call, CallStatus, CallType
from elspeth.contracts.audit_protocols import CallRecorder
from elspeth.contracts.call_data import CallPayload
from elspeth.contracts.token_usage import TokenUsage


class _AuditClientKwargs(TypedDict):
    state_id: str | None
    token_id: str | None
    operation_id: str | None


@dataclass(frozen=True, slots=True)
class LLMAuditParent:
    """Validated audit parent for an LLM provider call."""

    state_id: str | None = None
    token_id: str | None = None
    operation_id: str | None = None

    def __post_init__(self) -> None:
        row_parent = self.state_id is not None or self.token_id is not None
        operation_parent = self.operation_id is not None
        if row_parent == operation_parent:
            raise ValueError("LLMAuditParent requires exactly one row or operation parent")
        if row_parent and (not self.state_id or not self.state_id.strip() or not self.token_id or not self.token_id.strip()):
            raise ValueError("row audit parent requires non-empty state_id and token_id")
        if operation_parent and (not self.operation_id or not self.operation_id.strip()):
            raise ValueError("operation audit parent requires a non-empty operation_id")

    @classmethod
    def for_row(cls, *, state_id: str, token_id: str) -> LLMAuditParent:
        return cls(state_id=state_id, token_id=token_id)

    @classmethod
    def for_operation(cls, *, operation_id: str) -> LLMAuditParent:
        return cls(operation_id=operation_id)

    @property
    def cache_key(self) -> str:
        if self.operation_id is not None:
            return f"operation:{self.operation_id}"
        if self.state_id is None:
            raise RuntimeError("validated row parent lost state_id")
        return f"state:{self.state_id}"

    def client_kwargs(self) -> _AuditClientKwargs:
        return {
            "state_id": self.state_id,
            "token_id": self.token_id,
            "operation_id": self.operation_id,
        }

    def allocate_call_index(self, recorder: CallRecorder) -> int:
        """Allocate the next semantic-call index under this parent."""
        if self.operation_id is not None:
            return recorder.allocate_operation_call_index(self.operation_id)
        if self.state_id is None:
            raise RuntimeError("validated row parent lost state_id")
        return recorder.allocate_call_index(self.state_id)

    def record_call(
        self,
        recorder: CallRecorder,
        *,
        call_index: int,
        call_type: CallType,
        status: CallStatus,
        request_data: CallPayload,
        response_data: CallPayload | None = None,
        error: CallPayload | None = None,
        latency_ms: float | None = None,
        resolved_prompt_template_hash: str | None = None,
    ) -> Call:
        """Record a semantic call under this validated parent."""
        if self.operation_id is not None:
            return recorder.record_operation_call(
                operation_id=self.operation_id,
                call_index=call_index,
                call_type=call_type,
                status=status,
                request_data=request_data,
                response_data=response_data,
                error=error,
                latency_ms=latency_ms,
                resolved_prompt_template_hash=resolved_prompt_template_hash,
            )
        if self.state_id is None:
            raise RuntimeError("validated row parent lost state_id")
        return recorder.record_call(
            state_id=self.state_id,
            call_index=call_index,
            call_type=call_type,
            status=status,
            request_data=request_data,
            response_data=response_data,
            error=error,
            latency_ms=latency_ms,
            resolved_prompt_template_hash=resolved_prompt_template_hash,
        )


class FinishReason(StrEnum):
    """Validated finish reasons from LLM providers."""

    STOP = "stop"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    TOOL_CALLS = "tool_calls"


@dataclass(frozen=True, slots=True)
class UnrecognizedFinishReason:
    """Sentinel for finish reasons not in our FinishReason enum.

    Preserves the raw value for audit trail recording, unlike None which
    conflates "absent" (no finish_reason in response) with "unrecognized"
    (provider sent a value we don't know about).
    """

    raw: str

    def __post_init__(self) -> None:
        if not isinstance(self.raw, str):
            raise TypeError(f"raw must be a string, got {type(self.raw).__name__}: {self.raw!r}")


#: Type alias for parsed finish reasons.  ``None`` means the provider did
#: not include a finish_reason field at all (absent).
ParsedFinishReason = FinishReason | UnrecognizedFinishReason | None


@dataclass(frozen=True, slots=True)
class FinishReasonFailure:
    """Provider-neutral failure verdict for a non-success finish reason."""

    reason: str
    finish_reason: str
    error_message: str


_FINISH_REASON_FAILURES: dict[FinishReason, FinishReasonFailure] = {
    FinishReason.LENGTH: FinishReasonFailure(
        reason="response_truncated",
        finish_reason=FinishReason.LENGTH.value,
        error_message="Response truncated (finish_reason=length)",
    ),
    FinishReason.CONTENT_FILTER: FinishReasonFailure(
        reason="content_filtered",
        finish_reason=FinishReason.CONTENT_FILTER.value,
        error_message="Response blocked by provider content filter",
    ),
}


def classify_finish_reason_failure(finish_reason: ParsedFinishReason) -> FinishReasonFailure | None:
    """Return a bounded failure verdict, or ``None`` for accepted completion forms.

    Explicit ``stop`` and an absent finish reason are accepted. Every other
    value fails closed. The returned description is independent of transform
    row/query context so source and transform plugins can share the verdict.
    """
    if finish_reason is None or finish_reason == FinishReason.STOP:
        return None
    if isinstance(finish_reason, FinishReason):
        known_failure = _FINISH_REASON_FAILURES.get(finish_reason)
        if known_failure is not None:
            return known_failure
        raw_value = finish_reason.value
    else:
        raw_value = finish_reason.raw
    return FinishReasonFailure(
        reason="unexpected_finish_reason",
        finish_reason=raw_value,
        error_message=f"Unexpected finish reason: {raw_value}",
    )


def parse_finish_reason(raw: str | None) -> ParsedFinishReason:
    """Parse raw finish_reason string into validated enum.

    Returns:
        FinishReason: If the raw value is a known enum member.
        UnrecognizedFinishReason: If the raw value is not recognized.
            Preserves the raw string for audit recording.
        None: If raw is None (no finish_reason in response).

    Providers should normalize their known finish reasons BEFORE calling
    this function (e.g. Anthropic "end_turn" → "stop").

    IMPORTANT: Callers MUST NOT call this with raw=None to represent
    "no finish_reason in response" — pass None directly instead. This
    function should only be called when a non-None raw value exists.
    """
    if raw is None:
        return None
    try:
        return FinishReason(raw)
    except ValueError:
        return UnrecognizedFinishReason(raw)


@dataclass(frozen=True, slots=True)
class LLMQueryResult:
    """Normalized, validated result from any LLM provider.

    All Tier 3 validation has already happened inside the provider.
    Content is guaranteed non-null, non-empty, non-whitespace-only string.
    Usage is normalized via TokenUsage.known/unknown.

    NOTE: raw_response is NOT included here. Providers own audit recording
    via their Audited*Client (chat_completion/post methods record internally
    via their Landscape recorder) — the raw SDK/HTTP response stays within
    the provider boundary (D2 principle).
    """

    content: str
    usage: TokenUsage
    model: str
    finish_reason: ParsedFinishReason = None

    def __post_init__(self) -> None:
        if not self.content or not self.content.strip():
            raise ValueError("LLMQueryResult.content must be non-empty (whitespace-only rejected)")
        if not self.model or not self.model.strip():
            raise ValueError("LLMQueryResult.model must be non-empty")
        if not isinstance(self.usage, TokenUsage):
            raise TypeError(f"LLMQueryResult.usage must be a TokenUsage instance, got {type(self.usage).__name__}")
        if self.finish_reason is not None and not isinstance(self.finish_reason, (FinishReason, UnrecognizedFinishReason)):
            raise TypeError(
                f"LLMQueryResult.finish_reason must be FinishReason, UnrecognizedFinishReason, or None, "
                f"got {type(self.finish_reason).__name__}: {self.finish_reason!r}"
            )


@runtime_checkable
class LLMProvider(Protocol):
    """What LLMTransform needs from a provider. Narrow interface.

    Providers raise typed exceptions from elspeth.plugins.infrastructure.clients.llm:
    - RateLimitError: 429 / rate limit (retryable)
    - NetworkError: connection failures (retryable)
    - ServerError: 5xx errors (retryable)
    - ContentPolicyError: content filtering (not retryable)
    - ContextLengthError: context too long (not retryable)
    - LLMClientError: other failures (not retryable)

    Note: LLMClientError (exception in plugins/clients/llm.py) is NOT the
    same as LLMCallError (frozen dataclass in contracts/call_data.py for
    audit recording). Providers RAISE LLMClientError; they RECORD LLMCallError.
    """

    def execute_query(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        temperature: float,
        max_tokens: int | None,
        audit_parent: LLMAuditParent,
        response_format: dict[str, Any] | None = None,
    ) -> LLMQueryResult: ...

    def runtime_preflight(self, *, operation_id: str, model: str) -> None:
        """Validate provider/model reachability before row processing."""
        ...

    def close(self) -> None: ...
