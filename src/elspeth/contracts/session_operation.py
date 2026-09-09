"""Layer-zero identity contracts for fenced session operations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import final


class SessionOperationKind(StrEnum):
    CREATE = "create"
    COMPOSE = "compose"
    PROPOSAL = "proposal"
    EXECUTE = "execute"
    ARCHIVE = "archive"
    PROGRESS = "progress"
    BLOB_READ = "blob_read"
    SESSION_FORK = "session_fork"


def _require_nonblank(value: object, field_name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a nonblank exact string")


def _require_positive_int(value: object, field_name: str) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{field_name} must be a positive exact integer")


@final
@dataclass(frozen=True, slots=True)
class SessionOperationFence:
    session_id: str
    operation_id: str
    lease_token: str
    operation_epoch: int

    def __post_init__(self) -> None:
        _require_nonblank(self.session_id, "SessionOperationFence.session_id")
        _require_nonblank(self.operation_id, "SessionOperationFence.operation_id")
        _require_nonblank(self.lease_token, "SessionOperationFence.lease_token")
        _require_positive_int(self.operation_epoch, "SessionOperationFence.operation_epoch")


@final
@dataclass(frozen=True, slots=True)
class SessionOperationContext:
    fence: SessionOperationFence
    operation_kind: SessionOperationKind

    def __post_init__(self) -> None:
        if type(self.fence) is not SessionOperationFence:
            raise TypeError("SessionOperationContext.fence must be an exact SessionOperationFence")
        if type(self.operation_kind) is not SessionOperationKind:
            raise TypeError("SessionOperationContext.operation_kind must be an exact SessionOperationKind")
