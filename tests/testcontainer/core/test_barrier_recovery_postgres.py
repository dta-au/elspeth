"""PostgreSQL 16 backend support for committed barrier-result recovery.

This is local backend qualification only.  It deliberately does not invoke
the deployment-profile reporter or claim the maintained AWS composition.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]
from tests.fixtures.plugins import CollectSink
from tests.integration.pipeline.test_aggregation_recovery import (
    _build_eof_aggregation_pipeline,
    _EmptyBatchTransform,
    _EnrichingPassthroughBatchTransform,
    _LoadCountingSource,
    _SumBatchTransform,
)
from tests.integration.pipeline.test_barrier_intake_dispositions import (
    _T0,
    _arrive_via_intake,
    _branch_token,
    _coalesce_processor,
    _real_coalesce_executor,
    _usurp_seat,
    _work_item_row,
)

from elspeth.contracts import NodeType, RunStatus
from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.contracts.schema import SchemaConfig
from elspeth.core.checkpoint import CheckpointManager, RecoveryManager
from elspeth.core.config import CheckpointSettings
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import (
    aggregation_results_table,
    batches_table,
    coalesce_effects_table,
    token_lineage_frames_table,
    tokens_table,
)
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.clock import MockClock
from elspeth.engine.executors.aggregation import AggregationExecutor
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.processor import BarrierJournalRestoreContext, RowProcessor

pytestmark = pytest.mark.testcontainer


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as postgres:
        yield postgres.get_connection_url()


@pytest.mark.timeout(120)
def test_postgres_recovers_completed_coalesce_effect_without_remerge(
    postgres_url: str,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = LandscapeDB.from_url(postgres_url)
    factory = RecorderFactory(db, payload_store=FilesystemPayloadStore(tmp_path / "payloads"))
    factory.run_lifecycle.begin_run(
        config={},
        canonical_version="v1",
        run_id="test-run",
        leader_worker_id="seeder",
    )
    factory.data_flow.register_node(
        run_id="test-run",
        plugin_name="test-source",
        node_type=NodeType.SOURCE,
        plugin_version="1.0",
        config={},
        node_id="source-0",
        schema_config=SchemaConfig.from_dict({"mode": "observed"}),
    )
    clock = MockClock(start=_T0)
    executor_a = _real_coalesce_executor(factory, clock, policy="require_all")

    def crash_before_barrier_completion(self: RowProcessor, **kwargs: Any) -> None:
        del self, kwargs
        raise RuntimeError("injected crash before coalesce barrier completion")

    real_complete = RowProcessor._complete_coalesce_fire
    monkeypatch.setattr(RowProcessor, "_complete_coalesce_fire", crash_before_barrier_completion)
    processor_a = _coalesce_processor(factory, executor_a, clock)
    assert _arrive_via_intake(factory, processor_a, _branch_token("a")) == []
    with pytest.raises(RuntimeError, match="injected crash before coalesce barrier completion"):
        _arrive_via_intake(factory, processor_a, _branch_token("b"), ingest_sequence=1)
    monkeypatch.setattr(RowProcessor, "_complete_coalesce_fire", real_complete)

    with db.connection() as conn:
        effect = conn.execute(select(coalesce_effects_table)).mappings().one()
    assert effect["status"] == "completed"
    assert _work_item_row(db, "tok-branch-a")["status"] == TokenWorkStatus.BLOCKED.value
    assert _work_item_row(db, "tok-branch-b")["status"] == TokenWorkStatus.BLOCKED.value

    _usurp_seat(db, clock)
    executor_b = _real_coalesce_executor(factory, clock, policy="require_all")
    _coalesce_processor(
        factory,
        executor_b,
        clock,
        barrier_restore=BarrierJournalRestoreContext(
            resume_checkpoint_id="ckpt-postgres",
            barrier_scalars=None,
            batch_id_remap={},
        ),
        stamp_blocked_rows_adopted=False,
    )

    assert executor_b._pending == {}
    assert _work_item_row(db, "tok-branch-a")["status"] == TokenWorkStatus.TERMINAL.value
    assert _work_item_row(db, "tok-branch-b")["status"] == TokenWorkStatus.TERMINAL.value
    merged_work = _work_item_row(db, str(effect["result_token_id"]))
    assert merged_work["status"] == TokenWorkStatus.PENDING_SINK.value


@pytest.mark.timeout(120)
def test_postgres_recovers_committed_aggregation_result_without_plugin_replay(
    postgres_url: str,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PostgreSQL uses the epoch-32 receipt at the pre-expansion death seam."""
    db = LandscapeDB.from_url(postgres_url)
    payload_store = FilesystemPayloadStore(tmp_path / "aggregation-payloads")
    checkpoint_manager = CheckpointManager(db)
    source = _LoadCountingSource([{"value": 10}, {"value": 20}, {"value": 30}], on_success="batch_in")
    transform = _SumBatchTransform()
    sink = CollectSink("output")
    config, graph = _build_eof_aggregation_pipeline(source, transform, sink)
    orchestrator = Orchestrator(
        db=db,
        checkpoint_manager=checkpoint_manager,
        checkpoint_config=RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row")),
    )
    real_execute_flush = AggregationExecutor.execute_flush

    def crash_before_output_routing(self: AggregationExecutor, *args: Any, **kwargs: Any) -> None:
        real_execute_flush(self, *args, **kwargs)
        raise RuntimeError("injected postgres death before aggregation output routing")

    monkeypatch.setattr(AggregationExecutor, "execute_flush", crash_before_output_routing)
    with pytest.raises(RuntimeError, match="injected postgres death before aggregation output routing"):
        orchestrator.run(config, graph=graph, payload_store=payload_store)
    monkeypatch.setattr(AggregationExecutor, "execute_flush", real_execute_flush)

    with db.connection() as conn:
        batch = conn.execute(select(batches_table.c.run_id, batches_table.c.status)).one()
        assert batch.status == "completed"
        assert (
            conn.execute(
                select(tokens_table.c.token_id)
                .where(tokens_table.c.run_id == batch.run_id)
                .where(
                    tokens_table.c.token_id.in_(
                        select(token_lineage_frames_table.c.token_id).where(
                            token_lineage_frames_table.c.run_id == batch.run_id,
                            token_lineage_frames_table.c.kind == "expand",
                        )
                    )
                )
            ).all()
            == []
        )

    resume_point = RecoveryManager(db, checkpoint_manager).get_resume_point(str(batch.run_id), graph)
    assert resume_point is not None
    resumed = orchestrator.resume(
        resume_point=resume_point,
        config=config,
        graph=graph,
        payload_store=payload_store,
    )

    assert resumed.status is RunStatus.COMPLETED
    assert sink.results == [{"value": 60, "count": 3}]
    assert transform.batch_calls == 1
    assert source.load_invocations == 1


