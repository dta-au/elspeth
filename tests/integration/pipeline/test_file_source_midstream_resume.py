"""File-source interruption proves the current fail-closed resume boundary.

CSV and JSONL expose different source positions: CSV skips blank records,
while JSONL uses physical line numbers. Neither an ingested row count nor the
last ingested source_row_index is a durable cursor for unread input.
"""

from __future__ import annotations

import importlib
import threading
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from elspeth.contracts import Determinism, PipelineRow, ResumePoint
from elspeth.contracts.checkpoint import ResumeRefusalCause
from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
from elspeth.contracts.errors import GracefulShutdownError, IncompleteSourceResumeError, OrchestrationInvariantError
from elspeth.core.checkpoint import CheckpointManager, RecoveryManager
from elspeth.core.config import CheckpointSettings
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import operations_table, rows_table, run_sources_table, token_outcomes_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.engine.orchestrator.source_replay import prepare_audited_sources
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

    def __init__(self, shutdown_event: threading.Event, *, stop_at: int = 2, phase_events: list[str] | None = None) -> None:
        super().__init__({"schema": {"mode": "observed"}})
        self._shutdown_event = shutdown_event
        self._stop_at = stop_at
        self._phase_events = phase_events
        self._seen = 0

    def process(self, row: PipelineRow, ctx: Any) -> TransformResult:
        if self._phase_events is not None:
            self._phase_events.append("row")
        self._seen += 1
        if self._seen == self._stop_at:
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


