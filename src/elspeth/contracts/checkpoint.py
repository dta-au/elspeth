"""Checkpoint and recovery domain contracts.

These types are used for checkpoint validation and resume operations.
They are NOT persisted to the audit trail (those are in audit.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from elspeth.contracts.audit import Checkpoint
from elspeth.contracts.barrier_scalars import BarrierScalars
from elspeth.contracts.freeze import require_int


@dataclass(frozen=True, slots=True)
class CheckpointDraft:
    """Persistence-ready checkpoint input.

    The topology hash is computed before this reaches the repository layer so
    checkpoint persistence can stay decoupled from graph topology and hashing
    policy.
    """

    run_id: str
    sequence_number: int
    upstream_topology_hash: str
    barrier_scalars: BarrierScalars | None = None
    format_version: int = Checkpoint.CURRENT_FORMAT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str):
            raise TypeError(f"CheckpointDraft.run_id must be str, got {type(self.run_id).__name__}: {self.run_id!r}")
        if not self.run_id:
            raise ValueError("CheckpointDraft.run_id must not be empty")
        require_int(self.sequence_number, "CheckpointDraft.sequence_number", min_value=0)
        if not isinstance(self.upstream_topology_hash, str):
            raise TypeError(
                "CheckpointDraft.upstream_topology_hash must be str, "
                f"got {type(self.upstream_topology_hash).__name__}: {self.upstream_topology_hash!r}"
            )
        if not self.upstream_topology_hash:
            raise ValueError("CheckpointDraft.upstream_topology_hash must not be empty")
        if self.barrier_scalars is not None and not isinstance(self.barrier_scalars, BarrierScalars):
            raise TypeError(f"CheckpointDraft.barrier_scalars must be BarrierScalars or None, got {type(self.barrier_scalars).__name__}")
        require_int(self.format_version, "CheckpointDraft.format_version", min_value=0)


class ResumeRefusalCause(StrEnum):
    """Cause observed by a resume, abandon, or export admission decision."""

    RUN_NOT_FOUND = "run_not_found"
    RUN_TERMINAL = "run_terminal"
    RUN_NOT_RUNNING = "run_not_running"
    LEADER_LIVE = "leader_live"
    RUN_NOT_FINALIZED = "run_not_finalized"
    CHECKPOINT_MISSING = "checkpoint_missing"
    CHECKPOINT_NOT_LATEST = "checkpoint_not_latest"
    CHECKPOINT_FORMAT_MISSING = "checkpoint_format_missing"
    CHECKPOINT_FORMAT_INCOMPATIBLE = "checkpoint_format_incompatible"
    CHECKPOINT_TOPOLOGY_CHANGED = "checkpoint_topology_changed"
    PLUGIN_IMPLEMENTATION_CHANGED = "plugin_implementation_changed"
    FIRST_EFFECT_BOUNDARY_CROSSED = "first_effect_boundary_crossed"
    TERMINAL_STATUS_CHANGED = "terminal_status_changed"
    SOURCE_NOT_EXHAUSTED = "source_not_exhausted"
    UNCERTAIN_REMOTE_EFFECT = "uncertain_remote_effect"
    GROUP_UNSATISFIABLE = "group_unsatisfiable"
    EXPORT_ALREADY_COMPLETED = "export_already_completed"


@dataclass(frozen=True, slots=True)
class ResumeCheck:
    """Result of checking if a run can be resumed.

    Used by RecoveryManager and CheckpointCompatibilityValidator to
    communicate whether resume is possible and why/why not.
    """

    can_resume: bool
    reason: str | None = None
    cause: ResumeRefusalCause | None = None

    def __post_init__(self) -> None:
        if self.can_resume and self.reason is not None:
            raise ValueError("can_resume=True should not have a reason")
        if not self.can_resume and self.reason is None:
            raise ValueError("can_resume=False must have a reason explaining why")
        if self.can_resume and self.cause is not None:
            raise ValueError("can_resume=True must not have a cause")
        if not self.can_resume and not isinstance(self.cause, ResumeRefusalCause):
            raise ValueError("can_resume=False must have a ResumeRefusalCause")


@dataclass(frozen=True, slots=True)
class ResumePoint:
    """Information needed to resume a run.

    Contains all the data needed by Orchestrator.resume() to continue
    processing from where a failed run left off.
    """

    checkpoint: Checkpoint
    sequence_number: int
    barrier_scalars: BarrierScalars | None = None

    def __post_init__(self) -> None:
        """Validate resume point fields — Tier 1 crash on invalid data.

        Per the three-tier trust model (see
        docs/guides/data-trust-and-error-handling.md §The Three-Tier Trust Model),
        checkpoints are Tier 1 audit data. Wrong types indicate corrupted
        checkpoint data — crash immediately with distinct error messages.
        """
        if not isinstance(self.checkpoint, Checkpoint):
            raise TypeError(f"ResumePoint.checkpoint must be Checkpoint, got {type(self.checkpoint).__name__}")
        require_int(self.sequence_number, "ResumePoint.sequence_number", min_value=0)
        if self.barrier_scalars is not None and not isinstance(self.barrier_scalars, BarrierScalars):
            raise TypeError(f"ResumePoint.barrier_scalars must be BarrierScalars or None, got {type(self.barrier_scalars).__name__}")
        # Invariant: the duplicated field must match the embedded Checkpoint.
        # It exists for convenience access but is derived data, not an
        # independent input. Mismatch = corrupted construction.
        if self.sequence_number != self.checkpoint.sequence_number:
            raise ValueError(
                f"ResumePoint.sequence_number ({self.sequence_number}) does not match "
                f"checkpoint.sequence_number ({self.checkpoint.sequence_number})"
            )
