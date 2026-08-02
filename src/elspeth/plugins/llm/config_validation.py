"""Shared provider configuration policies for all LLM plugin surfaces."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from elspeth.contracts.value_source import CatalogValueSource, DerivedFromSiblingValueSource, ValueSource
from elspeth.plugins.infrastructure.url_validation import validate_credential_safe_https_url
from elspeth.plugins.llm.model_catalog import MODEL_CATALOG_OPENROUTER

AZURE_MODEL_VALUE_SOURCES: tuple[ValueSource, ...] = (
    DerivedFromSiblingValueSource(
        field_name="model",
        sibling_field="deployment_name",
        allow_empty_default=True,
    ),
)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_BASE_URL_APPLIES_WHEN = (("base_url", OPENROUTER_BASE_URL),)
OPENROUTER_MODEL_VALUE_SOURCES: tuple[ValueSource, ...] = (
    CatalogValueSource(
        field_name="model",
        catalog_id=MODEL_CATALOG_OPENROUTER,
        applies_when=OPENROUTER_BASE_URL_APPLIES_WHEN,
    ),
)

BEDROCK_MODEL_MIN_LENGTH = 9
BEDROCK_MODEL_MAX_LENGTH = 512
BEDROCK_REGION_MIN_LENGTH = 1
BEDROCK_REGION_MAX_LENGTH = 64
BEDROCK_REGION_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
BEDROCK_VALUE_SOURCES: tuple[ValueSource, ...] = ()

GATEWAY_VERSIONED_BASE = "/v1"
GATEWAY_LOOPBACK_HOST = "127.0.0.1"
GATEWAY_SUPPORTED_CAPABILITIES = frozenset({"text", "tools", "json_object", "json_schema", "seed", "usage"})
GATEWAY_SUPPORTED_CONTRACT_MAJORS = frozenset({1})
GATEWAY_MODEL_MIN_LENGTH = 1
GATEWAY_MODEL_MAX_LENGTH = 512
GATEWAY_TIMEOUT_MIN_EXCLUSIVE = 0
GATEWAY_TIMEOUT_MAX_SECONDS = 300
GATEWAY_MAX_TOKENS_MIN_EXCLUSIVE = 0
GATEWAY_MAX_TOKENS_LIMIT = 131072
GATEWAY_VALUE_SOURCES: tuple[ValueSource, ...] = ()


def validate_azure_endpoint(value: str) -> str:
    """Validate an Azure OpenAI endpoint under the shared bearer URL policy."""
    return validate_credential_safe_https_url(value, field_name="endpoint", allow_http_loopback=True)


def derive_azure_model(data: Any) -> Any:
    """Fill an omitted Azure model from its deployment name."""
    if isinstance(data, dict) and not data.get("model"):
        deployment = data.get("deployment_name")
        if deployment:
            data["model"] = deployment
    return data


def normalize_openrouter_base_url(value: str) -> str:
    """Normalize base URL spellings that runtime HTTP joining treats as identical."""
    parsed = urlsplit(value)
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def validate_openrouter_base_url(value: str) -> str:
    """Validate and normalize an OpenRouter-compatible bearer endpoint."""
    validated = validate_credential_safe_https_url(value, field_name="base_url", allow_http_loopback=True)
    return normalize_openrouter_base_url(validated)


def validate_bedrock_model(value: str) -> str:
    """Validate the LiteLLM Bedrock model identifier convention."""
    if value != value.strip() or not value.startswith("bedrock/") or not value.removeprefix("bedrock/"):
        raise ValueError("Bedrock model must be a non-empty LiteLLM 'bedrock/<model-id>' value without surrounding whitespace")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError("Bedrock model must not contain control characters")
    return value


def validate_gateway_endpoint(value: str) -> str:
    """Validate the gateway's credential-safe endpoint and versioned path."""
    validated = validate_credential_safe_https_url(value, field_name="endpoint", allow_http_loopback=True)
    parsed = urlsplit(validated)
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"endpoint must have a valid port: {exc}") from exc
    if parsed.scheme == "http" and parsed.hostname != GATEWAY_LOOPBACK_HOST:
        raise ValueError(f"endpoint must use HTTPS unless targeting the literal {GATEWAY_LOOPBACK_HOST} loopback host")
    if parsed.query:
        raise ValueError("endpoint must not contain a query string")
    if parsed.fragment:
        raise ValueError("endpoint must not contain a fragment")
    path_segments = parsed.path.split("/")
    if any(segment in ("", ".", "..") for segment in path_segments[1:]):
        raise ValueError("endpoint path must not contain empty, '.', or '..' segments")
    if not parsed.path.endswith(GATEWAY_VERSIONED_BASE):
        raise ValueError(f"endpoint must end with the versioned base path {GATEWAY_VERSIONED_BASE!r}")
    return validated


def validate_gateway_contract_major(value: int) -> int:
    """Validate a gateway wire-contract major."""
    if value not in GATEWAY_SUPPORTED_CONTRACT_MAJORS:
        raise ValueError(f"contract_major {value} is not supported; supported majors: {sorted(GATEWAY_SUPPORTED_CONTRACT_MAJORS)}")
    return value


def validate_gateway_capabilities(value: tuple[str, ...]) -> tuple[str, ...]:
    """Validate the closed, duplicate-free gateway capability set."""
    seen: set[str] = set()
    for capability in value:
        if capability not in GATEWAY_SUPPORTED_CAPABILITIES:
            raise ValueError(f"unknown gateway capability {capability!r}; supported: {sorted(GATEWAY_SUPPORTED_CAPABILITIES)}")
        if capability in seen:
            raise ValueError(f"duplicate gateway capability {capability!r}")
        seen.add(capability)
    return value


__all__ = [
    "AZURE_MODEL_VALUE_SOURCES",
    "BEDROCK_MODEL_MAX_LENGTH",
    "BEDROCK_MODEL_MIN_LENGTH",
    "BEDROCK_REGION_MAX_LENGTH",
    "BEDROCK_REGION_MIN_LENGTH",
    "BEDROCK_REGION_PATTERN",
    "BEDROCK_VALUE_SOURCES",
    "GATEWAY_LOOPBACK_HOST",
    "GATEWAY_MAX_TOKENS_LIMIT",
    "GATEWAY_MAX_TOKENS_MIN_EXCLUSIVE",
    "GATEWAY_MODEL_MAX_LENGTH",
    "GATEWAY_MODEL_MIN_LENGTH",
    "GATEWAY_SUPPORTED_CAPABILITIES",
    "GATEWAY_SUPPORTED_CONTRACT_MAJORS",
    "GATEWAY_TIMEOUT_MAX_SECONDS",
    "GATEWAY_TIMEOUT_MIN_EXCLUSIVE",
    "GATEWAY_VALUE_SOURCES",
    "GATEWAY_VERSIONED_BASE",
    "OPENROUTER_BASE_URL",
    "OPENROUTER_BASE_URL_APPLIES_WHEN",
    "OPENROUTER_MODEL_VALUE_SOURCES",
    "derive_azure_model",
    "normalize_openrouter_base_url",
    "validate_azure_endpoint",
    "validate_bedrock_model",
    "validate_gateway_capabilities",
    "validate_gateway_contract_major",
    "validate_gateway_endpoint",
    "validate_openrouter_base_url",
]
