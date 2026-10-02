"""A row whose claim died mid-row is re-driven by resume, never silently lost.

RULINGS 2026-09-26 Q2 (q2-systems information gap, measured) and the lane-owner
decision on E1's needs_ruling (option A2). When a claim raises out of its
traversal, the drain's exception arm marks the work item FAILED and the worker
exits; nothing records an outcome for that token. Measured before E1:
``Orchestrator.resume`` returned ``status=completed`` and the token had no
``token_outcomes`` row. E1 made ``complete_run`` refuse a success stamp over
such an item; that alone left the run with no exit (resume and abandon both
refused, measured).

Resume now returns each FAILED item whose token has no completed outcome to
READY through a recorded ``resume_requeue_failed`` scheduler event and
re-drives it at the collision-free claim base: the row reaches its outcome
once the cause is fixed, and a resume whose cause is not fixed fails loudly
again with nothing stamped COMPLETED.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from elspeth.contracts import RunStatus
from elspeth.contracts.config import RuntimeRetryConfig
from elspeth.contracts.coordination import DEFAULT_RUN_LIVENESS_WINDOW_SECONDS
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.scheduler import SchedulerEventType, TokenWorkStatus
from elspeth.contracts.tier_registry import FrameworkBugError
from elspeth.core.landscape.database_clock import read_landscape_transaction_time
from elspeth.core.landscape.schema import (
    node_states_table,
    run_workers_table,
    runs_table,
    scheduler_events_table,
    token_outcomes_table,
    token_work_items_table,
)
from elspeth.engine.clock import MockClock
from elspeth.engine.orchestrator.follower import build_follower_processor
from tests.e2e.recovery import harness
from tests.e2e.recovery.harness import (
    _DEFAULT_LEASE_SECONDS,
    _SOURCE_ROWS,
    _T0,
    _build_pipeline,
    _CrashedRun,
    _resume,
    _run_to_interrupted_checkpoint,
)
from tests.e2e.recovery.test_follower_join_and_drain import _join_follower, _seat_run_with_live_leader, _seed_ready_row
from tests.fixtures.landscape import member_token_for


def _follower_dies_mid_row(tmp_path: Path) -> tuple[_CrashedRun, str]:
    """A real follower claims a READY row and its transform raises Tier-1 mid-row; then the leader dies too.

    Returns the crashed run and the token whose FAILED item has no outcome.
    """
    clock = MockClock(start=_T0)
    crashed = _run_to_interrupted_checkpoint(tmp_path, clock)
    clock.advance(_DEFAULT_LEASE_SECONDS + 60)
    leader_token = _seat_run_with_live_leader(crashed, leader_id=f"worker:{crashed.run_id}:leader")
    follower_id = _join_follower(crashed, leader_token)
    token_id, _work_item_id = _seed_ready_row(crashed, ingest_sequence=7)

    config, graph, _sink, _source = _build_pipeline(_SOURCE_ROWS)
    member = member_token_for(crashed.db.engine, run_id=crashed.run_id, worker_id=follower_id)
    follower = build_follower_processor(
        factory=crashed.factory,
        member_token=member,
        graph=graph,
        config=config,
        payload_store=crashed.payload_store,
        clock=clock,
        retry_config=RuntimeRetryConfig(max_attempts=3, base_delay=0.01, max_delay=0.1, jitter=0.0, exponential_base=2.0),
    )
    (transform,) = config.transforms

    def tier_1_mid_row(row: Any, ctx: Any) -> Any:
        raise FrameworkBugError("follower-side Tier-1 mid-row")

    ctx = PluginContext(
        run_id=crashed.run_id,
        config={},
        landscape=crashed.factory.plugin_audit_writer(),
        payload_store=crashed.payload_store,
        member_token=member,
    )
    transform.on_start(ctx)
    with (
        pytest.MonkeyPatch.context() as patch,
        pytest.raises(FrameworkBugError, match="follower-side Tier-1 mid-row"),
    ):
        patch.setattr(transform, "process", tier_1_mid_row)
        follower._processor.drain_follower_ready_work(ctx)

    # The dead follower's registry heartbeat lapses; the leader crashes too.
    with crashed.db.engine.begin() as conn:
        stale = read_landscape_transaction_time(conn) - timedelta(seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS + 1)
        conn.execute(run_workers_table.update().where(run_workers_table.c.worker_id == follower_id).values(heartbeat_expires_at=stale))
    crashed.factory.run_coordination.release_seat(token=leader_token)
    with crashed.db.engine.begin() as conn:
        conn.execute(runs_table.update().where(runs_table.c.run_id == crashed.run_id).values(status=RunStatus.FAILED.value))
    assert _items(crashed, token_id) == [(TokenWorkStatus.FAILED.value, 1)]
    assert _completed_outcomes(crashed, token_id) == []
    return crashed, token_id


def _items(crashed: _CrashedRun, token_id: str) -> list[tuple[str, int]]:
    with crashed.db.engine.connect() as conn:
        rows = conn.execute(
            select(token_work_items_table.c.status, token_work_items_table.c.attempt).where(token_work_items_table.c.token_id == token_id)
        ).all()
    return [(str(row.status), int(row.attempt)) for row in rows]


def _completed_outcomes(crashed: _CrashedRun, token_id: str) -> list[tuple[str | None, str]]:
    with crashed.db.engine.connect() as conn:
        rows = conn.execute(
            select(token_outcomes_table.c.outcome, token_outcomes_table.c.path)
            .where(token_outcomes_table.c.token_id == token_id)
            .where(token_outcomes_table.c.completed == 1)
        ).all()
    return [(row.outcome, str(row.path)) for row in rows]


def _requeues(crashed: _CrashedRun, token_id: str) -> list[tuple[str, str, int, int]]:
    with crashed.db.engine.connect() as conn:
        rows = conn.execute(
            select(
                scheduler_events_table.c.from_status,
                scheduler_events_table.c.to_status,
                scheduler_events_table.c.from_attempt,
                scheduler_events_table.c.to_attempt,
            )
            .where(scheduler_events_table.c.token_id == token_id)
            .where(scheduler_events_table.c.event_type == SchedulerEventType.RESUME_REQUEUE_FAILED.value)
            .order_by(scheduler_events_table.c.seq)
        ).all()
    return [(str(row.from_status), str(row.to_status), int(row.from_attempt), int(row.to_attempt)) for row in rows]


def _node_state_attempts(crashed: _CrashedRun, token_id: str) -> list[tuple[int, str]]:
    with crashed.db.engine.connect() as conn:
        rows = conn.execute(
            select(node_states_table.c.attempt, node_states_table.c.status)
            .where(node_states_table.c.token_id == token_id)
            .where(node_states_table.c.node_id.like("transform_%"))
            .order_by(node_states_table.c.attempt)
        ).all()
    return [(int(row.attempt), str(row.status)) for row in rows]


def _run_status(crashed: _CrashedRun) -> str:
    with crashed.db.engine.connect() as conn:
        return str(conn.execute(select(runs_table.c.status).where(runs_table.c.run_id == crashed.run_id)).scalar_one())


@pytest.mark.timeout(120)
def test_resume_redrives_a_row_whose_follower_died_mid_row(tmp_path: Path) -> None:
    crashed, token_id = _follower_dies_mid_row(tmp_path)

    result, _sink, _source = _resume(crashed)

    assert result.status is RunStatus.COMPLETED
    assert _run_status(crashed) == RunStatus.COMPLETED.value
    assert _completed_outcomes(crashed, token_id) == [("success", "default_flow")]
    assert _requeues(crashed, token_id) == [(TokenWorkStatus.FAILED.value, TokenWorkStatus.READY.value, 1, 2)]
    assert _items(crashed, token_id) == [(TokenWorkStatus.TERMINAL.value, 2)]
    # The re-drive starts above the dead claim's recorded attempt.
    assert _node_state_attempts(crashed, token_id) == [(0, "failed"), (1, "completed")]
    crashed.db.close()


@pytest.mark.timeout(120)
def test_a_resume_whose_cause_is_not_fixed_fails_loudly_and_a_fixed_resume_completes(tmp_path: Path) -> None:
    crashed, token_id = _follower_dies_mid_row(tmp_path)

    def still_broken(self: Any, row: Any, ctx: Any) -> Any:
        raise FrameworkBugError("cause not fixed")

    with pytest.MonkeyPatch.context() as patch, pytest.raises(FrameworkBugError, match="cause not fixed"):
        patch.setattr(harness._PassthroughTransform, "process", still_broken)
        _resume(crashed)

    # Loud, and nothing stamped successful: the row is undecided again, one attempt higher.
    assert _run_status(crashed) == RunStatus.FAILED.value
    assert _completed_outcomes(crashed, token_id) == []
    assert _items(crashed, token_id) == [(TokenWorkStatus.FAILED.value, 2)]
    assert _node_state_attempts(crashed, token_id) == [(0, "failed"), (1, "failed")]

    result, _sink, _source = _resume(crashed)

    assert result.status is RunStatus.COMPLETED
    assert _completed_outcomes(crashed, token_id) == [("success", "default_flow")]
    assert _requeues(crashed, token_id) == [
        (TokenWorkStatus.FAILED.value, TokenWorkStatus.READY.value, 1, 2),
        (TokenWorkStatus.FAILED.value, TokenWorkStatus.READY.value, 2, 3),
    ]
    assert _items(crashed, token_id) == [(TokenWorkStatus.TERMINAL.value, 3)]
    assert _node_state_attempts(crashed, token_id) == [(0, "failed"), (1, "failed"), (2, "completed")]
    crashed.db.close()
