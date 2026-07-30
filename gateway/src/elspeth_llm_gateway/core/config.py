"""Fail-closed gateway configuration loaded from process environment.

``load_config`` is the sole entry point: it recognises exactly the
``ELSPETH_LLM_GATEWAY_`` namespace declared in ``KNOWN_ENV``. Any variable in
that namespace it does not recognise, any required variable that is absent,
and any recognised variable whose value fails its own rule are all collected
into one ``ConfigError`` — startup fails once, with the complete list of
problems, not on the first one found.

Every error is a safe code-like string (a fixed literal, or one prefixed with
an env var name / JSON object key — never a secret value). Validation runs
to completion *before* ``GatewayConfig`` is ever constructed, specifically so
that a secret-bearing field can never end up inside a pydantic
``ValidationError`` (whose ``.errors()`` echoes the raw input). Unrelated
environment variables (``PATH``, or anything outside the
``ELSPETH_LLM_GATEWAY_`` prefix) are ignored entirely — only this project's
own namespace is policed.
"""

import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, SecretStr

from elspeth_llm_gateway.core.contract import Bounds
from elspeth_llm_gateway.core.parsing import StrictJsonError, parse_strict_json

ENV_PREFIX = "ELSPETH_LLM_GATEWAY_"

KNOWN_ENV: frozenset[str] = frozenset(
    ENV_PREFIX + suffix
    for suffix in (
        "INBOUND_BEARER",
        "ADAPTER",
        "UPSTREAM_ORIGIN",
        "OAUTH_TOKEN_URL",
        "OAUTH_CLIENT_ID",
        "OAUTH_CLIENT_SECRET",
        "OAUTH_AUTH_METHOD",
        "OAUTH_SCOPES",
        "OAUTH_FIXED_LIFETIME_SECONDS",
        "REFRESH_SKEW_SECONDS",
        "REQUEST_TIMEOUT_SECONDS",
        "MAX_BODY_BYTES",
        "MAX_RESPONSE_BYTES",
        "MAX_MESSAGES",
        "MAX_TOOLS",
        "MAX_STRING_CHARS",
        "MAX_SCHEMA_BYTES",
        "MAX_SCHEMA_DEPTH",
        "TEMPERATURE_MIN",
        "TEMPERATURE_MAX",
        "MAX_MAX_TOKENS",
        "MODEL_MAPPINGS",
    )
)

_MODEL_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
# RFC 6749 section 3.3: scope-token = 1*( %x21 / %x23-5B / %x5D-7E )
# — printable ASCII excluding space, the quote character, and backslash.
_OAUTH_SCOPE_TOKEN_RE = re.compile(r"^[\x21\x23-\x5b\x5d-\x7e]+$")
_MIN_BEARER_LENGTH = 32
_MAX_MODEL_MAPPINGS_BYTES = 1_048_576
_LOOPBACK_HOST = "127.0.0.1"

_INBOUND_BEARER = ENV_PREFIX + "INBOUND_BEARER"
_ADAPTER = ENV_PREFIX + "ADAPTER"
_UPSTREAM_ORIGIN = ENV_PREFIX + "UPSTREAM_ORIGIN"
_OAUTH_TOKEN_URL = ENV_PREFIX + "OAUTH_TOKEN_URL"
_OAUTH_CLIENT_ID = ENV_PREFIX + "OAUTH_CLIENT_ID"
_OAUTH_CLIENT_SECRET = ENV_PREFIX + "OAUTH_CLIENT_SECRET"
_OAUTH_AUTH_METHOD = ENV_PREFIX + "OAUTH_AUTH_METHOD"
_OAUTH_SCOPES = ENV_PREFIX + "OAUTH_SCOPES"
_OAUTH_FIXED_LIFETIME_SECONDS = ENV_PREFIX + "OAUTH_FIXED_LIFETIME_SECONDS"
_REFRESH_SKEW_SECONDS = ENV_PREFIX + "REFRESH_SKEW_SECONDS"
_REQUEST_TIMEOUT_SECONDS = ENV_PREFIX + "REQUEST_TIMEOUT_SECONDS"
_MAX_BODY_BYTES = ENV_PREFIX + "MAX_BODY_BYTES"
_MAX_RESPONSE_BYTES = ENV_PREFIX + "MAX_RESPONSE_BYTES"
_MAX_MESSAGES = ENV_PREFIX + "MAX_MESSAGES"
_MAX_TOOLS = ENV_PREFIX + "MAX_TOOLS"
_MAX_STRING_CHARS = ENV_PREFIX + "MAX_STRING_CHARS"
_MAX_SCHEMA_BYTES = ENV_PREFIX + "MAX_SCHEMA_BYTES"
_MAX_SCHEMA_DEPTH = ENV_PREFIX + "MAX_SCHEMA_DEPTH"
_TEMPERATURE_MIN = ENV_PREFIX + "TEMPERATURE_MIN"
_TEMPERATURE_MAX = ENV_PREFIX + "TEMPERATURE_MAX"
_MAX_MAX_TOKENS = ENV_PREFIX + "MAX_MAX_TOKENS"
_MODEL_MAPPINGS = ENV_PREFIX + "MODEL_MAPPINGS"

