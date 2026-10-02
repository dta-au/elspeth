"""AWS Bedrock LLM provider implemented through LiteLLM."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from threading import Lock
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Self

from pydantic import Field, field_validator, model_validator

from elspeth.contracts import CallType
from elspeth.contracts.audit_protocols import PluginAuditWriter
from elspeth.contracts.call_governance import LLMCallGovernance
from elspeth.contracts.chat_parts import ChatMessage
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.enums import RunMode
from elspeth.contracts.value_source import ValueSource
from elspeth.plugins.infrastructure.clients.llm import (
    AuditedLLMClient,
    ContentPolicyError,
    ContextLengthError,
    LLMClientError,
    NetworkError,
    RateLimitError,
    ServerError,
    build_llm_call_request,
)
from elspeth.plugins.llm.config_validation import (
    BEDROCK_ACCESS_KEY_ID_MAX_LENGTH,
    BEDROCK_API_KEY_MAX_LENGTH,
    BEDROCK_CREDENTIAL_MIN_LENGTH,
    BEDROCK_MODEL_MAX_LENGTH,
    BEDROCK_MODEL_MIN_LENGTH,
    BEDROCK_REGION_MAX_LENGTH,
    BEDROCK_REGION_MIN_LENGTH,
    BEDROCK_REGION_PATTERN,
    BEDROCK_SECRET_ACCESS_KEY_MAX_LENGTH,
    BEDROCK_SESSION_TOKEN_MAX_LENGTH,
    BEDROCK_VALUE_SOURCES,
    validate_bedrock_credential_fields,
    validate_bedrock_model,
)
from elspeth.plugins.transforms.llm.base import LLMConfig
from elspeth.plugins.transforms.llm.provider import (
    FinishReason,
    LLMAuditParent,
    LLMQueryResult,
    UnrecognizedFinishReason,
    finish_reason_from_raw_response,
)

if TYPE_CHECKING:
    from elspeth.contracts.call_mode import CallModeSession
    from elspeth.plugins.infrastructure.clients.base import TelemetryEmitCallback

__all__ = ["BedrockConfig", "BedrockCredentials", "BedrockLLMProvider"]

_STATIC_BEDROCK_ERROR = "Bedrock LLM request failed"


@dataclass(frozen=True, slots=True)
class BedrockCredentials:
    """Resolved Bedrock credentials; no explicit credentials selects the AWS default chain.

    Holds secret VALUES, so every field is excluded from ``repr``. Instances
    are handed to :class:`_LiteLLMSDKAdapter` only — below the audited
    client — so a credential never enters the recorded request.
    """

    api_key: str | None = field(default=None, repr=False)
    aws_access_key_id: str | None = field(default=None, repr=False)
    aws_secret_access_key: str | None = field(default=None, repr=False)
    aws_session_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        validate_bedrock_credential_fields(
            api_key=self.api_key,
            aws_access_key_id=self.aws_access_key_id,
            aws_secret_access_key=self.aws_secret_access_key,
            aws_session_token=self.aws_session_token,
        )

    def litellm_kwargs(self) -> dict[str, str]:
        """Return the LiteLLM completion kwargs carrying these credentials."""
        candidates = (
            # LiteLLM sends a Bedrock ``api_key`` as ``Authorization: Bearer``
            # (the Amazon Bedrock API key) instead of SigV4-signing the call.
            ("api_key", self.api_key),
            ("aws_access_key_id", self.aws_access_key_id),
            ("aws_secret_access_key", self.aws_secret_access_key),
            ("aws_session_token", self.aws_session_token),
        )
        kwargs = {name: value for name, value in candidates if value is not None}
        if self.aws_access_key_id is not None and self.aws_session_token is None:
            # LiteLLM fills None from AWS_SESSION_TOKEN even with explicit keys.
            # An empty token blocks that fallback and is omitted by SigV4 signing.
            kwargs["aws_session_token"] = ""
        return kwargs


class BedrockConfig(LLMConfig):
    """LiteLLM Bedrock configuration; the AWS default chain unless a credential is wired."""

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
    api_key: str | None = Field(
        default=None,
        min_length=BEDROCK_CREDENTIAL_MIN_LENGTH,
        max_length=BEDROCK_API_KEY_MAX_LENGTH,
        repr=False,
        description="Optional resolved Amazon Bedrock API key (bearer token); mutually exclusive with the static AWS credential pair.",
    )
    aws_access_key_id: str | None = Field(
        default=None,
        min_length=BEDROCK_CREDENTIAL_MIN_LENGTH,
        max_length=BEDROCK_ACCESS_KEY_ID_MAX_LENGTH,
        repr=False,
        description="Optional resolved AWS access-key identifier; required together with aws_secret_access_key.",
    )
    aws_secret_access_key: str | None = Field(
        default=None,
        min_length=BEDROCK_CREDENTIAL_MIN_LENGTH,
        max_length=BEDROCK_SECRET_ACCESS_KEY_MAX_LENGTH,
        repr=False,
        description="Optional resolved AWS secret access key; required together with aws_access_key_id.",
    )
    aws_session_token: str | None = Field(
        default=None,
        min_length=BEDROCK_CREDENTIAL_MIN_LENGTH,
        max_length=BEDROCK_SESSION_TOKEN_MAX_LENGTH,
        repr=False,
        description="Optional resolved AWS session token for temporary static credentials.",
    )
    tracing: dict[str, Any] | None = Field(default=None, description="Tier 2 tracing (langfuse only)")

    @field_validator("model")
    @classmethod
    def _require_bedrock_prefix(cls, value: str) -> str:
        return validate_bedrock_model(value)

    @model_validator(mode="after")
    def _validate_credentials(self) -> Self:
        validate_bedrock_credential_fields(
            api_key=self.api_key,
            aws_access_key_id=self.aws_access_key_id,
            aws_secret_access_key=self.aws_secret_access_key,
            aws_session_token=self.aws_session_token,
        )
        return self

    def credentials(self) -> BedrockCredentials:
        """Return the explicit credentials this config carries (possibly none)."""
        return BedrockCredentials(
            api_key=self.api_key,
            aws_access_key_id=self.aws_access_key_id,
            aws_secret_access_key=self.aws_secret_access_key,
            aws_session_token=self.aws_session_token,
        )


class _LiteLLMSDKAdapter:
    """Expose ``litellm.completion`` through the SDK-shaped audited client API."""

    def __init__(self, *, region_name: str | None, credentials: BedrockCredentials) -> None:
        self._region_name = region_name
        self._credentials = credentials
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs: Any) -> Any:
        import litellm

        if self._region_name is not None and "aws_region_name" not in kwargs:
            # Precedence is explicit: a caller's own aws_region_name wins, and
            # the configured region fills in only when the call names none.
            kwargs["aws_region_name"] = self._region_name
        # Configured credentials are authoritative: they are injected here,
        # below the audited client, so they never enter the recorded request,
        # and no caller-supplied kwarg may substitute a different identity.
        kwargs.update(self._credentials.litellm_kwargs())
        # The engine owns retries, so each attempt crosses admission and audit.
        kwargs["num_retries"] = 0
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
        credentials: BedrockCredentials,
        recorder: PluginAuditWriter,
        run_id: str,
        telemetry_emit: TelemetryEmitCallback,
        limiter: Any = None,
        approved_prompt_artifact_hash: str | None = None,
        llm_call_governance: LLMCallGovernance | None = None,
        pricing_model: str | None = None,
        call_mode_session: CallModeSession | None = None,
    ) -> None:
        self._region_name = region_name
        self._credentials = credentials
        self._recorder = recorder
        self._run_id = run_id
        self._telemetry_emit = telemetry_emit
        self._limiter = limiter
        self._approved_prompt_artifact_hash = approved_prompt_artifact_hash
        self._llm_call_governance = llm_call_governance
        self._pricing_model = pricing_model
        self._call_mode_session = call_mode_session
        self._llm_clients: dict[str, AuditedLLMClient] = {}
        self._llm_clients_lock = Lock()
        self._underlying_client: _LiteLLMSDKAdapter | None = None
        self._underlying_client_lock = Lock()

    def execute_query(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        temperature: float | None,
        max_tokens: int | None,
        audit_parent: LLMAuditParent,
        response_format: dict[str, Any] | None = None,
    ) -> LLMQueryResult:
        """Execute one Bedrock request through the authoritative audit client."""
        cache_key = audit_parent.cache_key
        if self._call_mode_session is not None and self._call_mode_session.mode is RunMode.VERIFY:
            request = build_llm_call_request(
                model=model,
                messages=messages,
                temperature=temperature,
                provider="bedrock",
                max_tokens=max_tokens,
                response_format=response_format,
            )
            self._call_mode_session.preflight_verify_request(
                call_type=CallType.LLM,
                request_data=request.to_dict(),
                current_state_id=audit_parent.state_id,
                current_operation_id=audit_parent.operation_id,
            )
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
                    approved_prompt_artifact_hash=self._approved_prompt_artifact_hash,
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

        finish_reason = finish_reason_from_raw_response(response.raw_response)

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

    def runtime_preflight(self, *, operation_id: str, model: str, coordination_token: CoordinationToken) -> None:
        """Run a minimal audited Bedrock call under an operation parent."""
        smoke_messages = [ChatMessage(role="user", content="This is a pre-flight smoke test. Please reply with ok.")]
        if self._call_mode_session is not None and self._call_mode_session.mode is RunMode.VERIFY:
            request = build_llm_call_request(
                model=model,
                messages=smoke_messages,
                temperature=0.0,
                provider="bedrock",
                max_tokens=32,
            )
            self._call_mode_session.preflight_verify_request(
                call_type=CallType.LLM,
                request_data=request.to_dict(),
                current_state_id=None,
                current_operation_id=operation_id,
            )
        client = AuditedLLMClient(
            execution=self._recorder,
            state_id=None,
            operation_id=operation_id,
            coordination_token=coordination_token,
            run_id=self._run_id,
            telemetry_emit=self._telemetry_emit,
            underlying_client=None
            if self._call_mode_session is not None and self._call_mode_session.mode is RunMode.REPLAY
            else self._get_underlying_client(),
            provider="bedrock",
            pricing_model=self._pricing_model,
            limiter=self._limiter,
            llm_call_governance=self._llm_call_governance,
            call_mode_session=self._call_mode_session,
        )
        redacted_error: LLMClientError | None = None
        try:
            try:
                client.chat_completion(
                    model=model,
                    messages=smoke_messages,
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
                self._underlying_client = _LiteLLMSDKAdapter(region_name=self._region_name, credentials=self._credentials)
            return self._underlying_client

    def _get_llm_client(self, audit_parent: LLMAuditParent) -> AuditedLLMClient:
        cache_key = audit_parent.cache_key
        with self._llm_clients_lock:
            if cache_key not in self._llm_clients:
                self._llm_clients[cache_key] = AuditedLLMClient(
                    execution=self._recorder,
                    run_id=self._run_id,
                    telemetry_emit=self._telemetry_emit,
                    underlying_client=None
                    if self._call_mode_session is not None and self._call_mode_session.mode is RunMode.REPLAY
                    else self._get_underlying_client(),
                    provider="bedrock",
                    pricing_model=self._pricing_model,
                    limiter=self._limiter,
                    llm_call_governance=self._llm_call_governance,
                    call_mode_session=self._call_mode_session,
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
