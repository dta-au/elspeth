"""AWS Bedrock LLM provider implemented through LiteLLM."""

from __future__ import annotations

from collections.abc import Sequence
from threading import Lock
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import Field, field_validator

from elspeth.contracts.audit_protocols import PluginAuditWriter
from elspeth.contracts.chat_parts import ChatMessage
from elspeth.contracts.value_source import ValueSource
from elspeth.plugins.infrastructure.clients.llm import (
    AuditedLLMClient,
    ContentPolicyError,
    ContextLengthError,
    LLMClientError,
    NetworkError,
    RateLimitError,
    ServerError,
)
from elspeth.plugins.llm.config_validation import (
    BEDROCK_MODEL_MAX_LENGTH,
    BEDROCK_MODEL_MIN_LENGTH,
    BEDROCK_REGION_MAX_LENGTH,
    BEDROCK_REGION_MIN_LENGTH,
    BEDROCK_REGION_PATTERN,
    BEDROCK_VALUE_SOURCES,
    validate_bedrock_model,
)
from elspeth.plugins.transforms.llm.base import LLMConfig
from elspeth.plugins.transforms.llm.provider import (
    FinishReason,
    LLMAuditParent,
    LLMQueryResult,
    UnrecognizedFinishReason,
    parse_finish_reason,
)

if TYPE_CHECKING:
    from elspeth.plugins.infrastructure.clients.base import TelemetryEmitCallback

__all__ = ["BedrockConfig", "BedrockLLMProvider"]

_STATIC_BEDROCK_ERROR = "Bedrock LLM request failed"


class BedrockConfig(LLMConfig):
    """Keyless LiteLLM Bedrock configuration using the AWS default chain."""

    # Bedrock model availability is account/region scoped and resolved by AWS;
    # unlike OpenRouter there is no authoritative local catalog to validate.
    # The LLM plugin is explicitly registered with the value-source walker, so
    # every provider variant must still declare its participation contract.
    VALUE_SOURCES: ClassVar[tuple[ValueSource, ...]] = BEDROCK_VALUE_SOURCES

    provider: Literal["bedrock"] = Field(default="bedrock", description="LLM provider")
    model: str = Field(
        ...,
        min_length=BEDROCK_MODEL_MIN_LENGTH,
        max_length=BEDROCK_MODEL_MAX_LENGTH,
        description="LiteLLM Bedrock model id in bedrock/<id> form",
    )
    region_name: str | None = Field(
        default=None,
        min_length=BEDROCK_REGION_MIN_LENGTH,
        max_length=BEDROCK_REGION_MAX_LENGTH,
        pattern=BEDROCK_REGION_PATTERN,
        description="AWS region override; default AWS region resolution otherwise",
    )
    tracing: dict[str, Any] | None = Field(default=None, description="Tier 2 tracing (langfuse only)")

    @field_validator("model")
    @classmethod
    def _require_bedrock_prefix(cls, value: str) -> str:
        return validate_bedrock_model(value)


class _LiteLLMSDKAdapter:
    """Expose ``litellm.completion`` through the SDK-shaped audited client API."""

    def __init__(self, *, region_name: str | None) -> None:
        self._region_name = region_name
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs: Any) -> Any:
        import litellm

        if self._region_name is not None:
            kwargs.setdefault("aws_region_name", self._region_name)
        return litellm.completion(**kwargs)

    def close(self) -> None:
        """LiteLLM completion calls hold no provider client owned here."""


def _redacted_bedrock_error(error: LLMClientError) -> LLMClientError:
    """Preserve ELSPETH's typed category without provider-controlled text."""
    if isinstance(error, RateLimitError):
        return RateLimitError(_STATIC_BEDROCK_ERROR)
    if isinstance(error, ContentPolicyError):
        return ContentPolicyError(_STATIC_BEDROCK_ERROR)
    if isinstance(error, ContextLengthError):
        return ContextLengthError(_STATIC_BEDROCK_ERROR)
    if isinstance(error, ServerError):
        return ServerError(_STATIC_BEDROCK_ERROR)
    if isinstance(error, NetworkError):
        return NetworkError(_STATIC_BEDROCK_ERROR)
    return LLMClientError(_STATIC_BEDROCK_ERROR, retryable=error.retryable)


