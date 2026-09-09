# tests/e2e/recovery/test_barrier_timing_invariance.py
"""§H 476 pinned doctrine — barrier timing is invariant under leader takeover.

ADR-030 slice 3 (§E.2 backdated adoption): a batch's timeout fire time is a
pure function of durable state — ``barrier_blocked_at(oldest member) +
timeout_seconds`` — never of WHEN a leader's intake adopted the row.  Both
anchoring frames are pinned against the SAME MockClock schedule:

* **Frame A (live path):** the leader that blocked the rows adopts them at
  its own drain-iteration intake; ``TriggerEvaluator._first_accept_time`` /
  coalesce ``first_arrival`` anchor to the clamped wall→monotonic transform
  of T_b (the durable ``barrier_blocked_at``), NOT to adoption time.
* **Frame B (takeover restore path):** the seat is usurped mid-window and a
  new leader restores via ``BarrierRecoveryCoordinator.restore_from_journal``; the restored
  anchor is the SAME transform of the SAME durable stamp.

In both frames the trigger must NOT fire at T_b+timeout-ε and MUST fire at
T_b+timeout+ε — the fire-instant difference across the takeover is exactly
zero under MockClock — and batch composition (the ``batch_members`` set) is
identical in both frames.

Construction: the unit-engine builders (real LandscapeDB, real scheduler
journal, real run_coordination seat minted by ``begin_run``) with a REAL
takeover image — the seat epoch is bumped under a usurper identity exactly
like :func:`tests.e2e.recovery.harness._usurp_seat`, and the takeover
processor binds the post-usurpation token.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from elspeth.contracts import TokenInfo
from elspeth.contracts.enums import FrameKind, TerminalPath, TriggerType
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.contracts.schema_contract import SchemaContract
from elspeth.contracts.types import CoalesceName, NodeID
from elspeth.core.config import CoalesceSettings
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.database import begin_write
from elspeth.core.landscape.database_clock import read_landscape_transaction_time
from elspeth.core.landscape.schema import run_coordination_table, token_work_items_table
from elspeth.engine.clock import MockClock
from elspeth.engine.coalesce_executor import CoalesceExecutor
from elspeth.engine.processor import BarrierJournalRestoreContext, _LiveBarrierHold
from elspeth.engine.spans import SpanFactory
from elspeth.engine.tokens import TokenManager
from elspeth.testing import make_row
from tests.fixtures.factories import make_context
from tests.fixtures.group_lineage import ensure_fork_group_record
from tests.fixtures.landscape import age_barrier_hold, on_fresh_database_second
from tests.unit.engine.test_adr030_slice3_intake import (
    AGG_NODE,
    _agg_processor,
    _passthrough_flush_transform,
)
from tests.unit.engine.test_processor import (
    _make_factory,
    _make_processor,
    _make_source_row,
    _persist_blocked_scheduler_work,
)

_T0 = 1_750_000_000.0
# ``pytest.approx`` defaults to a RELATIVE 1e-6 tolerance: at a monotonic
# reading of 1.75e9 that is ±1750 s, which would let an anchor a whole
# timeout window off pass. The anchors are exact arithmetic on the mock
# clock, so pin them absolutely.
_ANCHOR_TOLERANCE_SECONDS = 1e-6
RUN_ID = "test-run"
USURPER = "worker-usurper"
TIMEOUT_SECONDS = 10.0
COALESCE_NODE = NodeID("coalesce::merge")


def _usurp_seat(db: LandscapeDB, run_id: str, clock: MockClock) -> None:
    """The in-DB image of a takeover (harness ``_usurp_seat``): epoch bump
    under a usurper identity. Expiry is kept live so the takeover processor's
    ``leader_coordination_token`` binding reads a current seat."""
    now = clock.now_utc()
    with begin_write(db.engine) as conn:
        conn.execute(
            update(run_coordination_table)
            .where(run_coordination_table.c.run_id == run_id)
            .values(
                leader_worker_id=USURPER,
                leader_epoch=run_coordination_table.c.leader_epoch + 1,
                # Live on the Landscape database clock (ADR-047), which is what
                # the seat's liveness is judged against.
                leader_heartbeat_expires_at=read_landscape_transaction_time(conn) + timedelta(seconds=300),
                updated_at=now,
            )
        )


def _restore_context() -> BarrierJournalRestoreContext:
    return BarrierJournalRestoreContext(
        resume_checkpoint_id="ckpt-takeover",
        barrier_scalars=None,
        batch_id_remap={},
    )


def _blocked_work_item_ids(db: LandscapeDB) -> list[str]:
    """Every BLOCKED journal row of the run, in ingest order (the holds a restore adopts)."""
    with db.engine.connect() as conn:
        rows = conn.execute(
            select(token_work_items_table.c.work_item_id)
            .where(token_work_items_table.c.run_id == RUN_ID)
            .where(token_work_items_table.c.status == TokenWorkStatus.BLOCKED.value)
            .order_by(token_work_items_table.c.ingest_sequence, token_work_items_table.c.work_item_id)
        ).all()
    return [str(row.work_item_id) for row in rows]


def _real_coalesce_executor(factory: Any, clock: MockClock, *, policy: str = "best_effort") -> CoalesceExecutor:
    """The production executor over the REAL repositories (no mocks)."""
    token_manager = TokenManager(factory.data_flow, step_resolver=lambda node_id: 2)
    executor = CoalesceExecutor(
        execution=factory.execution,
        span_factory=SpanFactory(),
        token_manager=token_manager,
        run_id=RUN_ID,
        step_resolver=lambda node_id: 2,
        clock=clock,
        data_flow=factory.data_flow,
        barrier_restore_reads=factory.barrier_restore,
    )
    executor.register_coalesce(
        CoalesceSettings(
            name="merge",
            branches=["a", "b"],
            policy=policy,
            merge="union",
            timeout_seconds=TIMEOUT_SECONDS,
            on_success="default",
        ),
        COALESCE_NODE,
        output_schema=SchemaContract(mode="OBSERVED", fields=(), locked=False),
    )
    return executor


@pytest.mark.timeout(120)
class TestAggregationTimeoutInvariance:
    """Aggregation arm: ``TriggerEvaluator._first_accept_time`` anchors to T_b."""

    def test_fire_instant_and_composition_invariant_across_takeover(self) -> None:
        clock = MockClock(start=_T0)
        db, factory = _make_factory()
        transform = _passthrough_flush_transform()
        processor_a = _agg_processor(factory, trigger={"timeout_seconds": TIMEOUT_SECONDS}, transform=transform, clock=clock)
        ctx = make_context(landscape=factory.plugin_audit_writer())

        # T_b: leader A blocks two rows; the SAME process_row calls run the
        # journal-first intake, so adoption happens on the live path.
        mono_at_tb = clock.monotonic()
        for index in range(2):
            results = processor_a.process_row(
                row_index=index,
                source_row=_make_source_row({"value": index}),
                transforms=[transform],
                ctx=ctx,
                source_row_index=index,
                ingest_sequence=index,
            )
            assert [(r.outcome, r.path) for r in results] == [(None, TerminalPath.BUFFERED)]
        assert processor_a.get_aggregation_buffer_count(AGG_NODE) == 2

        # (1) Frame A anchor == the clamped wall→monotonic transform of T_b.
        evaluator_a = processor_a._aggregation_executor._nodes[AGG_NODE].trigger
        assert evaluator_a._first_accept_time == pytest.approx(mono_at_tb, rel=0, abs=_ANCHOR_TOLERANCE_SECONDS)

        # Frame A batch composition (the open DRAFT batch A's adoptions filled).
        batches_a = factory.execution.get_batches(RUN_ID)
        assert len(batches_a) == 1
        members_a = {(m.token_id, m.ordinal) for m in factory.execution.get_batch_members(batches_a[0].batch_id)}
        assert len(members_a) == 2

        # (2) Frame A at T_b+timeout-ε: must not fire.
        clock.advance(TIMEOUT_SECONDS - 0.5)
        should_fire_a, _ = processor_a.check_aggregation_timeout(AGG_NODE)
        assert should_fire_a is False

        # ── Takeover mid-window: usurp the seat, restore as leader B. ──────
        # A restored hold's age is durable state measured on the Landscape
        # database clock (ADR-047); the MockClock's advance never reaches the
        # database, so the SAME T_b+timeout-ε age is written into the
        # database's past and the restore reads it inside one whole SQLite
        # second (a rollover would be reported, not silently scored).
        def take_over(_database_now: datetime) -> Any:
            for work_item_id in _blocked_work_item_ids(db):
                age_barrier_hold(db.engine, work_item_id, seconds_ago=TIMEOUT_SECONDS - 0.5)
            _usurp_seat(db, RUN_ID, clock)
            return _agg_processor(
                factory,
                trigger={"timeout_seconds": TIMEOUT_SECONDS},
                transform=_passthrough_flush_transform(),
                clock=clock,
                barrier_restore=_restore_context(),
            )

        processor_b = on_fresh_database_second(db.engine, take_over)

        # (3) Composition identical: restore created NO new batch and NO new
        # members — the durable membership set is byte-identical.
        batches_b = factory.execution.get_batches(RUN_ID)
        assert [b.batch_id for b in batches_b] == [batches_a[0].batch_id]
        members_b = {(m.token_id, m.ordinal) for m in factory.execution.get_batch_members(batches_a[0].batch_id)}
        assert members_b == members_a
        assert processor_b.get_aggregation_buffer_count(AGG_NODE) == 2

        # (1) Frame B anchor == the SAME transform of the SAME durable stamp.
        evaluator_b = processor_b._aggregation_executor._nodes[AGG_NODE].trigger
        assert evaluator_b._first_accept_time == pytest.approx(mono_at_tb, rel=0, abs=_ANCHOR_TOLERANCE_SECONDS)
        assert evaluator_b.batch_count == 2

        # (2) Frame B at the SAME instant (T_b+timeout-ε): must not fire.
        should_fire_b, _ = processor_b.check_aggregation_timeout(AGG_NODE)
        assert should_fire_b is False

        # (4) T_b+timeout+ε: BOTH frames flip between the same two clock
        # readings — the fire-instant difference across the takeover is zero.
        clock.advance(1.0)
        should_fire_b, trigger_b = processor_b.check_aggregation_timeout(AGG_NODE)
        assert should_fire_b is True
        assert trigger_b is TriggerType.TIMEOUT
        should_fire_a, trigger_a = processor_a.check_aggregation_timeout(AGG_NODE)
        assert should_fire_a is True, "frame A fires at the SAME instant (pure read; A is deposed but its memory is frame evidence)"
        assert trigger_a is TriggerType.TIMEOUT


@pytest.mark.timeout(120)
class TestCoalesceTimeoutInvariance:
    """Coalesce mirror: ``first_arrival`` anchors to T_b in both frames."""

    def test_first_arrival_anchor_and_fire_instant_invariant_across_takeover(self) -> None:
        clock = MockClock(start=_T0)
        db, factory = _make_factory()
        executor_a = _real_coalesce_executor(factory, clock)
        processor_a = _make_processor(
            factory,
            coalesce_executor=executor_a,
            coalesce_node_ids={CoalesceName("merge"): COALESCE_NODE},
            node_step_map={COALESCE_NODE: 2},
            clock=clock,
        )
        ctx = make_context(landscape=factory.plugin_audit_writer())

        # T_b: branch-a's BLOCKED row is deposited (live hold stashed exactly
        # as the drain would) and adopted by leader A's intake in the same
        # clock instant.
        token_a = TokenInfo(
            row_id="row-1",
            token_id="tok-branch-a",
            row_data=make_row({"amount": 1}),
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-row-1", member_key="a"),),
        )
        mono_at_tb = clock.monotonic()
        work_item_id = _persist_blocked_scheduler_work(
            factory, processor_a, token_a, node_id=COALESCE_NODE, barrier_key="merge", adopted=False, coalesce_name="merge"
        )
        # META-38: the crafted FORK group needs the group_records row a real
        # fork mints — the merge reads the written release fact for it.
        ensure_fork_group_record(factory, run_id="test-run", group_id="fg-row-1", opener_token_id=token_a.token_id)
        processor_a._live_barrier_holds[token_a.token_id] = _LiveBarrierHold(
            token=token_a, barrier_key="merge", arrived_monotonic=mono_at_tb
        )
        intake_results = processor_a.run_barrier_intake(ctx)
        assert intake_results == []

        # (1) Frame A anchor: the live-path accept was backdated to T_b.
        # Live accept() is fork_group_id-keyed (WS4 Task 8); token_a's FORK
        # frame group_id is "fg-row-1".
        pending_a = executor_a._pending[("merge", "fg-row-1")]
        assert pending_a.first_arrival == pytest.approx(mono_at_tb, rel=0, abs=_ANCHOR_TOLERANCE_SECONDS)

        # (2) Frame A at T_b+timeout-ε: no timeout fire (pure when not firing).
        clock.advance(TIMEOUT_SECONDS - 0.5)
        assert executor_a.check_timeouts("merge") == []

        # ── Takeover mid-window. ────────────────────────────────────────────
        # The restored hold's age is measured on the Landscape database clock
        # (ADR-047): write the SAME T_b+timeout-ε age into the database's past
        # and restore inside one whole SQLite second (see the aggregation twin).
        def take_over(_database_now: datetime) -> tuple[CoalesceExecutor, Any]:
            age_barrier_hold(db.engine, work_item_id, seconds_ago=TIMEOUT_SECONDS - 0.5)
            _usurp_seat(db, RUN_ID, clock)
            executor = _real_coalesce_executor(factory, clock)
            processor = _make_processor(
                factory,
                coalesce_executor=executor,
                coalesce_node_ids={CoalesceName("merge"): COALESCE_NODE},
                node_step_map={COALESCE_NODE: 2},
                clock=clock,
                barrier_restore=_restore_context(),
            )
            return executor, processor

        executor_b, processor_b = on_fresh_database_second(db.engine, take_over)
        assert processor_b.has_blocked_barrier_work() is True

        # (1) Frame B anchor: restored from the SAME durable barrier_blocked_at.
        # restore_from_journal now groups by fork_group_id too (WS4 Task 10) —
        # same key shape as pending_a's live-accept key above ("fg-row-1"),
        # closing the premise-break window T8-alone would have left.
        pending_b = executor_b._pending[("merge", "fg-row-1")]
        assert pending_b.first_arrival == pytest.approx(mono_at_tb, rel=0, abs=_ANCHOR_TOLERANCE_SECONDS)
        assert set(pending_b.branches) == {"a"}

        # (2) Frame B at the SAME instant: no fire.
        assert executor_b.check_timeouts("merge") == []

        # (4) T_b+timeout+ε: frame B fires — best_effort merges the arrived
        # branch. Frame A's fire predicate (now - first_arrival ≥ timeout)
        # flips at the same instant; asserted arithmetically because actually
        # firing BOTH executors would double-record the consumed branch's
        # terminal outcomes.
        clock.advance(1.0)
        fired = executor_b.check_timeouts("merge")
        assert len(fired) == 1
        assert fired[0].merged_token is not None
        assert {token.token_id for token in fired[0].consumed_tokens} == {"tok-branch-a"}
        assert clock.monotonic() - pending_a.first_arrival >= TIMEOUT_SECONDS, "frame A's fire instant is the same clock reading"
