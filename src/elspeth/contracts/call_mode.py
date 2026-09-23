"""Owned replay/verify call-port contracts used across runtime layers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from elspeth.contracts.enums import CallStatus, CallType, RunMode
from elspeth.contracts.freeze import deep_freeze


@dataclass(frozen=True, slots=True)
class ReplayCallEvidence:
    """A retained source call suitable for exact typed reconstruction."""

    source_call_id: str
    status: CallStatus
    response_data: Mapping[str, Any] | None
    error_data: Mapping[str, Any] | None
    latency_ms: float | None

    def __post_init__(self) -> None:
        if not self.source_call_id:
            raise ValueError("ReplayCallEvidence.source_call_id is required")
        object.__setattr__(self, "response_data", deep_freeze(self.response_data))
        object.__setattr__(self, "error_data", deep_freeze(self.error_data))


@dataclass(frozen=True, slots=True)
class VerificationDecision:
    """Persisted comparison outcome for one current-run call."""

    current_call_id: str
    source_call_id: str | None
    is_match: bool | None
    differences: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.current_call_id:
            raise ValueError("VerificationDecision.current_call_id is required")
        if self.is_match is True and (self.source_call_id is None or self.differences):
            raise ValueError("A matching verification requires a source call and no differences")
        object.__setattr__(self, "differences", deep_freeze(self.differences))


@dataclass(frozen=True, slots=True)
class ReplaySSRFRequest:
    """Archived DNS pin for a syntax-valid URL, before network resolution."""

    original_url: str
    resolved_ip: str
    host_header: str
    port: int
    path: str
    scheme: str
    bare_hostname: str


@dataclass(frozen=True, slots=True)
class SourceCallParentIdentity:
    """Uniquely matched source audit parent for run-scoped request fields."""

    source_run_id: str
    source_node_id: str
    source_state_id: str | None
    source_operation_id: str | None
    source_token_id: str | None


@dataclass(frozen=True, slots=True)
class RuntimeRunMode:
    """Validated run mode and source run selected at admission."""

    mode: RunMode
    replay_from: str | None


@dataclass(frozen=True, slots=True)
class ArchivedCallRequestEvidence:
    """Exact parent/index source request for a typed semantic comparison."""

    source_call_id: str
    request_data: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.source_call_id:
            raise ValueError("ArchivedCallRequestEvidence.source_call_id is required")
        object.__setattr__(self, "request_data", deep_freeze(self.request_data))


class CallModeSession(Protocol):
    """Per-run call lookup and durable comparison authority.

    Adapters build their ordinary request DTO, then call this port before
    dispatch in replay mode. ``current_state_id`` or ``current_operation_id``
    binds the call to its audit parent; exactly one must be provided. The
    ``current_call_index`` is the index allocated by that parent. The session
    refuses ambiguous source matching and missing response payloads.
    """

    @property
    def mode(self) -> RunMode: ...

    @property
    def source_run_id(self) -> str: ...

    def replay_call(
        self,
        *,
        call_type: CallType,
        request_data: Mapping[str, Any],
        current_state_id: str | None,
        current_operation_id: str | None,
        current_call_index: int,
    ) -> ReplayCallEvidence: ...

    def replay_ssrf_request(
        self,
        *,
        original_url: str,
        call_type: CallType,
        current_state_id: str | None,
        current_operation_id: str | None,
    ) -> ReplaySSRFRequest:
        """Recover a recorded DNS pin without doing current DNS resolution."""
        ...

    def source_parent_identity(
        self,
        *,
        call_type: CallType,
        current_state_id: str | None,
        current_operation_id: str | None,
    ) -> SourceCallParentIdentity:
        """Bind current parent to one source parent before request matching."""
        ...

    def archived_call_request(
        self,
        *,
        call_type: CallType,
        current_state_id: str | None,
        current_operation_id: str | None,
        current_call_index: int,
    ) -> ArchivedCallRequestEvidence:
        """Read the exact source request without matching run-scoped fields."""
        ...

    def admit_verify_call(
        self,
        *,
        call_type: CallType,
        request_data: Mapping[str, Any],
        current_state_id: str | None,
        current_operation_id: str | None,
        current_call_index: int | None,
    ) -> str:
        """Require a unique source call before a verify adapter dispatches live I/O.

        An adapter that records its semantic call after a nested transport may
        omit the not-yet-allocated index only when request hash and parent
        identify exactly one source call.
        """
        ...

    def preflight_verify_request(
        self,
        *,
        call_type: CallType,
        request_data: Mapping[str, Any],
        current_state_id: str | None,
        current_operation_id: str | None,
    ) -> str:
        """Check a unique parent-local semantic request before nested transport."""
        ...

    def preflight_verify_http_managed_identity(
        self,
        *,
        request_data: Mapping[str, Any],
        current_state_id: str | None,
        current_operation_id: str | None,
    ) -> ArchivedCallRequestEvidence:
        """Require one archived MI HTTP request before DNS or token acquisition."""
        ...

    def preflight_verify_http_request(
        self,
        *,
        request_data: Mapping[str, Any],
        current_state_id: str | None,
        current_operation_id: str | None,
    ) -> ArchivedCallRequestEvidence:
        """Require one exact archived HTTP request before current DNS resolution."""
        ...

    def admit_verify_http_managed_identity(
        self,
        *,
        request_data: Mapping[str, Any],
        current_state_id: str | None,
        current_operation_id: str | None,
        current_call_index: int,
        source_call_id: str,
    ) -> str:
        """Bind the current MI request to source identity, excluding only rotating auth."""
        ...

    def preflight_verify_operation_http_managed_identity(
        self,
        *,
        request_data: Mapping[str, Any],
        current_operation_id: str,
    ) -> ArchivedCallRequestEvidence:
        """Bind a source-load HTTP request before credential acquisition."""
        ...

    def admit_verify_operation_http_managed_identity(
        self,
        *,
        request_data: Mapping[str, Any],
        current_operation_id: str,
        current_call_index: int,
        source_call_id: str,
    ) -> str:
        """Bind the recorded source-load call to its pre-token identity."""
        ...

    def verify_call(
        self,
        *,
        call_type: CallType,
        request_data: Mapping[str, Any],
        current_state_id: str | None,
        current_operation_id: str | None,
        current_call_index: int,
        current_call_id: str,
        live_status: CallStatus,
        live_response_data: Mapping[str, Any] | None,
        live_error_data: Mapping[str, Any] | None,
    ) -> VerificationDecision: ...

    def finalize(self) -> None:
        """Refuse unmatched or unconsumed source calls and failed comparisons."""
        ...
