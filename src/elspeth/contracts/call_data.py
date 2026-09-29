"""Frozen dataclasses for LLM and HTTP call audit data.

Replaces loose ``dict[str, Any]`` at 23+ construction sites with typed,
immutable value objects that produce hash-stable dicts via ``to_dict()``.
Follows the ``TokenUsage`` precedent (commit dffe74a6).

Trust-tier notes
----------------
* Construction — used by our code (Tier 1/2).
* ``to_dict()`` — serialization boundary, produces identical dicts to
  the old inline dict construction for hash stability.
* ``raw_response`` on ``LLMCallResponse`` is intentionally ``dict[str, Any]``
  because it's Tier 3 SDK data that varies across providers/versions.

Deep immutability
-----------------
All mutable containers (``dict``, ``list``) are converted to immutable
equivalents (``MappingProxyType``, ``tuple``) in ``__post_init__``.
``to_dict()`` methods convert back to plain ``dict``/``list`` for wire
format stability.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Protocol, cast, get_args, runtime_checkable

from elspeth.contracts.composer_llm_audit import ComposerLLMProviderCostSource
from elspeth.contracts.freeze import deep_freeze, deep_thaw, freeze_fields, require_int
from elspeth.contracts.payload_store import IntegrityError
from elspeth.contracts.token_usage import TokenUsage

# ---------------------------------------------------------------------------
# Call payload protocol — satisfied by all 6 DTOs via structural subtyping
# ---------------------------------------------------------------------------


@runtime_checkable
class CallPayload(Protocol):
    """Protocol for typed external call payload data.

    All frozen call-data dataclasses (LLMCallRequest, HTTPCallResponse, etc.)
    satisfy this protocol structurally — no explicit inheritance needed.
    ``RawCallPayload`` wraps pre-serialized dicts from ``PluginContext``.
    """

    def to_dict(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class RawCallPayload:
    """Wrapper for pre-serialized call payload dicts from PluginContext.

    ``to_dict()`` returns a shallow copy to prevent callers from mutating
    the internal dict through the returned reference.
    """

    data: Mapping[str, Any]

    def __post_init__(self) -> None:
        freeze_fields(self, "data")

    def to_dict(self) -> dict[str, Any]:
        return {k: deep_thaw(v) for k, v in self.data.items()}


# ---------------------------------------------------------------------------
# LLM call data
# ---------------------------------------------------------------------------

# The output-budget parameter has two wire names: ``max_tokens`` and its
# successor ``max_completion_tokens``, which reasoning deployments require and
# which also counts reasoning tokens. The request records whichever name was
# sent, so both are reserved.
_LLM_MAX_TOKENS_PARAMS = frozenset({"max_tokens", "max_completion_tokens"})

_LLM_REQUEST_RESERVED_KEYS = frozenset({"model", "messages", "temperature", "provider"}) | _LLM_MAX_TOKENS_PARAMS


def _require_non_empty_str(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be str, got {type(value).__name__}: {value!r}")
    if not value.strip():
        raise ValueError(f"{field_name} must be non-empty")
    return value


def _require_str(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be str, got {type(value).__name__}: {value!r}")
    return value


def _require_finite_number(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{field_name} must be int or float, got {type(value).__name__}: {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite, got {value!r}")


def _require_mapping(value: object, field_name: str) -> None:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping, got {type(value).__name__}: {value!r}")


def _require_string_mapping(value: object, field_name: str) -> None:
    _require_mapping(value, field_name)
    assert isinstance(value, Mapping)
    for key, item in value.items():
        if type(key) is not str:
            raise TypeError(f"{field_name} key must be str, got {type(key).__name__}: {key!r}")
        if type(item) is not str:
            raise TypeError(f"{field_name}[{key!r}] must be str, got {type(item).__name__}: {item!r}")


def _require_message_sequence(value: object) -> None:
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        raise TypeError(f"messages must be a sequence of mappings, got {type(value).__name__}: {value!r}")


def _require_messages_tuple(value: object) -> None:
    if type(value) is not tuple:
        raise TypeError(f"messages must be tuple[Mapping[str, Any], ...], got {type(value).__name__}: {value!r}")
    for idx, message in enumerate(value):
        _require_mapping(message, f"messages[{idx}]")
        assert isinstance(message, Mapping)
        for key in message:
            if type(key) is not str:
                raise TypeError(f"messages[{idx}] key must be str, got {type(key).__name__}: {key!r}")


def _require_http_status_code(value: object, field_name: str, *, optional: bool = False) -> None:
    require_int(value, field_name, optional=optional, min_value=100)
    # require_int already proved value is int (or permitted None) — cast()
    # records that proof for the type checker without a redundant runtime
    # re-check of the first-party Tier-1 validation guarantee.
    checked = cast("int | None", value)
    if checked is not None and checked > 999:
        raise ValueError(f"{field_name} must be <= 999, got {checked!r}")


@dataclass(frozen=True, slots=True)
class LLMCallRequest:
    """Audit record for an outbound LLM API request."""

    model: str
    messages: Sequence[Mapping[str, Any]]
    # None = the request carried no temperature (provider default). Recorded
    # as absent, never as 0.0: the audit trail must not claim a value that
    # was not sent.
    temperature: float | None
    provider: str
    max_tokens: int | None = None
    # Wire name max_tokens was sent under (see _LLM_MAX_TOKENS_PARAMS).
    max_tokens_param: str = "max_tokens"
    extra_kwargs: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        _require_non_empty_str(self.model, "model")
        _require_message_sequence(self.messages)
        if self.temperature is not None:
            _require_finite_number(self.temperature, "temperature")
        _require_non_empty_str(self.provider, "provider")
        require_int(self.max_tokens, "max_tokens", optional=True, min_value=0)
        if self.max_tokens_param not in _LLM_MAX_TOKENS_PARAMS:
            raise ValueError(f"max_tokens_param must be one of {sorted(_LLM_MAX_TOKENS_PARAMS)}, got {self.max_tokens_param!r}")
        # Always deep-freeze inner message dicts — a pre-built tuple may
        # still contain mutable inner dicts (e.g. tuple([{"role": "user"}])).
        object.__setattr__(
            self,
            "messages",
            tuple(deep_freeze(m) for m in self.messages),
        )
        freeze_fields(self, "extra_kwargs")
        _require_messages_tuple(self.messages)
        _require_mapping(self.extra_kwargs, "extra_kwargs")
        if collisions := (_LLM_REQUEST_RESERVED_KEYS & self.extra_kwargs.keys()):
            msg = f"extra_kwargs contains reserved key(s) that would overwrite audit fields: {collisions}"
            raise ValueError(msg)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to audit-trail dict.

        Conditionally omits ``temperature`` and ``max_tokens`` when None,
        records ``max_tokens`` under the wire name it was sent with, and
        spreads ``extra_kwargs`` to match the old ``**kwargs`` pattern.
        """
        d: dict[str, Any] = {
            "model": self.model,
            "messages": [deep_thaw(m) for m in self.messages],
        }
        if self.temperature is not None:
            d["temperature"] = self.temperature
        d["provider"] = self.provider
        d.update(deep_thaw(self.extra_kwargs))
        if self.max_tokens is not None:
            d[self.max_tokens_param] = self.max_tokens
        return d


