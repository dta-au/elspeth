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
