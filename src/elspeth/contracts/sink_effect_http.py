"""Endpoint-fixed audited HTTP authority composed for one member attempt."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.freeze import freeze_fields
from elspeth.contracts.hashing import canonical_json

if TYPE_CHECKING:
    from elspeth.contracts.audit_protocols import CallRecorder
    from elspeth.contracts.contexts import RateLimitRegistryProtocol
    from elspeth.contracts.events import TelemetryEvent

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class SinkEffectHTTPPostRequest:
    """JSON only; endpoint, headers, credentials and limits are factory-owned."""

    json_body: Mapping[str, object]

    def __post_init__(self) -> None:
        freeze_fields(self, "json_body")
        if type(self.json_body) is not MappingProxyType:
            raise TypeError("json_body must be a JSON object")
        try:
            canonical_json(self.json_body)
        except (TypeError, ValueError) as exc:
            raise ValueError("json_body must contain finite JSON data") from exc


@dataclass(frozen=True, slots=True)
class SinkEffectHTTPPostResponse:
    status_code: int
    content_type: str | None
    body: bytes
    call_id: str
    request_ref: str
    response_ref: str

    def __post_init__(self) -> None:
        if type(self.status_code) is not int:
            raise TypeError("status_code must be an integer")
        if not 100 <= self.status_code <= 599:
            raise ValueError("status_code is outside the HTTP status range")
        if self.content_type is not None and type(self.content_type) is not str:
            raise TypeError("content_type must be str or None")
        if type(self.body) is not bytes:
            raise TypeError("body must be bytes")
        if type(self.call_id) is not str or not self.call_id:
            raise ValueError("call_id must be a nonempty audited call identity")
        for name, value in (("request_ref", self.request_ref), ("response_ref", self.response_ref)):
            if type(value) is not str or _DIGEST.fullmatch(value) is None:
                raise ValueError(f"{name} must be an actual lowercase SHA-256 payload reference")


@dataclass(frozen=True, slots=True, repr=False)
class SinkEffectHTTPEnvironment:
    """Current executor environment; never passed to the sink adapter."""

    telemetry_emit: Callable[[TelemetryEvent], None]
    rate_limit_registry: RateLimitRegistryProtocol | None = None

    def __post_init__(self) -> None:
        if not callable(self.telemetry_emit):
            raise TypeError("telemetry_emit must be callable")


@dataclass(frozen=True, slots=True, repr=False)
class SinkEffectHTTPBindContext:
    recorder: CallRecorder
    run_id: str
    operation_id: str
    coordination_token: CoordinationToken
    telemetry_emit: Callable[[TelemetryEvent], None]
    before_send: Callable[[], None]
    rate_limit_registry: RateLimitRegistryProtocol | None = None

    def __post_init__(self) -> None:
        if type(self.run_id) is not str or not self.run_id:
            raise ValueError("run_id must be nonempty")
        if type(self.operation_id) is not str or not self.operation_id:
            raise ValueError("operation_id must be nonempty")
        if type(self.coordination_token) is not CoordinationToken or self.coordination_token.run_id != self.run_id:
            raise TypeError("coordination_token must bind the current run")
        if not callable(self.telemetry_emit) or not callable(self.before_send):
            raise TypeError("telemetry_emit and before_send must be callable")


class SinkEffectHTTPPost(ABC):
    @abstractmethod
    def post_json(self, request: SinkEffectHTTPPostRequest) -> SinkEffectHTTPPostResponse:
        """Send one bounded JSON request under the bound attempt authority."""


class SinkEffectHTTPPostFactory(ABC):
    @property
    @abstractmethod
    def safe_config_fingerprint(self) -> str:
        """Fingerprint of the exact safe configuration owned by this factory."""

    @abstractmethod
    def bind(self, context: SinkEffectHTTPBindContext) -> SinkEffectHTTPPost:
        """Create a scoped capability without credentials, clients or I/O."""


class HTTPSinkEffectCapability:
    """Nominal declaration requiring an admitted composition HTTP factory."""