@pytest.mark.parametrize(
    ("filename", "contents", "source_type", "expected_indices", "quarantine"),
    [
        ("queries.csv", "query\nAlpha\n\nBeta\nGamma\nDelta\n", CSVSource, [0, 1, 2, 3], False),
        ("queries.jsonl", '{"query":"Alpha"}\n\n{"query":"Beta"}\n{"query":"Gamma"}\n{"query":"Delta"}\n', JSONSource, [0, 2, 3, 4], False),
        (
            "queries.jsonl",
            '{"query":"Alpha"}\n\n{"query":"Beta"}\ninvalid\n{"query":"Gamma"}\n{"query":"Delta"}\n',
            JSONSource,
            [0, 2, 3, 4, 5],
            True,
        ),
        (
            "queries.jsonl",
            '{"query":"Alpha"}\n123\n{"query":"Beta"}\n123\n{"query":"Gamma"}\n{"query":"Delta"}\n',
            JSONSource,
            [0, 1, 2, 3, 4, 5],
            True,
        ),
        (
            "queries.jsonl",
            '{"query":"Alpha"}\n{"query":"Beta"}\n123\n123\n{"query":"Gamma"}\n{"query":"Delta"}\n',
            JSONSource,
            [0, 1, 2, 3, 4, 5],
            True,
        ),
    ],
)
@pytest.mark.parametrize("resume_stop_at", [100, 1])
def test_snapshot_file_source_resumes_remaining_rows_without_reopening_input(
    tmp_path: Path,
    filename: str,
    contents: str,
    source_type: type[CSVSource] | type[JSONSource],
    expected_indices: list[int],
    quarantine: bool,
    resume_stop_at: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / filename
    source_path.write_text(contents)
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    payload_store = FilesystemPayloadStore(tmp_path / "payloads")
    original_store = payload_store.store
    snapshot_published = False

    def store_after_operation_commit(content: bytes) -> str:
        nonlocal snapshot_published
        if content.startswith(b'{"version": 1, "source_name":'):
            with db.engine.connect() as conn:
                operation = conn.execute(
                    select(operations_table.c.status, operations_table.c.output_data_ref).where(
                        operations_table.c.operation_type == "source_load"
                    )
                ).one()
            assert operation.status == "completed"
            assert operation.output_data_ref is not None
            snapshot_published = True
        return original_store(content)

    monkeypatch.setattr(payload_store, "store", store_after_operation_commit)
    checkpoint_manager = CheckpointManager(db)
    checkpoint_config = RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row"))
    shutdown_event = threading.Event()
    source_options = {
        "path": str(source_path),
        "schema": {"mode": "observed"},
        "on_validation_failure": "quarantine" if quarantine else "discard",
        "snapshot_for_resume": True,
    }
    source = source_type(source_options)
    transform = _StopAfterTwo(shutdown_event)
    sink = CollectSink("output")
    quarantine_sink = CollectSink("quarantine")
    sinks = {"output": as_sink(sink)}
    if quarantine:
        sinks["quarantine"] = as_sink(quarantine_sink)
    config = PipelineConfig(
        sources={"primary": as_source(source)},
        transforms=[as_transform(transform)],
        sinks=sinks,
    )
    graph = build_production_graph(config)
    orchestrator = Orchestrator(db=db, checkpoint_manager=checkpoint_manager, checkpoint_config=checkpoint_config)

    with pytest.raises(GracefulShutdownError) as interrupted:
        orchestrator.run(config, graph=graph, payload_store=payload_store, shutdown_event=shutdown_event)
    run_id = interrupted.value.run_id
    assert snapshot_published
    factory = RecorderFactory(db, payload_store=payload_store)
    original_error_ids = {error.error_id for error in factory.data_flow.get_validation_errors_for_run(run_id)}
    assert len(sink.results) == 2
    with db.engine.connect() as conn:
        lifecycle = conn.execute(select(run_sources_table.c.lifecycle_state).where(run_sources_table.c.run_id == run_id)).scalar_one()
    assert lifecycle == "exhausted"

    source_path.unlink()
    shutdown_event.clear()
    phase_events: list[str] = []
    resume_module = importlib.import_module("elspeth.engine.orchestrator.resume")
    original_resume_loop = resume_module.run_resume_processing_loop

    def traced_resume_loop(*args: Any, **kwargs: Any) -> bool:
        phase_events.append("prelude" if kwargs.get("flush_end_of_input") is False else "eof")
        return original_resume_loop(*args, **kwargs)

    monkeypatch.setattr(resume_module, "run_resume_processing_loop", traced_resume_loop)
    resumed_transform = _StopAfterTwo(shutdown_event, stop_at=resume_stop_at, phase_events=phase_events)
    resumed_config = PipelineConfig(
        sources={"primary": as_source(source_type(source_options))},
        transforms=[as_transform(resumed_transform)],
        sinks=sinks,
    )
    resumed_graph = build_production_graph(resumed_config)
    recovery = RecoveryManager(db, checkpoint_manager)
    assert recovery.can_resume(run_id, graph).can_resume
    resume_point = recovery.get_resume_point(run_id, graph)
    if resume_stop_at == 1:
        with pytest.raises(GracefulShutdownError):
            orchestrator.resume(
                resume_point=resume_point,
                config=resumed_config,
                graph=resumed_graph,
                payload_store=payload_store,
                shutdown_event=shutdown_event,
            )
        assert resumed_transform._seen == 1
        shutdown_event.clear()
        resumed_config = PipelineConfig(
            sources={"primary": as_source(source_type(source_options))},
            transforms=[as_transform(_StopAfterTwo(shutdown_event, stop_at=100))],
            sinks=sinks,
        )
        resumed_graph = build_production_graph(resumed_config)
        resume_point = recovery.get_resume_point(run_id, resumed_graph)
    orchestrator.resume(resume_point=resume_point, config=resumed_config, graph=resumed_graph, payload_store=payload_store)
    assert phase_events[:2] == ["prelude", "row"]

    with db.engine.connect() as conn:
        source_indices = (
            conn.execute(select(rows_table.c.source_row_index).where(rows_table.c.run_id == run_id).order_by(rows_table.c.ingest_sequence))
            .scalars()
            .all()
        )
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
    assert len(completed_outcomes) == len(set(completed_outcomes)) == len(expected_indices)
    assert [row["query"] for row in sink.results] == ["Alpha", "Beta", "Gamma", "Delta"]
    assert len(quarantine_sink.results) == len(expected_indices) - 4
    validation_errors = factory.data_flow.get_validation_errors_for_run(run_id)
    assert {error.error_id for error in validation_errors} == original_error_ids
    assert len(validation_errors) == len(quarantine_sink.results)
    assert all(error.row_id is not None for error in validation_errors)
    assert len({error.row_id for error in validation_errors}) == len(validation_errors)
    audited = prepare_audited_sources(factory, run_id, {"primary": source_type(source_options)})
    assert "primary" in audited


def test_snapshot_mode_refuses_multi_source_pipeline_before_dispatch(tmp_path: Path) -> None:
    options = {"path": str(tmp_path / "unused.csv"), "schema": {"mode": "observed"}, "on_validation_failure": "discard"}
    with pytest.raises(OrchestrationInvariantError, match="exactly one source"):
        PipelineConfig(
            sources={
                "first": as_source(CSVSource({**options, "snapshot_for_resume": True})),
                "second": as_source(CSVSource(options)),
            },
            transforms=[],
            sinks={"output": as_sink(CollectSink("output"))},
        )
