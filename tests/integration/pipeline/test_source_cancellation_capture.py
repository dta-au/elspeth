"""Fresh public-run controls for source entry and incomplete snapshot custody."""

import hashlib
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from elspeth.cli_helpers import instantiate_plugins_from_config
from elspeth.config_loading import load_settings_from_yaml_string
from elspeth.contracts import Determinism, ResumePoint, RunStatus, SourceProtocol, SourceRow
from elspeth.contracts.checkpoint import ResumeRefusalCause
from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
from elspeth.contracts.errors import FrameworkBugError, GracefulShutdownError, IncompleteSourceResumeError
from elspeth.core.checkpoint import CheckpointManager, RecoveryManager
from elspeth.core.config import CheckpointSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.execution_repository import ExecutionRepository
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import (
    RunSourceLifecycleState,
    operations_table,
    rows_table,
    run_sources_table,
    runs_table,
    token_outcomes_table,
)
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig, leader_drain
from elspeth.engine.orchestrator.preflight import assemble_and_validate_pipeline_config
from elspeth.engine.orchestrator.source_iteration import SourceIterationDriver
from elspeth.engine.orchestrator.source_lifecycle_recorder import SourceLifecycleRecorder
from elspeth.plugins.sources.json_source import JSONSource
from tests.fixtures.base_classes import as_sink, as_source, as_transform
from tests.fixtures.pipeline import build_production_graph
from tests.fixtures.plugins import CollectSink
from tests.integration.pipeline.test_file_source_midstream_resume import _StopAfterTwo


class _SnapshotSource(JSONSource):
    """Keep the source implementation stable while forbidding a cold reload."""

    determinism = Determinism.IO_READ
    forbid_load = False
    entries = 0

    def load(self, ctx: Any) -> Iterator[SourceRow]:
        assert not self.forbid_load, "resume reopened the original source"
        self.entries += 1
        yield from super().load(ctx)


