"""Real process-death recovery at each durable barrier family.

The child reaches the narrow window after the barrier executor has committed
its durable result/node-state evidence but before ``complete_barrier`` emits
the continuation.  The parent sends SIGKILL, and a fresh process reopens the
same SQLite database. Aggregation resumes through the maintained Orchestrator
entry point; coalesce, row-union and collector drive their real
executor/repository restore composition directly.

The collector family (seven scenarios, documented at its section header)
adds the two META-9.1 loss shapes — a loss reported by a worker that never
ran the opener, and a loss after resume, including a nested coalesce-in-scope
unwrap — a genuinely multi-worker leader+follower composition, and the
realistic opener -> transform -> collector resume. Every crashed or
handed-off image is pinned by exact durable preconditions before the second
process runs.

This file deliberately owns only the barrier-specific process seam.  It does
not label the direct-executor compositions as same-host follower or web-hosted
profiles: those labels require the barrier itself to run inside the named
deployment composition, not an unrelated topology control in a second DB.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from sqlalchemy import insert, select, update

from elspeth.contracts import Determinism, NodeType, RunStatus, TokenInfo
from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
from elspeth.contracts.enums import FrameKind, TerminalOutcome, TerminalPath
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.schema_contract import FieldContract, SchemaContract
from elspeth.contracts.types import BranchName, CoalesceName, CollectorName, NodeID, RowUnionName
from elspeth.core.checkpoint import CheckpointManager, RecoveryManager
from elspeth.core.checkpoint.recovery import GroupBindingView, check_group_satisfiability_resumable
from elspeth.core.config import CheckpointSettings, CoalesceSettings, CollectorSettings, RowUnionSettings, ScopeSettings
from elspeth.core.dag.group_bindings import CloserKind, GroupBinding, GroupBindingRegistry
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.scheduler.work_items import collector_barrier_key
from elspeth.core.landscape.schema import (
    aggregation_results_table,
    batches_table,
    coalesce_effects_table,
    group_losses_table,
    group_records_table,
    node_states_table,
    run_coordination_table,
    run_workers_table,
    token_lineage_frames_table,
    token_outcomes_table,
    token_parents_table,
    token_work_items_table,
)
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.clock import MockClock
from elspeth.engine.coalesce_executor import CoalesceExecutor
from elspeth.engine.executors.collector import CollectorExecutor
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.orchestrator.bootstrap import prepare_for_run
from elspeth.engine.processor import BarrierJournalRestoreContext, ProcessorMode, RowProcessor, _LiveBarrierHold
from elspeth.engine.row_union_executor import RowUnionExecutor
from elspeth.engine.spans import SpanFactory
from elspeth.engine.tokens import TokenManager
from elspeth.engine.work_items import WorkItem
from elspeth.testing import make_row, make_token_info
from tests.e2e.recovery.harness import _PassthroughTransform, spawn_database_process_at_seam, spawn_database_process_with_pause
from tests.e2e.recovery.test_sink_effect_process_death_matrix import (
    _PROCESS_TIMEOUT_SECONDS,
    _install_short_run_liveness,
    _wait_until_run_is_resumable,
)
from tests.fixtures.dag_scenario_corpus.plugins import CorpusBranchLossTransform
from tests.fixtures.factories import make_context
from tests.fixtures.plugins import CollectSink
from tests.integration.pipeline.test_aggregation_recovery import (
    _build_eof_aggregation_pipeline,
    _LoadCountingSource,
    _SumBatchTransform,
)
from tests.integration.pipeline.test_barrier_intake_dispositions import (
    RUN_ID,
    USURPER,
    _arrive_via_intake,
    _branch_token,
    _coalesce_processor,
    _real_coalesce_executor,
    _usurp_seat,
)
from tests.unit.engine.test_processor import _make_processor, _persist_blocked_scheduler_work, _persist_token_for_scheduler

if TYPE_CHECKING:
    from collections.abc import Callable

    from scripts.state_engine_profile_reporter import RuntimeProfileReporter


_CRASH_SEAM = "after-barrier-result-before-continuation"
_ROW_UNION = RowUnionName("variant_union")
_ROW_UNION_NODE = NodeID("row_union::variant_union")
_ROW_UNION_NEXT = NodeID("transform::after_union")
_T0 = 1_750_000_000.0
_SCHEMA_CONFIG = SchemaConfig(mode="observed", fields=None)
_COLLECTOR = CollectorName("stitch")
_COLLECTOR_NODE = NodeID("collector::stitch")
_OPENER_NODE = NodeID("transform::explode")
_ERR_NODE = NodeID("transform::err_page")
_OPENER_TOKEN = "parent-1"
_MERGE = CoalesceName("merge")
_COALESCE_NODE = NodeID("coalesce::merge")
_FORK_NODE = NodeID("gate::split")
_ERR_B_NODE = NodeID("transform::err_b")
_LEADER_HOLDING_SEAM = "leader-holding-open-collector-group"
_MEMBER_ROWS = [{"id": 1, "value": 10}, {"id": 1, "value": 20}]


def _new_barrier_factory(db: LandscapeDB, payload_path: str) -> RecorderFactory:
    """Create the repository graph used by direct real-executor compositions."""
    prepare_for_run()
    factory = RecorderFactory(db, payload_store=FilesystemPayloadStore(Path(payload_path)))
    factory.run_lifecycle.begin_run(
        config={"test": "barrier-process-death"},
        canonical_version="v1",
        run_id=RUN_ID,
        openrouter_catalog_sha256="0" * 64,
        openrouter_catalog_source="bundled",
        leader_worker_id="test-leader",
    )
    factory.data_flow.register_node(
        run_id=RUN_ID,
        plugin_name="test-source",
        node_type=NodeType.SOURCE,
        plugin_version="1.0",
        config={},
        node_id="source-0",
        schema_config=_SCHEMA_CONFIG,
    )
    return factory


def _run_aggregation_to_receipt_seam(
    db: LandscapeDB,
    pause: Callable[[], None],
    run_id: str,
    payload_path: str,
) -> None:
    """Commit the aggregation receipt, then pause before output routing."""
    from elspeth.engine.executors.aggregation import AggregationExecutor

    _install_short_run_liveness()
    source = _LoadCountingSource([{"value": 10}, {"value": 20}, {"value": 30}], on_success="batch_in")
    transform = _SumBatchTransform()
    config, graph = _build_eof_aggregation_pipeline(source, transform, CollectSink("output"))
    config = replace(config, sink_effect_modes={"output": "write"})
    real_execute_flush = AggregationExecutor.execute_flush

    def pause_after_receipt(self: AggregationExecutor, *args: Any, **kwargs: Any) -> None:
        real_execute_flush(self, *args, **kwargs)
        pause()

    AggregationExecutor.execute_flush = pause_after_receipt  # type: ignore[method-assign]
    checkpoint_manager = CheckpointManager(db)
    Orchestrator(
        db,
        checkpoint_manager=checkpoint_manager,
        checkpoint_config=RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row")),
    ).run(
        config,
        graph=graph,
        payload_store=FilesystemPayloadStore(Path(payload_path)),
        run_id=run_id,
        openrouter_catalog_sha256="0" * 64,
        openrouter_catalog_source="bundled",
    )


def _resume_aggregation_after_death(db: LandscapeDB, run_id: str, payload_path: str) -> None:
    """Fresh-process resume oracle: committed plugin work is never replayed."""
    source = _LoadCountingSource([{"value": 10}, {"value": 20}, {"value": 30}], on_success="batch_in")
    transform = _SumBatchTransform()
    sink = CollectSink("output")
    config, graph = _build_eof_aggregation_pipeline(source, transform, sink)
    config = replace(config, sink_effect_modes={"output": "write"})
    checkpoint_manager = CheckpointManager(db)
    resume_point = RecoveryManager(db, checkpoint_manager).get_resume_point(run_id, graph)
    assert resume_point is not None
    result = Orchestrator(
        db,
        checkpoint_manager=checkpoint_manager,
        checkpoint_config=RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row")),
    ).resume(
        resume_point,
        config,
        graph,
        payload_store=FilesystemPayloadStore(Path(payload_path)),
    )
    assert result.status is RunStatus.COMPLETED
    assert sink.results == [{"value": 60, "count": 3}]
    assert source.load_invocations == 0
    assert transform.batch_calls == 0


def _run_coalesce_to_completion_seam(db: LandscapeDB, pause: Callable[[], None], payload_path: str) -> None:
    """Complete the real merge effect, then pause before scheduler completion."""
    factory = _new_barrier_factory(db, payload_path)
    clock = MockClock(start=_T0)
    executor = _real_coalesce_executor(factory, clock, policy="require_all")
    real_complete = RowProcessor._complete_coalesce_fire

    def pause_before_completion(self: RowProcessor, **kwargs: Any) -> None:
        pause()
        real_complete(self, **kwargs)

    RowProcessor._complete_coalesce_fire = pause_before_completion  # type: ignore[method-assign]
    processor = _coalesce_processor(factory, executor, clock)
    assert _arrive_via_intake(factory, processor, _branch_token("a")) == []
    _arrive_via_intake(factory, processor, _branch_token("b"), ingest_sequence=1)


def _resume_coalesce_after_death(db: LandscapeDB, payload_path: str) -> None:
    """Reconcile completed merge evidence into one terminal continuation."""
    factory = RecorderFactory(db, payload_store=FilesystemPayloadStore(Path(payload_path)))
    clock = MockClock(start=_T0 + 10)
    _usurp_seat(db, clock)
    executor = _real_coalesce_executor(factory, clock, policy="require_all")
    processor = _coalesce_processor(
        factory,
        executor,
        clock,
        barrier_restore=BarrierJournalRestoreContext(
            resume_checkpoint_id="process-death-coalesce",
            barrier_scalars=None,
            batch_id_remap={},
        ),
        stamp_blocked_rows_adopted=False,
    )
    assert processor.has_blocked_barrier_work() is False


def _real_row_union_executor(factory: RecorderFactory, clock: MockClock) -> RowUnionExecutor:
    executor = RowUnionExecutor(
        execution=factory.execution,
        span_factory=SpanFactory(),
        run_id=RUN_ID,
        step_resolver=lambda _node_id: 2,
        data_flow=factory.data_flow,
        clock=clock,
        barrier_restore_reads=factory.barrier_restore,
    )
    executor.register_row_union(
        RowUnionSettings(name=str(_ROW_UNION), branches=["a", "b"], on_success="union_out"),
        _ROW_UNION_NODE,
    )
    return executor


def _row_union_processor(
    factory: RecorderFactory,
    executor: RowUnionExecutor,
    clock: MockClock,
    *,
    barrier_restore: BarrierJournalRestoreContext | None = None,
) -> RowProcessor:
    return _make_processor(
        factory,
        row_union_executor=executor,
        row_union_node_ids={_ROW_UNION: _ROW_UNION_NODE},
        branch_to_row_union={"a": _ROW_UNION, "b": _ROW_UNION},
        node_step_map={_ROW_UNION_NODE: 2, _ROW_UNION_NEXT: 3},
        node_to_next={_ROW_UNION_NODE: _ROW_UNION_NEXT, _ROW_UNION_NEXT: None},
        clock=clock,
        barrier_restore=barrier_restore,
        stamp_blocked_rows_adopted=False,
    )


def _arrive_row_union(factory: RecorderFactory, processor: RowProcessor, token: TokenInfo, *, ingest_sequence: int) -> list[Any]:
    _persist_blocked_scheduler_work(
        factory,
        processor,
        token,
        node_id=_ROW_UNION_NODE,
        barrier_key=str(_ROW_UNION),
        adopted=False,
        ingest_sequence=ingest_sequence,
        coalesce_name=str(_ROW_UNION),
    )
    processor._live_barrier_holds[token.token_id] = _LiveBarrierHold(token=token, barrier_key=str(_ROW_UNION))
    return processor.run_barrier_intake(make_context(landscape=factory.plugin_audit_writer()))


def _run_row_union_to_release_seam(db: LandscapeDB, pause: Callable[[], None], payload_path: str) -> None:
    """Complete both real union node states, then pause before READY emission."""
    factory = _new_barrier_factory(db, payload_path)
    clock = MockClock(start=_T0)
    executor = _real_row_union_executor(factory, clock)
    real_complete = RowProcessor._complete_row_union_fire

    def pause_before_completion(self: RowProcessor, **kwargs: Any) -> None:
        pause()
        real_complete(self, **kwargs)

    RowProcessor._complete_row_union_fire = pause_before_completion  # type: ignore[method-assign]
    processor = _row_union_processor(factory, executor, clock)
    assert _arrive_row_union(factory, processor, _branch_token("a"), ingest_sequence=0) == []
    _arrive_row_union(factory, processor, _branch_token("b"), ingest_sequence=1)


def _resume_row_union_after_death(db: LandscapeDB, payload_path: str) -> None:
    """Reconcile the released group into two ordered READY continuations."""
    factory = RecorderFactory(db, payload_store=FilesystemPayloadStore(Path(payload_path)))
    clock = MockClock(start=_T0 + 10)
    _usurp_seat(db, clock)
    executor = _real_row_union_executor(factory, clock)
    processor = _row_union_processor(
        factory,
        executor,
        clock,
        barrier_restore=BarrierJournalRestoreContext(
            resume_checkpoint_id="process-death-row-union",
            barrier_scalars=None,
            batch_id_remap={},
        ),
    )
    assert processor.has_blocked_barrier_work() is False


def _kill_at_seam(database_url: str, action: Callable[..., None], action_args: tuple[object, ...] = ()) -> None:
    with spawn_database_process_with_pause(
        database_url=database_url,
        seam=_CRASH_SEAM,
        action=action,
        action_args=action_args,
    ) as child:
        ready = child.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        assert ready.pid != os.getpid()
        assert ready.database_dialect == "sqlite"
        child.kill()
        assert child.wait_for_exit(timeout=_PROCESS_TIMEOUT_SECONDS).was_killed


def _run_fresh_recovery(database_url: str, action: Callable[..., None], action_args: tuple[object, ...] = ()) -> None:
    with spawn_database_process_at_seam(
        database_url=database_url,
        seam="fresh-process-recovery-completed",
        action=action,
        action_args=action_args,
    ) as child:
        child.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        child.release()
        assert child.wait_for_exit(timeout=_PROCESS_TIMEOUT_SECONDS).exitcode == 0


def _exercise_aggregation(tmp_path: Path) -> LandscapeDB:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database_url = f"sqlite:///{tmp_path / 'aggregation.db'}"
    payload_path = tmp_path / "payloads"
    run_id = "task8-aggregation-process-death"
    with LandscapeDB(database_url):
        pass
    _kill_at_seam(database_url, _run_aggregation_to_receipt_seam, (run_id, str(payload_path)))
    with LandscapeDB.from_url(database_url, create_tables=False) as killed_db, killed_db.connection() as conn:
        assert conn.execute(select(batches_table.c.status)).scalar_one() == "completed"
        assert conn.execute(select(aggregation_results_table.c.output_shape)).scalar_one() == "single"
        assert set(conn.execute(select(token_work_items_table.c.status)).scalars()) == {TokenWorkStatus.BLOCKED.value}
    # SIGKILL cannot execute the run-finalization ceremony.  The external
    # supervisor's classification is represented through the production
    # lifecycle writer; barrier recovery itself begins only after this point.
    with LandscapeDB.from_url(database_url, create_tables=False) as killed_db:
        RecorderFactory(killed_db).run_lifecycle.update_run_status(run_id, RunStatus.FAILED)
        with killed_db.write_connection() as conn:
            conn.execute(
                update(run_coordination_table)
                .where(run_coordination_table.c.run_id == run_id)
                .values(leader_heartbeat_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
    _wait_until_run_is_resumable(database_url, run_id)
    _run_fresh_recovery(database_url, _resume_aggregation_after_death, (run_id, str(payload_path)))
    recovered = LandscapeDB.from_url(database_url, create_tables=False)
    with recovered.connection() as conn:
        assert set(conn.execute(select(token_work_items_table.c.status)).scalars()) == {TokenWorkStatus.TERMINAL.value}
        assert len(conn.execute(select(aggregation_results_table.c.batch_id)).all()) == 1
        assert len(conn.execute(select(batches_table.c.batch_id)).all()) == 1
    return recovered


def _exercise_coalesce(tmp_path: Path) -> LandscapeDB:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database_url = f"sqlite:///{tmp_path / 'coalesce.db'}"
    payload_path = tmp_path / "payloads"
    with LandscapeDB(database_url):
        pass
    _kill_at_seam(database_url, _run_coalesce_to_completion_seam, (str(payload_path),))
    with LandscapeDB.from_url(database_url, create_tables=False) as killed_db, killed_db.connection() as conn:
        assert set(conn.execute(select(token_work_items_table.c.status)).scalars()) == {TokenWorkStatus.BLOCKED.value}
        assert len(conn.execute(select(node_states_table.c.state_id).where(node_states_table.c.status == "completed")).all()) == 2
        effect = conn.execute(select(coalesce_effects_table)).mappings().one()
        assert effect["status"] == "completed"
        assert (
            conn.execute(
                select(token_work_items_table.c.work_item_id).where(token_work_items_table.c.token_id == effect["result_token_id"])
            ).all()
            == []
        )
    _run_fresh_recovery(database_url, _resume_coalesce_after_death, (str(payload_path),))
    recovered = LandscapeDB.from_url(database_url, create_tables=False)
    with recovered.connection() as conn:
        rows = conn.execute(select(token_work_items_table.c.token_id, token_work_items_table.c.status)).all()
        terminal_ids = {row.token_id for row in rows if row.status == TokenWorkStatus.TERMINAL.value}
        pending_ids = {row.token_id for row in rows if row.status == TokenWorkStatus.PENDING_SINK.value}
        assert len(rows) == 3
        assert terminal_ids == {"tok-branch-a", "tok-branch-b"}
        assert pending_ids == {effect["result_token_id"]}
        assert conn.execute(select(coalesce_effects_table.c.effect_id)).all() == [(effect["effect_id"],)]
        assert len(conn.execute(select(node_states_table.c.state_id).where(node_states_table.c.status == "completed")).all()) == 2
    return recovered


def _exercise_row_union(tmp_path: Path) -> LandscapeDB:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database_url = f"sqlite:///{tmp_path / 'row-union.db'}"
    payload_path = tmp_path / "payloads"
    with LandscapeDB(database_url):
        pass
    _kill_at_seam(database_url, _run_row_union_to_release_seam, (str(payload_path),))
    with LandscapeDB.from_url(database_url, create_tables=False) as killed_db, killed_db.connection() as conn:
        assert set(conn.execute(select(token_work_items_table.c.status)).scalars()) == {TokenWorkStatus.BLOCKED.value}
        assert len(conn.execute(select(node_states_table.c.state_id).where(node_states_table.c.status == "completed")).all()) == 2
    _run_fresh_recovery(database_url, _resume_row_union_after_death, (str(payload_path),))
    recovered = LandscapeDB.from_url(database_url, create_tables=False)
    with recovered.connection() as conn:
        rows = conn.execute(
            select(token_work_items_table.c.token_id, token_work_items_table.c.status, token_work_items_table.c.node_id).order_by(
                token_work_items_table.c.created_at, token_work_items_table.c.work_item_id
            )
        ).all()
        assert [row.status for row in rows].count(TokenWorkStatus.TERMINAL.value) == 2
        ready = [row for row in rows if row.status == TokenWorkStatus.READY.value]
        assert [(row.token_id, row.node_id) for row in ready] == [
            ("tok-branch-a", str(_ROW_UNION_NEXT)),
            ("tok-branch-b", str(_ROW_UNION_NEXT)),
        ]
        assert len(conn.execute(select(node_states_table.c.state_id).where(node_states_table.c.status == "completed")).all()) == 2
    return recovered


# ===== Collector family (integration phase 1b, META-6.1: authored by integration) =====
#
# Seven scenarios over ONE declared scope (opener ``explode`` -> collector
# ``stitch``, require_all, outermost so group failure is terminal) with a two-member
# expansion. Each scenario's crashed/handed-off image is asserted by durable
# PRECONDITIONS (exact rows at that instant) before the second process runs.
#
#   (a) collector_arrival_death       — death between member 0's adoption and
#       member 1's arrival; the fresh process restores the hold and member 1
#       completes the roster (ordinal oracle: token_parents by opener).
#   (b) collector_flush_death         — death inside the closing arrival, BEFORE
#       any flush effect; both holds restore as a COMPLETE roster and the
#       META-31 post-restore sweep (barrier_coordination's held list) runs the
#       plugin exactly once, in the fresh process.
#   (c) collector_non_opener_worker_loss — the opener's worker exits CLEANLY;
#       a worker that never ran the opener loses member 1 at err_page. The
#       settle seam re-derives the EXPAND binding durably (META-9.1) and the
#       COLLECTOR arm fails the group.
#   (c') collector_follower_loss      — the genuinely multi-worker form: the
#       opener's LEADER is alive (paused holding the open group) while a real
#       FOLLOWER process loses member 1 — stages only (no executor), the loss
#       lands with adopted_epoch NULL; the released leader's intake replays it
#       (§E.5) through the collector arm against its in-memory registry.
#   (d) collector_post_resume_loss    — the opener's worker is KILLED after
#       member 0's adoption; the resumed process loses member 1 (flat).
#   (d') collector_post_resume_loss_nested — coalesce-in-scope: each member
#       forks (a, b) into a require_all coalesce whose merge feeds the
#       collector. Killed with member 0 MERGED and held at the collector and
#       member 1's branch a held at the coalesce (two durably-BLOCKED rows at
#       two nesting levels — the open mid-unwrap image Task 5a measured to
#       exist only before the failing claim). Post-resume the loss of branch b
#       fails the coalesce, and the escalation walk crosses the FORK->EXPAND
#       boundary through the re-derivation to fail the collector group. The
#       satisfiability gate (spec §8) is pinned on both sides of the loss.
#   (e) collector_realistic_shape_resume — explode -> transform -> collector
#       through the REAL drain (the member COMPLETES err_page before holding),
#       killed after member 0's hold: the restore cross-check must not refuse
#       (META-35 site 2) and member 1 completes the roster via the same drain.
#
# NOT hosted — item 12 (integration worklist), the multi-worker CONCURRENT-
# ADOPTION RACE ("multiple real worker processes racing to adopt the same
# BLOCKED collector row"): adoption is a leader-fenced CAS on the single
# run_coordination seat, so a race needs two processes each holding a leader
# fence at once — the Postgres multi-worker suite's composition
# (test_barrier_recovery_postgres.py), not this single-seat SQLite harness,
# whose second leader is always a fresh-process TAKEOVER of an expired seat.
# The non-opener-worker LOSS half of "multi-worker" (item 13) IS hosted, as
# (c) and (c').


class _RecordingSumBatch(_SumBatchTransform):
    """The collector plugin: records the value order of every flush it sees."""

    name = "sum_batch"
    determinism = Determinism.DETERMINISTIC

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[list[int]] = []

    def process(self, row: Any, ctx: Any) -> Any:
        if isinstance(row, list):
            self.seen.append([r["value"] for r in row])
        return super().process(row, ctx)


_EXPAND_BINDING = GroupBinding(
    kind=FrameKind.EXPAND,
    opener_node_id=_OPENER_NODE,
    opener_name="explode",
    closer_node_id=_COLLECTOR_NODE,
    closer_name=str(_COLLECTOR),
    closer_kind=CloserKind.COLLECTOR,
    policy="require_all",
    member_roster=(),
)
_FORK_BINDING = GroupBinding(
    kind=FrameKind.FORK,
    opener_node_id=_FORK_NODE,
    opener_name="split",
    closer_node_id=_COALESCE_NODE,
    closer_name=str(_MERGE),
    closer_kind=CloserKind.COALESCE,
    policy="require_all",
    member_roster=("a", "b"),
)


def _collector_registry(*, nested: bool = False) -> GroupBindingRegistry:
    return GroupBindingRegistry(bindings=(_EXPAND_BINDING, _FORK_BINDING) if nested else (_EXPAND_BINDING,))


def _binding_view(registry: GroupBindingRegistry) -> GroupBindingView:
    """``group_binding_view_from_graph``'s projection over a bare registry (no graph here)."""
    fork_closers: dict[str, str] = {}
    fork_rosters: dict[str, tuple[str, ...]] = {}
    scope_closers: dict[str, str] = {}
    for binding in registry.bindings:
        if binding.kind is FrameKind.FORK:
            for branch in binding.member_roster:
                fork_closers[branch] = binding.closer_name
                fork_rosters[branch] = tuple(binding.member_roster)
        else:
            scope_closers[str(binding.opener_node_id)] = binding.closer_name
    return GroupBindingView(fork_branch_closers=fork_closers, fork_branch_rosters=fork_rosters, scope_opener_closers=scope_closers)


