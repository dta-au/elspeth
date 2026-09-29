"""File-source interruption proves the current fail-closed resume boundary.

CSV and JSONL expose different source positions: CSV skips blank records,
while JSONL uses physical line numbers. Neither an ingested row count nor the
last ingested source_row_index is a durable cursor for unread input.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from elspeth.contracts import Determinism, PipelineRow, ResumePoint
from elspeth.contracts.checkpoint import ResumeRefusalCause
from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
from elspeth.contracts.errors import GracefulShutdownError, IncompleteSourceResumeError
from elspeth.core.checkpoint import CheckpointManager, RecoveryManager
from elspeth.core.config import CheckpointSettings
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import rows_table, run_sources_table, token_outcomes_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.sources.csv_source import CSVSource
from elspeth.plugins.sources.json_source import JSONSource
from tests.fixtures.base_classes import _TestSchema, as_sink, as_source, as_transform
from tests.fixtures.pipeline import build_production_graph
from tests.fixtures.plugins import CollectSink


class _StopAfterTwo(BaseTransform):
    name = "stop_after_two_file_rows"
    determinism = Determinism.DETERMINISTIC
    input_schema = _TestSchema
    output_schema = _TestSchema

    def __init__(self, shutdown_event: threading.Event) -> None:
        super().__init__({"schema": {"mode": "observed"}})
        self._shutdown_event = shutdown_event
        self._seen = 0

    def process(self, row: PipelineRow, ctx: Any) -> TransformResult:
        self._seen += 1
        if self._seen == 2:
            self._shutdown_event.set()
        return TransformResult.success(row, success_reason={"action": "read"})


@pytest.mark.parametrize(
    ("filename", "contents", "source_type", "expected_indices"),
    [
        ("companies.csv", "query\nAlpha\n\nBeta\nGamma\nDelta\n", CSVSource, [0, 1]),
        (
            "companies.jsonl",
            '{"query":"Alpha"}\n\n{"query":"Beta"}\n{"query":"Gamma"}\n{"query":"Delta"}\n',
            JSONSource,
            [0, 2],
        ),
    ],
)
def test_interrupted_file_source_refuses_resume_without_losing_unread_rows(
    tmp_path: Path,
    filename: str,
    contents: str,
    source_type: type[CSVSource] | type[JSONSource],
    expected_indices: list[int],
) -> None:
    """A checkpoint and two terminal rows cannot imply file-source exhaustion."""
    source_path = tmp_path / filename
    source_path.write_text(contents)
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    payload_store = FilesystemPayloadStore(tmp_path / "payloads")
    checkpoint_manager = CheckpointManager(db)
    checkpoint_config = RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row"))
    shutdown_event = threading.Event()
    source = source_type({"path": str(source_path), "schema": {"mode": "observed"}, "on_validation_failure": "discard"})
    transform = _StopAfterTwo(shutdown_event)
    sink = CollectSink("output")
    config = PipelineConfig(
        sources={"primary": as_source(source)},
        transforms=[as_transform(transform)],
        sinks={"output": as_sink(sink)},
    )
    graph = build_production_graph(config)
    orchestrator = Orchestrator(db=db, checkpoint_manager=checkpoint_manager, checkpoint_config=checkpoint_config)

    with pytest.raises(GracefulShutdownError) as interrupted:
        orchestrator.run(config, graph=graph, payload_store=payload_store, shutdown_event=shutdown_event)
    run_id = interrupted.value.run_id

    with db.engine.connect() as conn:
        source_indices = (
            conn.execute(select(rows_table.c.source_row_index).where(rows_table.c.run_id == run_id).order_by(rows_table.c.ingest_sequence))
            .scalars()
            .all()
        )
        lifecycle = conn.execute(select(run_sources_table.c.lifecycle_state).where(run_sources_table.c.run_id == run_id)).scalar_one()
        completed_outcomes = (
            conn.execute(
                select(token_outcomes_table.c.token_id).where(
                    token_outcomes_table.c.run_id == run_id, token_outcomes_table.c.completed == 1
                )
            )
            .scalars()
            .all()
        )
    assert source_indices == expected_indices
    assert lifecycle == "interrupted"
    assert len(completed_outcomes) == 2
    assert len(set(completed_outcomes)) == 2
    assert len(sink.results) == 2

    checkpoint = checkpoint_manager.get_latest_checkpoint(run_id)
    assert checkpoint is not None
    check = RecoveryManager(db, checkpoint_manager).can_resume(run_id, graph)
    assert not check.can_resume
    assert check.cause is ResumeRefusalCause.SOURCE_NOT_EXHAUSTED
    assert check.reason is not None and "primary=interrupted" in check.reason

    resume_point = ResumePoint(checkpoint=checkpoint, sequence_number=checkpoint.sequence_number)
    with pytest.raises(IncompleteSourceResumeError, match=r"primary.*interrupted"):
        orchestrator.resume(resume_point=resume_point, config=config, graph=graph, payload_store=payload_store)
    assert len(sink.results) == 2
