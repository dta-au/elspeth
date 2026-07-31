"""Closed, persistence-safe contracts for compatible web overlap.

Version 1 covers membership, operation and run fences, typed run-start
permits, the cross-database start saga, atomic baseline creation,
cancellation, recovery, bounded cleanup claims, and execution-authority
checks. An incompatible semantic or persisted-state change in any of those
areas must increment :data:`WEB_COORDINATION_PROTOCOL_VERSION` and use a hard
maintenance cut. Compatible telemetry and provider/documentation-only work do
not bump the protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import final

from elspeth.contracts.session_operation import (
    SessionOperationContext as SessionOperationContext,
)
from elspeth.contracts.session_operation import SessionOperationFence as SessionOperationFence
from elspeth.contracts.session_operation import SessionOperationKind as SessionOperationKind

WEB_COORDINATION_PROTOCOL_VERSION = 1

PROTOCOL_BUMP_REQUIRED_CHANGES: frozenset[str] = frozenset(
    {
        "membership",
        "session_operation_fence",
        "run_ownership_fence",
        "typed_run_start_permit",
        "run_start_saga",
        "atomic_baseline",
        "cancellation",
        "recovery",
        "cleanup_claim",
        "execution_authority",
    }
)
PROTOCOL_BUMP_NOT_REQUIRED_CHANGES: frozenset[str] = frozenset(
    {
        "compatible_telemetry",
        "provider_only",
        "documentation_only",
    }
)


class InstanceState(StrEnum):
    ACTIVE = "active"
    DRAINING = "draining"
    STOPPED = "stopped"


class ArchiveDeleteReconciliation(StrEnum):
    CURRENT = "current"
    CONSUMED = "consumed"


class ArchiveManifestRelation(StrEnum):
    CURRENT_OPERATION = "current_operation"
    STALE_OPERATION = "stale_operation"


class SessionOperationLeaseDisposition(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    CONSUMED = "consumed"
    LOST = "lost"
    UNKNOWN = "unknown"


class StartPermitState(StrEnum):
    PENDING = "pending"
    START_PERMITTED = "start_permitted"
    CANCELLED_BEFORE_PERMIT = "cancelled_before_permit"


class RunSagaState(StrEnum):
    DRAFT = "draft"
    START_INTENT = "start_intent"
    START_PERMIT_ISSUED = "start_permit_issued"
    BASELINE_CHECKPOINTED = "baseline_checkpointed"
    RUNNING = "running"
    RECOVERY_REQUIRED = "recovery_required"
    CANCEL_PENDING = "cancel_pending"
    TERMINAL = "terminal"
    TERMINAL_CANCELLED = "terminal_cancelled"


class CancellationSource(StrEnum):
    USER = "user"
    OPERATOR = "operator"
    SHUTDOWN = "shutdown"
    RECONCILER = "reconciler"


class RecoveryRequiredReason(StrEnum):
    IMPLEMENTATION_DRIFT = "implementation_drift"
    GENERATION_DRIFT = "generation_drift"
    COMPATIBILITY_MISMATCH = "compatibility_mismatch"
    MISSING_BASELINE = "missing_baseline"
    INCOMPLETE_SOURCE = "incomplete_source"
    SECRET_VERSION_UNAVAILABLE = "secret_version_unavailable"
    UNSAFE_EFFECT = "unsafe_effect"
    AUTHORITY_LOST = "authority_lost"
    UNKNOWN = "unknown"


class FenceLossReason(StrEnum):
    MISSING = "missing"
    STALE_EPOCH = "stale_epoch"
    TOKEN_MISMATCH = "token_mismatch"
    LEASE_EXPIRED = "lease_expired"
    RELEASED = "released"
    OWNER_INACTIVE = "owner_inactive"


def _require_nonblank(value: object, field_name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a nonblank exact string")


def _require_positive_int(value: object, field_name: str) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{field_name} must be a positive exact integer")


@final
@dataclass(frozen=True, slots=True)
class CompatibilityKey:
    session_epoch: int
    landscape_epoch: int
    coordination_protocol: int

    def __post_init__(self) -> None:
        _require_positive_int(self.session_epoch, "CompatibilityKey.session_epoch")
        _require_positive_int(self.landscape_epoch, "CompatibilityKey.landscape_epoch")
        _require_positive_int(self.coordination_protocol, "CompatibilityKey.coordination_protocol")


@final
@dataclass(frozen=True, slots=True)
class RunOwnershipFence:
    run_id: str
    owner_instance_id: str
    owner_epoch: int

    def __post_init__(self) -> None:
        _require_nonblank(self.run_id, "RunOwnershipFence.run_id")
        _require_nonblank(self.owner_instance_id, "RunOwnershipFence.owner_instance_id")
        _require_positive_int(self.owner_epoch, "RunOwnershipFence.owner_epoch")


class _LeakSafeCoordinationError(RuntimeError):
    """Low-cardinality error base that accepts no identifying context."""

    _AUTHORITY = "coordination"

    def __init__(self, reason: FenceLossReason) -> None:
        if type(reason) is not FenceLossReason:
            raise TypeError("reason must be a FenceLossReason")
        self.reason = reason
        super().__init__(f"{self._AUTHORITY} authority lost: {reason.value}")


class SessionOperationFenceLost(_LeakSafeCoordinationError):
    _AUTHORITY = "session operation"


class SessionOperationTerminalOutcomeUnknown(RuntimeError):
    """Leak-safe terminal error for an archive outcome the authority cannot prove."""

    def __init__(self) -> None:
        super().__init__("session operation terminal outcome is unknown")


class RunOwnershipFenceLost(_LeakSafeCoordinationError):
    _AUTHORITY = "run ownership"


class CleanupClaimLost(_LeakSafeCoordinationError):
    _AUTHORITY = "sessions cleanup"
