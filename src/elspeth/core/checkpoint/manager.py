"""CheckpointManager for creating and loading checkpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import asc, delete, desc, select

from elspeth.contracts import Checkpoint, CheckpointDraft
from elspeth.contracts.coordination import DEFAULT_RUN_LIVENESS_WINDOW_SECONDS, CoordinationToken
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.core.checkpoint.compatibility import IncompatibleCheckpointError as IncompatibleCheckpointError
from elspeth.core.checkpoint.serialization import checkpoint_dumps
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction
from elspeth.core.landscape.schema import checkpoints_table

_MAX_BARRIER_SCALARS_BYTES = 10_000_000


class CheckpointCorruptionError(Exception):
    """Raised when checkpoint data integrity verification fails.

    This indicates corruption in the audit trail - a Tier 1 failure that must
    be treated as unrecoverable, per the three-tier trust model
    (docs/guides/data-trust-and-error-handling.md §The Three-Tier Trust Model).
    """

    pass


def _validate_barrier_scalars_json_size(serialized: str) -> None:
    """Hard-fail size guard at the single checkpoint persistence boundary.

    Post-F1, the checkpoint carries only scalar barrier metadata (two float
    trigger latches per in-flight aggregation node plus lost-branch records
    per pending coalesce key) — payloads are tiny by construction. A payload
    anywhere near the limit indicates corrupted state construction upstream,
    not a large pipeline, so this is a crash, not a warning.
    """
    serialized_bytes = len(serialized.encode("utf-8"))
    if serialized_bytes <= _MAX_BARRIER_SCALARS_BYTES:
        return
    raise OrchestrationInvariantError(
        f"Checkpoint barrier_scalars size {serialized_bytes / 1_000_000:.1f}MB exceeds 10MB limit. "
        f"Barrier scalars carry only trigger latches and lost-branch records; "
        f"a payload this large indicates a bug in barrier state construction."
    )


def _validated_persisted_barrier_scalars_json(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"barrier_scalars_json must be str or None, got {type(value).__name__}")
    try:
        _validate_barrier_scalars_json_size(value)
    except OrchestrationInvariantError as exc:
        raise ValueError(f"barrier_scalars_json is invalid: {exc}") from exc
    return value


def _checkpoint_from_row(row: Any, *, requested_run_id: str) -> Checkpoint:
    try:
        return Checkpoint(
            checkpoint_id=row.checkpoint_id,
            run_id=row.run_id,
            sequence_number=row.sequence_number,
            created_at=row.created_at,
            upstream_topology_hash=row.upstream_topology_hash,
            barrier_scalars_json=_validated_persisted_barrier_scalars_json(row.barrier_scalars_json),
            format_version=row.format_version,  # None for legacy checkpoints
        )
    except ValueError as e:
        raise CheckpointCorruptionError(
            f"Checkpoint corruption detected for run '{requested_run_id}', checkpoint '{row.checkpoint_id}': {e}"
        ) from e


class CheckpointManager:
    """Manages checkpoint creation and retrieval.

    Checkpoints capture run progress at sink-durability boundaries,
    enabling resume after crash. Each checkpoint records:
    - A monotonic sequence number for ordering
    - The full-topology hash for compatibility validation
    - Optional scalar barrier metadata (BarrierScalars) for in-flight
      aggregation/coalesce barriers — buffered tokens themselves live in
      token_work_items journal BLOCKED rows (F1 durability unification)
    """

    def __init__(self, db: LandscapeDB) -> None:
        """Initialize with Landscape database.

        Args:
            db: LandscapeDB instance for storage
        """
        self._db = db

    def create_checkpoint(
        self,
        *,
        draft: CheckpointDraft,
        coordination_token: CoordinationToken,
    ) -> Checkpoint:
        """Create a checkpoint at current progress point.

        ADR-048 §2: the run being checkpointed IS ``coordination_token.run_id``;
        a draft addressed to any other run is refused before any database
        effect. ADR-030 §C.4 row 5: the verify-and-extend epoch fence is the
        FIRST statement of the write transaction, so a deposed leader's INSERT
        is refused before the duplicate-sequence guard or the UNIQUE
        constraint is even reached (both stay beneath as the durable
        backstop), with zero mutation and one ``fence_refusal`` event.

        Args:
            draft: Persistence-ready checkpoint data. The topology hash is
                computed by the orchestration/compatibility boundary before
                reaching this repository.
            coordination_token: The run's current leader token, carried by
                value from the seat mint (never re-read, never minted here).

        Returns:
            The created Checkpoint
        """
        if not isinstance(draft, CheckpointDraft):
            raise TypeError(f"draft must be CheckpointDraft, got {type(draft).__name__}")
        if draft.run_id != coordination_token.run_id:
            raise OrchestrationInvariantError(
                f"Checkpoint draft for run {draft.run_id!r} presented under a leader token for run "
                f"{coordination_token.run_id!r}; the token's run is the only run a checkpoint can belong to (ADR-048 §2)."
            )

        # All checkpoint data generation happens INSIDE transaction for atomicity
        with fenced_leader_transaction(
            self._db.engine,
            token=coordination_token,
            window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
            verb="create_checkpoint",
        ) as conn:
            existing_sequence = conn.execute(
                select(checkpoints_table.c.checkpoint_id)
                .where(
                    (checkpoints_table.c.run_id == coordination_token.run_id)
                    & (checkpoints_table.c.sequence_number == draft.sequence_number)
                )
                .limit(1)
            ).fetchone()
            if existing_sequence is not None:
                raise OrchestrationInvariantError(
                    f"Duplicate checkpoint sequence_number={draft.sequence_number} for run '{coordination_token.run_id}' "
                    f"would make resume ordering ambiguous; existing checkpoint={existing_sequence.checkpoint_id}"
                )

            # Generate IDs and timestamps within transaction boundary
            checkpoint_id = f"cp-{uuid.uuid4().hex}"
            created_at = datetime.now(UTC)

            # Serialize barrier scalars JSON.
            # checkpoint_dumps() handles:
            # - NaN/Infinity rejection for audit integrity (see the
            #   engine-patterns-reference skill §Canonical JSON)
            # Note: We don't use canonical_json because it normalizes floats to
            # integers, breaking round-trip for the float trigger-offset latches.
            scalars_json: str | None = None
            if draft.barrier_scalars is not None and draft.barrier_scalars.has_state:
                scalars_json = checkpoint_dumps(draft.barrier_scalars.to_dict())
                _validate_barrier_scalars_json_size(scalars_json)

            conn.execute(
                checkpoints_table.insert().values(
                    checkpoint_id=checkpoint_id,
                    run_id=coordination_token.run_id,
                    sequence_number=draft.sequence_number,
                    barrier_scalars_json=scalars_json,
                    created_at=created_at,
                    upstream_topology_hash=draft.upstream_topology_hash,
                    format_version=draft.format_version,
                )
            )
            # begin() auto-commits on clean exit, auto-rollbacks on exception

        return Checkpoint(
            checkpoint_id=checkpoint_id,
            run_id=coordination_token.run_id,
            sequence_number=draft.sequence_number,
            created_at=created_at,
            upstream_topology_hash=draft.upstream_topology_hash,
            barrier_scalars_json=scalars_json,
            format_version=draft.format_version,
        )

    def get_latest_checkpoint(self, run_id: str) -> Checkpoint | None:
        """Get the most recent checkpoint for a run.

        Args:
            run_id: The run to get checkpoint for

        Returns:
            Latest Checkpoint or None if no checkpoints exist

        This is a raw persistence read. Resume compatibility policy is enforced
        by CheckpointCompatibilityValidator, not by the repository boundary.
        """
        with self._db.engine.connect() as conn:
            result = conn.execute(
                select(checkpoints_table)
                .where(checkpoints_table.c.run_id == run_id)
                .order_by(desc(checkpoints_table.c.sequence_number))
                .limit(1)
            ).fetchone()

        if result is None:
            return None

        return _checkpoint_from_row(result, requested_run_id=run_id)

    def get_checkpoints(self, run_id: str) -> list[Checkpoint]:
        """Get all checkpoints for a run, ordered by sequence.

        Args:
            run_id: The run to get checkpoints for

        Returns:
            List of Checkpoints ordered by sequence_number
        """
        with self._db.engine.connect() as conn:
            results = conn.execute(
                select(checkpoints_table).where(checkpoints_table.c.run_id == run_id).order_by(asc(checkpoints_table.c.sequence_number))
            ).fetchall()

        checkpoints = []
        for r in results:
            checkpoints.append(_checkpoint_from_row(r, requested_run_id=run_id))
        return checkpoints

    def delete_checkpoints(self, *, coordination_token: CoordinationToken) -> int:
        """Delete all checkpoints of the run the token leads.

        Called after successful run completion to clean up. Checkpoints are deletable
        progress state — node_states.resume_checkpoint_id is a marker-only id (no FK),
        so the resume-provenance fact endures on node_states even after its checkpoint
        row is purged here.

        Args:
            coordination_token: The run's current leader token (ADR-048 §2:
                the run is ``coordination_token.run_id``). ADR-030 §C.4 row 5:
                the epoch fence is the transaction's first statement, so a
                deposed leader cannot destroy the new leader's resume anchors.

        Returns:
            Number of checkpoints deleted
        """
        with fenced_leader_transaction(
            self._db.engine,
            token=coordination_token,
            window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
            verb="delete_checkpoints",
        ) as conn:
            result = conn.execute(delete(checkpoints_table).where(checkpoints_table.c.run_id == coordination_token.run_id))
            # begin() auto-commits on clean exit, auto-rollbacks on exception
            return result.rowcount
