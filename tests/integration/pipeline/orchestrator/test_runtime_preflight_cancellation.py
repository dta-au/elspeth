"""Ordinary local public-run controls for accepted readiness interruption policy."""

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from elspeth.contracts import Determinism, FrameworkBugError, ResumePoint, RunStatus, SourceRow
from elspeth.contracts.checkpoint import ResumeRefusalCause
from elspeth.contracts.config.runtime import RuntimeCheckpointConfig, RuntimeRetryConfig
from elspeth.contracts.errors import GracefulShutdownError, IncompleteSourceResumeError, RuntimePreflightFailedError
from elspeth.contracts.plugin_context import PluginContext
from elspeth.core.checkpoint import CheckpointManager, RecoveryManager
from elspeth.core.config import CheckpointSettings, ElspethSettings, RetrySettings, SinkSettings, SourceSettings
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.execution_repository import ExecutionRepository
from elspeth.core.landscape.schema import operations_table, rows_table, run_sources_table, runs_table, token_outcomes_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.engine.orchestrator.cleanup import cleanup_plugins
from elspeth.engine.orchestrator.runtime_preflight import run_transform_runtime_preflights
from elspeth.engine.retry import RetryManager
from elspeth.plugins.infrastructure.clients.llm import RateLimitError
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.sources.json_source import JSONSource
from tests.fixtures.base_classes import as_sink, as_source, as_transform
from tests.fixtures.landscape import RecorderSetup, make_recorder_with_run, register_test_node
from tests.fixtures.pipeline import build_production_graph
from tests.fixtures.plugins import CollectSink, ListSource, PassTransform


class _Readiness(PassTransform):
    name = "local_shutdown_readiness"
    determinism = Determinism.DETERMINISTIC
    requires_runtime_preflight = True

    def __init__(self, event: threading.Event, mode: str, *, required: bool = True) -> None:
        super().__init__()
        self.event = event
        self.mode = mode
        self.requires_runtime_preflight = required
        self.attempts = 0
        self.closed = 0
        self.contexts: list[tuple[str | None, str | None]] = []
        self.plugin_error = InterruptedError("ordinary plugin interruption")
        self.cleanup_error: FrameworkBugError | None = None
        self.stop_after: int | None = None
        self.processed = 0

    def runtime_preflight(self, ctx: Any) -> None:
        self.attempts += 1
        self.contexts.append((ctx.node_id, ctx.operation_id))
        if self.mode in {"success_cancel", "retry_cancel", "plugin_interrupt", "failure"}:
            self.event.set()
        if self.mode in {"retry_cancel", "retry_wait"}:
            raise RuntimePreflightFailedError(
                plugin_name=self.name, provider="local-control", cause=RateLimitError("local transient error")
            )
        if self.mode == "plugin_interrupt":
            raise self.plugin_error
        if self.mode == "failure":
            raise ValueError("local readiness failure")

    def process(self, row: Any, ctx: Any) -> TransformResult:
        self.processed += 1
        if self.processed == self.stop_after:
            self.event.set()
        return super().process(row, ctx)

    def close(self) -> None:
        self.closed += 1
        if self.cleanup_error is not None:
            raise self.cleanup_error
        super().close()


class _Source(JSONSource):
    determinism = Determinism.IO_READ

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.entries = 0
        self.closed = 0
        self.forbid_load = False

    def load(self, ctx: Any) -> Iterator[SourceRow]:
        assert not self.forbid_load, "resume reopened the original source"
        self.entries += 1
        yield from super().load(ctx)

    def close(self) -> None:
        self.closed += 1
        super().close()


