"""A run is never stamped successful over a row whose claim died mid-row.

RULINGS 2026-09-26 Q2 (q2-systems information gap, measured). When a claim
raises out of its traversal, the drain's exception arm marks the work item
FAILED and the worker exits; nothing records an outcome for that token. FAILED
was absent from both finalisation backstops (the active-work count and
``complete_run``'s quiescence arm cover READY/LEASED/BLOCKED/PENDING_SINK), so
the leader's live finalisation stamped the run COMPLETED with the row silently
missing. Measured before the fix on this exact scenario: ``Orchestrator.resume``
returned ``status=completed`` and the token had no ``token_outcomes`` row.

``complete_run`` now refuses a SUCCESS status while a FAILED work item's token
has no completed outcome, in the same statement that stamps it; the resume
finalises FAILED instead, loudly.
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
from elspeth.contracts.errors import FrameworkBugError, OrchestrationInvariantError
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.core.landscape.database_clock import read_landscape_transaction_time
from elspeth.core.landscape.schema import run_workers_table, runs_table, token_outcomes_table, token_work_items_table
from elspeth.engine.clock import MockClock
from elspeth.engine.orchestrator.follower import build_follower_processor
from tests.e2e.recovery.harness import (
    _DEFAULT_LEASE_SECONDS,
    _SOURCE_ROWS,
    _T0,
    _build_pipeline,
    _resume,
    _run_to_interrupted_checkpoint,
)
from tests.e2e.recovery.test_follower_join_and_drain import _join_follower, _seat_run_with_live_leader, _seed_ready_row
from tests.fixtures.landscape import member_token_for


@pytest.mark.timeout(120)
def test_resume_refuses_to_complete_a_run_whose_follower_died_mid_row(tmp_path: Path) -> None:
    clock = MockClock(start=_T0)
    crashed = _run_to_interrupted_checkpoint(tmp_path, clock)
    clock.advance(_DEFAULT_LEASE_SECONDS + 60)
    leader_token = _seat_run_with_live_leader(crashed, leader_id=f"worker:{crashed.run_id}:leader")
    follower_id = _join_follower(crashed, leader_token)
    token_id, _work_item_id = _seed_ready_row(crashed, ingest_sequence=7)

    # A real follower claims the READY row; its transform raises a Tier-1
    # error mid-row, so the drain marks the item FAILED and the follower dies.
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

    with pytest.raises(OrchestrationInvariantError, match="FAILED scheduler work whose token has no terminal outcome") as refused:
        _resume(crashed)

    assert token_id in str(refused.value)
    with crashed.db.engine.connect() as conn:
        run_status = conn.execute(select(runs_table.c.status).where(runs_table.c.run_id == crashed.run_id)).scalar_one()
        outcomes = conn.execute(select(token_outcomes_table.c.outcome_id).where(token_outcomes_table.c.token_id == token_id)).all()
        items = conn.execute(select(token_work_items_table.c.status).where(token_work_items_table.c.token_id == token_id)).scalars().all()
    assert run_status == RunStatus.FAILED.value
    assert outcomes == []
    assert items == [TokenWorkStatus.FAILED.value]
    crashed.db.close()