def _require_llm_pricing(
    pricing_model: str | None, provider_cost: float | None, provider_cost_source: ComposerLLMProviderCostSource
) -> None:
    if pricing_model is not None:
        _require_non_empty_str(pricing_model, "pricing_model")
        if not pricing_model.strip():
            raise ValueError("pricing_model must not be blank")
    if provider_cost_source not in get_args(ComposerLLMProviderCostSource):
        raise ValueError("provider_cost_source is not recognized")
    if provider_cost is not None:
        _require_finite_number(provider_cost, "provider_cost")
        if provider_cost < 0:
            raise ValueError("provider_cost must be nonnegative")
    if (provider_cost is None) != (provider_cost_source == "not_available"):
        raise ValueError("provider_cost and provider_cost_source must agree on availability")


@dataclass(frozen=True, slots=True)
class LLMCallResponse:
    """Audit record for an LLM API response."""

    content: str
    model: str
    usage: TokenUsage
    raw_response: Mapping[str, Any]
    pricing_model: str | None = None
    provider_cost: float | None = None
    provider_cost_source: ComposerLLMProviderCostSource = "not_available"

    def __post_init__(self) -> None:
        freeze_fields(self, "raw_response")
        _require_str(self.content, "content")
        _require_non_empty_str(self.model, "model")
        if type(self.usage) is not TokenUsage:
            raise TypeError(f"usage must be TokenUsage, got {type(self.usage).__name__}: {self.usage!r}")
        _require_mapping(self.raw_response, "raw_response")
        _require_llm_pricing(self.pricing_model, self.provider_cost, self.provider_cost_source)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to audit-trail dict.

        Calls ``self.usage.to_dict()`` for the usage field.
        """
        return {
            "content": self.content,
            "model": self.model,
            "usage": self.usage.to_dict(),
            "raw_response": deep_thaw(self.raw_response),
            "pricing_model": self.pricing_model,
            "provider_cost": self.provider_cost,
            "provider_cost_source": self.provider_cost_source,
        }


LLMErrorCategory = Literal[
    "rate_limit",
    "content_policy",
    "context_length",
    "server",
    "network",
    "client",
    "unknown",
    "response_processing",
]


@dataclass(frozen=True, slots=True)
class LLMCallError:
    """Audit record for an LLM API error."""

    type: str
    message: str
    retryable: bool
    pricing_model: str | None = None
    provider_cost: float | None = None
    provider_cost_source: ComposerLLMProviderCostSource = "not_available"
    category: LLMErrorCategory | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.type, "LLMCallError.type")
        _require_non_empty_str(self.message, "LLMCallError.message")
        if type(self.retryable) is not bool:
            raise TypeError(f"LLMCallError.retryable must be bool, got {type(self.retryable).__name__}: {self.retryable!r}")
        _require_llm_pricing(self.pricing_model, self.provider_cost, self.provider_cost_source)
        if self.category is not None and self.category not in get_args(LLMErrorCategory):
            raise ValueError(f"LLMCallError.category is not recognized: {self.category!r}")

    def to_dict(self) -> dict[str, Any]:
        """Serialize to audit-trail dict.

        All fields always present (retryable is never optional).
        """
        return {
            "type": self.type,
            "message": self.message,
            "retryable": self.retryable,
            "pricing_model": self.pricing_model,
            "provider_cost": self.provider_cost,
            "provider_cost_source": self.provider_cost_source,
            **({"category": self.category} if self.category is not None else {}),
        }


# ---------------------------------------------------------------------------
# HTTP call data
# ---------------------------------------------------------------------------


def encode_urlencoded_form(form: Sequence[tuple[str, str]]) -> bytes:
    """Encode ordered form fields for both the wire request and audit digest."""
    if not form:
        raise ValueError("form must contain at least one field")
    for pair in form:
        if type(pair) is not tuple or len(pair) != 2 or type(pair[0]) is not str or type(pair[1]) is not str or not pair[0]:
            raise ValueError("form fields must be nonempty names paired with string values")
    return urllib.parse.urlencode(form).encode("ascii")


_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")


def _multipart_header_text(value: str, *, field_name: str) -> str:
    if type(value) is not str or not value or len(value) > 200 or any(ord(char) < 32 or ord(char) > 126 for char in value):
        raise ValueError(f"multipart {field_name} must be 1-200 printable ASCII characters")
    return value


@dataclass(frozen=True, slots=True)
class MultipartPart:
    """One ordered text field or one content-addressed file field."""

    name: str
    value: str | None = None
    blob_ref: str | None = None
    filename: str | None = None
    content_type: str | None = None

    def __post_init__(self) -> None:
        _multipart_header_text(self.name, field_name="name")
        if (self.value is None) == (self.blob_ref is None):
            raise ValueError("multipart part needs exactly one of value or blob_ref")
        if self.value is not None:
            if type(self.value) is not str or self.filename is not None or self.content_type is not None:
                raise ValueError("multipart text part must contain only a string value")
            return
        if type(self.blob_ref) is not str or _SHA256_HEX.fullmatch(self.blob_ref) is None:
            raise ValueError("multipart blob_ref must be a lowercase SHA-256 hash")
        if self.filename is None or self.content_type is None:
            raise ValueError("multipart blob part requires filename and content_type")
        _multipart_header_text(self.filename, field_name="filename")
        if "/" in self.filename or "\\" in self.filename:
            raise ValueError("multipart filename must not contain path separators")
        _multipart_header_text(self.content_type, field_name="content_type")
        if "/" not in self.content_type:
            raise ValueError("multipart content_type must be a MIME type")

    def to_dict(self) -> dict[str, str]:
        if self.value is not None:
            return {"name": self.name, "value": self.value}
        if self.blob_ref is None or self.filename is None or self.content_type is None:
            raise ValueError("multipart blob part is incomplete")
        return {"name": self.name, "blob_ref": self.blob_ref, "filename": self.filename, "content_type": self.content_type}


@dataclass(frozen=True, slots=True)
class MultipartMetadata:
    """Versioned manifest and digest for exact multipart wire bytes."""

    parts: tuple[MultipartPart, ...]
    boundary: str
    body_sha256: str
    body_size: int

    def __post_init__(self) -> None:
        if not self.parts or any(type(part) is not MultipartPart for part in self.parts):
            raise ValueError("multipart metadata needs typed parts")
        if type(self.boundary) is not str or re.fullmatch(r"elspeth-[0-9a-f]{32}", self.boundary) is None:
            raise ValueError("multipart metadata has invalid boundary")
        if type(self.body_sha256) is not str or _SHA256_HEX.fullmatch(self.body_sha256) is None:
            raise ValueError("multipart metadata has invalid body hash")
        require_int(self.body_size, "body_size", min_value=1)

    @property
    def content_type(self) -> str:
        return f"multipart/form-data; boundary={self.boundary}"


def encode_multipart_form(
    parts: tuple[MultipartPart, ...], blobs: Mapping[str, bytes], *, max_body_bytes: int
) -> tuple[bytes, MultipartMetadata]:
    """Encode ordered parts with a reproducible boundary and a hard byte cap."""
    if not 1 <= len(parts) <= 256 or any(type(part) is not MultipartPart for part in parts):
        raise ValueError("multipart form needs 1 to 256 typed parts")
    if max_body_bytes <= 0:
        raise ValueError("max_body_bytes must be positive")
    if any(part.value is not None and len(part.value.encode("utf-8")) > max_body_bytes for part in parts):
        raise ValueError("multipart text part exceeds max_body_bytes")
    manifest = json.dumps([part.to_dict() for part in parts], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    boundary = "elspeth-" + hashlib.sha256(manifest).hexdigest()[:32]
    boundary_bytes = boundary.encode("ascii")
    body = bytearray()

    def append(chunk: bytes) -> None:
        if len(body) + len(chunk) > max_body_bytes:
            raise ValueError("multipart body exceeds max_body_bytes")
        body.extend(chunk)

    for part in parts:
        name = part.name.replace("\\", "\\\\").replace('"', '\\"')
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if part.value is not None:
            content = part.value.encode("utf-8")
        else:
            if part.blob_ref is None or part.filename is None or part.content_type is None:
                raise ValueError("multipart blob part is incomplete")
            content = blobs[part.blob_ref]
            if type(content) is not bytes or hashlib.sha256(content).hexdigest() != part.blob_ref:
                raise IntegrityError("Multipart blob bytes do not match the declared reference")
            filename = part.filename.replace("\\", "\\\\").replace('"', '\\"')
            disposition += f'; filename="{filename}"'
        if b"--" + boundary_bytes in content:
            raise ValueError("multipart content contains the generated boundary")
        append(b"--" + boundary_bytes + b"\r\n")
        append(disposition.encode("ascii") + b"\r\n")
        if part.content_type is not None:
            append(f"Content-Type: {part.content_type}\r\n".encode("ascii"))
        append(b"\r\n")
        append(content)
        append(b"\r\n")
    append(b"--" + boundary_bytes + b"--\r\n")
    encoded = bytes(body)
    return encoded, MultipartMetadata(
        parts=parts, boundary=boundary, body_sha256=hashlib.sha256(encoded).hexdigest(), body_size=len(encoded)
    )


@dataclass(frozen=True, slots=True)
class HTTPCallRequest:
    """Audit record for an outbound HTTP request.

    Handles three request shapes:

    * **Standard** (``resolved_ip`` is None): includes json/params by method.
    * **SSRF-safe** (``resolved_ip`` set, no hop): includes resolved_ip.
    * **Redirect hop** (``hop_number`` set): includes hop tracking fields.
      Successful hops include ``resolved_ip``; blocked pre-validation hops
      record ``redirect_from`` without fabricating a resolved IP.
    * **Audit-only metadata** (``audit_metadata`` set): includes request-linked
      provenance that was not sent over the wire.
    """

    method: str
    url: str
    headers: Mapping[str, str]
    json: Mapping[str, Any] | None = None
    form: tuple[tuple[str, str], ...] | None = None
    multipart: MultipartMetadata | None = None
    params: Mapping[str, Any] | None = None
    audit_metadata: Mapping[str, Any] | None = None
    resolved_ip: str | None = None
    hop_number: int | None = None
    redirect_from: str | None = None

    def __post_init__(self) -> None:
        freeze_fields(self, "headers", "json", "form", "params", "audit_metadata")
        method = _require_non_empty_str(self.method, "method")
        if method.upper() != method:
            raise ValueError(f"method must be uppercase, got {method!r}")
        _require_non_empty_str(self.url, "url")
        _require_string_mapping(self.headers, "headers")
        if self.form is not None:
            if method != "POST" or self.json is not None or self.multipart is not None:
                raise ValueError("form requires POST without a JSON body")
            encode_urlencoded_form(self.form)
        if self.multipart is not None and (method != "POST" or self.json is not None or self.form is not None):
            raise ValueError("multipart requires POST without another body")
        require_int(self.hop_number, "hop_number", optional=True, min_value=1)
        if self.resolved_ip is not None:
            _require_non_empty_str(self.resolved_ip, "resolved_ip")
        if self.redirect_from is not None:
            _require_non_empty_str(self.redirect_from, "redirect_from")
        if self.hop_number is not None and self.resolved_ip is None and self.redirect_from is None:
            msg = "hop_number without resolved_ip requires redirect_from for blocked redirect attempts"
            raise ValueError(msg)
        if self.redirect_from is not None and self.hop_number is None:
            msg = "redirect_from requires hop_number"
            raise ValueError(msg)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to audit-trail dict.

        SSRF fields (resolved_ip, hop_number, redirect_from) are additive —
        they never suppress other fields.  json/params are serialized for
        all request shapes using hash-stability rules: POST always emits
        json (even None), GET always emits params (even None), other
        methods emit when non-None.
        """
        d: dict[str, Any] = {"method": self.method, "url": self.url}
        if self.resolved_ip is not None:
            d["resolved_ip"] = self.resolved_ip
        if self.hop_number is not None:
            d["hop_number"] = self.hop_number
        if self.redirect_from is not None:
            d["redirect_from"] = self.redirect_from
        d["headers"] = dict(self.headers)
        # POST always emits json (even None) and GET always emits params
        # (even None) for hash stability with existing audit records.
        # All other methods emit json/params when non-None — no silent drops.
        if self.json is not None or self.method == "POST":
            d["json"] = deep_thaw(self.json) if self.json is not None else None
        if self.form is not None:
            d["form"] = deep_thaw(self.form)
            d["body_encoding"] = "urlencoded-v1"
            d["body_sha256"] = hashlib.sha256(encode_urlencoded_form(self.form)).hexdigest()
        if self.multipart is not None:
            d["multipart"] = [part.to_dict() for part in self.multipart.parts]
            d["body_encoding"] = "multipart-v1"
            d["body_sha256"] = self.multipart.body_sha256
            d["body_size"] = self.multipart.body_size
            d["body_boundary"] = self.multipart.boundary
        if self.params is not None or self.method == "GET":
            d["params"] = deep_thaw(self.params) if self.params is not None else None
        if self.audit_metadata is not None:
            d["audit_metadata"] = deep_thaw(self.audit_metadata)
        return d


