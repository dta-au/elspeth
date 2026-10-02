"""Terminal reconciliation owns a seat without reopening the engine run."""

import json

import pytest
from sqlalchemy import select

from elspeth.contracts import RunStatus
from elspeth.contracts.coordination import mint_worker_id
from elspeth.core.checkpoint.recovery import NonResumableRunError
from elspeth.core.landscape.schema import run_coordination_events_table, runs_table
from tests.fixtures.landscape import leader_token_for, make_factory, make_landscape_db


@pytest.mark.parametrize("status", [RunStatus.FAILED, RunStatus.INTERRUPTED, RunStatus.COMPLETED])
def test_reconciliation_seat_preserves_terminal_status_and_records_purpose(status: RunStatus) -> None:
    with make_landscape_db() as db:
        factory = make_factory(db)
        factory.run_lifecycle.begin_run({}, "v1", run_id="reconcile")
        owner = leader_token_for(db, "reconcile")
        factory.run_lifecycle.complete_run(status, coordination_token=owner)
        factory.run_coordination.release_seat(token=owner)
        with db.engine.connect() as conn:
            before = conn.execute(select(runs_table)).one()
        reconciler = factory.run_coordination.acquire_reconciliation_leadership(
            run_id="reconcile",
            worker_id=mint_worker_id("reconcile"),
            window_seconds=30,
            expected_status=status,
        )
        with db.engine.connect() as conn:
            assert conn.execute(select(runs_table)).one() == before
            events = conn.execute(
                select(run_coordination_events_table).where(run_coordination_events_table.c.worker_id == reconciler.worker_id)
            ).all()
        assert all(json.loads(event.context_json)["entry_point"] == "reconciliation" for event in events)
        if status is not RunStatus.COMPLETED:
            with pytest.raises(NonResumableRunError):
                factory.run_coordination.acquire_run_leadership(
                    run_id="reconcile", worker_id=mint_worker_id("reconcile"), window_seconds=30
                )
        factory.run_coordination.release_seat(token=reconciler)


def test_changed_terminal_snapshot_refuses_without_coordination_writes() -> None:
    with make_landscape_db() as db:
        factory = make_factory(db)
        factory.run_lifecycle.begin_run({}, "v1", run_id="reconcile")
        owner = leader_token_for(db, "reconcile")
        factory.run_lifecycle.complete_run(RunStatus.INTERRUPTED, coordination_token=owner)
        factory.run_coordination.release_seat(token=owner)
        with db.engine.connect() as conn:
            before = conn.execute(select(run_coordination_events_table)).all()
        with pytest.raises(NonResumableRunError):
            factory.run_coordination.acquire_reconciliation_leadership(
                run_id="reconcile",
                worker_id=mint_worker_id("reconcile"),
                window_seconds=30,
                expected_status=RunStatus.FAILED,
            )
        with db.engine.connect() as conn:
            assert conn.execute(select(run_coordination_events_table)).all() == before