def _assert_satisfiable(db: LandscapeDB, *, nested: bool) -> None:
    """Spec §8: the shared gate must not refuse this image (both resume
    surfaces call exactly this function)."""
    gate = check_group_satisfiability_resumable(db, RUN_ID, _binding_view(_collector_registry(nested=nested)))
    assert gate.check.can_resume, gate.check.reason
    assert gate.unsatisfiable_members == ()


def _real_collector_executor(factory: RecorderFactory, clock: MockClock) -> CollectorExecutor:
    executor = CollectorExecutor(
        execution=factory.execution,
        span_factory=SpanFactory(),
        token_manager=TokenManager(factory.data_flow, step_resolver=lambda _node_id: 3),
        run_id=RUN_ID,
        step_resolver=lambda _node_id: 3,
        data_flow=factory.data_flow,
        clock=clock,
        barrier_restore_reads=factory.barrier_restore,
    )
    executor.register_collector(
        CollectorSettings(name=str(_COLLECTOR), plugin="sum_batch", input="pages", on_success="out"),
        ScopeSettings(name="document_pages", opener="explode", closer=str(_COLLECTOR), policy="require_all"),
        _COLLECTOR_NODE,
        _RecordingSumBatch(),
    )
    return executor


def _real_nested_coalesce_executor(factory: RecorderFactory, clock: MockClock) -> CoalesceExecutor:
    """The coalesce INSIDE the scope: on_success names the collector, not a sink."""
    executor = CoalesceExecutor(
        execution=factory.execution,
        span_factory=SpanFactory(),
        token_manager=TokenManager(factory.data_flow, step_resolver=lambda _node_id: 4),
        run_id=RUN_ID,
        step_resolver=lambda _node_id: 4,
        clock=clock,
        data_flow=factory.data_flow,
        barrier_restore_reads=factory.barrier_restore,
    )
    executor.register_coalesce(
        CoalesceSettings(name=str(_MERGE), branches=["a", "b"], policy="require_all", merge="union", on_success=str(_COLLECTOR)),
        _COALESCE_NODE,
        output_schema=SchemaContract(mode="OBSERVED", fields=(), locked=False),
    )
    return executor


