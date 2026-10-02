# tests/integration/pipeline/test_transform_failure_counts.py
"""The ordinary run's failure counts: some rows pass a transform, one fails there (elspeth-5887fb7928).

The counting readers (web discard summary, web failure categories, MCP run
summary and error analysis) count a token at the node whose transform error
decided it, unless THAT token completed the node. Other rows completing the
same node is the normal shape of every run with a failure, and must not hide
the failed row. The unit cases in
``tests/unit/core/landscape/test_terminal_transform_failures.py`` build the
audit state by hand. This one runs the real engine on both on_error arms.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from elspeth.contracts import Determinism, NodeStateStatus, PipelineRow, RunStatus
from elspeth.core.config import SourceSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import node_states_table, transform_errors_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.mcp.analyzers.reports import get_error_analysis, get_run_summary
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.web.execution.discard_summary import load_discard_summaries_from_db
from elspeth.web.execution.failure_samples import load_top_failure_categories
from tests.fixtures.base_classes import _TestSchema, as_sink, as_source, as_transform
from tests.fixtures.factories import wire_transforms
from tests.fixtures.plugins import CollectSink, ListSource


class _FailRowTwenty(BaseTransform):
    """Passes every row except ``value == 20``, which it fails with a returned error."""

    name = "fail_row_twenty"
    determinism = Determinism.DETERMINISTIC
    input_schema = _TestSchema
    output_schema = _TestSchema

    def __init__(self, *, on_error: str) -> None:
        super().__init__({"schema": {"mode": "observed"}})
        self.on_error = on_error

    def process(self, row: PipelineRow, ctx: Any) -> TransformResult:
        if row["value"] == 20:
            return TransformResult.error({"reason": "validation_failed", "error": "row 20"})
        return TransformResult.success(row, success_reason={"action": "passthrough"})


@pytest.mark.parametrize("error_sink", [None, "quarantine"], ids=["discard", "routed"])
def test_one_failed_row_among_rows_that_completed_the_transform_counts_once(tmp_path: Path, error_sink: str | None) -> None:
    transform = as_transform(_FailRowTwenty(on_error=error_sink or "discard"))
    source = ListSource([{"value": 10}, {"value": 20}, {"value": 30}], on_success="primary_out")
    sinks = {"default": CollectSink("default")}
    if error_sink is not None:
        sinks[error_sink] = CollectSink(error_sink)
    config = PipelineConfig(
        sources={"primary": as_source(source)},
        transforms=[transform],
        sinks={name: as_sink(sink) for name, sink in sinks.items()},
    )
    graph = ExecutionGraph.from_plugin_instances(
        sources=config.sources,
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="primary_out", options={})},
        transforms=wire_transforms([transform], source_connection="primary_out", final_sink="default"),
        sinks=config.sinks,
        aggregations={},
        gates=[],
    )
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    try:
        result = Orchestrator(db=db).run(config, graph=graph, payload_store=FilesystemPayloadStore(tmp_path / "payloads"))

        assert result.status == RunStatus.COMPLETED_WITH_FAILURES
        assert [row["value"] for row in sinks["default"].results] == [10, 30]
        if error_sink is not None:
            assert [row["value"] for row in sinks[error_sink].results] == [20]
        run_id = result.run_id
        with db.connection() as conn:
            failing_node = conn.execute(
                select(transform_errors_table.c.transform_id).where(transform_errors_table.c.run_id == run_id)
            ).scalar_one()
            completed_at_failing_node = (
                conn.execute(
                    select(node_states_table.c.token_id)
                    .where(node_states_table.c.run_id == run_id)
                    .where(node_states_table.c.node_id == failing_node)
                    .where(node_states_table.c.status == NodeStateStatus.COMPLETED)
                )
                .scalars()
                .all()
            )
        assert len(completed_at_failing_node) == 2, (
            f"control: two other rows COMPLETED the failing transform, got {completed_at_failing_node}"
        )

        summaries = load_discard_summaries_from_db(db, [run_id])
        categories = load_top_failure_categories(db, run_id)
        factory = RecorderFactory(db)
        run_summary: Any = get_run_summary(db, factory, run_id)
        analysis: Any = get_error_analysis(db, factory, run_id)
    finally:
        db.close()

    if error_sink is None:
        assert summaries[run_id].transform_errors == 1
    else:
        assert run_id not in summaries, "a routed row failed but was not discarded"
    assert [(c.transform_id, c.category, c.count) for c in categories] == [(failing_node, "validation_failed", 1)]
    assert run_summary["errors"]["transform"] == 1
    assert analysis["transform_errors"]["total"] == 1
    assert analysis["transform_errors"]["by_transform"] == [{"transform_plugin": "fail_row_twenty", "count": 1}]