@pytest.mark.timeout(120)
@pytest.mark.parametrize(
    ("output_mode", "expected_shape", "expected_rows"),
    [
        ("transform", "empty", []),
        ("passthrough", "empty", []),
        (
            "passthrough",
            "multi",
            [
                {"value": 10, "batch_enriched": True},
                {"value": 20, "batch_enriched": True},
            ],
        ),
    ],
)
def test_postgres_recovers_empty_and_passthrough_aggregation_receipts(
    postgres_url: str,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    output_mode: str,
    expected_shape: str,
    expected_rows: list[dict[str, object]],
) -> None:
    """PostgreSQL matches SQLite for the remaining epoch-32 receipt modes."""
    db = LandscapeDB.from_url(postgres_url)
    payload_store = FilesystemPayloadStore(tmp_path / f"{output_mode}-payloads")
    checkpoint_manager = CheckpointManager(db)
    source = _LoadCountingSource([{"value": 10}, {"value": 20}], on_success="batch_in")
    transform = _EmptyBatchTransform() if expected_shape == "empty" else _EnrichingPassthroughBatchTransform()
    sink = CollectSink("output")
    config, graph = _build_eof_aggregation_pipeline(source, transform, sink, output_mode=output_mode)
    orchestrator = Orchestrator(
        db=db,
        checkpoint_manager=checkpoint_manager,
        checkpoint_config=RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row")),
    )
    real_execute_flush = AggregationExecutor.execute_flush

    def crash_before_output_routing(self: AggregationExecutor, *args: Any, **kwargs: Any) -> None:
        real_execute_flush(self, *args, **kwargs)
        raise RuntimeError("injected postgres death before aggregation mode routing")

    monkeypatch.setattr(AggregationExecutor, "execute_flush", crash_before_output_routing)
    with pytest.raises(RuntimeError, match="injected postgres death before aggregation mode routing"):
        orchestrator.run(config, graph=graph, payload_store=payload_store)
    monkeypatch.setattr(AggregationExecutor, "execute_flush", real_execute_flush)

    with db.connection() as conn:
        receipt = (
            conn.execute(
                select(aggregation_results_table)
                .where(aggregation_results_table.c.output_mode == output_mode)
                .where(aggregation_results_table.c.output_shape == expected_shape)
                .order_by(aggregation_results_table.c.created_at.desc())
                .limit(1)
            )
            .mappings()
            .one()
        )
    assert receipt["expansion_parent_token_id"] is None

    resume_point = RecoveryManager(db, checkpoint_manager).get_resume_point(str(receipt["run_id"]), graph)
    assert resume_point is not None
    resumed = orchestrator.resume(
        resume_point=resume_point,
        config=config,
        graph=graph,
        payload_store=payload_store,
    )

    assert resumed.status is RunStatus.COMPLETED
    assert sink.results == expected_rows
    assert transform.batch_calls == 1
    assert source.load_invocations == 1