@pytest.mark.parametrize(
    "snapshot,preset,cancel_after,empty",
    [
        (False, False, None, False),
        (True, False, None, False),
        (False, True, None, False),
        (False, False, 1, False),
        (True, True, None, False),
        (True, False, 1, False),
        (True, False, 2, False),
        (True, False, 3, False),
        (True, False, None, True),
        (True, True, None, True),
        (True, False, 0, True),
    ],
)
@pytest.mark.parametrize("idle_path", [False, True])
def test_source_cancellation_does_not_advance_or_seal_an_incomplete_input(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    snapshot: bool,
    preset: bool,
    cancel_after: int | None,
    idle_path: bool,
    empty: bool,
) -> None:
    source_path = tmp_path / "source.jsonl"
    source_path.write_text("" if empty else '{"query":"a"}\n{"query":"b"}\n{"query":"c"}\n')
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    request.addfinalizer(db.close)
    checkpoints = CheckpointManager(db)
    payloads = FilesystemPayloadStore(tmp_path / "payloads")
    snapshots: list[bytes] = []
    original_store = payloads.store

    def observe_store(content: bytes) -> str:
        if content.startswith(b'{"version": 1, "source_name":'):
            snapshots.append(content)
        return original_store(content)

    monkeypatch.setattr(payloads, "store", observe_store)
    event = threading.Event()
    # This controls only the driver's mechanical direct/idle fetch branch;
    # it is not evidence of aggregation timeout eligibility or timing.
    monkeypatch.setattr(SourceIterationDriver, "_requires_idle_aggregation_polling", lambda *args, **kwargs: idle_path)
    if preset:
        original_preflight = leader_drain.run_transform_runtime_preflights

        def stop_after_readiness(*args: Any, **kwargs: Any) -> None:
            original_preflight(*args, **kwargs)
            event.set()

        monkeypatch.setattr(leader_drain, "run_transform_runtime_preflights", stop_after_readiness)
    entered: list[str] = []
    advanced: list[int] = []
    exhausted: list[bool] = []
    original = JSONSource.load

    def counted(source: JSONSource, ctx: Any):
        entered.append(source.name)
        for index, row in enumerate(original(source, ctx)):
            assert row.source_row_index is not None
            advanced.append(row.source_row_index)
            if cancel_after == index + 1:
                event.set()
            yield row
        if cancel_after == 0:
            event.set()
        exhausted.append(True)

    monkeypatch.setattr(JSONSource, "load", counted)
    source = JSONSource(
        {"path": str(source_path), "snapshot_for_resume": snapshot, "schema": {"mode": "observed"}, "on_validation_failure": "discard"}
    )
    sink = CollectSink("output")
    config = PipelineConfig(
        sources={"primary": as_source(source)},
        transforms=[as_transform(_StopAfterTwo(event, stop_at=100))],
        sinks={"output": as_sink(sink)},
    )
    graph = build_production_graph(config)
    orchestrator = Orchestrator(
        db=db,
        checkpoint_manager=checkpoints,
        checkpoint_config=RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row")),
    )
    cancelled = preset or cancel_after is not None
    if cancelled:
        with pytest.raises(GracefulShutdownError) as error:
            orchestrator.run(config, graph=graph, payload_store=payloads, shutdown_event=event)
        run_id = error.value.run_id
        returned_ordinary_row = not snapshot and not preset
        assert error.value.rows_processed == int(returned_ordinary_row)
        assert len(entered) == (0 if preset else 1)
        assert advanced == ([] if preset else list(range(cancel_after)))
        assert exhausted == ([True] if cancel_after == 0 else [])
        assert snapshots == []
        assert sink.results == []
        with db.connection() as connection:
            assert connection.execute(select(runs_table.c.status)).scalar_one() == RunStatus.INTERRUPTED.value
            assert connection.execute(select(run_sources_table.c.lifecycle_state)).scalar_one() == "interrupted"
            retained_rows = connection.execute(select(rows_table.c.source_row_index)).scalars().all()
            assert retained_rows == ([0] if returned_ordinary_row else [])
            outcomes = connection.execute(
                select(
                    token_outcomes_table.c.outcome,
                    token_outcomes_table.c.path,
                    token_outcomes_table.c.completed,
                    token_outcomes_table.c.error_hash,
                )
            ).all()
            assert outcomes == ([(None, "abandoned", False, None)] if returned_ordinary_row else [])
            loads = connection.execute(
                select(operations_table.c.status, operations_table.c.output_data_ref).where(
                    operations_table.c.operation_type == "source_load"
                )
            ).all()
        if snapshot:
            assert loads == [("failed", None)]
        check = RecoveryManager(db, checkpoints).can_resume(run_id, graph)
        assert not check.can_resume
        assert check.cause is ResumeRefusalCause.SOURCE_NOT_EXHAUSTED
        checkpoint = checkpoints.get_latest_checkpoint(run_id)
        assert checkpoint is not None
        with pytest.raises(IncompleteSourceResumeError, match=r"primary.*interrupted"):
            orchestrator.resume(
                resume_point=ResumePoint(checkpoint=checkpoint, sequence_number=checkpoint.sequence_number),
                config=config,
                graph=graph,
                payload_store=payloads,
            )
    else:
        result = orchestrator.run(config, graph=graph, payload_store=payloads, shutdown_event=event)
        assert result.status is (RunStatus.EMPTY if empty else RunStatus.COMPLETED)
        assert len(entered) == 1
        assert advanced == ([] if empty else [0, 1, 2])
        assert exhausted == [True]
        assert [row["query"] for row in sink.results] == ([] if empty else ["a", "b", "c"])
        assert len(snapshots) == int(snapshot)