@dataclass(frozen=True, slots=True)
class HTTPResponseTransport:
    """Exact observable HTTP response needed to reconstruct an ``httpx.Response``.

    This is present only when the response can be retained without bypassing
    the audit redaction policy. An absent transport means replay must fail
    closed, even when the parsed response body is available.
    """

    body_b64: str
    headers: tuple[tuple[str, str], ...]
    request_url: str
    logical_url: str | None = None
    redirect_hops: tuple[HTTPRedirectReplayHop, ...] = ()

    def __post_init__(self) -> None:
        _require_str(self.body_b64, "HTTPResponseTransport.body_b64")
        _require_non_empty_str(self.request_url, "HTTPResponseTransport.request_url")
        if self.logical_url is not None:
            _require_non_empty_str(self.logical_url, "HTTPResponseTransport.logical_url")
        if type(self.headers) is not tuple:
            raise TypeError("HTTPResponseTransport.headers must be a tuple")
        for name, value in self.headers:
            _require_non_empty_str(name, "HTTPResponseTransport.header name")
            _require_str(value, "HTTPResponseTransport.header value")
        if type(self.redirect_hops) is not tuple or any(type(hop) is not HTTPRedirectReplayHop for hop in self.redirect_hops):
            raise TypeError("HTTPResponseTransport.redirect_hops must contain HTTPRedirectReplayHop values")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "body_b64": self.body_b64,
            "headers": [[name, value] for name, value in self.headers],
            "request_url": self.request_url,
        }
        if self.logical_url is not None:
            result["logical_url"] = self.logical_url
        if self.redirect_hops:
            result["redirect_hops"] = [hop.to_dict() for hop in self.redirect_hops]
        return result


