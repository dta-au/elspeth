"""Checkpoint test helpers."""

from __future__ import annotations

from elspeth.contracts import Checkpoint, CheckpointDraft
from elspeth.contracts.barrier_scalars import BarrierScalars
from elspeth.contracts.coordination import CoordinationToken
from elspeth.core.checkpoint import CheckpointCompatibilityValidator
from elspeth.core.checkpoint.manager import CheckpointManager
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository
from tests.fixtures.landscape import expire_leader_seat, leader_token_for


def checkpoint_draft(
    *,
    run_id: str,
    sequence_number: int,
    graph: ExecutionGraph,
    barrier_scalars: BarrierScalars | None = None,
) -> CheckpointDraft:
    """Build the persistence-ready checkpoint record expected by the manager."""
    return CheckpointDraft(
        run_id=run_id,
        sequence_number=sequence_number,
        barrier_scalars=barrier_scalars,
        upstream_topology_hash=CheckpointCompatibilityValidator().compute_full_topology_hash(graph),
    )


def create_checkpoint(
    checkpoint_manager: CheckpointManager,
    *,
    run_id: str,
    sequence_number: int,
    graph: ExecutionGraph,
    barrier_scalars: BarrierScalars | None = None,
    coordination_token: CoordinationToken | None = None,
) -> Checkpoint:
    """Create a checkpoint through the persistence-ready draft boundary.

    ``create_checkpoint`` is leader-fenced with no unfenced arm (ADR-048 §2).
    A caller that holds a specific token (a stale image, a takeover seat)
    passes it and the write behaves exactly as in production — the fence
    extends the seat. Otherwise the run's OWN seat is read back (ADR-048 §5,
    :func:`tests.fixtures.landscape.leader_token_for`) — never minted here —
    so a run whose seat is missing fails loudly instead of being written
    under a self-issued token; and when that seat had already LAPSED (the
    fixture's leader is dead: ``insert_crashed_leader_seat`` /
    ``expire_leader_seat``), it is lapsed again after the write, so the
    checkpoint reads as the dead leader's last act rather than reviving it
    and blocking the resume takeover the fixture exists to exercise.
    """
    if coordination_token is not None:
        token = coordination_token
        leader_was_dead = False
    else:
        db = checkpoint_manager._db
        token = leader_token_for(db, run_id)
        incumbent = RunCoordinationRepository(db.engine).live_leader(run_id=run_id)
        leader_was_dead = incumbent is not None and not incumbent.seat_live
    checkpoint = checkpoint_manager.create_checkpoint(
        draft=checkpoint_draft(
            run_id=run_id,
            sequence_number=sequence_number,
            graph=graph,
            barrier_scalars=barrier_scalars,
        ),
        coordination_token=token,
    )
    if leader_was_dead:
        expire_leader_seat(checkpoint_manager._db, run_id)
    return checkpoint