@pytest.mark.parametrize("with_retry", [False, True])
@pytest.mark.parametrize(
    "mode,required",
    [
        ("normal", True),
        ("preset", True),
        ("success_cancel", True),
        ("retry_cancel", True),
        ("plugin_interrupt", True),
        ("failure", True),
        ("normal", False),
        ("preset", False),
    ],
)
def test_public_readiness_shutdown_policy(
    tmp_path: Path, request: pytest.FixtureRequest, mode: str, required: bool, with_retry: bool
) -> None:
    path = tmp_path / "input.jsonl"
    path.write_text('{"query":"local"}\n')
    source = _Source({"path": str(path), "snapshot_for_resume": True, "schema": {"mode": "observed"}, "on_validation_failure": "discard"})
    event = threading.Event()
    if mode == "preset":
        event.set()
    first = _Readiness(event, mode, required=required)
    later = _Readiness(event, "normal", required=required)
    sink = CollectSink("output")
    config = PipelineConfig(
        sources={"primary": as_source(source)}, transforms=[as_transform(first), as_transform(later)], sinks={"output": as_sink(sink)}
    )
    graph = build_production_graph(config)
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    request.addfinalizer(db.close)
    checkpoints = CheckpointManager(db)
    payloads = FilesystemPayloadStore(tmp_path / "payloads")
    orchestrator = Orchestrator(
        db,
        checkpoint_manager=checkpoints,
        checkpoint_config=RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row")),
    )
    settings = None
    if with_retry:
        settings = ElspethSettings(
            sources={"primary": SourceSettings(plugin="json", on_success="output")},
            sinks={"output": SinkSettings(plugin="collect", on_write_failure="discard")},
            retry=RetrySettings(max_attempts=2, initial_delay_seconds=0.01, max_delay_seconds=0.1),
        )
    run_id = "local-readiness-shutdown"
    interrupted = mode in {"preset", "success_cancel"} or (mode == "retry_cancel" and with_retry)
    failed = mode in {"plugin_interrupt", "failure"} or (mode == "retry_cancel" and not with_retry)
    kwargs = {"graph": graph, "payload_store": payloads, "shutdown_event": event, "run_id": run_id, "settings": settings}
    if interrupted:
        with pytest.raises(GracefulShutdownError) as error:
            orchestrator.run(config, **kwargs)
        assert error.value.run_id == run_id
        assert error.value.rows_processed == 0
    elif failed:
        expected = {"plugin_interrupt": InterruptedError, "failure": ValueError, "retry_cancel": RuntimePreflightFailedError}[mode]
        with pytest.raises(expected) as error:
            orchestrator.run(config, **kwargs)
        if mode == "plugin_interrupt":
            assert error.value is first.plugin_error
    else:
        result = orchestrator.run(config, **kwargs)
        assert result.status is RunStatus.COMPLETED
        assert result.rows_processed == 1

    assert first.closed == later.closed == source.closed == 1
    with db.connection() as connection:
        status = connection.execute(select(runs_table.c.status)).scalar_one()
        lifecycle = connection.execute(select(run_sources_table.c.lifecycle_state)).scalar_one()
        preflights = connection.execute(
            select(operations_table.c.node_id, operations_table.c.status).where(operations_table.c.operation_type == "runtime_preflight")
        ).all()
        source_loads = connection.execute(
            select(operations_table.c.operation_id).where(operations_table.c.operation_type == "source_load")
        ).all()
        rows = connection.execute(select(rows_table.c.row_id)).all()
        outcomes = connection.execute(select(token_outcomes_table.c.token_id)).all()
    assert status == (RunStatus.INTERRUPTED if interrupted else RunStatus.FAILED if failed else RunStatus.COMPLETED).value
    if interrupted or failed:
        assert source.entries == 0
        assert lifecycle == "ready"
        assert source_loads == rows == outcomes == []
        assert sink.results == []
        assert later.attempts == 0
        check = RecoveryManager(db, checkpoints).can_resume(run_id, graph)
        assert not check.can_resume
        assert check.cause is ResumeRefusalCause.SOURCE_NOT_EXHAUSTED
        checkpoint = checkpoints.get_latest_checkpoint(run_id)
        assert checkpoint is not None
        point = ResumePoint(checkpoint=checkpoint, sequence_number=checkpoint.sequence_number)
        with pytest.raises(IncompleteSourceResumeError, match=r"primary.*ready"):
            orchestrator.resume(resume_point=point, config=config, graph=graph, payload_store=payloads)
        assert source.entries == 0
        assert sink.results == []
    else:
        assert source.entries == 1
        assert [row["query"] for row in sink.results] == ["local"]
        assert first.attempts == later.attempts == int(required)
    if mode == "preset":
        assert first.attempts == 0
        assert preflights == []
    elif required:
        expected_status = "failed" if failed or mode == "retry_cancel" else "completed"
        expected_preflights = [(first.node_id, expected_status)]
        if not interrupted and not failed:
            expected_preflights.append((later.node_id, "completed"))
        assert sorted(preflights) == sorted(expected_preflights)
        assert first.contexts[0][0] == first.node_id
        assert first.contexts[0][1] is not None
    else:
        assert preflights == []


