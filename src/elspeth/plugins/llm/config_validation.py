"""Shared provider configuration policies for all LLM plugin surfaces."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.contracts.value_source import CatalogValueSource, DerivedFromSiblingValueSource, ValueSource
from elspeth.core.llm_provider_validation import (
    GATEWAY_LOOPBACK_HOST,
    GATEWAY_SUPPORTED_CAPABILITIES,
    GATEWAY_SUPPORTED_CONTRACT_MAJORS,
    GATEWAY_VERSIONED_BASE,
    validate_bedrock_model,
    validate_gateway_capabilities,
    validate_gateway_contract_major,
    validate_gateway_endpoint,
)
from elspeth.core.url_validation import validate_credential_safe_https_url
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
BEDROCK_CREDENTIAL_MIN_LENGTH = 1
BEDROCK_API_KEY_MAX_LENGTH = 16384
BEDROCK_ACCESS_KEY_ID_MAX_LENGTH = 256
BEDROCK_SECRET_ACCESS_KEY_MAX_LENGTH = 4096
BEDROCK_SESSION_TOKEN_MAX_LENGTH = 16384
_LITELLM_ENV_INDIRECTION_PREFIX = "os.environ/"


def validate_bedrock_credential_fields(
    *,
    api_key: str | None,
    aws_access_key_id: str | None,
    aws_secret_access_key: str | None,
    aws_session_token: str | None,
) -> None:
    """Enforce the cross-field Bedrock credential contract shared by both LLM surfaces.

    Every credential is optional: with none configured the provider uses
    boto3's default AWS credential chain (task role, environment, profile),
    which stays the deployment default. An explicit credential is exactly one
    of a Bedrock API key (``api_key``, sent as a bearer token) or a static
    IAM pair (``aws_access_key_id`` + ``aws_secret_access_key``, with
    ``aws_session_token`` admitted only alongside the pair). Raises
    ``ValueError`` with a field-precise message that never echoes a value.

    A value beginning ``os.environ/`` is refused: LiteLLM's Bedrock
    credential resolver treats that prefix as an instruction to read the
    named variable from the PROCESS environment, so a user-supplied secret
    of that shape would make the server dereference its own environment.
    """
    for field_name, value in (
        ("api_key", api_key),
        ("aws_access_key_id", aws_access_key_id),
        ("aws_secret_access_key", aws_secret_access_key),
        ("aws_session_token", aws_session_token),
    ):
        if value is not None and value.startswith(_LITELLM_ENV_INDIRECTION_PREFIX):
            raise ValueError(f"{field_name} must be a literal credential, not an environment-variable indirection")
    access_present = aws_access_key_id is not None
    secret_present = aws_secret_access_key is not None
    session_present = aws_session_token is not None
    if api_key is not None and (access_present or secret_present or session_present):
        raise ValueError("api_key and static AWS credentials are mutually exclusive; configure exactly one Bedrock credential")
    if access_present != secret_present:
        raise ValueError("aws_access_key_id and aws_secret_access_key are required together as a credential pair")
    if session_present and not access_present:
        raise ValueError("aws_session_token requires the access and secret credential pair")


#: The capability a ``response_format="structured"`` query consumes. Standard
#: mode asks only for ``json_object``; structured mode sends an API-native
#: JSON Schema the gateway must be able to enforce.
GATEWAY_STRUCTURED_OUTPUT_CAPABILITY = "json_schema"
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


# First Azure OpenAI API version whose chat-completions contract defines
# ``max_completion_tokens`` — the only output-budget parameter every Azure
# call sends (reasoning deployments reject ``max_tokens``).
AZURE_MIN_DATED_API_VERSION = "2024-09-01-preview"
_AZURE_DATED_API_VERSION = re.compile(r"\d{4}-\d{2}-\d{2}")


def validate_azure_api_version(value: str) -> str:
    """Reject a dated Azure API version that predates ``max_completion_tokens``.

    An older version answers the parameter with HTTP 400 "Unrecognized request
    argument", which reaches the operator only as the audit-safe provider
    error, so the cause is named here instead. Undated versions (the v1
    ``preview``/``latest`` aliases) pass through unchanged.
    """
    dated = _AZURE_DATED_API_VERSION.match(value)
    if dated is not None and dated.group() < AZURE_MIN_DATED_API_VERSION[:10]:
        raise ValueError(
            f"api_version {value!r} predates {AZURE_MIN_DATED_API_VERSION}: the Azure provider sends the output budget as "
            "max_completion_tokens, which older API versions reject"
        )
    return value


@trust_boundary(
    tier=3,
    source="raw pydantic ``mode='before'`` input for an Azure LLM config — authored settings YAML or a composer-authored proposal payload, ahead of field validation",
    source_param="data",
    suppresses=("R5",),
    invariant=(
        "Returns its input unchanged for anything that is not a mapping, and for a mapping that already "
        "carries a non-empty ``model`` or carries no ``deployment_name``. Never raises: an input pydantic "
        "will reject is handed back for pydantic to reject."
    ),
    non_raising=True,
)
def derive_azure_model(data: Any) -> Any:
    """Fill an omitted, null, or empty Azure model from its deployment name.

    Tier-3 boundary. A ``mode="before"`` validator sees whatever the caller
    passed — a mapping from YAML or from the composer, an already-built
    model instance, or something else entirely — so the mapping check is the
    admission test, not defensive re-typing of a validated value. The
    ``tier_model`` rule exempts that construct inline (``R5`` is allowed in a
    pydantic before-validator); this site sits outside the exemption only
    because it is factored into one shared helper so the Azure transform
    config and the Azure source config cannot drift on the derivation.
    """
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


def validate_gateway_structured_output_capability(
    structured_query_names: tuple[str, ...],
    required_capabilities: tuple[str, ...],
) -> None:
    """Require the structured-output capability when any query is structured.

    ``LLMTransform`` lowers a structured query's ``output_fields`` into an
    API-native ``{"type": "json_schema", ...}`` response format and the gateway
    provider forwards it verbatim. A gateway that never declared the capability
    cannot honor that payload, so the run fails on the first completion call —
    after rows have been read and the call has been billed.

    ``required_capabilities`` is the operator's declaration of what the gateway
    must offer, and the readiness probe already refuses a gateway that does not
    report every declared capability. Tying the two together moves the failure
    from mid-run to configuration time, where the operator can act on it.

    Takes plain names rather than query objects so this module stays free of a
    dependency on the multi-query authoring models.
    """
    if not structured_query_names:
        return
    if GATEWAY_STRUCTURED_OUTPUT_CAPABILITY in required_capabilities:
        return
    names = ", ".join(repr(name) for name in structured_query_names)
    raise ValueError(
        f"queries {names} set response_format='structured', which sends an API-native "
        f"{GATEWAY_STRUCTURED_OUTPUT_CAPABILITY!r} response format the gateway must be able to "
        f"enforce, but required_capabilities does not declare {GATEWAY_STRUCTURED_OUTPUT_CAPABILITY!r}. "
        f"Add {GATEWAY_STRUCTURED_OUTPUT_CAPABILITY!r} to required_capabilities so readiness rejects an "
        "incapable gateway before the run starts, or set response_format='standard' on those queries."
    )


def validate_gateway_single_prompt_structured_output_capability(
    structured: bool,
    required_capabilities: tuple[str, ...],
) -> None:
    """Single-prompt twin of :func:`validate_gateway_structured_output_capability`.

    The top-level ``response_format='structured'`` pair (single-prompt
    transforms and the LLM source) lowers ``output_fields`` into the same
    API-native ``{"type": "json_schema", ...}`` payload a structured query
    does, so it makes the identical capability demand — caught at
    configuration time for the same reason.
    """
    if not structured:
        return
    if GATEWAY_STRUCTURED_OUTPUT_CAPABILITY in required_capabilities:
        return
    raise ValueError(
        f"response_format='structured' sends an API-native {GATEWAY_STRUCTURED_OUTPUT_CAPABILITY!r} "
        f"response format the gateway must be able to enforce, but required_capabilities does not "
        f"declare {GATEWAY_STRUCTURED_OUTPUT_CAPABILITY!r}. Add {GATEWAY_STRUCTURED_OUTPUT_CAPABILITY!r} "
        "to required_capabilities so readiness rejects an incapable gateway before the run starts, "
        "or set response_format='standard'."
    )


__all__ = [
    "AZURE_MODEL_VALUE_SOURCES",
    "BEDROCK_ACCESS_KEY_ID_MAX_LENGTH",
    "BEDROCK_API_KEY_MAX_LENGTH",
    "BEDROCK_CREDENTIAL_MIN_LENGTH",
    "BEDROCK_MODEL_MAX_LENGTH",
    "BEDROCK_MODEL_MIN_LENGTH",
    "BEDROCK_REGION_MAX_LENGTH",
    "BEDROCK_REGION_MIN_LENGTH",
    "BEDROCK_REGION_PATTERN",
    "BEDROCK_SECRET_ACCESS_KEY_MAX_LENGTH",
    "BEDROCK_SESSION_TOKEN_MAX_LENGTH",
    "BEDROCK_VALUE_SOURCES",
    "GATEWAY_LOOPBACK_HOST",
    "GATEWAY_MAX_TOKENS_LIMIT",
    "GATEWAY_MAX_TOKENS_MIN_EXCLUSIVE",
    "GATEWAY_MODEL_MAX_LENGTH",
    "GATEWAY_MODEL_MIN_LENGTH",
    "GATEWAY_STRUCTURED_OUTPUT_CAPABILITY",
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
    "validate_bedrock_credential_fields",
    "validate_bedrock_model",
    "validate_gateway_capabilities",
    "validate_gateway_contract_major",
    "validate_gateway_endpoint",
    "validate_gateway_single_prompt_structured_output_capability",
    "validate_gateway_structured_output_capability",
    "validate_openrouter_base_url",
]
