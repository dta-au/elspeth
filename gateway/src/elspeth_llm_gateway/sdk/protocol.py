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

``ModelTargetValidator`` is a deliberately *separate*, optional protocol
rather than a sixth member of ``AdapterProtocol``. ``AdapterProtocol`` is
``runtime_checkable``, so adding a member to its body would change what
``isinstance(adapter, AdapterProtocol)`` returns for an already-shipped
agency adapter — a silent break of the adapter API at the same
``ADAPTER_API_MAJOR``. Keeping it separate makes model-target validation
purely additive: existing adapters stay conformant and keep passing
readiness, while the conformance kit requires the method of any adapter
seeking image qualification.
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
            if lowered in normalized:
                raise ValueError(f"header name {lowered!r} collides with another header after lower-casing")
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


class ModelTargetValidator(Protocol):
    """Optional adapter extension: "can I actually use this model target?".

    A model mapping's value (``model_target``) is opaque to the gateway core
    — only the adapter knows what shape its own ``build_invoke`` will read out
    of it. Without this hook the core can only check that mappings *exist*,
    so a deployment configured with a target the adapter cannot consume passes
    readiness and then fails every completion. An adapter that implements
    this method gets each configured target handed to it once, at readiness,
    before the deployment is admitted.

    Contract:

    - ``target`` is a deep copy of one configured mapping value; mutating it
      has no effect on the live configuration.
    - Return ``None`` if the target is usable. Raise **any** exception if it
      is not — the core catches it and records a fixed readiness error code.
      The exception's message is never rendered into the readiness payload
      or any log line, so it may quote the target freely for local debugging;
      it will not be published.
    - The call must be purely computational: no I/O, no filesystem or network
      access, no credential or token lookup. ``/readyz`` makes no OAuth call
      and no upstream call, and this hook must not be what changes that.

    Not part of ``AdapterProtocol`` on purpose (see the module docstring):
    it is probed for by name, so omitting it is compatible, not fatal. The
    conformance kit nonetheless requires it — a derived image whose adapter
    cannot validate its own targets does not qualify.
    """

    def validate_model_target(self, target: dict) -> None: ...