def _collector_plugin(executor: CollectorExecutor) -> _RecordingSumBatch:
    plugin = executor._transforms[str(_COLLECTOR)]
    assert isinstance(plugin, _RecordingSumBatch)
    return plugin


def _page_transform(factory: RecorderFactory, *, fails: bool, node_id: NodeID = _ERR_NODE) -> Any:
    """The row transform between opener and collector: an identity pass or the
    corpus branch-loss transform (on_error: discard) at the same node."""
    transform: Any = CorpusBranchLossTransform({"schema": {"mode": "observed"}}) if fails else _PassthroughTransform()
    transform.node_id = str(node_id)
    transform.on_start(make_context(landscape=factory.plugin_audit_writer()))
    return transform


def _collector_processor(
    factory: RecorderFactory,
    executor: CollectorExecutor | None,
    clock: MockClock,
    *,
    page_transform: Any | None = None,
    barrier_restore: BarrierJournalRestoreContext | None = None,
    mode: ProcessorMode = ProcessorMode.LEADER,
    scheduler_lease_owner: str | None = None,
) -> RowProcessor:
    """opener -> err_page (a row transform the members pass through) -> collector -> sink."""
    return _make_processor(
        factory,
        collector_executor=executor,
        collector_node_ids={_COLLECTOR: _COLLECTOR_NODE},
        collector_on_success_map={_COLLECTOR: "out"} if executor is not None else None,
        node_step_map={_OPENER_NODE: 1, _ERR_NODE: 2, _COLLECTOR_NODE: 3},
        node_to_next={_OPENER_NODE: _ERR_NODE, _ERR_NODE: _COLLECTOR_NODE, _COLLECTOR_NODE: None},
        node_to_plugin={_ERR_NODE: page_transform} if page_transform is not None else {},
        structural_node_ids=frozenset({NodeID("source-0"), _OPENER_NODE}),
        group_bindings=_collector_registry(),
        sink_names=frozenset({"out"}),
        clock=clock,
        barrier_restore=barrier_restore,
        stamp_blocked_rows_adopted=False,
        mode=mode,
        scheduler_lease_owner=scheduler_lease_owner,
    )