class BedrockLLMProvider:
    """LiteLLM Bedrock provider with audited calls and bounded error egress."""

    def __init__(
        self,
        *,
        region_name: str | None,
        recorder: PluginAuditWriter,
        run_id: str,
        telemetry_emit: TelemetryEmitCallback,
        limiter: Any = None,
        resolved_prompt_template_hash: str | None = None,
    ) -> None:
        self._region_name = region_name
        self._recorder = recorder
        self._run_id = run_id
        self._telemetry_emit = telemetry_emit
        self._limiter = limiter
        self._resolved_prompt_template_hash = resolved_prompt_template_hash
        self._llm_clients: dict[str, AuditedLLMClient] = {}
        self._llm_clients_lock = Lock()
        self._underlying_client: _LiteLLMSDKAdapter | None = None
        self._underlying_client_lock = Lock()

    def execute_query(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        temperature: float,
        max_tokens: int | None,
        audit_parent: LLMAuditParent,
        response_format: dict[str, Any] | None = None,
    ) -> LLMQueryResult:
        """Execute one Bedrock request through the authoritative audit client."""
        cache_key = audit_parent.cache_key
        redacted_error: LLMClientError | None = None
        response = None
        try:
            client = self._get_llm_client(audit_parent)
            try:
                response = client.chat_completion(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    resolved_prompt_template_hash=self._resolved_prompt_template_hash,
                )
            except LLMClientError as error:
                redacted_error = _redacted_bedrock_error(error)
        finally:
            with self._llm_clients_lock:
                self._llm_clients.pop(cache_key, None)

        if redacted_error is not None:
            raise redacted_error from None
        if response is None:
            raise RuntimeError("Bedrock response absent without a typed client error")

        finish_reason = None
        if response.raw_response is not None:
            choices = response.raw_response.get("choices")
            if choices:
                raw_finish_reason = choices[0].get("finish_reason")
                if raw_finish_reason is not None:
                    finish_reason = parse_finish_reason(str(raw_finish_reason))

        if not response.content or not response.content.strip():
            if finish_reason == FinishReason.TOOL_CALLS:
                raise LLMClientError("Bedrock returned tool_calls response (not supported by ELSPETH)", retryable=False)
            safe_finish_reason = "unrecognized" if isinstance(finish_reason, UnrecognizedFinishReason) else finish_reason
            raise ContentPolicyError(f"Bedrock LLM returned empty content (finish_reason={safe_finish_reason})")

        return LLMQueryResult(
            content=response.content,
            usage=response.usage,
            model=response.model,
            finish_reason=finish_reason,
        )

    def runtime_preflight(self, *, operation_id: str, model: str) -> None:
        """Run a minimal audited Bedrock call under an operation parent."""
        client = AuditedLLMClient(
            execution=self._recorder,
            state_id=None,
            operation_id=operation_id,
            run_id=self._run_id,
            telemetry_emit=self._telemetry_emit,
            underlying_client=self._get_underlying_client(),
            provider="bedrock",
            limiter=self._limiter,
        )
        redacted_error: LLMClientError | None = None
        try:
            try:
                client.chat_completion(
                    model=model,
                    messages=[ChatMessage(role="user", content="This is a pre-flight smoke test. Please reply with ok.")],
                    temperature=0.0,
                    max_tokens=32,
                )
            except LLMClientError as error:
                redacted_error = _redacted_bedrock_error(error)
        finally:
            client.close()
        if redacted_error is not None:
            raise redacted_error from None

    def _get_underlying_client(self) -> _LiteLLMSDKAdapter:
        with self._underlying_client_lock:
            if self._underlying_client is None:
                self._underlying_client = _LiteLLMSDKAdapter(region_name=self._region_name)
            return self._underlying_client

    def _get_llm_client(self, audit_parent: LLMAuditParent) -> AuditedLLMClient:
        cache_key = audit_parent.cache_key
        with self._llm_clients_lock:
            if cache_key not in self._llm_clients:
                self._llm_clients[cache_key] = AuditedLLMClient(
                    execution=self._recorder,
                    run_id=self._run_id,
                    telemetry_emit=self._telemetry_emit,
                    underlying_client=self._get_underlying_client(),
                    provider="bedrock",
                    limiter=self._limiter,
                    **audit_parent.client_kwargs(),
                )
            return self._llm_clients[cache_key]

    def close(self) -> None:
        """Release cached audited clients and the stateless adapter."""
        with self._llm_clients_lock:
            self._llm_clients.clear()
        with self._underlying_client_lock:
            if self._underlying_client is not None:
                self._underlying_client.close()
            self._underlying_client = None
