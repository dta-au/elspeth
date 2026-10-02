"""Resume returns a FAILED item whose token has no outcome to READY; a decided FAILED item stays.

Lane-owner decision on E1's needs_ruling (RULINGS 2026-09-26, option A2). A
claim that raises out of its traversal leaves its work item FAILED with no
outcome. The live drain keeps that disposition; resume re-drives the row:

- ``mark_failed`` purges the row payload only when the token is decided, so
  an undecided item keeps the payload the re-drive rebuilds the row from;
- ``requeue_undecided_failed_work`` (resume's leader verb) rotates each such
  item to READY at ``attempt + 1`` with a fresh work_item_id and records a
  ``resume_requeue_failed`` event; the re-claim then starts above the dead
  claim's recorded node_state attempts;
- the predicate is the one ``complete_run`` refuses on (``completed == 1``):
  a token holding only a non-completed outcome (BUFFERED) is undecided;
- an undecided item whose payload was purged, or whose token a group-loss
  record names, is refused (Tier-1) rather than re-driven.

Real SQLite Landscape, real follower and leader processors, no mock replaces
membership, claim, traversal or disposition. The PostgreSQL module
``tests/testcontainer/core/test_resume_requeue_failed_work_postgres.py`` runs
the same scenarios.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import insert, select, update

from elspeth.contracts import RunStatus
from elspeth.contracts.errors import AuditIntegrityError, OrchestrationInvariantError
from elspeth.contracts.scheduler import SchedulerEventType, TokenWorkStatus
from elspeth.contracts.tier_registry import FrameworkBugError
from elspeth.core.landscape.scheduler.payload_codec import scrubbed_row_payload_json
from elspeth.core.landscape.schema import group_losses_table, scheduler_events_table, token_outcomes_table, token_work_items_table
from elspeth.engine.clock import MockClock
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.results import TransformResult
from tests.integration.engine.test_follower_retry_and_reclaim import (
    _OK,
    _TRANSIENT,
    _begin_run,
    _Run,
    _ScriptedTransform,
)


def _payload(run: _Run) -> str:
    with run.db.connection() as conn:
        return str(
            conn.execute(
                select(token_work_items_table.c.row_payload_json).where(token_work_items_table.c.token_id == run.token_id)
            ).scalar_one()
        )


def _work_item_id(run: _Run) -> str:
    with run.db.connection() as conn:
        return str(
            conn.execute(
                select(token_work_items_table.c.work_item_id).where(token_work_items_table.c.token_id == run.token_id)
            ).scalar_one()
        )


def _requeue_events(run: _Run) -> list[dict[str, Any]]:
    with run.db.connection() as conn:
        rows = (
            conn.execute(
                select(scheduler_events_table)
                .where(scheduler_events_table.c.run_id == run.run_id)
                .where(scheduler_events_table.c.token_id == run.token_id)
                .where(scheduler_events_table.c.event_type == SchedulerEventType.RESUME_REQUEUE_FAILED.value)
                .order_by(scheduler_events_table.c.seq)
            )
            .mappings()
            .all()
        )
    return [
        {
            "from": (row["from_status"], row["from_attempt"]),
            "to": (row["to_status"], row["to_attempt"]),
            "work_item_id": row["work_item_id"],
            "context": json.loads(row["context_json"]),
            "caller_owner": row["caller_owner"],
        }
        for row in rows
    ]


def _claim_dies_mid_row(tmp_path: Path, *, db_url: str | None) -> tuple[_Run, BaseTransform, str]:
    """A real follower claims the READY row; its transform raises Tier-1 mid-row.

    Returns the run, the transform and the READY payload the claim started from.
    """

    def build(bust: Callable[[str], None], _clock: MockClock) -> list[BaseTransform]:
        return [_ScriptedTransform((_OK,), input_connection="inbound", on_success="output", bust=bust)]

    run = _begin_run(tmp_path, build, db_url=db_url)
    ready_payload = _payload(run)
    follower, ctx = run.follower()
    (transform,) = run.transforms

    def tier_1_mid_row(row: Any, ctx: Any) -> TransformResult:
        raise FrameworkBugError("follower-side Tier-1 mid-row")

    with pytest.MonkeyPatch.context() as patch, pytest.raises(FrameworkBugError):
        patch.setattr(transform, "process", tier_1_mid_row)
        follower._processor.drain_follower_ready_work(ctx)
    assert run.work_items() == [(TokenWorkStatus.FAILED.value, 1, None, None)]
    assert run.outcomes() == []
    assert run.node_states(transform) == [(0, "failed")]
    return run, transform, ready_payload


# ---------------------------------------------------------------------------
# The undecided item keeps its payload, is requeued with a recorded event, and re-drives
# ---------------------------------------------------------------------------


def test_an_undecided_failed_item_is_requeued_and_the_redrive_writes_a_fresh_attempt(tmp_path: Path) -> None:
    scenario_undecided_failed_item_is_requeued_and_redriven(tmp_path, db_url=None)


def scenario_undecided_failed_item_is_requeued_and_redriven(tmp_path: Path, *, db_url: str | None) -> None:
    run, transform, ready_payload = _claim_dies_mid_row(tmp_path, db_url=db_url)
    # Undecided: mark_failed kept the claim-start payload (a decided item is purged, see below).
    assert _payload(run) == ready_payload
    failed_id = _work_item_id(run)

    requeued = run.factory.scheduler.leases.requeue_undecided_failed_work(coordination_token=run.leader)

    assert requeued == 1
    assert run.work_items() == [(TokenWorkStatus.READY.value, 2, None, None)]
    assert _payload(run) == ready_payload
    new_id = _work_item_id(run)
    assert new_id != failed_id
    assert _requeue_events(run) == [
        {
            "from": (TokenWorkStatus.FAILED.value, 1),
            "to": (TokenWorkStatus.READY.value, 2),
            "work_item_id": new_id,
            "context": {"previous_work_item_id": failed_id},
            "caller_owner": run.leader.worker_id,
        }
    ]
    # Idempotent: nothing undecided remains.
    assert run.factory.scheduler.leases.requeue_undecided_failed_work(coordination_token=run.leader) == 0

    # The leader/resume drain re-claims it above the dead claim's attempt 0.
    processor, ctx = run.leader_processor()
    results = processor.drain_scheduled_work(ctx)

    assert [result.token.token_id for result in results] == [run.token_id]
    assert run.node_states(transform) == [(0, "failed"), (1, "completed")]
    assert run.work_items() == [(TokenWorkStatus.PENDING_SINK.value, 2, "output", "success")]
    assert run.transform_errors() == []


# ---------------------------------------------------------------------------
# A decided FAILED item is purged and never requeued
# ---------------------------------------------------------------------------


def test_a_decided_failed_item_is_purged_and_never_requeued(tmp_path: Path) -> None:
    scenario_decided_failed_item_is_not_requeued(tmp_path, db_url=None)


def scenario_decided_failed_item_is_not_requeued(tmp_path: Path, *, db_url: str | None) -> None:
    def build(bust: Callable[[str], None], _clock: MockClock) -> list[BaseTransform]:
        return [_ScriptedTransform((_TRANSIENT,), input_connection="inbound", on_success="output", bust=bust)]

    run = _begin_run(tmp_path, build, db_url=db_url)
    follower, ctx = run.follower()
    follower._processor.drain_follower_ready_work(ctx)
    assert run.work_items() == [(TokenWorkStatus.FAILED.value, 1, None, None)]
    assert run.outcomes() == [("failure", "quarantined_at_source", True)]
    failed_id = _work_item_id(run)
    assert _payload(run) == scrubbed_row_payload_json(failed_id)

    assert run.factory.scheduler.leases.requeue_undecided_failed_work(coordination_token=run.leader) == 0

    assert run.work_items() == [(TokenWorkStatus.FAILED.value, 1, None, None)]
    assert _work_item_id(run) == failed_id
    assert _requeue_events(run) == []


# ---------------------------------------------------------------------------
# One predicate with complete_run: only a COMPLETED outcome decides a token
# ---------------------------------------------------------------------------


def test_a_token_holding_only_a_buffered_outcome_is_undecided(tmp_path: Path) -> None:
    scenario_token_holding_only_a_buffered_outcome_is_undecided(tmp_path, db_url=None)


def scenario_token_holding_only_a_buffered_outcome_is_undecided(tmp_path: Path, *, db_url: str | None) -> None:
    run, _transform, _ready_payload = _claim_dies_mid_row(tmp_path, db_url=db_url)
    with run.db.engine.begin() as conn:
        conn.execute(
            insert(token_outcomes_table).values(
                outcome_id=f"outcome-{uuid4().hex}",
                run_id=run.run_id,
                token_id=run.token_id,
                outcome=None,
                path="buffered",
                completed=0,
                recorded_at=datetime.now(UTC),
            )
        )

    with pytest.raises(OrchestrationInvariantError, match=r"token\(s\) have no completed terminal outcome"):
        run.factory.run_lifecycle.complete_run(RunStatus.COMPLETED, coordination_token=run.leader)
    assert run.factory.scheduler.leases.requeue_undecided_failed_work(coordination_token=run.leader) == 1
    assert run.work_items() == [(TokenWorkStatus.READY.value, 2, None, None)]


# ---------------------------------------------------------------------------
# Refusals: a purged payload, or a token a group-loss record already names
# ---------------------------------------------------------------------------


def test_an_undecided_item_whose_payload_was_purged_is_refused(tmp_path: Path) -> None:
    scenario_undecided_item_with_purged_payload_is_refused(tmp_path, db_url=None)


def scenario_undecided_item_with_purged_payload_is_refused(tmp_path: Path, *, db_url: str | None) -> None:
    run, _transform, _ready_payload = _claim_dies_mid_row(tmp_path, db_url=db_url)
    failed_id = _work_item_id(run)
    with run.db.engine.begin() as conn:
        conn.execute(
            update(token_work_items_table)
            .where(token_work_items_table.c.work_item_id == failed_id)
            .values(row_payload_json=scrubbed_row_payload_json(failed_id))
        )

    with pytest.raises(AuditIntegrityError, match="row payload was purged") as refused:
        run.factory.scheduler.leases.requeue_undecided_failed_work(coordination_token=run.leader)

    assert run.token_id in str(refused.value)
    assert run.work_items() == [(TokenWorkStatus.FAILED.value, 1, None, None)]
    assert _requeue_events(run) == []


def test_an_undecided_item_whose_token_a_group_loss_names_is_refused(tmp_path: Path) -> None:
    scenario_undecided_item_named_by_a_group_loss_is_refused(tmp_path, db_url=None)


def scenario_undecided_item_named_by_a_group_loss_is_refused(tmp_path: Path, *, db_url: str | None) -> None:
    run, _transform, _ready_payload = _claim_dies_mid_row(tmp_path, db_url=db_url)
    with run.db.engine.begin() as conn:
        conn.execute(
            insert(group_losses_table).values(
                loss_id=f"loss-{uuid4().hex}",
                run_id=run.run_id,
                closer_name="merge",
                group_id="group-1",
                member_key="left",
                token_id=run.token_id,
                reason="failed",
                recorded_by=run.leader.worker_id,
                recorded_at=datetime.now(UTC),
            )
        )

    with pytest.raises(AuditIntegrityError, match="a group-loss record already names them") as refused:
        run.factory.scheduler.leases.requeue_undecided_failed_work(coordination_token=run.leader)

    assert run.token_id in str(refused.value)
    assert run.work_items() == [(TokenWorkStatus.FAILED.value, 1, None, None)]
    assert _requeue_events(run) == []