class _StopDuringWait(threading.Event):
    def wait(self, timeout: float | None = None) -> bool:
        assert timeout is not None and timeout > 0
        self.set()
        return True


def _readiness_boundary(
    tmp_path: Path, request: pytest.FixtureRequest, event: threading.Event, mode: str = "normal"
) -> tuple[RecorderSetup, FilesystemPayloadStore, _Readiness, PipelineConfig, PluginContext]:
    payloads = FilesystemPayloadStore(tmp_path / "payloads")
    setup = make_recorder_with_run(payload_store=payloads)
    request.addfinalizer(setup.db.close)
    transform = _Readiness(event, mode)
    transform.node_id = register_test_node(setup.data_flow, setup.run_id, "readiness", plugin_name=transform.name)
    config = PipelineConfig(
        sources={"primary": as_source(ListSource([]))},
        transforms=[as_transform(transform)],
        sinks={"default": as_sink(CollectSink())},
    )
    ctx = PluginContext(
        run_id=setup.run_id,
        node_id=setup.source_node_id,
        config={},
        landscape=setup.factory.plugin_audit_writer(),
        coordination_token=setup.coordination_token,
    )
    return setup, payloads, transform, config, ctx


@pytest.mark.parametrize("with_retry", [False, True])
def test_shutdown_after_operation_begin_does_not_count_an_unstarted_attempt(
    tmp_path: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, with_retry: bool
) -> None:
    event = threading.Event()
    setup, payloads, transform, config, ctx = _readiness_boundary(tmp_path, request, event)
    original = ExecutionRepository.begin_operation

    def begin_then_stop(repository: ExecutionRepository, **kwargs: Any) -> Any:
        operation = original(repository, **kwargs)
        assert kwargs["operation_type"] == "runtime_preflight"
        event.set()
        return operation

    monkeypatch.setattr(ExecutionRepository, "begin_operation", begin_then_stop)
    manager = RetryManager(RuntimeRetryConfig.default()) if with_retry else None
    with pytest.raises(GracefulShutdownError):
        run_transform_runtime_preflights(
            setup.factory,
            setup.run_id,
            config,
            ctx,
            retry_manager=manager,
            shutdown_event=event,
        )
    assert transform.attempts == 0
    assert ctx.node_id == setup.source_node_id
    assert ctx.operation_id is None
    operations = setup.execution.get_operations_for_run(setup.run_id)
    assert len(operations) == 1
    assert operations[0].status == "failed"
    assert operations[0].output_data_ref is not None
    assert json.loads(payloads.retrieve(operations[0].output_data_ref)) == {
        "attempt_count": 0,
        "retry_count": 0,
        "retry_errors": [],
    }


@pytest.mark.parametrize("mode", ["preset", "success_cancel", "retry_cancel"])
def test_coordination_refusal_wins_over_coincident_shutdown(tmp_path: Path, request: pytest.FixtureRequest, mode: str) -> None:
    event = threading.Event()
    setup, _payloads, transform, config, ctx = _readiness_boundary(tmp_path, request, event, mode)
    refusal = FrameworkBugError("controlled coordination refusal")
    if mode == "preset":
        event.set()

    def check_latch() -> None:
        if event.is_set():
            raise refusal

    with pytest.raises(FrameworkBugError) as error:
        run_transform_runtime_preflights(
            setup.factory,
            setup.run_id,
            config,
            ctx,
            retry_manager=RetryManager(RuntimeRetryConfig.default()),
            shutdown_event=event,
            check_coordination_latch=check_latch,
        )
    assert error.value is refusal
    assert ctx.node_id == setup.source_node_id
    assert ctx.operation_id is None
    operations = setup.execution.get_operations_for_run(setup.run_id)
    if mode == "preset":
        assert transform.attempts == 0
        assert operations == []
    else:
        assert transform.attempts == 1
        assert len(operations) == 1
        assert operations[0].status == ("completed" if mode == "success_cancel" else "failed")
    with setup.db.connection() as connection:
        assert connection.execute(select(runs_table.c.status)).scalar_one() == RunStatus.RUNNING.value


