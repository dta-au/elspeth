"""Provider binding policies shared by profile admission and plugin configuration.

These functions validate values without importing or constructing plugin runtime
objects. The application provider registry must implement the complete declared
provider set.
"""

from urllib.parse import urlsplit

from elspeth.core.url_validation import validate_credential_safe_https_url

LLM_PROVIDER_NAMES = frozenset({"azure", "openrouter", "bedrock", "gateway"})

GATEWAY_VERSIONED_BASE = "/v1"
GATEWAY_LOOPBACK_HOST = "127.0.0.1"
GATEWAY_SUPPORTED_CAPABILITIES = frozenset({"text", "tools", "json_object", "json_schema", "seed", "usage"})
GATEWAY_SUPPORTED_CONTRACT_MAJORS = frozenset({1})


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
