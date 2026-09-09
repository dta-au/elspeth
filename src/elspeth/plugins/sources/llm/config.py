"""Provider-specific configuration for the single-prompt LLM source."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from jinja2.meta import find_undeclared_variables
from pydantic import Field, field_validator, model_validator

from elspeth.contracts.identifiers import validate_field_name
from elspeth.contracts.value_source import ValueSource
from elspeth.plugins.infrastructure.config_base import DataPluginConfig
from elspeth.plugins.infrastructure.templates import TemplateError, create_sandboxed_environment
from elspeth.plugins.llm.config_validation import (
    AZURE_MODEL_VALUE_SOURCES,
    BEDROCK_MODEL_MAX_LENGTH,
    BEDROCK_MODEL_MIN_LENGTH,
    BEDROCK_REGION_MAX_LENGTH,
    BEDROCK_REGION_MIN_LENGTH,
    BEDROCK_REGION_PATTERN,
    BEDROCK_VALUE_SOURCES,
    GATEWAY_MAX_TOKENS_LIMIT,
    GATEWAY_MAX_TOKENS_MIN_EXCLUSIVE,
    GATEWAY_MODEL_MAX_LENGTH,
    GATEWAY_MODEL_MIN_LENGTH,
    GATEWAY_TIMEOUT_MAX_SECONDS,
    GATEWAY_TIMEOUT_MIN_EXCLUSIVE,
    GATEWAY_VALUE_SOURCES,
    OPENROUTER_BASE_URL,
    OPENROUTER_MODEL_VALUE_SOURCES,
    derive_azure_model,
    validate_azure_endpoint,
    validate_bedrock_model,
    validate_gateway_capabilities,
    validate_gateway_contract_major,
    validate_gateway_endpoint,
    validate_gateway_single_prompt_structured_output_capability,
    validate_openrouter_base_url,
)
from elspeth.plugins.transforms.llm import LLM_GUARANTEED_SUFFIXES, build_llm_source_output_schema_config
from elspeth.plugins.transforms.llm.multi_query import OutputFieldConfig, ResponseFormat
from elspeth.plugins.transforms.llm.templates import PromptTemplate

_SOURCE_PROMPT_CONTEXT_NAMES = frozenset({"lookup"})
_SOURCE_PROMPT_GLOBAL_NAMES = frozenset(create_sandboxed_environment().globals)


class LLMSourceConfig(DataPluginConfig):
    """Common configuration for one static LLM request emitted as a source row."""

    _plugin_component_type: ClassVar[str | None] = "source"

    provider: Literal["azure", "openrouter", "bedrock", "gateway"] = Field(..., description="LLM provider")
    model: str | None = Field(default=None, description="Model identifier")
    prompt_template: str = Field(..., description="Static Jinja2 prompt template")
    system_prompt: str | None = Field(default=None, description="Optional system prompt")
    temperature: float = Field(default=0.0, ge=0.0, le=2.0, description="Sampling temperature")
    max_tokens: int | None = Field(default=None, gt=0, description="Maximum tokens in response")
    response_field: str = Field(default="llm_response", description="Field name for LLM response in output")

    # Top-level structured output — the same pair the single-prompt LLM
    # transform carries (LLMConfig.response_format / LLMConfig.output_fields),
    # with identical semantics: structured mode lowers output_fields into an
    # API-native json_schema response_format; standard mode appends the field
    # contract to the prompt and requests json_object. Extracted fields are
    # emitted unprefixed beside response_field.
    response_format: ResponseFormat = Field(
        default=ResponseFormat.STANDARD,
        description="Response format mode (standard JSON object vs. enforced json_schema)",
    )
    output_fields: list[OutputFieldConfig] | None = Field(
        default=None,
        min_length=1,
        description="Typed structured-output field definitions (None = unstructured response)",
    )

    on_validation_failure: str = Field(
        ...,
        description="Sink name for non-conformant output, or 'discard' for explicit drop",
    )

    lookup: dict[str, Any] | None = Field(default=None, description="Lookup data loaded from YAML file")
    prompt_template_source: str | None = Field(default=None, description="Prompt template file path for audit")
    lookup_source: str | None = Field(default=None, description="Lookup file path for audit")
    system_prompt_source: str | None = Field(default=None, description="System prompt file path for audit")

    @field_validator("prompt_template")
    @classmethod
    def _validate_prompt_template(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("prompt_template cannot be empty")

        try:
            PromptTemplate(value)
        except TemplateError as exc:
            raise ValueError(f"Invalid Jinja2 template: {exc}") from exc

        environment = create_sandboxed_environment()
        names = find_undeclared_variables(environment.parse(value))
        unsupported = sorted(names - _SOURCE_PROMPT_CONTEXT_NAMES - _SOURCE_PROMPT_GLOBAL_NAMES)
        if unsupported:
            raise ValueError(
                "prompt_template references unavailable name(s): "
                f"{', '.join(unsupported)}. Static source prompts may use only "
                "'lookup' and Jinja globals; no 'row' binding exists."
            )
        return value

    @field_validator("response_field")
    @classmethod
    def _validate_response_field(cls, value: str) -> str:
        return validate_field_name(value, "response_field")

    @field_validator("on_validation_failure")
    @classmethod
    def _validate_on_validation_failure(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("on_validation_failure must be a sink name or 'discard'")
        return value.strip()

    @model_validator(mode="after")
    def _validate_structured_output(self) -> LLMSourceConfig:
        """Enforce the structured-output rules (mirrors ``LLMConfig``'s single-prompt arm).

        Structured with nothing to enforce is an authoring mistake (the
        json_schema is built FROM output_fields), and a suffix colliding with
        the response/operational fields the source itself writes would be
        destroyed on emission.
        """
        if self.response_format is ResponseFormat.STRUCTURED and self.output_fields is None:
            raise ValueError("response_format='structured' requires output_fields: the enforced json_schema is built from them")
        if self.output_fields is not None:
            reserved = {f"{self.response_field}{suffix}" for suffix in LLM_GUARANTEED_SUFFIXES}
            seen: set[str] = set()
            for field in self.output_fields:
                if field.suffix in seen:
                    raise ValueError(f"Duplicate output field suffix '{field.suffix}'. Each output field must be unique")
                seen.add(field.suffix)
                if field.suffix in reserved:
                    raise ValueError(
                        f"Output field suffix '{field.suffix}' collides with the source's own "
                        f"response/operational fields {sorted(reserved)}; choose another suffix "
                        "or rename response_field"
                    )
        return self

    @model_validator(mode="after")
    def _augment_output_schema(self) -> LLMSourceConfig:
        object.__setattr__(
            self,
            "schema_config",
            build_llm_source_output_schema_config(
                self.schema_config,
                self.response_field,
                tuple(self.output_fields or ()),
            ),
        )
        return self


class AzureOpenAILLMSourceConfig(LLMSourceConfig):
    """Azure OpenAI settings for one source request."""

    provider: Literal["azure"] = Field(default="azure", description="LLM provider")
    model: str = Field(default="", description="Model identifier (defaults to deployment_name)")
    deployment_name: str = Field(..., description="Azure deployment name")
    endpoint: str = Field(..., description="Azure OpenAI endpoint URL")
    api_key: str = Field(..., description="Azure OpenAI API key")
    api_version: str = Field(default="2024-10-21", description="Azure API version")
    tracing: dict[str, Any] | None = Field(default=None, description="Tier 2 tracing configuration")

    @field_validator("endpoint")
    @classmethod
    def _validate_endpoint_url(cls, value: str) -> str:
        return validate_azure_endpoint(value)

    @model_validator(mode="before")
    @classmethod
    def _set_model_from_deployment(cls, data: Any) -> Any:
        return derive_azure_model(data)

    VALUE_SOURCES: ClassVar[tuple[ValueSource, ...]] = AZURE_MODEL_VALUE_SOURCES


class OpenRouterLLMSourceConfig(LLMSourceConfig):
    """OpenRouter settings for one source request."""

    provider: Literal["openrouter"] = Field(default="openrouter", description="LLM provider")
    model: str = Field(..., description="Model identifier")
    api_key: str = Field(..., description="OpenRouter API key")
    base_url: str = Field(default=OPENROUTER_BASE_URL, description="OpenRouter API base URL")
    timeout_seconds: float = Field(default=60.0, gt=0, description="Request timeout")
    tracing: dict[str, Any] | None = Field(default=None, description="Tier 2 tracing configuration")

    @field_validator("base_url")
    @classmethod
    def _normalize_base_url(cls, value: str) -> str:
        return validate_openrouter_base_url(value)

    VALUE_SOURCES: ClassVar[tuple[ValueSource, ...]] = OPENROUTER_MODEL_VALUE_SOURCES


class BedrockLLMSourceConfig(LLMSourceConfig):
    """Keyless LiteLLM Bedrock settings for one source request."""

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
    tracing: dict[str, Any] | None = Field(default=None, description="Tier 2 tracing configuration")

    @field_validator("model")
    @classmethod
    def _require_bedrock_prefix(cls, value: str) -> str:
        return validate_bedrock_model(value)

    VALUE_SOURCES: ClassVar[tuple[ValueSource, ...]] = BEDROCK_VALUE_SOURCES


class GatewayLLMSourceConfig(LLMSourceConfig):
    """ELSPETH LLM Gateway settings for one source request."""

    provider: Literal["gateway"] = Field(default="gateway", description="LLM provider")
    model: str = Field(
        ...,
        min_length=GATEWAY_MODEL_MIN_LENGTH,
        max_length=GATEWAY_MODEL_MAX_LENGTH,
        description="Logical model alias",
    )
    endpoint: str = Field(..., description="Gateway base URL ending in '/v1'")
    api_key: str = Field(..., description="Resolved gateway bearer credential")
    contract_major: int = Field(default=1, description="Gateway contract major version")
    required_capabilities: tuple[str, ...] = Field(default=(), description="Required gateway capabilities")
    timeout_seconds: float = Field(
        default=60.0,
        gt=GATEWAY_TIMEOUT_MIN_EXCLUSIVE,
        le=GATEWAY_TIMEOUT_MAX_SECONDS,
        description="Request timeout",
    )
    max_tokens: int | None = Field(
        default=None,
        gt=GATEWAY_MAX_TOKENS_MIN_EXCLUSIVE,
        le=GATEWAY_MAX_TOKENS_LIMIT,
        description="Maximum tokens in response",
    )
    tracing: dict[str, Any] | None = Field(default=None, description="Tier 2 tracing configuration")

    @field_validator("endpoint")
    @classmethod
    def _validate_endpoint(cls, value: str) -> str:
        return validate_gateway_endpoint(value)

    @field_validator("contract_major")
    @classmethod
    def _validate_contract_major(cls, value: int) -> int:
        return validate_gateway_contract_major(value)

    @field_validator("required_capabilities")
    @classmethod
    def _validate_required_capabilities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return validate_gateway_capabilities(value)

    @model_validator(mode="after")
    def _validate_structured_output_capability(self) -> GatewayLLMSourceConfig:
        """Reject a structured request the declared gateway cannot be trusted to serve.

        Same demand the transform's ``GatewayConfig`` makes: the lowered
        json_schema payload is identical, so it is caught at configuration
        time rather than on the first billed completion call.
        """
        validate_gateway_single_prompt_structured_output_capability(
            self.response_format is ResponseFormat.STRUCTURED,
            self.required_capabilities,
        )
        return self

    VALUE_SOURCES: ClassVar[tuple[ValueSource, ...]] = GATEWAY_VALUE_SOURCES


SOURCE_PROVIDER_CONFIGS: dict[str, type[LLMSourceConfig]] = {
    "azure": AzureOpenAILLMSourceConfig,
    "openrouter": OpenRouterLLMSourceConfig,
    "bedrock": BedrockLLMSourceConfig,
    "gateway": GatewayLLMSourceConfig,
}

__all__ = [
    "SOURCE_PROVIDER_CONFIGS",
    "AzureOpenAILLMSourceConfig",
    "BedrockLLMSourceConfig",
    "GatewayLLMSourceConfig",
    "LLMSourceConfig",
    "OpenRouterLLMSourceConfig",
]