_VALID_OAUTH_AUTH_METHODS = frozenset({"client_secret_basic", "client_secret_post"})


class ConfigError(Exception):
    """Startup configuration failed: every problem found, collected once.

    ``errors`` holds only safe code-like strings — fixed literals such as
    ``"bearer_too_short"``, or a literal prefixed with an env var name or a
    model-mapping alias (both drawn from a closed namespace / operator
    config, never from request or secret data).
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(f"gateway configuration invalid: {', '.join(errors)}")


class GatewayConfig(BaseModel):
    """The gateway's fully-validated runtime configuration.

    Constructed only by ``load_config`` once every field has already passed
    its own validation rule — this model itself carries no validators, so it
    can never raise a pydantic ``ValidationError`` that might echo a secret
    field's raw value back through ``.errors()``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    inbound_bearer: SecretStr
    adapter_name: str
    upstream_origin: str
    oauth_token_url: str
    oauth_client_id: SecretStr
    oauth_client_secret: SecretStr
    oauth_auth_method: Literal["client_secret_basic", "client_secret_post"]
    oauth_scopes: tuple[str, ...] = ()
    oauth_fixed_lifetime_seconds: int | None = None
    refresh_skew_seconds: int = 60
    request_timeout_seconds: float = 60.0
    max_body_bytes: int = 1_048_576
    max_response_bytes: int = 4_194_304
    bounds: Bounds
    model_mappings: dict[str, dict]

    @property
    def mapping_generation(self) -> str:
        """A short, stable fingerprint of ``model_mappings``.

        Stable across key order: the mapping is re-serialised with sorted
        keys and no incidental whitespace before hashing.
        """
        canonical = json.dumps(self.model_mappings, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _read(environ: Mapping[str, str], key: str) -> str | None:
    if key in environ:
        return environ[key]
    return None


def _validate_origin(value: str) -> bool:
    """``https://host[:port]`` with no path/query/userinfo, or exactly ``http://127.0.0.1:<port>``.

    ``value`` must be its own canonical ``urlsplit``/``urlunsplit`` round
    trip. ``urlsplit`` silently drops embedded CR/LF and strips surrounding
    whitespace before parsing, so without this check a value carrying a
    CRLF or leading/trailing space could pass structural validation while
    smuggling that ambiguous byte sequence into whatever a later stage
    concatenates the stored origin into. The round trip also rejects a
    non-canonical scheme casing (``HTTPS://``) rather than silently
    normalising it.
    """
    # Require every character to be printable, non-space ASCII up front.
    # urlsplit's own cleanup silently drops some whitespace and control
    # characters at the ends (embedded CR/LF, a stray leading space) while
    # leaving other placements (trailing space inside the netloc, an
    # embedded NUL/control byte inside the host) in a form that still
    # round-trips through urlunsplit unchanged — so this class of ambiguity
    # can't be caught by the round-trip check below and must be refused
    # up front instead.
    if not all(0x21 <= ord(ch) <= 0x7E for ch in value):
        return False

    try:
        parsed = urlsplit(value)
    except ValueError:
        return False

    if urlunsplit(parsed) != value:
        return False
    if parsed.path != "" or parsed.query != "" or parsed.fragment != "":
        return False
    if parsed.username is not None or parsed.password is not None:
        return False

    hostname = parsed.hostname
    if not hostname:
        return False

    try:
        port = parsed.port
    except ValueError:
        return False

    scheme = parsed.scheme
    if scheme == "https":
        return True
    if scheme == "http":
        return hostname == _LOOPBACK_HOST and port is not None
    return False


def _parse_int(raw: str, *, env_key: str, errors: list[str], minimum: int | None = None) -> int | None:
    try:
        value = int(raw)
    except ValueError:
        errors.append(f"invalid_int:{env_key}")
        return None
    if minimum is not None and value < minimum:
        errors.append(f"out_of_range:{env_key}")
        return None
    return value


def _parse_float(raw: str, *, env_key: str, errors: list[str], minimum: float | None = None) -> float | None:
    try:
        value = float(raw)
    except ValueError:
        errors.append(f"invalid_float:{env_key}")
        return None
    if not math.isfinite(value):
        errors.append(f"non_finite_float:{env_key}")
        return None
    if minimum is not None and value < minimum:
        errors.append(f"out_of_range:{env_key}")
        return None
    return value


_ALIAS_ECHO_LIMIT = 40


def _safe_fragment(text: str) -> str:
    """Bound an operator-controlled string before it lands in an error code.

    A rejected model-mapping alias is, by definition, not known to match the
    alias charset — it could carry control characters or run to several
    kilobytes. Truncating and stripping non-printable characters keeps the
    error code both grep-able and safe to write straight to startup logs.
    """
    cleaned = "".join(ch if ch.isprintable() else "?" for ch in text[:_ALIAS_ECHO_LIMIT])
    suffix = "..." if len(text) > _ALIAS_ECHO_LIMIT else ""
    return cleaned + suffix


def _parse_model_mappings(raw: str, errors: list[str]) -> dict[str, dict] | None:
    try:
        parsed = parse_strict_json(raw.encode("utf-8"), max_bytes=_MAX_MODEL_MAPPINGS_BYTES)
    except StrictJsonError:
        errors.append("invalid_model_mappings_json")
        return None

    if not isinstance(parsed, dict):
        errors.append("model_mappings_not_object")
        return None
    if not parsed:
        errors.append("empty_model_mappings")
        return None

    valid = True
    for alias, mapping in parsed.items():
        # JSON object keys are always strings, so `alias` needs no type check.
        if not _MODEL_ALIAS_RE.match(alias):
            errors.append(f"invalid_model_alias:{_safe_fragment(alias)}")
            valid = False
        if not isinstance(mapping, dict):
            errors.append(f"invalid_model_mapping_value:{_safe_fragment(alias)}")
            valid = False

    if not valid:
        return None
    return parsed


def load_config(environ: Mapping[str, str]) -> GatewayConfig:
    """Build a ``GatewayConfig`` from ``environ``, or raise ``ConfigError``.

    Every unknown ``ELSPETH_LLM_GATEWAY_`` variable, every missing required
    variable, and every value that fails its own rule are collected into a
    single ``ConfigError`` — this never raises on just the first problem
    found. Environment outside the ``ELSPETH_LLM_GATEWAY_`` namespace is
    ignored entirely.
    """
    errors: list[str] = []

    for key in environ:
        if key.startswith(ENV_PREFIX) and key not in KNOWN_ENV:
            errors.append(f"unknown_env:{key}")

    required = (
        _INBOUND_BEARER,
        _ADAPTER,
        _UPSTREAM_ORIGIN,
        _OAUTH_TOKEN_URL,
        _OAUTH_CLIENT_ID,
        _OAUTH_CLIENT_SECRET,
        _OAUTH_AUTH_METHOD,
        _MODEL_MAPPINGS,
        _MAX_MESSAGES,
        _MAX_TOOLS,
        _MAX_STRING_CHARS,
        _MAX_SCHEMA_BYTES,
        _MAX_SCHEMA_DEPTH,
    )
    for key in required:
        if key not in environ:
            errors.append(f"missing_env:{key}")

    inbound_bearer = _read(environ, _INBOUND_BEARER)
    if inbound_bearer is not None and len(inbound_bearer) < _MIN_BEARER_LENGTH:
        errors.append("bearer_too_short")

    # ADAPTER / OAUTH_TOKEN_URL / OAUTH_CLIENT_ID / OAUTH_CLIENT_SECRET have no
    # structural check of their own (unlike bearer/origin/auth_method/mappings,
    # which reject an empty value transitively); an env var present-but-set-to-
    # "" must still fail closed instead of silently loading a blank credential.
    for empty_check_key in (_ADAPTER, _OAUTH_TOKEN_URL, _OAUTH_CLIENT_ID, _OAUTH_CLIENT_SECRET):
        raw_value = _read(environ, empty_check_key)
        if raw_value is not None and raw_value.strip() == "":
            errors.append(f"empty_env:{empty_check_key}")

    upstream_origin = _read(environ, _UPSTREAM_ORIGIN)
    if upstream_origin is not None and not _validate_origin(upstream_origin):
        errors.append("invalid_origin")

    oauth_auth_method_raw = _read(environ, _OAUTH_AUTH_METHOD)
    if oauth_auth_method_raw is not None and oauth_auth_method_raw not in _VALID_OAUTH_AUTH_METHODS:
        errors.append("invalid_oauth_auth_method")

    oauth_scopes_raw = _read(environ, _OAUTH_SCOPES)
    oauth_scopes: tuple[str, ...] = ()
    if oauth_scopes_raw is not None:
        scope_tokens = tuple(part for part in oauth_scopes_raw.split(" ") if part)
        if any(not _OAUTH_SCOPE_TOKEN_RE.match(token) for token in scope_tokens):
            errors.append(f"invalid_oauth_scopes:{_OAUTH_SCOPES}")
        else:
            oauth_scopes = scope_tokens

    oauth_fixed_lifetime_raw = _read(environ, _OAUTH_FIXED_LIFETIME_SECONDS)
    oauth_fixed_lifetime_seconds = None
    if oauth_fixed_lifetime_raw is not None:
        oauth_fixed_lifetime_seconds = _parse_int(oauth_fixed_lifetime_raw, env_key=_OAUTH_FIXED_LIFETIME_SECONDS, errors=errors, minimum=1)

    refresh_skew_raw = _read(environ, _REFRESH_SKEW_SECONDS)
    refresh_skew_seconds = 60
    if refresh_skew_raw is not None:
        parsed_skew = _parse_int(refresh_skew_raw, env_key=_REFRESH_SKEW_SECONDS, errors=errors, minimum=0)
        if parsed_skew is not None:
            refresh_skew_seconds = parsed_skew

    request_timeout_raw = _read(environ, _REQUEST_TIMEOUT_SECONDS)
    request_timeout_seconds = 60.0
    if request_timeout_raw is not None:
        parsed_timeout = _parse_float(request_timeout_raw, env_key=_REQUEST_TIMEOUT_SECONDS, errors=errors)
        if parsed_timeout is not None:
            if parsed_timeout <= 0:
                errors.append(f"out_of_range:{_REQUEST_TIMEOUT_SECONDS}")
            else:
                request_timeout_seconds = parsed_timeout

    max_body_bytes_raw = _read(environ, _MAX_BODY_BYTES)
    max_body_bytes = 1_048_576
    if max_body_bytes_raw is not None:
        parsed_body = _parse_int(max_body_bytes_raw, env_key=_MAX_BODY_BYTES, errors=errors, minimum=1)
        if parsed_body is not None:
            max_body_bytes = parsed_body

    max_response_bytes_raw = _read(environ, _MAX_RESPONSE_BYTES)
    max_response_bytes = 4_194_304
    if max_response_bytes_raw is not None:
        parsed_response = _parse_int(max_response_bytes_raw, env_key=_MAX_RESPONSE_BYTES, errors=errors, minimum=1)
        if parsed_response is not None:
            max_response_bytes = parsed_response

    max_messages_raw = _read(environ, _MAX_MESSAGES)
    max_messages = _parse_int(max_messages_raw, env_key=_MAX_MESSAGES, errors=errors, minimum=1) if max_messages_raw is not None else None

    max_tools_raw = _read(environ, _MAX_TOOLS)
    max_tools = _parse_int(max_tools_raw, env_key=_MAX_TOOLS, errors=errors, minimum=1) if max_tools_raw is not None else None

    max_string_chars_raw = _read(environ, _MAX_STRING_CHARS)
    max_string_chars = (
        _parse_int(max_string_chars_raw, env_key=_MAX_STRING_CHARS, errors=errors, minimum=1) if max_string_chars_raw is not None else None
    )

    max_schema_bytes_raw = _read(environ, _MAX_SCHEMA_BYTES)
    max_schema_bytes = (
        _parse_int(max_schema_bytes_raw, env_key=_MAX_SCHEMA_BYTES, errors=errors, minimum=1) if max_schema_bytes_raw is not None else None
    )

    max_schema_depth_raw = _read(environ, _MAX_SCHEMA_DEPTH)
    max_schema_depth = (
        _parse_int(max_schema_depth_raw, env_key=_MAX_SCHEMA_DEPTH, errors=errors, minimum=1) if max_schema_depth_raw is not None else None
    )

    bounds_kwargs: dict[str, int | float] = {}
    if max_messages is not None:
        bounds_kwargs["max_messages"] = max_messages
    if max_tools is not None:
        bounds_kwargs["max_tools"] = max_tools
    if max_string_chars is not None:
        bounds_kwargs["max_string_chars"] = max_string_chars
    if max_schema_bytes is not None:
        bounds_kwargs["max_schema_bytes"] = max_schema_bytes
    if max_schema_depth is not None:
        bounds_kwargs["max_schema_depth"] = max_schema_depth

    temperature_min_raw = _read(environ, _TEMPERATURE_MIN)
    if temperature_min_raw is not None:
        parsed_temp_min = _parse_float(temperature_min_raw, env_key=_TEMPERATURE_MIN, errors=errors)
        if parsed_temp_min is not None:
            bounds_kwargs["temperature_min"] = parsed_temp_min

    temperature_max_raw = _read(environ, _TEMPERATURE_MAX)
    if temperature_max_raw is not None:
        parsed_temp_max = _parse_float(temperature_max_raw, env_key=_TEMPERATURE_MAX, errors=errors)
        if parsed_temp_max is not None:
            bounds_kwargs["temperature_max"] = parsed_temp_max

    max_max_tokens_raw = _read(environ, _MAX_MAX_TOKENS)
    if max_max_tokens_raw is not None:
        parsed_max_max_tokens = _parse_int(max_max_tokens_raw, env_key=_MAX_MAX_TOKENS, errors=errors, minimum=1)
        if parsed_max_max_tokens is not None:
            bounds_kwargs["max_max_tokens"] = parsed_max_max_tokens

    model_mappings_raw = _read(environ, _MODEL_MAPPINGS)
    model_mappings = _parse_model_mappings(model_mappings_raw, errors) if model_mappings_raw is not None else None

    if errors:
        errors.sort()
        raise ConfigError(errors)

    bounds = Bounds(**bounds_kwargs)

    return GatewayConfig(
        inbound_bearer=SecretStr(inbound_bearer),
        adapter_name=_read(environ, _ADAPTER),
        upstream_origin=upstream_origin,
        oauth_token_url=_read(environ, _OAUTH_TOKEN_URL),
        oauth_client_id=SecretStr(_read(environ, _OAUTH_CLIENT_ID)),
        oauth_client_secret=SecretStr(_read(environ, _OAUTH_CLIENT_SECRET)),
        oauth_auth_method=oauth_auth_method_raw,
        oauth_scopes=oauth_scopes,
        oauth_fixed_lifetime_seconds=oauth_fixed_lifetime_seconds,
        refresh_skew_seconds=refresh_skew_seconds,
        request_timeout_seconds=request_timeout_seconds,
        max_body_bytes=max_body_bytes,
        max_response_bytes=max_response_bytes,
        bounds=bounds,
        model_mappings=model_mappings,
    )
