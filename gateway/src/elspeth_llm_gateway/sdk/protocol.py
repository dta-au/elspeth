"""Adapter protocol and the InvokePlan safety boundary.

``InvokePlan`` is what an adapter hands back to the gateway core to describe
the upstream HTTP call it wants made. Because adapter code is third-party and
untrusted, the validators here are fail-closed: a path that could escape the
adapter's configured origin (an absolute path, a scheme, `..` traversal, a
query string, a fragment, or embedded whitespace) is rejected outright, and
header names that could smuggle credentials or spoof routing (Authorization,
Host, Cookie, X-Forwarded-For) are rejected case-insensitively.

``CLASSIFIABLE_CODES`` is a literal, standalone vocabulary: this module must
not import anything from ``elspeth_llm_gateway.core`` so that the SDK ships
independently to adapter authors. ``core`` is responsible for asserting, at
its own import time, that this set is a subset of its own error-code enum.
"""

import re
from typing import Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, model_validator

from elspeth_llm_gateway.sdk.types import CanonicalRequest, CanonicalResponse, Capability

CLASSIFIABLE_CODES: frozenset[str] = frozenset(
    {
        "context_length_exceeded",
        "content_policy_rejected",
        "upstream_rate_limited",
        "upstream_timeout",
        "upstream_unavailable",
        "upstream_response_invalid",
    }
)

_ADAPTER_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")

_FORBIDDEN_HEADER_NAMES = frozenset({"authorization", "host", "cookie", "x-forwarded-for"})

_MAX_HEADER_VALUE_LENGTH = 1024


class AdapterDescriptor(BaseModel):
    """Static identity and capabilities an adapter declares to the gateway."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: str
    adapter_api_major: int
    capabilities: frozenset[Capability]

    @model_validator(mode="after")
    def _check_name_and_capabilities(self) -> Self:
        if not _ADAPTER_NAME_RE.fullmatch(self.name):
            raise ValueError("name must match ^[a-z][a-z0-9_]{2,63}$")
        if Capability.TEXT not in self.capabilities:
            raise ValueError("capabilities must include Capability.TEXT")
        return self


class InvokePlan(BaseModel):
    """The upstream HTTP call an adapter wants the gateway core to make.

    Fail-closed by construction: ``path`` cannot escape the adapter's
    configured origin (no leading slash, no scheme, no `..`, no `?`/`#`, no
    whitespace) and headers cannot smuggle credentials or spoof routing
    (forbidden names rejected case-insensitively; values capped at 1024
    chars).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    headers: dict[str, str] = {}
    body: dict

    @model_validator(mode="after")
    def _check_path(self) -> Self:
        path = self.path
        if path.startswith("/"):
            raise ValueError("path must not start with '/'")
        if "://" in path:
            raise ValueError("path must not contain a scheme ('://')")
        if ".." in path:
            raise ValueError("path must not contain '..'")
        if "?" in path:
            raise ValueError("path must not contain '?'")
        if "#" in path:
            raise ValueError("path must not contain '#'")
        if any(ch.isspace() for ch in path):
            raise ValueError("path must not contain whitespace")
        return self

    @model_validator(mode="after")
    def _check_headers(self) -> Self:
        normalized: dict[str, str] = {}
        for name, value in self.headers.items():
            lowered = name.lower()
            if lowered in _FORBIDDEN_HEADER_NAMES:
                raise ValueError(f"header {lowered!r} is forbidden")
            if not isinstance(value, str):
                raise ValueError("header values must be str")
            if len(value) > _MAX_HEADER_VALUE_LENGTH:
                raise ValueError(f"header value for {lowered!r} exceeds {_MAX_HEADER_VALUE_LENGTH} chars")
            normalized[lowered] = value
        object.__setattr__(self, "headers", normalized)
        return self


class UpstreamFailure(BaseModel):
    """An upstream HTTP failure, with its already-bounded parsed body."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: int
    body: dict | None


class ErrorClassification(BaseModel):
    """An adapter's classification of an ``UpstreamFailure``.

    ``code`` must be one of ``CLASSIFIABLE_CODES`` — the closed set of
    outcomes an adapter is permitted to report. Anything else (e.g. an
    internal-error code) is not adapter-classifiable and is rejected.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    retryable: bool

    @model_validator(mode="after")
    def _check_code(self) -> Self:
        if self.code not in CLASSIFIABLE_CODES:
            raise ValueError(f"code must be one of {sorted(CLASSIFIABLE_CODES)}")
        return self


@runtime_checkable
class AdapterProtocol(Protocol):
    """The contract every gateway adapter must satisfy."""

    def descriptor(self) -> AdapterDescriptor: ...

    def validate_configuration(self, options: dict) -> None: ...

    def build_invoke(self, request: CanonicalRequest) -> InvokePlan: ...

    def parse_success(self, body: dict) -> CanonicalResponse: ...

    def classify_error(self, failure: UpstreamFailure) -> ErrorClassification: ...