def _nested_collector_processor(
    factory: RecorderFactory,
    collector: CollectorExecutor,
    coalesce: CoalesceExecutor,
    clock: MockClock,
    *,
    branch_transform: Any | None = None,
    barrier_restore: BarrierJournalRestoreContext | None = None,
) -> RowProcessor:
    """opener -> split(a, b) -> [err_b on b] -> merge (require_all) -> collector -> sink."""
    return _make_processor(
        factory,
        collector_executor=collector,
        collector_node_ids={_COLLECTOR: _COLLECTOR_NODE},
        collector_on_success_map={_COLLECTOR: "out"},
        coalesce_executor=coalesce,
        coalesce_node_ids={_MERGE: _COALESCE_NODE},
        branch_to_coalesce={BranchName("a"): _MERGE, BranchName("b"): _MERGE},
        coalesce_on_success_map={_MERGE: str(_COLLECTOR)},
        node_step_map={_OPENER_NODE: 1, _FORK_NODE: 2, _ERR_B_NODE: 3, _COALESCE_NODE: 4, _COLLECTOR_NODE: 5},
        node_to_next={
            _OPENER_NODE: _FORK_NODE,
            _FORK_NODE: _COALESCE_NODE,
            _ERR_B_NODE: _COALESCE_NODE,
            _COALESCE_NODE: _COLLECTOR_NODE,
            _COLLECTOR_NODE: None,
        },
        node_to_plugin={_ERR_B_NODE: branch_transform} if branch_transform is not None else {},
        structural_node_ids=frozenset({NodeID("source-0"), _OPENER_NODE, _FORK_NODE}),
        group_bindings=_collector_registry(nested=True),
        sink_names=frozenset({"out"}),
        clock=clock,
        barrier_restore=barrier_restore,
        stamp_blocked_rows_adopted=False,
    )


def _mint_expand_group(factory: RecorderFactory, processor: RowProcessor) -> tuple[list[TokenInfo], str]:
    """Run the opener for real: its completed node_state, then expand_token
    (durable group_records + member frames + token_parents ordinals)."""
    parent = make_token_info(row_id="row-1", token_id=_OPENER_TOKEN, data={"id": 1, "value": 0})
    _persist_token_for_scheduler(factory, parent)
    factory.execution.record_completed_node_state(
        token_id=parent.token_id,
        node_id=str(_OPENER_NODE),
        run_id=RUN_ID,
        step_index=1,
        input_data={"id": 1},
        output_data={"id": 1},
        duration_ms=1.0,
    )
    contract = SchemaContract(
        mode="OBSERVED",
        fields=(
            FieldContract(normalized_name="id", original_name="id", python_type=int, required=False, source="inferred"),
            FieldContract(normalized_name="value", original_name="value", python_type=int, required=False, source="inferred"),
        ),
        locked=True,
    )
    return processor._token_manager.expand_token(
        parent_token=parent, expanded_rows=list(_MEMBER_ROWS), output_contract=contract, node_id=_OPENER_NODE, run_id=RUN_ID
    )


def _members_by_ordinal(db: LandscapeDB) -> list[tuple[str, int]]:
    """META-9.2 ordinal oracle: the opener's children from token_parents, by ordinal."""
    with db.connection() as conn:
        rows = conn.execute(
            select(token_parents_table.c.token_id, token_parents_table.c.ordinal)
            .where(token_parents_table.c.parent_token_id == _OPENER_TOKEN)
            .order_by(token_parents_table.c.ordinal)
        ).all()
    return [(row.token_id, row.ordinal) for row in rows]


def _member_token_id(db: LandscapeDB, ordinal: int) -> str:
    return next(tid for tid, ordinal_seen in _members_by_ordinal(db) if ordinal_seen == ordinal)


def _expand_group_id(db: LandscapeDB) -> str:
    with db.connection() as conn:
        return str(
            conn.execute(select(group_records_table.c.group_id).where(group_records_table.c.opener_token_id == _OPENER_TOKEN)).scalar_one()
        )


def _rebuild_token(factory: RecorderFactory, token_id: str, ordinal: int) -> TokenInfo:
    """A token as a process that never minted it sees it: identity from the
    durable ordinal oracle, lineage from token_lineage_frames, the row the
    opener minted (fork children copy their parent's row)."""
    paths = factory.data_flow.load_lineage_paths(RUN_ID, [token_id])
    return TokenInfo(row_id="row-1", token_id=token_id, row_data=make_row(dict(_MEMBER_ROWS[ordinal])), lineage_path=paths[token_id])


def _rebuild_member(factory: RecorderFactory, db: LandscapeDB, ordinal: int) -> TokenInfo:
    return _rebuild_token(factory, _member_token_id(db, ordinal), ordinal)


def _fork_child_token_id(db: LandscapeDB, member_token_id: str, branch: str) -> str:
    """The fork child of ``member_token_id`` on ``branch`` — its FORK group is
    the one group_records row opened by that member."""
    with db.connection() as conn:
        fork_group_id = conn.execute(
            select(group_records_table.c.group_id).where(
                group_records_table.c.opener_token_id == member_token_id, group_records_table.c.kind == FrameKind.FORK.value
            )
        ).scalar_one()
        return str(
            conn.execute(
                select(token_lineage_frames_table.c.token_id).where(
                    token_lineage_frames_table.c.group_id == fork_group_id, token_lineage_frames_table.c.member_key == branch
                )
            ).scalar_one()
        )