def test_tier_one_cleanup_failure_preserves_its_identity_over_shutdown(tmp_path: Path, request: pytest.FixtureRequest) -> None:
    event = threading.Event()
    setup, _payloads, transform, config, ctx = _readiness_boundary(tmp_path, request, event, "success_cancel")
    with pytest.raises(GracefulShutdownError) as stop:
        run_transform_runtime_preflights(setup.factory, setup.run_id, config, ctx, shutdown_event=event)
    corruption = FrameworkBugError("controlled cleanup corruption")
    transform.cleanup_error = corruption
    with pytest.raises(FrameworkBugError) as error:
        cleanup_plugins(config, ctx, include_source=True, pending_exc=stop.value)
    assert error.value is corruption
    assert ctx.node_id == setup.source_node_id
    assert ctx.operation_id is None


@pytest.mark.parametrize(
    "mode,audit_failure",
    [("normal", False), ("plugin_interrupt", False), ("retry_cancel", False), ("retry_wait", False), ("retry_cancel", True)],
)
def test_readiness_operation_restores_context_and_counts_started_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, audit_failure: bool
) -> None:
    payloads = FilesystemPayloadStore(tmp_path / "payloads")
    setup = make_recorder_with_run(payload_store=payloads)
    event = _StopDuringWait() if mode == "retry_wait" else threading.Event()
    transform = _Readiness(event, mode)
    transform.node_id = register_test_node(setup.data_flow, setup.run_id, "readiness", plugin_name=transform.name)
    config = PipelineConfig(
        sources={"primary": as_source(ListSource([]))},
        transforms=[as_transform(transform)],
        sinks={"default": as_sink(CollectSink())},
    )
    ctx = PluginContext(
        run_id=setup.run_id,
        node_id=setup.source_node_id,
        config={},
        landscape=setup.factory.plugin_audit_writer(),
        coordination_token=setup.coordination_token,
    )
    prior = setup.execution.begin_operation(
        coordination_token=setup.coordination_token,
        node_id=setup.source_node_id,
        operation_type="source_load",
    )
    ctx.operation_id = prior.operation_id
    manager = RetryManager(RuntimeRetryConfig(max_attempts=3, base_delay=0.01, max_delay=0.1, jitter=0.0, exponential_base=2.0))
    corruption = FrameworkBugError("controlled readiness audit failure")

    def fail_completion(_repository: ExecutionRepository, **kwargs: Any) -> None:
        assert kwargs["operation_id"] != prior.operation_id
        raise corruption

    try:
        with monkeypatch.context() as patch:
            if audit_failure:
                patch.setattr(ExecutionRepository, "complete_operation", fail_completion)
            expected = FrameworkBugError if audit_failure else InterruptedError if mode == "plugin_interrupt" else GracefulShutdownError
            if mode == "normal":
                run_transform_runtime_preflights(setup.factory, setup.run_id, config, ctx, retry_manager=manager, shutdown_event=event)
            else:
                with pytest.raises(expected) as error:
                    run_transform_runtime_preflights(setup.factory, setup.run_id, config, ctx, retry_manager=manager, shutdown_event=event)
                if audit_failure:
                    assert error.value is corruption
                elif mode == "plugin_interrupt":
                    assert error.value is transform.plugin_error
        assert ctx.node_id == setup.source_node_id
        assert ctx.operation_id == prior.operation_id
        assert transform.attempts == 1
        operations = [op for op in setup.execution.get_operations_for_run(setup.run_id) if op.operation_type == "runtime_preflight"]
        assert len(operations) == 1
        if audit_failure:
            assert operations[0].status == "open"
        else:
            assert operations[0].status == ("completed" if mode == "normal" else "failed")
            assert operations[0].output_data_ref is not None
            metadata = json.loads(payloads.retrieve(operations[0].output_data_ref))
            assert metadata == {
                "attempt_count": 1,
                "retry_count": int(mode == "retry_wait"),
                "retry_errors": [{"attempt": 0, "error_type": "RuntimePreflightFailedError"}] if mode == "retry_wait" else [],
            }
    finally:
        setup.execution.complete_operation(
            coordination_token=setup.coordination_token,
            operation_id=prior.operation_id,
            status="completed",
            output_data=None,
            error=None,
            duration_ms=0.0,
        )
        setup.db.close()