def test_cancellation_during_snapshot_publication_preserves_cold_resume(
    tmp_path: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once metadata is committed, finish publication before honoring a stop."""
    source_path = tmp_path / "source.jsonl"
    source_path.write_text('{"query":"a"}\n{"query":"b"}\n{"query":"c"}\n')
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    request.addfinalizer(db.close)
    checkpoints = CheckpointManager(db)
    payloads = FilesystemPayloadStore(tmp_path / "payloads")
    event = threading.Event()
    published: list[str] = []
    original_store = payloads.store

    def stop_during_publication(content: bytes) -> str:
        if content.startswith(b'{"version": 1, "source_name":'):
            with db.connection() as connection:
                operation = connection.execute(
                    select(operations_table.c.status, operations_table.c.output_data_ref).where(
                        operations_table.c.operation_type == "source_load"
                    )
                ).one()
            assert operation.status == "completed"
            assert operation.output_data_ref is not None
            event.set()
            reference = original_store(content)
            assert reference == hashlib.sha256(content).hexdigest()
            published.append(reference)
            return reference
        return original_store(content)

    monkeypatch.setattr(payloads, "store", stop_during_publication)
    options = {"path": str(source_path), "snapshot_for_resume": True, "schema": {"mode": "observed"}, "on_validation_failure": "discard"}
    source = _SnapshotSource(options)
    sink = CollectSink("output")
    config = PipelineConfig(
        sources={"primary": as_source(source)},
        transforms=[as_transform(_StopAfterTwo(event, stop_at=100))],
        sinks={"output": as_sink(sink)},
    )
    graph = build_production_graph(config)
    orchestrator = Orchestrator(
        db=db,
        checkpoint_manager=checkpoints,
        checkpoint_config=RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row")),
    )
    with pytest.raises(GracefulShutdownError) as stopped:
        orchestrator.run(config, graph=graph, payload_store=payloads, shutdown_event=event)
    assert stopped.value.rows_processed == 0
    assert source.entries == 1
    assert sink.results == []
    assert len(published) == 1
    with db.connection() as connection:
        assert connection.execute(select(run_sources_table.c.lifecycle_state)).scalar_one() == "exhausted"
        assert connection.execute(select(rows_table.c.row_id)).all() == []

    source_path.unlink()
    event.clear()
    resumed_source = _SnapshotSource(options)
    resumed_source.forbid_load = True
    resumed_config = PipelineConfig(
        sources={"primary": as_source(resumed_source)},
        transforms=[as_transform(_StopAfterTwo(event, stop_at=100))],
        sinks={"output": as_sink(sink)},
    )
    resumed_graph = build_production_graph(resumed_config)
    recovery = RecoveryManager(db, checkpoints)
    assert recovery.can_resume(stopped.value.run_id, resumed_graph).can_resume
    result = Orchestrator(db=db, checkpoint_manager=checkpoints).resume(
        resume_point=recovery.get_resume_point(stopped.value.run_id, resumed_graph),
        config=resumed_config,
        graph=resumed_graph,
        payload_store=payloads,
    )
    assert result.status is RunStatus.COMPLETED
    assert resumed_source.entries == 0
    assert [row["query"] for row in sink.results] == ["a", "b", "c"]
    assert len(published) == 1
    with db.connection() as connection:
        assert connection.execute(select(rows_table.c.source_row_index).order_by(rows_table.c.ingest_sequence)).scalars().all() == [0, 1, 2]
        outcomes = connection.execute(select(token_outcomes_table.c.token_id).where(token_outcomes_table.c.completed == 1)).scalars().all()
    assert len(outcomes) == len(set(outcomes)) == 3


@pytest.mark.parametrize("cancel_second", [False, True])
def test_later_source_cancellation_flushes_previously_accepted_output(
    tmp_path: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, cancel_second: bool
) -> None:
    """An unstarted later source must not discard the earlier source's work."""
    first_path = tmp_path / "first.jsonl"
    second_path = tmp_path / "second.jsonl"
    output_path = tmp_path / "output.jsonl"
    first_path.write_text('{"query":"a1"}\n{"query":"a2"}\n')
    second_path.write_text('{"query":"b1"}\n')
    settings = load_settings_from_yaml_string(f"""
sources:
  first:
    plugin: json
    on_success: output
    options:
      path: {first_path}
      schema: {{mode: observed}}
      on_validation_failure: discard
  second:
    plugin: json
    on_success: output
    options:
      path: {second_path}
      schema: {{mode: observed}}
      on_validation_failure: discard
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      schema: {{mode: observed}}
""")
    bundle = instantiate_plugins_from_config(settings)
    graph = ExecutionGraph.from_plugin_instances(
        sources=bundle.sources,
        source_settings_map=bundle.source_settings_map,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        gates=list(settings.gates),
        queues=settings.queues,
    )
    config = assemble_and_validate_pipeline_config(
        sources=bundle.sources,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        settings=settings,
        graph=graph,
    )
    event = threading.Event()
    original_lifecycle = SourceLifecycleRecorder.record_run_source_lifecycle

    def stop_before_second(
        recorder: SourceLifecycleRecorder,
        factory: RecorderFactory,
        source_id: Any,
        source_name: str,
        active_source: SourceProtocol,
        lifecycle_state: RunSourceLifecycleState,
        **kwargs: Any,
    ) -> None:
        original_lifecycle(recorder, factory, source_id, source_name, active_source, lifecycle_state, **kwargs)
        if cancel_second and source_name == "second" and lifecycle_state is RunSourceLifecycleState.LOADING:
            event.set()

    monkeypatch.setattr(SourceLifecycleRecorder, "record_run_source_lifecycle", stop_before_second)
    opened: list[str] = []
    original_load = JSONSource.load

    def observe_load(source: JSONSource, ctx: Any) -> Iterator[SourceRow]:
        filename = Path(source.config["path"]).name
        opened.append(filename)
        assert not (cancel_second and filename == second_path.name)
        yield from original_load(source, ctx)

    monkeypatch.setattr(JSONSource, "load", observe_load)
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    request.addfinalizer(db.close)
    checkpoints = CheckpointManager(db)
    orchestrator = Orchestrator(
        db=db,
        checkpoint_manager=checkpoints,
        checkpoint_config=RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row")),
    )
    payloads = FilesystemPayloadStore(tmp_path / "payloads")
    if cancel_second:
        with pytest.raises(GracefulShutdownError) as stopped:
            orchestrator.run(config, graph=graph, settings=settings, payload_store=payloads, shutdown_event=event)
        assert stopped.value.rows_processed == 2
        check = RecoveryManager(db, checkpoints).can_resume(stopped.value.run_id, graph)
        assert check.cause is ResumeRefusalCause.SOURCE_NOT_EXHAUSTED
    else:
        result = orchestrator.run(config, graph=graph, settings=settings, payload_store=payloads, shutdown_event=event)
        assert result.status is RunStatus.COMPLETED
        assert result.rows_processed == 3
    assert opened == ([first_path.name] if cancel_second else [first_path.name, second_path.name])
    expected = ["a1", "a2"] if cancel_second else ["a1", "a2", "b1"]
    assert [json.loads(line)["query"] for line in output_path.read_text().splitlines()] == expected
    with db.connection() as connection:
        assert dict(connection.execute(select(run_sources_table.c.source_name, run_sources_table.c.lifecycle_state)).all()) == {
            "first": "exhausted",
            "second": "interrupted" if cancel_second else "exhausted",
        }
        outcomes = connection.execute(select(token_outcomes_table.c.token_id).where(token_outcomes_table.c.completed == 1)).scalars().all()
    assert len(outcomes) == len(set(outcomes)) == len(expected)


@pytest.mark.parametrize("snapshot", [False, True])
@pytest.mark.parametrize("failure_kind", ["source-value", "source-interrupted", "audit"])
def test_real_source_and_audit_failures_escape_a_concurrent_stop(
    tmp_path: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, snapshot: bool, failure_kind: str
) -> None:
    source_path = tmp_path / "source.jsonl"
    source_path.write_text('{"query":"a"}\n{"query":"b"}\n')
    event = threading.Event()
    failure = (
        FrameworkBugError("controlled source audit failure")
        if failure_kind == "audit"
        else InterruptedError("ordinary source interrupt")
        if failure_kind == "source-interrupted"
        else ValueError("ordinary source error")
    )
    original_load = JSONSource.load

    def fail_or_stop(source: JSONSource, ctx: Any) -> Iterator[SourceRow]:
        if failure_kind != "audit":
            event.set()
            raise failure
        for row in original_load(source, ctx):
            event.set()
            yield row

    monkeypatch.setattr(JSONSource, "load", fail_or_stop)
    if failure_kind == "audit":

        def fail_completion(*args: Any, **kwargs: Any) -> None:
            raise failure

        monkeypatch.setattr(ExecutionRepository, "complete_operation", fail_completion)
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    request.addfinalizer(db.close)
    source = JSONSource(
        {"path": str(source_path), "snapshot_for_resume": snapshot, "schema": {"mode": "observed"}, "on_validation_failure": "discard"}
    )
    sink = CollectSink("output")
    config = PipelineConfig(sources={"primary": as_source(source)}, transforms=[], sinks={"output": as_sink(sink)})
    with pytest.raises(type(failure)) as raised:
        Orchestrator(db=db).run(
            config, graph=build_production_graph(config), payload_store=FilesystemPayloadStore(tmp_path / "payloads"), shutdown_event=event
        )
    assert raised.value is failure
    assert sink.results == []
    with db.connection() as connection:
        assert connection.execute(select(runs_table.c.status)).scalar_one() == RunStatus.FAILED.value
        expected_lifecycle = "interrupted" if failure_kind == "audit" and not snapshot else "loading"
        assert connection.execute(select(run_sources_table.c.lifecycle_state)).scalar_one() == expected_lifecycle
        assert connection.execute(select(operations_table.c.output_data_ref)).scalars().all() == [None]