def _arrive_collector(factory: RecorderFactory, processor: RowProcessor, token: TokenInfo, *, ingest_sequence: int) -> list[Any]:
    """One member arrival on the live path: BLOCKED deposit under the compound
    key + live stash + intake (the item-1 writer, driven the way the drain does)."""
    barrier_key = collector_barrier_key(str(_COLLECTOR), token.lineage_path[-1].group_id)
    _persist_blocked_scheduler_work(
        factory,
        processor,
        token,
        node_id=_COLLECTOR_NODE,
        barrier_key=barrier_key,
        adopted=False,
        ingest_sequence=ingest_sequence,
        collector_name=str(_COLLECTOR),
    )
    processor._live_barrier_holds[token.token_id] = _LiveBarrierHold(token=token, barrier_key=barrier_key)
    return processor.run_barrier_intake(make_context(landscape=factory.plugin_audit_writer()))


def _arrive_coalesce(factory: RecorderFactory, processor: RowProcessor, token: TokenInfo, *, ingest_sequence: int) -> list[Any]:
    """A fork branch's arrival at the in-scope coalesce (the coalesce twin of
    ``_arrive_collector``; the fork group's record was minted by fork_token)."""
    _persist_blocked_scheduler_work(
        factory,
        processor,
        token,
        node_id=_COALESCE_NODE,
        barrier_key=str(_MERGE),
        adopted=False,
        ingest_sequence=ingest_sequence,
        coalesce_name=str(_MERGE),
    )
    processor._live_barrier_holds[token.token_id] = _LiveBarrierHold(token=token, barrier_key=str(_MERGE))
    return processor.run_barrier_intake(make_context(landscape=factory.plugin_audit_writer()))


def _drive_member_through_page(factory: RecorderFactory, processor: RowProcessor, token: TokenInfo) -> list[Any]:
    """A member on the REAL drain from err_page: the transform runs (completes,
    or errors with on_error: discard), and the traversal itself deposits the
    collector hold / settles the loss — nothing crafted past this call."""
    item = WorkItem(token=token, current_node_id=_ERR_NODE, collector_name=_COLLECTOR)
    return processor._drain_durable_work_queue(item, make_context(landscape=factory.plugin_audit_writer()))


def _lose_fork_branch(factory: RecorderFactory, processor: RowProcessor, token: TokenInfo) -> list[Any]:
    """A fork branch errors at err_b (on_error: discard) on the REAL drain, its
    cursor naming the in-scope coalesce."""
    item = WorkItem(token=token, current_node_id=_ERR_B_NODE, coalesce_node_id=_COALESCE_NODE, coalesce_name=_MERGE)
    return processor._drain_durable_work_queue(item, make_context(landscape=factory.plugin_audit_writer()))


def _register_worker(db: LandscapeDB, worker_id: str, *, role: str) -> None:
    """The takeover leader must be an active run_workers member for the drain's
    membership fence (ADR-030 §G) — the usurped seat alone is not a registration."""
    registered_at = datetime.now(UTC)
    with db.write_connection() as conn:
        conn.execute(
            insert(run_workers_table).values(
                worker_id=worker_id,
                run_id=RUN_ID,
                role=role,
                status="active",
                registered_at=registered_at,
                heartbeat_expires_at=registered_at + timedelta(hours=1),
            )
        )


# ----- durable-image readers (every scenario's preconditions read these) -----


def _work_rows(conn: Any) -> dict[str, tuple[str, str | None, int | None]]:
    """token_id -> (status, barrier_key, barrier_adopted_epoch)."""
    rows = conn.execute(
        select(
            token_work_items_table.c.token_id,
            token_work_items_table.c.status,
            token_work_items_table.c.barrier_key,
            token_work_items_table.c.barrier_adopted_epoch,
        )
    ).all()
    return {row.token_id: (row.status, row.barrier_key, row.barrier_adopted_epoch) for row in rows}


def _outcome_rows(conn: Any) -> dict[str, tuple[str, str]]:
    rows = conn.execute(select(token_outcomes_table.c.token_id, token_outcomes_table.c.outcome, token_outcomes_table.c.path)).all()
    return {row.token_id: (row.outcome, row.path) for row in rows}


def _loss_rows(conn: Any) -> list[tuple[str, str, str, str, str, int | None]]:
    rows = conn.execute(
        select(
            group_losses_table.c.closer_name,
            group_losses_table.c.group_id,
            group_losses_table.c.member_key,
            group_losses_table.c.token_id,
            group_losses_table.c.reason,
            group_losses_table.c.adopted_epoch,
        ).order_by(group_losses_table.c.closer_name)
    ).all()
    return [tuple(row) for row in rows]


def _group_rows(conn: Any) -> list[tuple[str, int, str | None]]:
    """(kind, member_count, closes_group_id) sorted — a release group is the only
    row whose closes_group_id is non-NULL (META-38 written fact)."""
    rows = conn.execute(select(group_records_table.c.kind, group_records_table.c.member_count, group_records_table.c.closes_group_id)).all()
    return sorted((row.kind, row.member_count, row.closes_group_id) for row in rows)


def _node_state_rows(conn: Any, node_id: NodeID) -> dict[str, tuple[str, bool]]:
    """token_id -> (status, completed_at is set) at one node."""
    rows = conn.execute(
        select(node_states_table.c.token_id, node_states_table.c.status, node_states_table.c.completed_at).where(
            node_states_table.c.node_id == str(node_id)
        )
    ).all()
    return {row.token_id: (row.status, row.completed_at is not None) for row in rows}


def _collector_key(db: LandscapeDB) -> str:
    return collector_barrier_key(str(_COLLECTOR), _expand_group_id(db))


# ----- first-process actions (module-level: they cross the spawn boundary) -----


def _run_collector_to_arrival_seam(db: LandscapeDB, pause: Callable[[], None], payload_path: str) -> None:
    """Mint a two-member group, adopt member 0, then pause before member 1 arrives."""
    factory = _new_barrier_factory(db, payload_path)
    clock = MockClock(start=_T0)
    executor = _real_collector_executor(factory, clock)
    processor = _collector_processor(factory, executor, clock)
    children, _group_id = _mint_expand_group(factory, processor)
    assert _arrive_collector(factory, processor, children[0], ingest_sequence=1) == []
    pause()
    _arrive_collector(factory, processor, children[1], ingest_sequence=2)


def _run_collector_to_flush_seam(db: LandscapeDB, pause: Callable[[], None], payload_path: str) -> None:
    """Adopt both members; the closing arrival's flush pauses BEFORE any flush effect."""
    factory = _new_barrier_factory(db, payload_path)
    clock = MockClock(start=_T0)
    executor = _real_collector_executor(factory, clock)
    real_flush = CollectorExecutor._execute_flush

    def pause_before_flush(self: CollectorExecutor, *args: Any, **kwargs: Any) -> Any:
        pause()
        return real_flush(self, *args, **kwargs)

    CollectorExecutor._execute_flush = pause_before_flush  # type: ignore[method-assign]
    processor = _collector_processor(factory, executor, clock)
    children, _group_id = _mint_expand_group(factory, processor)
    assert _arrive_collector(factory, processor, children[0], ingest_sequence=1) == []
    _arrive_collector(factory, processor, children[1], ingest_sequence=2)


def _run_collector_to_arrival_clean(db: LandscapeDB, payload_path: str) -> None:
    """The opener's worker: mint, adopt member 0, exit cleanly (no crash image)."""
    factory = _new_barrier_factory(db, payload_path)
    clock = MockClock(start=_T0)
    executor = _real_collector_executor(factory, clock)
    processor = _collector_processor(factory, executor, clock)
    children, _group_id = _mint_expand_group(factory, processor)
    assert _arrive_collector(factory, processor, children[0], ingest_sequence=1) == []


def _run_leader_holding_open_group(db: LandscapeDB, pause: Callable[[], None], payload_path: str) -> None:
    """(c') The opener's LEADER: adopt member 0, pause holding the open group
    while a follower process loses member 1, then replay that loss on release."""
    factory = _new_barrier_factory(db, payload_path)
    clock = MockClock(start=_T0)
    executor = _real_collector_executor(factory, clock)
    processor = _collector_processor(factory, executor, clock)
    children, _group_id = _mint_expand_group(factory, processor)
    assert _arrive_collector(factory, processor, children[0], ingest_sequence=1) == []
    assert processor._group_bindings.binding_for(children[1].lineage_path[-1]) is not None  # the opener's own registry
    pause()
    # The replay is the leader's ONLY notify for a collector loss (spec §6.2 /
    # the COLLECTOR arm of _replay_group_losses): one intake pass adopts the
    # follower's NULL-epoch row and fails the roster through the collector arm.
    assert _collector_plugin(executor).batch_calls == 0
    results = processor.run_barrier_intake(make_context(landscape=factory.plugin_audit_writer()))
    assert [(result.token.token_id, result.outcome, result.path) for result in results] == [
        (children[0].token_id, TerminalOutcome.FAILURE, TerminalPath.UNROUTED)
    ]
    assert _collector_plugin(executor).batch_calls == 0
    assert processor.has_blocked_barrier_work() is False


