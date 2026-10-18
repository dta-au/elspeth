"""E2E tests for partial failure scenarios.

Tests verify that ELSPETH handles partial failures correctly:
- Failed rows are recorded in the audit trail
- Successful rows still reach the sink
- All rows have terminal outcomes

Uses file-based SQLite and real payload stores. No mocks except
external services.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import and_, select

from elspeth.contracts import (
    Determinism,
    PipelineRow,
    PluginSchema,
    RunStatus,
    TerminalOutcome,
    TerminalPath,
)
from elspeth.core.config import SourceSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.lineage import explain
from elspeth.core.landscape.schema import (
    rows_table,
    token_outcomes_table,
    tokens_table,
    transform_errors_table,
)
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.testing import make_pipeline_row
from tests.fixtures.base_classes import (
    as_sink,
    as_source,
    as_transform,
)
from tests.fixtures.factories import wire_transforms
from tests.fixtures.plugins import CollectSink, ListSource

# ---------------------------------------------------------------------------
# Shared test schema and plugins
# ---------------------------------------------------------------------------


class _PartialSchema(PluginSchema):
    """Schema for partial failure test rows."""

    id: int
    value: int


class _SelectiveErrorTransform(BaseTransform):
    """Transform that errors on rows where id is in the fail set."""

    name = "selective_error"
    input_schema = _PartialSchema
    output_schema = _PartialSchema
    determinism = Determinism.DETERMINISTIC
    on_error = "discard"

    def __init__(self, fail_ids: set[int]) -> None:
        super().__init__({"schema": {"mode": "observed"}})
        self._fail_ids = fail_ids

    def process(self, row: PipelineRow, ctx: Any) -> TransformResult:
        if row["id"] in self._fail_ids:
            return TransformResult.error(
                {
                    "reason": "test_error",
                    "error": f"Row {row['id']} deliberately failed",
                }
            )
        return TransformResult.success(
            make_pipeline_row(row.to_dict()),
            success_reason={"action": "passed"},
        )


def _build_linear_graph(config: PipelineConfig) -> ExecutionGraph:
    """Build a simple linear graph for partial failure tests.

    Uses from_plugin_instances() for production-path fidelity.
    """
    transforms = list(config.transforms)
    sink_name = next(iter(config.sinks))

    # Set source routing
    source_connection = "source_out" if transforms else sink_name
    config.sources["primary"].on_success = source_connection
    source_settings = SourceSettings(plugin=config.sources["primary"].name, on_success=source_connection, options={})

    # Wire transforms with explicit routing
    wired = wire_transforms(transforms, source_connection=source_connection, final_sink=sink_name)

    graph = ExecutionGraph.from_plugin_instances(
        sources={"primary": config.sources["primary"]},
        source_settings_map={"primary": source_settings},
        transforms=wired,
        sinks=config.sinks,
        aggregations={},
        gates=[],
    )
    return graph


class TestPartialFailure:
    """Tests for partial failure scenarios."""

    def test_partial_failures_produce_correct_audit_trail(self, tmp_path: Path) -> None:
        """Every completed fixture token has its exact source, outcome and errors."""
        with LandscapeDB(f"sqlite:///{tmp_path}/audit.db") as db:
            payload_store = FilesystemPayloadStore(tmp_path / "payloads")
            source_data = [{"id": i, "value": i * 10} for i in range(10)]
            fail_ids = {2, 5, 8}
            source = ListSource(source_data)
            transform = _SelectiveErrorTransform(fail_ids=fail_ids)
            sink = CollectSink("default")
            config = PipelineConfig(
                sources={"primary": as_source(source)},
                transforms=[as_transform(transform)],
                sinks={"default": as_sink(sink)},
            )
            graph = _build_linear_graph(config)
            result = Orchestrator(db).run(config, graph=graph, payload_store=payload_store)
            assert result.status == RunStatus.COMPLETED_WITH_FAILURES
            success_ids = {row["id"] for row in source_data} - fail_ids
            assert len(sink.results) == len(success_ids)
            assert {row["id"] for row in sink.results} == success_ids

            with db.engine.connect() as conn:
                errors = conn.execute(select(transform_errors_table).where(transform_errors_table.c.run_id == result.run_id)).fetchall()
                completed = conn.execute(
                    select(
                        token_outcomes_table,
                        rows_table.c.row_id,
                        rows_table.c.source_node_id,
                        rows_table.c.source_row_index,
                        rows_table.c.ingest_sequence,
                        rows_table.c.source_data_hash,
                    )
                    .select_from(
                        token_outcomes_table.join(
                            tokens_table,
                            and_(
                                tokens_table.c.token_id == token_outcomes_table.c.token_id,
                                tokens_table.c.run_id == token_outcomes_table.c.run_id,
                            ),
                        ).join(
                            rows_table,
                            and_(rows_table.c.row_id == tokens_table.c.row_id, rows_table.c.run_id == tokens_table.c.run_id),
                        )
                    )
                    .where(token_outcomes_table.c.run_id == result.run_id)
                    .where(token_outcomes_table.c.completed == 1)
                ).fetchall()

            assert len(completed) == len(source_data)
            assert len({row.token_id for row in completed}) == len(source_data)
            assert len({row.row_id for row in completed}) == len(source_data)
            assert {row.source_row_index for row in completed} == set(range(len(source_data)))
            assert len(errors) == len(fail_ids)
            errors_by_token = {error.token_id: error for error in errors}
            assert len(errors_by_token) == len(fail_ids)
            factory = RecorderFactory(db, payload_store=payload_store)
            explained_failures: set[int] = set()
            explained_successes: set[int] = set()
            for row in completed:
                expected_source = source_data[row.source_row_index]
                expected_id = expected_source["id"]
                expected_outcome = TerminalOutcome.FAILURE if expected_id in fail_ids else TerminalOutcome.SUCCESS
                assert row.outcome == expected_outcome.value
                lineage = explain(factory.query, factory.data_flow, result.run_id, token_id=row.token_id)
                assert lineage is not None, f"Missing lineage for source row {row.source_row_index} ({expected_outcome.value})"
                assert lineage.token.token_id == row.token_id
                assert lineage.token.run_id == result.run_id
                assert lineage.token.row_id == row.row_id
                assert lineage.source_row.row_id == row.row_id
                assert lineage.source_row.run_id == result.run_id
                assert lineage.source_row.source_node_id == row.source_node_id
                assert lineage.source_row.source_row_index == row.source_row_index
                assert lineage.source_row.ingest_sequence == row.ingest_sequence
                assert lineage.source_row.source_data_hash == row.source_data_hash
                assert lineage.source_row.payload_available is True
                assert lineage.source_row.source_data == expected_source
                assert lineage.outcome is not None
                assert lineage.outcome.outcome_id == row.outcome_id
                assert lineage.outcome.token_id == row.token_id
                assert lineage.outcome.run_id == result.run_id
                assert lineage.outcome.completed is True
                assert lineage.outcome.outcome is expected_outcome
                assert lineage.outcome.path.value == row.path
                assert lineage.outcome.sink_name == row.sink_name
                assert lineage.outcome.batch_id == row.batch_id
                assert lineage.outcome.error_hash == row.error_hash
                assert lineage.outcome.context_json == row.context_json

                if expected_id in fail_ids:
                    explained_failures.add(expected_id)
                    assert len(lineage.transform_errors) == 1
                    error = lineage.transform_errors[0]
                    recorded_error = errors_by_token[row.token_id]
                    assert error.error_id == recorded_error.error_id
                    assert error.token_id == recorded_error.token_id == row.token_id
                    assert error.run_id == recorded_error.run_id == result.run_id
                    assert error.transform_id == recorded_error.transform_id == graph.get_transform_id_map()[0]
                    assert error.row_hash == recorded_error.row_hash
                    assert error.destination == recorded_error.destination == "discard"
                    assert error.row_data_json is not None
                    assert error.error_details_json is not None
                    assert error.row_data_json == recorded_error.row_data_json
                    assert error.error_details_json == recorded_error.error_details_json
                    assert json.loads(error.row_data_json) == expected_source
                    assert json.loads(error.error_details_json) == {
                        "reason": "test_error",
                        "error": f"Row {expected_id} deliberately failed",
                    }
                else:
                    explained_successes.add(expected_id)
                    assert lineage.outcome.path is TerminalPath.DEFAULT_FLOW
                    assert lineage.transform_errors == ()
                    assert row.token_id not in errors_by_token

            assert explained_failures == fail_ids
            assert explained_successes == success_ids

    def test_first_row_failure_doesnt_prevent_rest(self, tmp_path: Path) -> None:
        """Run where row 0 fails but rows 1-4 succeed.

        Verify all 4 remaining rows reach sink.
        """
        db = LandscapeDB(f"sqlite:///{tmp_path}/audit.db")
        payload_store = FilesystemPayloadStore(tmp_path / "payloads")

        source_data = [{"id": i, "value": i * 10} for i in range(5)]
        fail_ids = {0}  # First row fails

        source = ListSource(source_data)
        transform = _SelectiveErrorTransform(fail_ids=fail_ids)
        sink = CollectSink("default")

        config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(transform)],
            sinks={"default": as_sink(sink)},
        )

        graph = _build_linear_graph(config)

        orchestrator = Orchestrator(db)
        result = orchestrator.run(config, graph=graph, payload_store=payload_store)

        assert result.status == RunStatus.COMPLETED_WITH_FAILURES

        # All 4 remaining rows reach sink
        assert len(sink.results) == 4
        sink_ids = {r["id"] for r in sink.results}
        assert sink_ids == {1, 2, 3, 4}

        # 1 error recorded
        with db.engine.connect() as conn:
            errors = conn.execute(select(transform_errors_table).where(transform_errors_table.c.run_id == result.run_id)).fetchall()

        assert len(errors) == 1
        error_details = json.loads(errors[0].error_details_json)
        assert error_details["reason"] == "test_error"
        assert "Row 0 deliberately failed" in error_details["error"]

        db.close()

    def test_last_row_failure_doesnt_corrupt_prior_output(self, tmp_path: Path) -> None:
        """Run where last row fails but all prior rows succeed.

        Verify all prior rows in sink and correctly recorded.
        """
        db = LandscapeDB(f"sqlite:///{tmp_path}/audit.db")
        payload_store = FilesystemPayloadStore(tmp_path / "payloads")

        source_data = [{"id": i, "value": i * 10} for i in range(5)]
        fail_ids = {4}  # Last row fails

        source = ListSource(source_data)
        transform = _SelectiveErrorTransform(fail_ids=fail_ids)
        sink = CollectSink("default")

        config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(transform)],
            sinks={"default": as_sink(sink)},
        )

        graph = _build_linear_graph(config)

        orchestrator = Orchestrator(db)
        result = orchestrator.run(config, graph=graph, payload_store=payload_store)

        assert result.status == RunStatus.COMPLETED_WITH_FAILURES

        # 4 rows reach sink (all except last)
        assert len(sink.results) == 4
        sink_ids = {r["id"] for r in sink.results}
        assert sink_ids == {0, 1, 2, 3}

        # 1 error recorded for the last row
        with db.engine.connect() as conn:
            errors = conn.execute(select(transform_errors_table).where(transform_errors_table.c.run_id == result.run_id)).fetchall()

        assert len(errors) == 1
        error_details = json.loads(errors[0].error_details_json)
        assert error_details["reason"] == "test_error"
        assert "Row 4 deliberately failed" in error_details["error"]

        # All 5 rows have terminal outcomes
        with db.engine.connect() as conn:
            outcomes = conn.execute(
                select(token_outcomes_table.c.outcome)
                .where(token_outcomes_table.c.run_id == result.run_id)
                .where(token_outcomes_table.c.completed == 1)
            ).fetchall()

        assert len(outcomes) == 5

        db.close()