@pytest.mark.parametrize("mode", ["preset", "success_cancel", "retry_cancel"])
@pytest.mark.parametrize("checkpoint_fault", [None, "write", "cleanup"])
def test_cancelled_resume_readiness_preserves_snapshot_suffix_and_old_outcomes(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    checkpoint_fault: str | None,
) -> None:
    source_path = tmp_path / "resume.jsonl"
    source_path.write_text("".join(json.dumps({"query": name}) + "\n" for name in ("Alpha", "Beta", "Gamma", "Delta")))
    options = {
        "path": str(source_path),
        "snapshot_for_resume": True,
        "schema": {"mode": "observed"},
        "on_validation_failure": "discard",
    }
    db = LandscapeDB(f"sqlite:///{tmp_path / 'resume.db'}")
    request.addfinalizer(db.close)
    payloads = FilesystemPayloadStore(tmp_path / "payloads")
    checkpoints = CheckpointManager(db)
    checkpoint_config = RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row"))
    settings = ElspethSettings(
        sources={"primary": SourceSettings(plugin="json", on_success="output")},
        sinks={"output": SinkSettings(plugin="collect", on_write_failure="discard")},
        retry=RetrySettings(max_attempts=2, initial_delay_seconds=0.01, max_delay_seconds=0.1),
    )
    event = threading.Event()
    sink = CollectSink("output")

    def build(readiness_mode: str, *, resuming: bool) -> tuple[PipelineConfig, _Source, _Readiness, _Readiness]:
        source = _Source(options)
        source.forbid_load = resuming
        first = _Readiness(event, readiness_mode)
        later = _Readiness(event, "normal")
        later.stop_after = None if resuming else 2
        config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(first), as_transform(later)],
            sinks={"output": as_sink(sink)},
        )
        return config, source, first, later

    initial, source, first, _later = build("normal", resuming=False)
    initial_graph = build_production_graph(initial)
    orchestrator = Orchestrator(db, checkpoint_manager=checkpoints, checkpoint_config=checkpoint_config)
    with pytest.raises(GracefulShutdownError) as first_stop:
        orchestrator.run(initial, graph=initial_graph, payload_store=payloads, settings=settings, shutdown_event=event)
    run_id = first_stop.value.run_id
    assert first.processed == 2
    assert source.entries == 1
    assert [row["query"] for row in sink.results] == ["Alpha", "Beta"]
    with db.connection() as connection:
        assert connection.execute(select(run_sources_table.c.lifecycle_state)).scalar_one() == "exhausted"
        old_outcomes = connection.execute(select(token_outcomes_table).order_by(token_outcomes_table.c.token_id)).all()
        old_rows = connection.execute(select(rows_table).order_by(rows_table.c.ingest_sequence)).all()
        source_operations = connection.execute(select(operations_table).where(operations_table.c.operation_type == "source_load")).all()
        old_preflights = set(
            connection.execute(
                select(operations_table.c.operation_id).where(operations_table.c.operation_type == "runtime_preflight")
            ).scalars()
        )
    assert len(old_outcomes) == len(old_rows) == 2
    assert len(source_operations) == 1
    assert source_operations[0].status == "completed"
    assert source_operations[0].output_data_ref is not None
    snapshot_metadata = json.loads(payloads.retrieve(source_operations[0].output_data_ref))
    snapshot_ref = snapshot_metadata["source_snapshot_ref"]
    original_snapshot = payloads.retrieve(snapshot_ref)
    source_path.unlink()
    event.clear()
    if mode == "preset":
        event.set()
    resumed, resumed_source, resumed_first, resumed_later = build(mode, resuming=True)
    resumed_graph = build_production_graph(resumed)
    recovery = RecoveryManager(db, checkpoints)
    assert recovery.can_resume(run_id, resumed_graph).can_resume
    point_before_readiness = recovery.get_resume_point(run_id, resumed_graph)
    resumed_orchestrator = Orchestrator(db, checkpoint_manager=checkpoints, checkpoint_config=checkpoint_config)
    checkpoint_error = RuntimeError("controlled checkpoint write failure")
    cleanup_error = FrameworkBugError("controlled checkpoint cleanup corruption")

    def fail_checkpoint(**kwargs: Any) -> None:
        assert kwargs["draft"].sequence_number > point_before_readiness.sequence_number
        raise checkpoint_error

    if checkpoint_fault == "cleanup":
        resumed_first.cleanup_error = cleanup_error
    expected_error = FrameworkBugError if checkpoint_fault == "cleanup" else RuntimeError if checkpoint_fault else GracefulShutdownError
    with monkeypatch.context() as patch:
        if checkpoint_fault:
            patch.setattr(checkpoints, "create_checkpoint", fail_checkpoint)
        with pytest.raises(expected_error) as stop:
            resumed_orchestrator.resume(
                resume_point=point_before_readiness,
                config=resumed,
                graph=resumed_graph,
                payload_store=payloads,
                settings=settings,
                shutdown_event=event,
            )
    if checkpoint_fault:
        assert stop.value is (cleanup_error if checkpoint_fault == "cleanup" else checkpoint_error)
        assert resumed_first.closed == 1
        assert resumed_later.closed == int(checkpoint_fault == "write")
    else:
        assert isinstance(stop.value, GracefulShutdownError)
        assert stop.value.run_id == run_id
        assert stop.value.rows_processed == stop.value.rows_succeeded == stop.value.rows_failed == 0
    assert resumed_source.entries == resumed_source.closed == 0
    assert resumed_first.attempts == int(mode != "preset")
    assert resumed_later.attempts == resumed_first.processed == resumed_later.processed == 0
    point_after_readiness = recovery.get_resume_point(run_id, resumed_graph)
    if checkpoint_fault:
        assert point_after_readiness.sequence_number == point_before_readiness.sequence_number
    else:
        assert point_after_readiness.sequence_number > point_before_readiness.sequence_number
    assert [row["query"] for row in sink.results] == ["Alpha", "Beta"]
    assert payloads.retrieve(snapshot_ref) == original_snapshot
    with db.connection() as connection:
        status = connection.execute(select(runs_table.c.status)).scalar_one()
        if checkpoint_fault:
            assert status != RunStatus.INTERRUPTED.value
        else:
            assert status == RunStatus.INTERRUPTED.value
        assert connection.execute(select(token_outcomes_table).order_by(token_outcomes_table.c.token_id)).all() == old_outcomes
        assert connection.execute(select(rows_table).order_by(rows_table.c.ingest_sequence)).all() == old_rows
        assert (
            connection.execute(select(operations_table).where(operations_table.c.operation_type == "source_load")).all()
            == source_operations
        )
        new_preflights = connection.execute(
            select(operations_table.c.operation_id, operations_table.c.node_id, operations_table.c.status).where(
                operations_table.c.operation_type == "runtime_preflight"
            )
        ).all()
    assert [(op.node_id, op.status) for op in new_preflights if op.operation_id not in old_preflights] == (
        [] if mode == "preset" else [(resumed_first.node_id, "failed" if mode == "retry_cancel" else "completed")]
    )

    if checkpoint_fault:
        return

    event.clear()
    healthy, healthy_source, _healthy_first, _healthy_later = build("normal", resuming=True)
    healthy_graph = build_production_graph(healthy)
    healthy_orchestrator = Orchestrator(db, checkpoint_manager=checkpoints, checkpoint_config=checkpoint_config)
    assert recovery.can_resume(run_id, healthy_graph).can_resume
    result = healthy_orchestrator.resume(
        resume_point=recovery.get_resume_point(run_id, healthy_graph),
        config=healthy,
        graph=healthy_graph,
        payload_store=payloads,
        settings=settings,
        shutdown_event=event,
    )
    assert result.status is RunStatus.COMPLETED
    assert healthy_source.entries == 0
    assert [row["query"] for row in sink.results] == ["Alpha", "Beta", "Gamma", "Delta"]
    with db.connection() as connection:
        assert connection.execute(select(rows_table.c.source_row_index).order_by(rows_table.c.ingest_sequence)).scalars().all() == [
            0,
            1,
            2,
            3,
        ]
        final_outcomes = connection.execute(select(token_outcomes_table).order_by(token_outcomes_table.c.token_id)).all()
    assert len(final_outcomes) == 4
    assert all(outcome.completed for outcome in final_outcomes)
    assert len({outcome.token_id for outcome in final_outcomes}) == 4
    assert set(old_outcomes).issubset(final_outcomes)