def _run_follower_losing_member(db: LandscapeDB, payload_path: str) -> None:
    """(c') A real FOLLOWER (no executor, no fence, fresh registry) loses member
    1 on its drain: the settle seam re-derives the EXPAND binding (META-9.1)
    and STAGES the loss; its claim disposition commits it with adopted_epoch
    NULL. The leader is alive and holds the seat — no takeover here."""
    factory = RecorderFactory(db, payload_store=FilesystemPayloadStore(Path(payload_path)))
    clock = MockClock(start=_T0 + 5)
    processor = _collector_processor(
        factory,
        None,
        clock,
        page_transform=_page_transform(factory, fails=True),
        mode=ProcessorMode.FOLLOWER,
        scheduler_lease_owner="worker-follower",
    )
    member_1 = _rebuild_member(factory, db, 1)
    assert processor._group_bindings.binding_for(member_1.lineage_path[-1]) is None  # never ran the opener
    results = _drive_member_through_page(factory, processor, member_1)
    assert [(result.token.token_id, result.outcome, result.path) for result in results] == [
        (member_1.token_id, TerminalOutcome.FAILURE, TerminalPath.QUARANTINED_AT_SOURCE)
    ]


def _run_collector_realistic_to_arrival_seam(db: LandscapeDB, pause: Callable[[], None], payload_path: str) -> None:
    """(e) Member 0 travels the REAL drain: completes err_page, holds at the
    collector (adopted by the drain's own intake); pause before member 1."""
    factory = _new_barrier_factory(db, payload_path)
    clock = MockClock(start=_T0)
    executor = _real_collector_executor(factory, clock)
    processor = _collector_processor(factory, executor, clock, page_transform=_page_transform(factory, fails=False))
    children, _group_id = _mint_expand_group(factory, processor)
    assert _drive_member_through_page(factory, processor, children[0]) == []
    pause()
    _drive_member_through_page(factory, processor, children[1])


def _run_nested_to_mid_unwrap_seam(db: LandscapeDB, pause: Callable[[], None], payload_path: str) -> None:
    """(d') Member 0 forks, both branches merge (the merged token holds at the
    collector); member 1 forks and branch a holds at the coalesce; pause."""
    factory = _new_barrier_factory(db, payload_path)
    clock = MockClock(start=_T0)
    collector = _real_collector_executor(factory, clock)
    coalesce = _real_nested_coalesce_executor(factory, clock)
    processor = _nested_collector_processor(factory, collector, coalesce, clock)
    children, _group_id = _mint_expand_group(factory, processor)
    branches_0, _fork_0 = processor._token_manager.fork_token(children[0], ["a", "b"], node_id=_FORK_NODE, run_id=RUN_ID)
    assert _arrive_coalesce(factory, processor, branches_0[0], ingest_sequence=1) == []
    assert _arrive_coalesce(factory, processor, branches_0[1], ingest_sequence=2) == []  # merged -> held at the collector
    branches_1, _fork_1 = processor._token_manager.fork_token(children[1], ["a", "b"], node_id=_FORK_NODE, run_id=RUN_ID)
    assert _arrive_coalesce(factory, processor, branches_1[0], ingest_sequence=3) == []
    assert processor.has_blocked_barrier_work() is True
    pause()
    _lose_fork_branch(factory, processor, branches_1[1])


# ----- second-process actions -----


def _takeover_collector_processor(
    db: LandscapeDB, payload_path: str, *, page_fails: bool | None, checkpoint_id: str
) -> tuple[RecorderFactory, RowProcessor, CollectorExecutor]:
    factory = RecorderFactory(db, payload_store=FilesystemPayloadStore(Path(payload_path)))
    clock = MockClock(start=_T0 + 10)
    _usurp_seat(db, clock)
    _register_worker(db, USURPER, role="leader")
    executor = _real_collector_executor(factory, clock)
    processor = _collector_processor(
        factory,
        executor,
        clock,
        page_transform=None if page_fails is None else _page_transform(factory, fails=page_fails),
        barrier_restore=BarrierJournalRestoreContext(resume_checkpoint_id=checkpoint_id, barrier_scalars=None, batch_id_remap={}),
    )
    return factory, processor, executor


def _assert_group_released_in_ordinal_order(db: LandscapeDB, executor: CollectorExecutor) -> None:
    plugin = _collector_plugin(executor)
    assert plugin.batch_calls == 1
    ordinal_values = [_MEMBER_ROWS[ordinal]["value"] for _token_id, ordinal in _members_by_ordinal(db)]
    assert plugin.seen == [ordinal_values]
    member_ids = {token_id for token_id, _ordinal in _members_by_ordinal(db)}
    with db.connection() as conn:
        work = _work_rows(conn)
        assert {token_id for token_id, (status, _key, _epoch) in work.items() if status == TokenWorkStatus.TERMINAL.value} == member_ids
        assert [status for status, _key, _epoch in work.values()].count(TokenWorkStatus.PENDING_SINK.value) == 1
        outcomes = _outcome_rows(conn)
        assert {outcomes[token_id] for token_id in member_ids} == {(TerminalOutcome.SUCCESS.value, TerminalPath.COALESCED.value)}
        groups = _group_rows(conn)
        assert [(kind, count) for kind, count, _closes in groups] == [("expand", 1), ("expand", 2)]
        assert [closes for _kind, count, closes in groups if count == 1] == [_expand_group_id(db)]  # the release group
        holds = _node_state_rows(conn, _COLLECTOR_NODE)
        assert set(holds) == member_ids | {_OPENER_TOKEN} and all(completed for _status, completed in holds.values())
        assert _loss_rows(conn) == []


def _resume_collector_complete_roster(db: LandscapeDB, payload_path: str) -> None:
    """(a) Fresh process: restore the one adopted hold, then member 1 arrives here."""
    factory, processor, executor = _takeover_collector_processor(
        db, payload_path, page_fails=None, checkpoint_id="process-death-collector-arrival"
    )
    assert processor.has_blocked_barrier_work() is True
    assert _collector_plugin(executor).batch_calls == 0
    _arrive_collector(factory, processor, _rebuild_member(factory, db, 1), ingest_sequence=2)
    _assert_group_released_in_ordinal_order(db, executor)


def _resume_collector_sweep(db: LandscapeDB, payload_path: str) -> None:
    """(b) Fresh process: both holds restore as a COMPLETE roster, parked; the
    first ctx-bearing intake pass sweeps it (META-31) and the plugin runs
    exactly once, here."""
    factory, processor, executor = _takeover_collector_processor(
        db, payload_path, page_fails=None, checkpoint_id="process-death-collector-flush"
    )
    assert processor.has_blocked_barrier_work() is True
    assert _collector_plugin(executor).batch_calls == 0
    processor.run_barrier_intake(make_context(landscape=factory.plugin_audit_writer()))
    _assert_group_released_in_ordinal_order(db, executor)


def _resume_collector_realistic(db: LandscapeDB, payload_path: str) -> None:
    """(e) Fresh process: the restore cross-check sees err_page in the group's
    completion evidence and must NOT refuse (META-35 site 2); member 1 then
    travels the same real drain and completes the roster."""
    factory, processor, executor = _takeover_collector_processor(
        db, payload_path, page_fails=False, checkpoint_id="process-death-collector-realistic"
    )
    assert processor.has_blocked_barrier_work() is True
    assert _collector_plugin(executor).batch_calls == 0
    assert _drive_member_through_page(factory, processor, _rebuild_member(factory, db, 1)) != []  # the release routed to the sink
    _assert_group_released_in_ordinal_order(db, executor)


def _assert_flat_group_failed_by_lost_member_1(db: LandscapeDB, *, adopted_epoch: int) -> None:
    member_0, member_1 = _member_token_id(db, 0), _member_token_id(db, 1)
    with db.connection() as conn:
        assert _loss_rows(conn) == [(str(_COLLECTOR), _expand_group_id(db), member_1, member_1, "quarantined", adopted_epoch)]
        outcomes = _outcome_rows(conn)
        assert outcomes[member_1] == (TerminalOutcome.FAILURE.value, TerminalPath.QUARANTINED_AT_SOURCE.value)
        assert outcomes[member_0] == (TerminalOutcome.FAILURE.value, TerminalPath.UNROUTED.value)
        assert _work_rows(conn)[member_0][0] == TokenWorkStatus.TERMINAL.value
        assert _group_rows(conn) == [("expand", 2, None)]  # no release group
        assert _node_state_rows(conn, _COLLECTOR_NODE) == {member_0: ("failed", True)}
        assert _node_state_rows(conn, _ERR_NODE) == {member_1: ("failed", True)}  # META-35: this row is in the resolver's set