@dataclass(frozen=True, slots=True)
class HTTPCallResponse:
    """Audit record for an HTTP response.

    Redirect hop responses omit ``body_size`` and ``body`` (only
    ``status_code`` and ``headers`` are meaningful for intermediate hops).
    """

    status_code: int
    headers: Mapping[str, str]
    body_size: int | None = None
    body: Mapping[str, Any] | tuple[Any, ...] | str | None = None
    redirect_count: int = 0
    transport: HTTPResponseTransport | None = None

    def __post_init__(self) -> None:
        _require_http_status_code(self.status_code, "status_code")
        require_int(self.body_size, "body_size", optional=True, min_value=0)
        require_int(self.redirect_count, "redirect_count", min_value=0)
        if self.body is not None and self.body_size is None:
            raise ValueError(
                "HTTPCallResponse.body requires body_size — without it, "
                "to_dict() silently drops body from the audit record. "
                "Set body_size=len(content) or omit body for redirect hops."
            )
        if self.transport is not None and not isinstance(self.transport, HTTPResponseTransport):
            raise TypeError("HTTPCallResponse.transport must be HTTPResponseTransport")
        freeze_fields(self, "headers", "body")

    def to_dict(self) -> dict[str, Any]:
        """Serialize to audit-trail dict.

        Includes ``body_size``/``body`` when ``body_size`` is not None.
        Includes ``redirect_count`` when > 0.
        """
        d: dict[str, Any] = {
            "status_code": self.status_code,
            "headers": dict(self.headers),
        }
        if self.body_size is not None:
            d["body_size"] = self.body_size
            if type(self.body) in (MappingProxyType, dict, tuple):
                d["body"] = deep_thaw(self.body)
            else:
                d["body"] = self.body
        if self.redirect_count > 0:
            d["redirect_count"] = self.redirect_count
        if self.transport is not None:
            d["transport"] = self.transport.to_dict()
        return d


@dataclass(frozen=True, slots=True)
class HTTPRedirectReplayHop:
    """A recorded redirect's complete request and response audit payloads."""

    request: HTTPCallRequest
    response: HTTPCallResponse

    def __post_init__(self) -> None:
        if not isinstance(self.request, HTTPCallRequest) or not isinstance(self.response, HTTPCallResponse):
            raise TypeError("HTTPRedirectReplayHop requires HTTP call payloads")

    def to_dict(self) -> dict[str, Any]:
        return {"request": self.request.to_dict(), "response": self.response.to_dict()}


@dataclass(frozen=True, slots=True)
class HTTPCallError:
    """Audit record for an HTTP error.

    Network errors (timeout, connection refused) omit ``status_code``.
    HTTP errors (4xx/5xx) include ``status_code``.
    """

    type: str
    message: str
    status_code: int | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.type, "HTTPCallError.type")
        _require_non_empty_str(self.message, "HTTPCallError.message")
        _require_http_status_code(self.status_code, "status_code", optional=True)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to audit-trail dict.

        Includes ``status_code`` only when not None.
        """
        d: dict[str, Any] = {
            "type": self.type,
            "message": self.message,
        }
        if self.status_code is not None:
            d["status_code"] = self.status_code
        return d
