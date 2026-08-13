"""Real process-death recovery at each durable barrier family.

The child reaches the narrow window after the barrier executor has committed
its durable result/node-state evidence but before ``complete_barrier`` emits
the continuation.  The parent sends SIGKILL, and a fresh process reopens the
same SQLite database. Aggregation resumes through the maintained Orchestrator
entry point; coalesce and row-union drive their real executor/repository
restore composition directly.

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
from sqlalchemy import select, update

from elspeth.contracts import NodeType, RunStatus, TokenInfo
from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.types import NodeID, RowUnionName
from elspeth.core.checkpoint import CheckpointManager, RecoveryManager
from elspeth.core.config import CheckpointSettings, RowUnionSettings
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import (
    aggregation_results_table,
    batches_table,
    coalesce_effects_table,
    node_states_table,
    run_coordination_table,
    token_work_items_table,
)
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.clock import MockClock
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.orchestrator.bootstrap import prepare_for_run
from elspeth.engine.processor import BarrierJournalRestoreContext, RowProcessor, _LiveBarrierHold
from elspeth.engine.row_union_executor import RowUnionExecutor
from elspeth.engine.spans import SpanFactory
from tests.e2e.recovery.harness import spawn_database_process_at_seam, spawn_database_process_with_pause
from tests.e2e.recovery.test_sink_effect_process_death_matrix import (
    _PROCESS_TIMEOUT_SECONDS,
    _install_short_run_liveness,
    _wait_until_run_is_resumable,
)
from tests.fixtures.factories import make_context
from tests.fixtures.plugins import CollectSink
from tests.integration.pipeline.test_aggregation_recovery import (
    _build_eof_aggregation_pipeline,
    _LoadCountingSource,
    _SumBatchTransform,
)
from tests.integration.pipeline.test_barrier_intake_dispositions import (
    RUN_ID,
    _arrive_via_intake,
    _branch_token,
    _coalesce_processor,
    _real_coalesce_executor,
    _usurp_seat,
)
from tests.unit.engine.test_processor import _make_processor, _persist_blocked_scheduler_work

if TYPE_CHECKING:
    from collections.abc import Callable

    from scripts.state_engine_profile_reporter import RuntimeProfileReporter


_CRASH_SEAM = "after-barrier-result-before-continuation"
_ROW_UNION = RowUnionName("variant_union")
_ROW_UNION_NODE = NodeID("row_union::variant_union")
_ROW_UNION_NEXT = NodeID("transform::after_union")
_T0 = 1_750_000_000.0
_SCHEMA_CONFIG = SchemaConfig(mode="observed", fields=None)


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
@pytest.mark.parametrize("barrier_family", ("aggregation", "coalesce", "row_union"))
def test_single_process_leader_barrier_process_death_matrix(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    barrier_family: str,
) -> None:
    exercises = {
        "aggregation": _exercise_aggregation,
        "coalesce": _exercise_coalesce,
        "row_union": _exercise_row_union,
    }
    db = exercises[barrier_family](tmp_path)
    try:
        # One reporter observation covers the three selected family nodes in
        # this session; the row-union case is last by declared parameter order.
        if barrier_family == "row_union":
            _observe_single_process(db, request)
    finally:
        db.close()