def _takeover_and_lose_member(db: LandscapeDB, payload_path: str) -> None:
    """(c)/(d) A worker that never ran the opener loses member 1 on the REAL
    drain: the settle seam re-derives the EXPAND binding durably (META-9.1 —
    the failed err_page node_state is in the resolver's set, META-35 site 1),
    stages the loss, and the intake replay settles the roster through the
    COLLECTOR arm — require_all fails the group, member 0 (held) terminalizes."""
    factory, processor, executor = _takeover_collector_processor(
        db, payload_path, page_fails=True, checkpoint_id="process-death-collector-loss"
    )
    member_1 = _rebuild_member(factory, db, 1)
    assert processor._group_bindings.binding_for(member_1.lineage_path[-1]) is None  # fresh registry: the miss META-9.1 exists for
    _drive_member_through_page(factory, processor, member_1)
    processor.run_barrier_intake(make_context(landscape=factory.plugin_audit_writer()))
    assert _collector_plugin(executor).batch_calls == 0
    _assert_flat_group_failed_by_lost_member_1(db, adopted_epoch=2)  # adopted by the takeover epoch
    assert processor.has_blocked_barrier_work() is False


def _takeover_and_lose_nested_branch(db: LandscapeDB, payload_path: str) -> None:
    """(d') Fresh process: both barriers restore (member 0's merged token at the
    collector, member 1's branch a at the coalesce). Losing branch b fails the
    coalesce; its consumed sibling's REMAINING lineage is the EXPAND frame,
    which the escalation walk resolves through the re-derivation (fresh
    registry) and fails the collector group — the unwrap crosses the
    FORK->EXPAND boundary in a process that never minted either group."""
    factory = RecorderFactory(db, payload_store=FilesystemPayloadStore(Path(payload_path)))
    clock = MockClock(start=_T0 + 10)
    _usurp_seat(db, clock)
    _register_worker(db, USURPER, role="leader")
    _assert_satisfiable(db, nested=True)  # the open mid-unwrap image is resumable (spec §8)
    collector = _real_collector_executor(factory, clock)
    coalesce = _real_nested_coalesce_executor(factory, clock)
    processor = _nested_collector_processor(
        factory,
        collector,
        coalesce,
        clock,
        branch_transform=_page_transform(factory, fails=True, node_id=_ERR_B_NODE),
        barrier_restore=BarrierJournalRestoreContext(
            resume_checkpoint_id="process-death-collector-nested", barrier_scalars=None, batch_id_remap={}
        ),
    )
    assert processor.has_blocked_barrier_work() is True
    member_1 = _member_token_id(db, 1)
    branch_b = _rebuild_token(factory, _fork_child_token_id(db, member_1, "b"), 1)
    assert [frame.kind for frame in branch_b.lineage_path] == [FrameKind.EXPAND, FrameKind.FORK]
    assert processor._group_bindings.binding_for(branch_b.lineage_path[0]) is None  # the EXPAND frame: fresh registry
    results = _lose_fork_branch(factory, processor, branch_b)
    results.extend(processor.run_barrier_intake(make_context(landscape=factory.plugin_audit_writer())))
    branch_a = _fork_child_token_id(db, member_1, "a")
    with db.connection() as conn:
        outcomes = _outcome_rows(conn)
        # Member 0's merged successor: the one token that held under the
        # collector's compound key (its id is minted by the merge).
        merged_0 = next(
            token_id
            for token_id, (status, key, _epoch) in _work_rows(conn).items()
            if key == _collector_key(db) and status == TokenWorkStatus.TERMINAL.value
        )
        assert outcomes[branch_b.token_id] == (TerminalOutcome.FAILURE.value, TerminalPath.QUARANTINED_AT_SOURCE.value)
        assert outcomes[branch_a] == (TerminalOutcome.FAILURE.value, TerminalPath.UNROUTED.value)
        assert outcomes[merged_0] == (TerminalOutcome.FAILURE.value, TerminalPath.UNROUTED.value)
        fork_group_1 = branch_b.lineage_path[1].group_id
        assert _loss_rows(conn) == [
            (str(_MERGE), fork_group_1, "b", branch_b.token_id, "quarantined", 2),
            (str(_COLLECTOR), _expand_group_id(db), member_1, member_1, "group_failed", 2),
        ]
        assert _group_rows(conn) == [("expand", 2, None), ("fork", 2, None), ("fork", 2, None)]  # no release group anywhere
        statuses = {status for status, _key, _epoch in _work_rows(conn).values()}
        assert TokenWorkStatus.BLOCKED.value not in statuses and TokenWorkStatus.PENDING_SINK.value not in statuses
    assert {(result.token.token_id, result.outcome) for result in results} == {
        (branch_b.token_id, TerminalOutcome.FAILURE),
        (branch_a, TerminalOutcome.FAILURE),
        (merged_0, TerminalOutcome.FAILURE),
    }
    assert _collector_plugin(collector).batch_calls == 0
    assert processor.has_blocked_barrier_work() is False
    _assert_satisfiable(db, nested=True)  # settled by losses, not stranded


# ----- crashed-image preconditions -----


def _assert_killed_collector_image(database_url: str, *, adopted_ordinals: tuple[int, ...], page_completed: bool) -> None:
    """The exact durable image SIGKILL left: adopted BLOCKED holds under the
    compound key for exactly ``adopted_ordinals``, no flush effect (no release
    group, no member terminal, no completed collector node_state)."""
    with LandscapeDB.from_url(database_url, create_tables=False) as killed_db, killed_db.connection() as conn:
        held = {_member_token_id(killed_db, ordinal) for ordinal in adopted_ordinals}
        assert _work_rows(conn) == {token_id: (TokenWorkStatus.BLOCKED.value, _collector_key(killed_db), 1) for token_id in held}
        assert _node_state_rows(conn, _COLLECTOR_NODE) == dict.fromkeys(held, ("open", False))
        assert _node_state_rows(conn, _ERR_NODE) == (dict.fromkeys(held, ("completed", True)) if page_completed else {})
        assert _group_rows(conn) == [("expand", 2, None)]
        assert _outcome_rows(conn) == {_OPENER_TOKEN: (TerminalOutcome.TRANSIENT.value, TerminalPath.EXPAND_PARENT.value)}
        assert _loss_rows(conn) == []


def _assert_killed_nested_image(database_url: str) -> None:
    """(d') Two durably-BLOCKED rows at two nesting levels: member 0's merged
    token at the collector (compound key) and member 1's branch a at the
    coalesce; both fork parents and member 0's branches terminal; no losses,
    no release group."""
    with LandscapeDB.from_url(database_url, create_tables=False) as killed_db, killed_db.connection() as conn:
        member_0, member_1 = _member_token_id(killed_db, 0), _member_token_id(killed_db, 1)
        a_0, b_0, a_1 = (
            _fork_child_token_id(killed_db, member_0, "a"),
            _fork_child_token_id(killed_db, member_0, "b"),
            _fork_child_token_id(killed_db, member_1, "a"),
        )
        work = _work_rows(conn)
        blocked = {token_id: (key, epoch) for token_id, (status, key, epoch) in work.items() if status == TokenWorkStatus.BLOCKED.value}
        assert a_1 in blocked and len(blocked) == 2, blocked
        merged_0 = next(token_id for token_id in blocked if token_id != a_1)  # the merged successor's id is minted by the merge
        assert blocked == {merged_0: (_collector_key(killed_db), 1), a_1: (str(_MERGE), 1)}
        assert {token_id: status for token_id, (status, _key, _epoch) in work.items() if token_id != merged_0 and token_id != a_1} == {
            a_0: TokenWorkStatus.TERMINAL.value,
            b_0: TokenWorkStatus.TERMINAL.value,
        }
        assert _outcome_rows(conn) == {
            _OPENER_TOKEN: (TerminalOutcome.TRANSIENT.value, TerminalPath.EXPAND_PARENT.value),
            member_0: (TerminalOutcome.TRANSIENT.value, TerminalPath.FORK_PARENT.value),
            member_1: (TerminalOutcome.TRANSIENT.value, TerminalPath.FORK_PARENT.value),
            a_0: (TerminalOutcome.SUCCESS.value, TerminalPath.COALESCED.value),
            b_0: (TerminalOutcome.SUCCESS.value, TerminalPath.COALESCED.value),
        }
        assert _node_state_rows(conn, _COLLECTOR_NODE) == {merged_0: ("open", False)}
        assert _node_state_rows(conn, _COALESCE_NODE) == {a_0: ("completed", True), b_0: ("completed", True), a_1: ("open", False)}
        assert _group_rows(conn) == [("expand", 2, None), ("fork", 2, None), ("fork", 2, None)]
        assert _loss_rows(conn) == []


