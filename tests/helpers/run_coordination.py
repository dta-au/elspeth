"""Test helper: mint a run's epoch-1 seat in its own transaction.

Production has exactly one seat-minting composition — ``begin_run`` calls
``RunCoordinationRepository.register_run_leader_on`` inside ITS transaction
so the ``runs`` INSERT and the seat commit atomically (the Task 8B epoch-one
exception pinned by ``test_web_landscape_mutation_fencing``). Repository-level
tests that want a seat without a run-lifecycle walk used to call a standalone
``register_run_leader`` wrapper on the repository; that wrapper was a second
public minting surface with zero production callers, so it was deleted and
this helper composes the same ``begin_write`` + ``register_run_leader_on``
pair from the test side.
"""

from __future__ import annotations

from datetime import datetime

from elspeth.contracts.coordination import CoordinationToken
from elspeth.core.landscape.database import begin_write
from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository


def register_run_leader(
    repo: RunCoordinationRepository,
    *,
    run_id: str,
    worker_id: str,
    now: datetime,
    window_seconds: float,
    entry_point: str = "run",
) -> CoordinationToken:
    """Mint ``run_id``'s epoch-1 seat on ``repo`` in a standalone transaction."""
    if type(repo) is not RunCoordinationRepository:
        raise TypeError("repo must be an exact RunCoordinationRepository")
    with begin_write(repo._engine) as conn:
        return repo.register_run_leader_on(
            conn,
            run_id=run_id,
            worker_id=worker_id,
            now=now,
            window_seconds=window_seconds,
            entry_point=entry_point,
        )
