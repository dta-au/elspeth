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
    """Fill an omitted, null, or empty Azure model from its deployment name."""
    if isinstance(data, dict) and ("model" not in data or data["model"] is None or data["model"] == "") and "deployment_name" in data:
        deployment = data["deployment_name"]
        data["model"] = deployment
    return data


# elspeth-5653909057: character sets for the base_url path ambiguity checks.
# RFC 3986 unreserved characters — a percent-encoded octet in this set is a
# respelling of the plain character whose wire treatment is server-dependent.
_URL_PATH_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_URL_HEX_DIGITS = frozenset("0123456789ABCDEFabcdef")


def _remove_dot_segments(path: str) -> str:
    """Collapse ``.``/``..`` path segments per RFC 3986 section 5.2.4.

    elspeth-5653909057: mirrors httpx's client-side normalization so the
    stored config value matches the path the runtime actually puts on the
    wire. Empty segments are preserved (the wire algorithm never merges
    ``//``) except where a following ``..`` pops one, exactly as httpx does.
    """
    if not path:
        return path
    collapsed: list[str] = []
    for segment in path.split("/")[1:]:
        if segment == ".":
            continue
        if segment == "..":
            if collapsed:
                collapsed.pop()
            continue
        collapsed.append(segment)
    return "/" + "/".join(collapsed)


def _assert_unambiguous_path_percent_encoding(path: str) -> None:
    """Reject percent-encodings whose wire equivalence is server-dependent.

    elspeth-5653909057: httpx sends percent triplets in the path as-is, so a
    percent-encoded unreserved octet (``/%61pi/v1``) may or may not be decoded
    to the canonical ``/api/v1`` by the server — an ambiguous respelling that
    would otherwise slip past the catalogue ``applies_when`` gate. Malformed
    triplets are rejected for the same reason. Percent-encoded *reserved*
    octets (e.g. ``%2F``) are not equivalent to their decoded form per RFC
    3986 and pass through unchanged.
    """
    index = path.find("%")
    while index != -1:
        triplet = path[index + 1 : index + 3]
        if len(triplet) != 2 or any(char not in _URL_HEX_DIGITS for char in triplet):
            raise ValueError("base_url path contains malformed percent-encoding")
        if chr(int(triplet, 16)) in _URL_PATH_UNRESERVED:
            raise ValueError(
                f"base_url path contains ambiguous percent-encoding %{triplet} of an unreserved character; spell the character directly"
            )
        index = path.find("%", index + 3)


def normalize_openrouter_base_url(value: str) -> str:
    """Normalize base URL spellings the runtime treats as identical; reject wire-ambiguous ones.

    elspeth-5653909057: the catalogue ``applies_when`` gate compares this
    function's output against the canonical OpenRouter endpoint by string
    equality, so every spelling the wire treats as canonical must normalize to
    it — otherwise the spelling silently disables model-catalogue enforcement.
    Two deliberate treatments:

    * **Normalize** spellings the client itself rewrites, so they are
      wire-identical: host case, default port, trailing slashes (runtime HTTP
      joining), and ``.``/``..`` dot segments (httpx applies RFC 3986
      remove_dot_segments before sending).
    * **Reject** spellings that reach the wire unchanged but whose server-side
      treatment is undefined — percent-encoded unreserved octets, empty path
      segments, and a host trailing dot (Host header/SNI differ). Rewriting
      them would change the wire bytes; accepting them would leave the gate
      defeatable. This matches ``validate_gateway_endpoint``'s stance on
      ambiguous segments.
    """
    parsed = urlsplit(value)
    hostname = parsed.hostname
    if hostname is None:
        raise ValueError("base_url must include a hostname")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"base_url must have a valid port: {exc}") from exc
    normalized_host = hostname.lower()
    if normalized_host.endswith("."):
        # elspeth-5653909057: a root-qualified host resolves to the same DNS
        # name but sends a different Host header and SNI — server-dependent.
        raise ValueError("base_url host must not end with a trailing dot")
    if ":" in normalized_host:
        normalized_host = f"[{normalized_host}]"
    default_port = 443 if parsed.scheme == "https" else 80 if parsed.scheme == "http" else None
    netloc = normalized_host if port is None or port == default_port else f"{normalized_host}:{port}"
    _assert_unambiguous_path_percent_encoding(parsed.path)
    path = _remove_dot_segments(parsed.path).rstrip("/")
    if "" in path.split("/")[1:]:
        # elspeth-5653909057: httpx preserves ``//`` on the wire; whether the
        # server collapses it is server-dependent. (An empty segment consumed
        # by a following ``..`` never reaches the wire and is fine.)
        raise ValueError("base_url path must not contain empty path segments")
    return urlunsplit((parsed.scheme, netloc, path, parsed.query, parsed.fragment))


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