def _assert_follower_handoff_image(database_url: str) -> None:
    """(c') Between the follower's exit and the leader's release: the loss is
    durable with adopted_epoch NULL, member 1 is terminal, the group is still
    OPEN (member 0 BLOCKED, no collector node_state for member 1, no release)."""
    with LandscapeDB.from_url(database_url, create_tables=False) as db, db.connection() as conn:
        member_0, member_1 = _member_token_id(db, 0), _member_token_id(db, 1)
        assert _loss_rows(conn) == [(str(_COLLECTOR), _expand_group_id(db), member_1, member_1, "quarantined", None)]
        # The WHOLE journal: member 0's adopted hold, and member 1's own claim
        # row FAILED under the same compound key (the follower's cursor), never
        # adopted (a follower carries no fence) — nothing else.
        assert _work_rows(conn) == {
            member_0: (TokenWorkStatus.BLOCKED.value, _collector_key(db), 1),
            member_1: (TokenWorkStatus.FAILED.value, _collector_key(db), None),
        }
        assert _outcome_rows(conn) == {
            _OPENER_TOKEN: (TerminalOutcome.TRANSIENT.value, TerminalPath.EXPAND_PARENT.value),
            member_1: (TerminalOutcome.FAILURE.value, TerminalPath.QUARANTINED_AT_SOURCE.value),
        }
        assert _node_state_rows(conn, _COLLECTOR_NODE) == {member_0: ("open", False)}
        assert _node_state_rows(conn, _ERR_NODE) == {member_1: ("failed", True)}
        assert _group_rows(conn) == [("expand", 2, None)]


# ----- exercises -----


def _fresh_database(tmp_path: Path, name: str) -> tuple[str, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database_url = f"sqlite:///{tmp_path / name}"
    with LandscapeDB(database_url):
        pass
    return database_url, str(tmp_path / "payloads")


def _exercise_collector_arrival_death(tmp_path: Path) -> LandscapeDB:
    database_url, payload_path = _fresh_database(tmp_path, "collector-arrival.db")
    _kill_at_seam(database_url, _run_collector_to_arrival_seam, (payload_path,))
    _assert_killed_collector_image(database_url, adopted_ordinals=(0,), page_completed=False)
    _run_fresh_recovery(database_url, _resume_collector_complete_roster, (payload_path,))
    return LandscapeDB.from_url(database_url, create_tables=False)


def _exercise_collector_flush_death(tmp_path: Path) -> LandscapeDB:
    database_url, payload_path = _fresh_database(tmp_path, "collector-flush.db")
    _kill_at_seam(database_url, _run_collector_to_flush_seam, (payload_path,))
    _assert_killed_collector_image(database_url, adopted_ordinals=(0, 1), page_completed=False)
    _run_fresh_recovery(database_url, _resume_collector_sweep, (payload_path,))
    return LandscapeDB.from_url(database_url, create_tables=False)


def _exercise_collector_non_opener_worker_loss(tmp_path: Path) -> LandscapeDB:
    """(c) No crash: the opener's worker exits cleanly; a second worker that never
    ran the opener takes the seat and loses a member."""
    database_url, payload_path = _fresh_database(tmp_path, "collector-non-opener-loss.db")
    _run_fresh_recovery(database_url, _run_collector_to_arrival_clean, (payload_path,))
    _assert_killed_collector_image(database_url, adopted_ordinals=(0,), page_completed=False)  # same image, clean exit
    _run_fresh_recovery(database_url, _takeover_and_lose_member, (payload_path,))
    return LandscapeDB.from_url(database_url, create_tables=False)


def _exercise_collector_follower_loss(tmp_path: Path) -> LandscapeDB:
    """(c') Leader alive and paused holding the open group; a follower process
    loses member 1; the leader is released and replays the loss."""
    database_url, payload_path = _fresh_database(tmp_path, "collector-follower-loss.db")
    with spawn_database_process_with_pause(
        database_url=database_url,
        seam=_LEADER_HOLDING_SEAM,
        action=_run_leader_holding_open_group,
        action_args=(payload_path,),
    ) as leader:
        ready = leader.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        assert ready.pid != os.getpid()
        _assert_killed_collector_image(database_url, adopted_ordinals=(0,), page_completed=False)
        _run_fresh_recovery(database_url, _run_follower_losing_member, (payload_path,))
        _assert_follower_handoff_image(database_url)
        leader.release()
        assert leader.wait_for_exit(timeout=_PROCESS_TIMEOUT_SECONDS).exitcode == 0
    recovered = LandscapeDB.from_url(database_url, create_tables=False)
    _assert_flat_group_failed_by_lost_member_1(recovered, adopted_epoch=1)  # adopted by the LIVE leader's epoch
    return recovered


def _exercise_collector_post_resume_loss(tmp_path: Path) -> LandscapeDB:
    """(d) The opener's worker is KILLED after member 0's adoption; the resumed
    process loses member 1."""
    database_url, payload_path = _fresh_database(tmp_path, "collector-post-resume-loss.db")
    _kill_at_seam(database_url, _run_collector_to_arrival_seam, (payload_path,))
    _assert_killed_collector_image(database_url, adopted_ordinals=(0,), page_completed=False)
    _run_fresh_recovery(database_url, _takeover_and_lose_member, (payload_path,))
    return LandscapeDB.from_url(database_url, create_tables=False)


def _exercise_collector_post_resume_loss_nested(tmp_path: Path) -> LandscapeDB:
    """(d') Killed mid-unwrap with two open barriers; the resumed process loses a
    fork branch and the escalation crosses into the scope."""
    database_url, payload_path = _fresh_database(tmp_path, "collector-post-resume-loss-nested.db")
    _kill_at_seam(database_url, _run_nested_to_mid_unwrap_seam, (payload_path,))
    _assert_killed_nested_image(database_url)
    _run_fresh_recovery(database_url, _takeover_and_lose_nested_branch, (payload_path,))
    return LandscapeDB.from_url(database_url, create_tables=False)


def _exercise_collector_realistic_shape_resume(tmp_path: Path) -> LandscapeDB:
    """(e) explode -> transform -> collector on the real drain, killed after
    member 0's hold; resume must not refuse and must complete the roster."""
    database_url, payload_path = _fresh_database(tmp_path, "collector-realistic.db")
    _kill_at_seam(database_url, _run_collector_realistic_to_arrival_seam, (payload_path,))
    _assert_killed_collector_image(database_url, adopted_ordinals=(0,), page_completed=True)
    _run_fresh_recovery(database_url, _resume_collector_realistic, (payload_path,))
    return LandscapeDB.from_url(database_url, create_tables=False)


def _observe_single_process(db: LandscapeDB, request: pytest.FixtureRequest) -> None:
    if not request.config.pluginmanager.hasplugin("scripts.state_engine_profile_reporter"):
        return
    reporter = cast("RuntimeProfileReporter", request.getfixturevalue("state_engine_profile"))
    raw = db.engine.raw_connection()
    try:
        connection = raw.driver_connection
        assert isinstance(connection, sqlite3.Connection)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        reporter.observe_sqlite(connection, deployment="single-process-leader")
    finally:
        raw.close()


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL exit-code oracle is POSIX-specific")
@pytest.mark.parametrize(
    "barrier_family",
    (
        "aggregation",
        "coalesce",
        "row_union",
        "collector_arrival_death",
        "collector_flush_death",
        "collector_non_opener_worker_loss",
        "collector_follower_loss",
        "collector_post_resume_loss",
        "collector_post_resume_loss_nested",
        "collector_realistic_shape_resume",
    ),
)
def test_single_process_leader_barrier_process_death_matrix(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    barrier_family: str,
) -> None:
    exercises = {
        "aggregation": _exercise_aggregation,
        "coalesce": _exercise_coalesce,
        "row_union": _exercise_row_union,
        "collector_arrival_death": _exercise_collector_arrival_death,
        "collector_flush_death": _exercise_collector_flush_death,
        "collector_non_opener_worker_loss": _exercise_collector_non_opener_worker_loss,
        "collector_follower_loss": _exercise_collector_follower_loss,
        "collector_post_resume_loss": _exercise_collector_post_resume_loss,
        "collector_post_resume_loss_nested": _exercise_collector_post_resume_loss_nested,
        "collector_realistic_shape_resume": _exercise_collector_realistic_shape_resume,
    }
    db = exercises[barrier_family](tmp_path)
    try:
        # One reporter observation covers the three selected family nodes in
        # this session; the row-union case is last by declared parameter order.
        if barrier_family == "row_union":
            _observe_single_process(db, request)
    finally:
        db.close()
