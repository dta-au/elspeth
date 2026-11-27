"""Genuine process-death recovery matrix for pipeline sink effects."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from elspeth.contracts import Determinism
from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
from elspeth.contracts.errors import SinkRequiredFieldsViolation
from elspeth.contracts.events import EngineSpanCompleted, EngineSpanName, EngineSpanStatus
from elspeth.contracts.scheduler import SchedulerEventType
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.core.checkpoint import CheckpointManager, RecoveryManager, check_run_status_resumable
from elspeth.core.config import CheckpointSettings, QueueSettings, SourceSettings, TransformSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.wiring import WiredTransform
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.executors.sink_effects import SinkEffectCoordinator, SinkEffectExecutionSeam
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.engine.orchestrator.run_status import derive_resume_terminal_status_from_audit
from elspeth.engine.processor import RowProcessor
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.sinks import _local_file_effects
from elspeth.plugins.sinks.json_sink import JSONSink
from elspeth.telemetry import TelemetryManager
from tests.e2e.recovery.harness import (
    _InterruptibleSource,
    _PassthroughTransform,
    spawn_database_process_at_seam,
    spawn_database_process_with_pause,
)
from tests.fixtures.base_classes import as_sink, as_source, as_transform
from tests.fixtures.landscape import expire_leader_seat, expire_lease
from tests.fixtures.telemetry import MockTelemetryConfig, TelemetryTestExporter
from tests.helpers.state_engine import StateEngineImage, capture_state_engine_image

if TYPE_CHECKING:
    from scripts.state_engine_profile_reporter import RuntimeProfileReporter

_PROCESS_TIMEOUT_SECONDS = 15.0
_LEASE_SECONDS = 0.2
_ROWS = [{"id": 1, "value": 10}]
_VISIBLE_PUBLICATION_SEAM = "after_visible_publication"
_SCHEDULER_CALLBACK_SEAM = "scheduler_callback"
_EXPECTED_BYTES = b'{"id": 1, "value": 10}\n'
_SINK_BATCHES_SUFFIX = ".sink-batches.json"
_PROCESS_DEATH_SEAMS = (
    SinkEffectExecutionSeam.BEFORE_RESERVATION.value,
    SinkEffectExecutionSeam.AFTER_RESERVATION.value,
    SinkEffectExecutionSeam.AFTER_PREPARATION_CLAIM.value,
    SinkEffectExecutionSeam.AFTER_INSPECTION.value,
    SinkEffectExecutionSeam.AFTER_PLAN_CAS.value,
    SinkEffectExecutionSeam.BEFORE_EFFECT.value,
    _VISIBLE_PUBLICATION_SEAM,
    SinkEffectExecutionSeam.AFTER_EFFECT_BEFORE_RETURN.value,
    SinkEffectExecutionSeam.AFTER_RETURN_BEFORE_FINALIZE.value,
    SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE.value,
    _SCHEDULER_CALLBACK_SEAM,
)

_KILLED_ATTEMPTS: dict[str, tuple[tuple[str, str, int], ...]] = {
    SinkEffectExecutionSeam.AFTER_RESERVATION.value: (),
    SinkEffectExecutionSeam.AFTER_PREPARATION_CLAIM.value: (),
    SinkEffectExecutionSeam.AFTER_INSPECTION.value: (("inspect", "returned", 1),),
    SinkEffectExecutionSeam.AFTER_PLAN_CAS.value: (("inspect", "returned", 1),),
    SinkEffectExecutionSeam.BEFORE_EFFECT.value: (
        ("inspect", "returned", 1),
        ("reconcile", "returned", 2),
        ("commit", "intent", 2),
    ),
    _VISIBLE_PUBLICATION_SEAM: (
        ("inspect", "returned", 1),
        ("reconcile", "returned", 2),
        ("commit", "intent", 2),
    ),
    SinkEffectExecutionSeam.AFTER_EFFECT_BEFORE_RETURN.value: (
        ("inspect", "returned", 1),
        ("reconcile", "returned", 2),
        ("commit", "intent", 2),
    ),
    SinkEffectExecutionSeam.AFTER_RETURN_BEFORE_FINALIZE.value: (
        ("inspect", "returned", 1),
        ("reconcile", "returned", 2),
        ("commit", "returned", 2),
    ),
    SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE.value: (
        ("inspect", "returned", 1),
        ("reconcile", "returned", 2),
        ("commit", "returned", 2),
    ),
    _SCHEDULER_CALLBACK_SEAM: (
        ("inspect", "returned", 1),
        ("reconcile", "returned", 2),
        ("commit", "returned", 2),
    ),
}

_FINAL_ATTEMPTS: dict[str, tuple[tuple[str, str, int], ...]] = {
    SinkEffectExecutionSeam.BEFORE_RESERVATION.value: (
        ("inspect", "returned", 1),
        ("reconcile", "returned", 2),
        ("commit", "returned", 2),
    ),
    SinkEffectExecutionSeam.AFTER_RESERVATION.value: (
        ("inspect", "returned", 1),
        ("reconcile", "returned", 2),
        ("commit", "returned", 2),
    ),
    SinkEffectExecutionSeam.AFTER_PREPARATION_CLAIM.value: (
        ("inspect", "returned", 2),
        ("reconcile", "returned", 3),
        ("commit", "returned", 3),
    ),
    SinkEffectExecutionSeam.AFTER_INSPECTION.value: (
        ("inspect", "returned", 1),
        ("reconcile", "returned", 3),
        ("commit", "returned", 3),
    ),
    SinkEffectExecutionSeam.AFTER_PLAN_CAS.value: (
        ("inspect", "returned", 1),
        ("reconcile", "returned", 2),
        ("commit", "returned", 2),
    ),
    SinkEffectExecutionSeam.BEFORE_EFFECT.value: (
        ("inspect", "returned", 1),
        ("reconcile", "returned", 2),
        ("commit", "response_lost", 2),
        ("reconcile", "returned", 3),
        ("commit", "returned", 3),
    ),
    _VISIBLE_PUBLICATION_SEAM: (
        ("inspect", "returned", 1),
        ("reconcile", "returned", 2),
        ("commit", "response_lost", 2),
        ("reconcile", "returned", 3),
    ),
    SinkEffectExecutionSeam.AFTER_EFFECT_BEFORE_RETURN.value: (
        ("inspect", "returned", 1),
        ("reconcile", "returned", 2),
        ("commit", "response_lost", 2),
        ("reconcile", "returned", 3),
    ),
    SinkEffectExecutionSeam.AFTER_RETURN_BEFORE_FINALIZE.value: (
        ("inspect", "returned", 1),
        ("reconcile", "returned", 2),
        ("commit", "returned", 2),
    ),
    SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE.value: (
        ("inspect", "returned", 1),
        ("reconcile", "returned", 2),
        ("commit", "returned", 2),
    ),
    _SCHEDULER_CALLBACK_SEAM: (
        ("inspect", "returned", 1),
        ("reconcile", "returned", 2),
        ("commit", "returned", 2),
    ),
}

_KILLED_EFFECT_GENERATIONS: dict[str, int | None] = {
    SinkEffectExecutionSeam.BEFORE_RESERVATION.value: None,
    SinkEffectExecutionSeam.AFTER_RESERVATION.value: 0,
    SinkEffectExecutionSeam.AFTER_PREPARATION_CLAIM.value: 1,
    SinkEffectExecutionSeam.AFTER_INSPECTION.value: 1,
    SinkEffectExecutionSeam.AFTER_PLAN_CAS.value: 1,
    SinkEffectExecutionSeam.BEFORE_EFFECT.value: 2,
    _VISIBLE_PUBLICATION_SEAM: 2,
    SinkEffectExecutionSeam.AFTER_EFFECT_BEFORE_RETURN.value: 2,
    SinkEffectExecutionSeam.AFTER_RETURN_BEFORE_FINALIZE.value: 2,
    SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE.value: 2,
    _SCHEDULER_CALLBACK_SEAM: 2,
}

_FINAL_EFFECT_GENERATIONS: dict[str, int] = {
    SinkEffectExecutionSeam.BEFORE_RESERVATION.value: 2,
    SinkEffectExecutionSeam.AFTER_RESERVATION.value: 2,
    SinkEffectExecutionSeam.AFTER_PREPARATION_CLAIM.value: 3,
    SinkEffectExecutionSeam.AFTER_INSPECTION.value: 3,
    SinkEffectExecutionSeam.AFTER_PLAN_CAS.value: 2,
    SinkEffectExecutionSeam.BEFORE_EFFECT.value: 3,
    _VISIBLE_PUBLICATION_SEAM: 3,
    SinkEffectExecutionSeam.AFTER_EFFECT_BEFORE_RETURN.value: 3,
    SinkEffectExecutionSeam.AFTER_RETURN_BEFORE_FINALIZE.value: 3,
    SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE.value: 2,
    _SCHEDULER_CALLBACK_SEAM: 2,
}


@dataclass(frozen=True)
class _TargetImage:
    exists: bool
    content_hash: str | None
    size_bytes: int | None
    file_id: tuple[int, int] | None


def _capture_target_image(path: Path) -> _TargetImage:
    if not path.exists():
        return _TargetImage(exists=False, content_hash=None, size_bytes=None, file_id=None)
    payload = path.read_bytes()
    stat_result = path.stat()
    return _TargetImage(
        exists=True,
        content_hash=sha256(payload).hexdigest(),
        size_bytes=len(payload),
        file_id=(stat_result.st_dev, stat_result.st_ino),
    )


class _StopAfterFirstTransform(_PassthroughTransform):
    """Request shutdown after A completes while the sealed snapshot still holds B."""

    determinism = Determinism.DETERMINISTIC

    def __init__(self, shutdown_event: threading.Event | None) -> None:
        super().__init__()
        self._shutdown_event = shutdown_event

    def process(self, row: PipelineRow, ctx: Any) -> TransformResult:
        result = super().process(row, ctx)
        if self._shutdown_event is not None:
            self._shutdown_event.set()
        return result


def _build_pipeline(
    output_path: Path,
    *,
    rows: list[dict[str, Any]] | None = None,
    snapshot_for_resume: bool = False,
    shutdown_event: threading.Event | None = None,
) -> tuple[PipelineConfig, ExecutionGraph]:
    source = _InterruptibleSource(_ROWS if rows is None else rows, on_success="inbound")
    if snapshot_for_resume:
        source.config["snapshot_for_resume"] = True
    transform = _StopAfterFirstTransform(shutdown_event) if snapshot_for_resume else _PassthroughTransform()
    schema: dict[str, Any] = {"mode": "observed"}
    if snapshot_for_resume:
        schema["required_fields"] = ["must_exist"]
    sink = JSONSink(
        {
            "path": str(output_path),
            "format": "jsonl",
            "mode": "write",
            "schema": schema,
        }
    )
    sources = {"primary": as_source(source)}
    sinks = {"output": as_sink(sink)}
    graph = ExecutionGraph.from_plugin_instances(
        sources=sources,
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="inbound", options={})},
        transforms=[
            WiredTransform(
                plugin=as_transform(transform),
                settings=TransformSettings(
                    name="passthrough_0",
                    plugin=transform.name,
                    input="inbound",
                    on_success="output",
                    on_error="discard",
                    options={},
                ),
            )
        ],
        sinks=sinks,
        queues={"inbound": QueueSettings(description="sink-effect process-death matrix")},
    )
    return (
        PipelineConfig(
            sources=sources,
            transforms=[as_transform(transform)],
            sinks=sinks,
            sink_effect_modes={"output": "write"},
        ),
        graph,
    )


def _install_short_sink_lease() -> None:
    original_init = SinkEffectCoordinator.__init__

    def short_init(self: SinkEffectCoordinator, *args: object, **kwargs: object) -> None:
        kwargs.setdefault("lease_ttl", timedelta(seconds=_LEASE_SECONDS))
        kwargs.setdefault("poll_interval", 0.02)
        original_init(self, *args, **kwargs)

    SinkEffectCoordinator.__init__ = short_init  # type: ignore[method-assign]


def _run_to_sink_seam(
    db: LandscapeDB,
    pause: object,
    run_id: str,
    output_path: str,
    payload_path: str,
    seam_value: str,
    rows: list[dict[str, Any]] | None = None,
    snapshot_for_resume: bool = False,
) -> None:
    assert callable(pause)
    _install_short_sink_lease()
    if seam_value == _VISIBLE_PUBLICATION_SEAM:
        _local_file_effects._after_replace = lambda _target: pause()
    elif seam_value == _SCHEDULER_CALLBACK_SEAM:
        original_terminalize = RowProcessor.mark_sink_bound_scheduler_terminal_many

        def pause_before_scheduler_callback(self: RowProcessor, token_ids: tuple[str, ...]) -> None:
            pause()
            original_terminalize(self, token_ids)

        RowProcessor.mark_sink_bound_scheduler_terminal_many = pause_before_scheduler_callback
    else:
        target_seam = SinkEffectExecutionSeam(seam_value)
        original_fault = SinkEffectCoordinator._fault

        def pause_at_target(self: SinkEffectCoordinator, seam: SinkEffectExecutionSeam) -> None:
            if seam is target_seam:
                pause()
            original_fault(self, seam)

        SinkEffectCoordinator._fault = pause_at_target  # type: ignore[method-assign]
    shutdown_event = threading.Event() if snapshot_for_resume else None
    config, graph = _build_pipeline(Path(output_path), rows=rows, snapshot_for_resume=snapshot_for_resume, shutdown_event=shutdown_event)
    checkpoint_config = RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row"))
    Orchestrator(
        db,
        checkpoint_manager=CheckpointManager(db),
        checkpoint_config=checkpoint_config,
    ).run(
        config,
        graph=graph,
        payload_store=FilesystemPayloadStore(Path(payload_path)),
        run_id=run_id,
        openrouter_catalog_sha256="0" * 64,
        openrouter_catalog_source="bundled",
        shutdown_event=shutdown_event,
    )


def _resume_run(
    db: LandscapeDB,
    run_id: str,
    output_path: str,
    payload_path: str,
    rows: list[dict[str, Any]] | None = None,
    snapshot_for_resume: bool = False,
    expect_boundary_failure: bool = False,
    witness_sink_batches: bool = False,
) -> None:
    _install_short_sink_lease()
    config, graph = _build_pipeline(Path(output_path), rows=rows, snapshot_for_resume=snapshot_for_resume)
    checkpoint_manager = CheckpointManager(db)
    resume_point = RecoveryManager(db, checkpoint_manager).get_resume_point(run_id, graph)
    assert resume_point is not None
    # The witness observes the production sink spans through the public
    # telemetry constructor argument. It is opt-in so the seam matrix keeps
    # resuming with telemetry disabled.
    exporter = TelemetryTestExporter()
    telemetry = TelemetryManager(MockTelemetryConfig(), exporters=[exporter]) if witness_sink_batches else None
    try:
        orchestrator = Orchestrator(
            db,
            checkpoint_manager=checkpoint_manager,
            checkpoint_config=RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row")),
            telemetry_manager=telemetry,
        )
        if expect_boundary_failure:
            with pytest.raises(SinkRequiredFieldsViolation, match="must_exist"):
                orchestrator.resume(resume_point, config, graph, payload_store=FilesystemPayloadStore(Path(payload_path)))
        else:
            result = orchestrator.resume(resume_point, config, graph, payload_store=FilesystemPayloadStore(Path(payload_path)))
            if snapshot_for_resume:
                Path(output_path).with_suffix(".result.json").write_text(
                    json.dumps(
                        {
                            "status": result.status.value,
                            "processed": result.rows_processed,
                            "succeeded": result.rows_succeeded,
                            "failed": result.rows_failed,
                        }
                    )
                )
        if telemetry is not None:
            telemetry.flush()
            _write_sink_batches(Path(output_path), exporter)
    finally:
        if telemetry is not None:
            telemetry.close()


def _write_sink_batches(output_path: Path, exporter: TelemetryTestExporter) -> None:
    """Record each sink invocation's token membership and span status."""
    batches: list[dict[str, object]] = []
    for event in exporter.events:
        if type(event) is not EngineSpanCompleted or event.name is not EngineSpanName.SINK:
            continue
        token_ids = event.attributes["token.ids"]
        assert type(token_ids) is tuple
        batches.append({"token_ids": list(token_ids), "status": event.status.value})
    output_path.with_suffix(_SINK_BATCHES_SUFFIX).write_text(json.dumps(batches))


def _wait_until_run_is_resumable(database_url: str, run_id: str) -> None:
    deadline = time.monotonic() + _PROCESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        with LandscapeDB.from_url(database_url, create_tables=False) as db:
            _status, check = check_run_status_resumable(db, run_id)
        if check.can_resume:
            return
        time.sleep(0.02)
    raise AssertionError(f"run {run_id!r} did not become resumable")


def _effect_operations(image: StateEngineImage) -> tuple[dict[str, object], ...]:
    return tuple(row for row in image.tables["operations"] if row["sink_effect_id"] is not None)


def _assert_attempt_evidence(
    image: StateEngineImage,
    *,
    effect_id: object,
    expected: tuple[tuple[str, str, int], ...],
) -> None:
    effect = next(row for row in image.tables["sink_effects"] if row["effect_id"] == effect_id)
    attempts = tuple(row for row in image.tables["sink_effect_attempts"] if row["effect_id"] == effect_id)
    assert all(0 <= int(attempt["generation"]) <= int(effect["generation"]) for attempt in attempts)
    for attempt in attempts:
        if attempt["state"] == "returned":
            assert attempt["evidence_json"] is not None
            assert attempt["evidence_hash"] is not None
            assert attempt["completed_at"] is not None
            assert attempt["latency_ms"] is not None
        elif attempt["state"] == "response_lost":
            assert attempt["evidence_json"] == '{"classification":"response_lost"}'
            assert attempt["evidence_hash"] is not None
            assert attempt["completed_at"] is not None
            assert float(attempt["latency_ms"]) >= 0.0
        else:
            assert attempt["state"] == "intent"
            assert attempt["evidence_json"] is None
            assert attempt["evidence_hash"] is None
            assert attempt["completed_at"] is None
            assert attempt["latency_ms"] is None
    assert Counter((str(row["action"]), str(row["state"]), int(row["generation"])) for row in attempts) == Counter(expected)


def _observe_single_process_profile(db: LandscapeDB, reporter: RuntimeProfileReporter) -> None:
    raw = db.engine.raw_connection()
    try:
        connection = raw.driver_connection
        assert isinstance(connection, sqlite3.Connection)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        reporter.observe_sqlite(connection, deployment="single-process-leader")
    finally:
        raw.close()


def _exercise_process_death_seam(
    tmp_path: Path,
    seam_value: str,
    *,
    observe_profile: Callable[[LandscapeDB], None] | None = None,
) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database_url = f"sqlite:///{tmp_path / f'{seam_value}.db'}"
    payload_path = tmp_path / "payloads"
    output_path = tmp_path / "output.jsonl"
    run_id = f"run-task9-{seam_value}"

    with LandscapeDB(database_url):
        pass

    with spawn_database_process_with_pause(
        database_url=database_url,
        seam=seam_value,
        action=_run_to_sink_seam,
        action_args=(run_id, str(output_path), str(payload_path), seam_value),
    ) as child:
        ready = child.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        assert ready.pid != os.getpid()
        assert ready.database_dialect == "sqlite"
        child.kill()
        assert child.wait_for_exit(timeout=_PROCESS_TIMEOUT_SECONDS).was_killed

    killed_target = _capture_target_image(output_path)
    with LandscapeDB.from_url(database_url, create_tables=False) as killed_db:
        killed_image = capture_state_engine_image(killed_db, run_id=run_id)
    assert len(killed_image.tables["token_work_items"]) == 1
    assert killed_image.tables["token_work_items"][0]["status"] == "pending_sink"
    assert all(
        event["event_type"] != SchedulerEventType.MARK_PENDING_SINK_TERMINAL.value for event in killed_image.tables["scheduler_events"]
    )
    killed_effect_id: object | None = None
    killed_operation_id: object | None = None
    killed_stage = _TargetImage(exists=False, content_hash=None, size_bytes=None, file_id=None)
    if seam_value == SinkEffectExecutionSeam.BEFORE_RESERVATION.value:
        assert _KILLED_EFFECT_GENERATIONS[seam_value] is None
        assert killed_image.tables["sink_effects"] == ()
        assert _effect_operations(killed_image) == ()
    else:
        assert len(killed_image.tables["sink_effects"]) == 1
        killed_effect = killed_image.tables["sink_effects"][0]
        killed_operations = _effect_operations(killed_image)
        assert len(killed_operations) == 1
        killed_effect_id = killed_effect["effect_id"]
        killed_operation_id = killed_operations[0]["operation_id"]
        assert int(killed_effect["generation"]) == _KILLED_EFFECT_GENERATIONS[seam_value]
        assert killed_operations[0]["sink_effect_id"] == killed_effect_id
        expected_status = (
            "completed"
            if seam_value
            in {
                SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE.value,
                _SCHEDULER_CALLBACK_SEAM,
            }
            else "open"
        )
        assert killed_operations[0]["status"] == expected_status
        expected_effect_state = {
            SinkEffectExecutionSeam.AFTER_RESERVATION.value: "reserved",
            SinkEffectExecutionSeam.AFTER_PREPARATION_CLAIM.value: "reserved",
            SinkEffectExecutionSeam.AFTER_INSPECTION.value: "reserved",
            SinkEffectExecutionSeam.AFTER_PLAN_CAS.value: "prepared",
            SinkEffectExecutionSeam.BEFORE_EFFECT.value: "in_flight",
            _VISIBLE_PUBLICATION_SEAM: "in_flight",
            SinkEffectExecutionSeam.AFTER_EFFECT_BEFORE_RETURN.value: "in_flight",
            SinkEffectExecutionSeam.AFTER_RETURN_BEFORE_FINALIZE.value: "in_flight",
            SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE.value: "finalized",
            _SCHEDULER_CALLBACK_SEAM: "finalized",
        }[seam_value]
        assert killed_effect["state"] == expected_effect_state
        _assert_attempt_evidence(
            killed_image,
            effect_id=killed_effect_id,
            expected=_KILLED_ATTEMPTS[seam_value],
        )
    publication_visible = seam_value in {
        _VISIBLE_PUBLICATION_SEAM,
        SinkEffectExecutionSeam.AFTER_EFFECT_BEFORE_RETURN.value,
        SinkEffectExecutionSeam.AFTER_RETURN_BEFORE_FINALIZE.value,
        SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE.value,
        _SCHEDULER_CALLBACK_SEAM,
    }
    assert killed_target.exists is publication_visible
    if publication_visible:
        assert killed_target.content_hash == sha256(_EXPECTED_BYTES).hexdigest()
        assert killed_target.size_bytes == len(_EXPECTED_BYTES)
    staged_seams = {
        SinkEffectExecutionSeam.AFTER_PLAN_CAS.value,
        SinkEffectExecutionSeam.BEFORE_EFFECT.value,
    }
    effect_stage_paths = tuple(path for path in tmp_path.iterdir() if path.name.endswith(".stage"))
    building_paths = tuple(path for path in tmp_path.iterdir() if path.name.endswith(".building"))
    assert building_paths == ()
    if seam_value in staged_seams:
        assert killed_effect_id is not None
        expected_stage = output_path.with_name(f".{output_path.name}.elspeth-{killed_effect_id}.stage")
        assert effect_stage_paths == (expected_stage,)
        killed_stage = _capture_target_image(expected_stage)
        assert killed_stage.exists
        assert killed_stage.content_hash == sha256(_EXPECTED_BYTES).hexdigest()
        assert killed_stage.size_bytes == len(_EXPECTED_BYTES)
    else:
        assert effect_stage_paths == ()
    if seam_value in {
        SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE.value,
        _SCHEDULER_CALLBACK_SEAM,
    }:
        assert len(killed_image.tables["artifacts"]) == 1
        assert len(killed_image.tables["token_outcomes"]) == 1
    else:
        assert killed_image.tables["artifacts"] == ()
        assert killed_image.tables["token_outcomes"] == ()

    # Every path above has observed the original child's SIGKILL exit. Move
    # its abandoned seat into database time's past, leaving live run budgets
    # at their production values throughout the crash-seam execution.
    with LandscapeDB.from_url(database_url, create_tables=False) as killed_db:
        expire_leader_seat(killed_db, run_id)
    _wait_until_run_is_resumable(database_url, run_id)
    with spawn_database_process_at_seam(
        database_url=database_url,
        seam="resume-completed",
        action=_resume_run,
        action_args=(run_id, str(output_path), str(payload_path)),
    ) as child:
        child.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        child.release()
        assert child.wait_for_exit(timeout=_PROCESS_TIMEOUT_SECONDS).exitcode == 0

    assert output_path.read_bytes() == _EXPECTED_BYTES
    resumed_target = _capture_target_image(output_path)
    assert resumed_target.exists
    if killed_target.exists:
        assert resumed_target == killed_target
    if killed_stage.exists:
        assert resumed_target.file_id == killed_stage.file_id
    with LandscapeDB.from_url(database_url, create_tables=False) as resumed_db:
        if observe_profile is not None:
            observe_profile(resumed_db)
        resumed_image = capture_state_engine_image(resumed_db, run_id=run_id)
    assert len(resumed_image.tables["sink_effects"]) == 1
    resumed_effect = resumed_image.tables["sink_effects"][0]
    assert resumed_effect["state"] == "finalized"
    assert int(resumed_effect["generation"]) == _FINAL_EFFECT_GENERATIONS[seam_value]
    resumed_operations = _effect_operations(resumed_image)
    assert len(resumed_operations) == 1
    assert resumed_operations[0]["status"] == "completed"
    assert resumed_operations[0]["sink_effect_id"] == resumed_effect["effect_id"]
    if killed_effect_id is not None:
        assert resumed_effect["effect_id"] == killed_effect_id
        assert resumed_operations[0]["operation_id"] == killed_operation_id
    _assert_attempt_evidence(
        resumed_image,
        effect_id=resumed_effect["effect_id"],
        expected=_FINAL_ATTEMPTS[seam_value],
    )
    assert len(resumed_image.tables["artifacts"]) == 1
    assert len(resumed_image.tables["token_outcomes"]) == 1
    artifact = resumed_image.tables["artifacts"][0]
    member = resumed_image.tables["sink_effect_members"][0]
    outcome = resumed_image.tables["token_outcomes"][0]
    assert artifact["sink_effect_id"] == resumed_effect["effect_id"]
    assert artifact["path_or_uri"] == output_path.as_uri()
    assert artifact["content_hash"] == resumed_target.content_hash
    assert artifact["size_bytes"] == resumed_target.size_bytes
    assert outcome["token_id"] == member["token_id"]
    assert tuple(path for path in tmp_path.iterdir() if path.name.endswith((".stage", ".building"))) == ()
    assert len(resumed_image.tables["token_work_items"]) == 1
    assert resumed_image.tables["token_work_items"][0]["status"] == "terminal"
    terminal_events = tuple(
        row for row in resumed_image.tables["scheduler_events"] if row["event_type"] == SchedulerEventType.MARK_PENDING_SINK_TERMINAL.value
    )
    assert len(terminal_events) == 1


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL exit-code oracle is POSIX-specific")
@pytest.mark.parametrize(
    ("second_row_valid", "witness_sink_batches"),
    [(False, True), (False, False), (True, True)],
    ids=["boundary-rejected", "boundary-rejected-telemetry-off", "valid-control"],
)
@pytest.mark.timing_sensitive
def test_snapshot_tail_joins_published_member_on_real_kill_resume(
    tmp_path: Path, second_row_valid: bool, witness_sink_batches: bool
) -> None:
    rows = [
        {"id": 1, "value": 10, "must_exist": True},
        {"id": 2, "value": 20, **({"must_exist": True} if second_row_valid else {})},
    ]
    expected_a = b'{"id": 1, "must_exist": true, "value": 10}\n'
    expected_b = b'{"id": 2, "must_exist": true, "value": 20}\n'
    database_url = f"sqlite:///{tmp_path / 'audit.db'}"
    output_path = tmp_path / "output.jsonl"
    payload_path = tmp_path / "payloads"
    run_id = "run-snapshot-tail"
    with LandscapeDB(database_url):
        pass

    with spawn_database_process_with_pause(
        database_url=database_url,
        seam=_VISIBLE_PUBLICATION_SEAM,
        action=_run_to_sink_seam,
        action_args=(run_id, str(output_path), str(payload_path), _VISIBLE_PUBLICATION_SEAM, rows, True),
    ) as child:
        ready = child.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        assert ready.pid != os.getpid()
        child.kill()
        assert child.wait_for_exit(timeout=_PROCESS_TIMEOUT_SECONDS).was_killed

    assert output_path.read_bytes() == expected_a
    killed_target = _capture_target_image(output_path)
    with LandscapeDB.from_url(database_url, create_tables=False) as db:
        killed_image = capture_state_engine_image(db, run_id=run_id)
        expire_leader_seat(db, run_id)
    assert len(killed_image.tables["rows"]) == 1
    assert killed_image.tables["rows"][0]["source_row_index"] == 0
    assert len(killed_image.tables["sink_effects"]) == 1
    killed_effect = killed_image.tables["sink_effects"][0]
    effect_id = killed_effect["effect_id"]
    assert killed_effect["state"] == "in_flight"
    assert len(killed_image.tables["sink_effect_members"]) == 1
    a_token_id = killed_image.tables["sink_effect_members"][0]["token_id"]
    assert type(a_token_id) is str
    assert killed_image.tables["sink_effect_members"][0]["effect_id"] == effect_id
    _assert_attempt_evidence(killed_image, effect_id=effect_id, expected=_KILLED_ATTEMPTS[_VISIBLE_PUBLICATION_SEAM])

    _wait_until_run_is_resumable(database_url, run_id)
    with spawn_database_process_at_seam(
        database_url=database_url,
        seam="resume-completed",
        action=_resume_run,
        action_args=(run_id, str(output_path), str(payload_path), rows, True, not second_row_valid, witness_sink_batches),
    ) as child:
        child.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        child.release()
        assert child.wait_for_exit(timeout=_PROCESS_TIMEOUT_SECONDS).exitcode == 0

    with LandscapeDB.from_url(database_url, create_tables=False) as db:
        first_resume_image = capture_state_engine_image(db, run_id=run_id)
    assert len(first_resume_image.tables["rows"]) == 2
    assert sorted(row["source_row_index"] for row in first_resume_image.tables["rows"]) == [0, 1]
    b_row_id = next(row["row_id"] for row in first_resume_image.tables["rows"] if row["source_row_index"] == 1)
    b_token_id = next(token["token_id"] for token in first_resume_image.tables["tokens"] if token["row_id"] == b_row_id)
    assert type(b_token_id) is str
    if witness_sink_batches:
        # The first resume must hand the re-driven member A and the fresh row B to
        # ONE sink invocation; that invocation is the one the boundary rejects.
        sink_batches = json.loads(output_path.with_suffix(_SINK_BATCHES_SUFFIX).read_text())
        assert [sorted(batch["token_ids"]) for batch in sink_batches] == [sorted([a_token_id, b_token_id])], (
            f"first resume did not write A and B in one sink invocation: A={a_token_id!r}, B={b_token_id!r}, sink batches={sink_batches!r}"
        )
        expected_span_status = EngineSpanStatus.OK if second_row_valid else EngineSpanStatus.ERROR
        assert sink_batches[0]["status"] == expected_span_status.value, (
            f"the A+B sink invocation ended {sink_batches[0]['status']!r}, expected {expected_span_status.value!r}"
        )
    else:
        # The telemetry-off run is the operator default; it keeps every durable
        # assertion below and must leave no witness sidecar behind. It does not
        # witness that A and B shared one sink invocation: only the
        # boundary-rejected sibling proves that.
        assert not output_path.with_suffix(_SINK_BATCHES_SUFFIX).exists()

    if not second_row_valid:
        rejected_image = first_resume_image
        sink_node_id = killed_effect["sink_node_id"]
        assert [
            state["status"]
            for state in rejected_image.tables["node_states"]
            if state["token_id"] == b_token_id and state["node_id"] == sink_node_id
        ] == ["failed"]
        assert [
            outcome["outcome"]
            for outcome in rejected_image.tables["token_outcomes"]
            if outcome["token_id"] == b_token_id and outcome["completed"] == 1
        ] == ["failure"]
        # Ordered so the pre-fix regression (A failed with B's batch) is named
        # before the generic "A has no outcome yet" check can trip on it.
        a_failure_outcomes = [
            (outcome["outcome"], outcome["path"], outcome["completed"])
            for outcome in rejected_image.tables["token_outcomes"]
            if outcome["token_id"] == a_token_id and (outcome["outcome"] == "failure" or outcome["path"] == "unrouted")
        ]
        assert a_failure_outcomes == [], (
            f"B's boundary rejection marked the published member A FAILURE/UNROUTED: (outcome, path, completed)={a_failure_outcomes!r}"
        )
        failure_token_ids = {outcome["token_id"] for outcome in rejected_image.tables["token_outcomes"] if outcome["outcome"] == "failure"}
        assert failure_token_ids == {b_token_id}, (
            f"only B may carry a FAILURE outcome after the boundary rejection: B={b_token_id!r}, failed tokens={failure_token_ids!r}"
        )
        a_completed_outcomes = [
            (outcome["outcome"], outcome["path"])
            for outcome in rejected_image.tables["token_outcomes"]
            if outcome["token_id"] == a_token_id and outcome["completed"] == 1
        ]
        assert a_completed_outcomes == [], f"A must stay undecided until its effect reconciles: (outcome, path)={a_completed_outcomes!r}"
        a_sink_states = [
            state["status"]
            for state in rejected_image.tables["node_states"]
            if state["token_id"] == a_token_id and state["node_id"] == sink_node_id
        ]
        assert a_sink_states == ["open"], f"B's boundary rejection closed the published member A's sink state: {a_sink_states!r}"
        rejected_effect = next(effect for effect in rejected_image.tables["sink_effects"] if effect["effect_id"] == effect_id)
        assert rejected_effect["state"] == "in_flight", (
            f"A's in-flight effect changed state across B's rejection: {rejected_effect['state']!r}"
        )
        failed_operations = [
            operation
            for operation in rejected_image.tables["operations"]
            if operation["operation_type"] == "sink_write" and operation["status"] == "failed"
        ]
        assert len(failed_operations) == 1
        assert failed_operations[0]["sink_effect_id"] is None
        assert "missing ['must_exist']" in str(failed_operations[0]["error_message"])
        assert output_path.read_bytes() == expected_a
        with LandscapeDB.from_url(database_url, create_tables=False) as db:
            for item in rejected_image.tables["token_work_items"]:
                if item["status"] == "leased":
                    expire_lease(db.engine, item["work_item_id"])
        _wait_until_run_is_resumable(database_url, run_id)
        with spawn_database_process_at_seam(
            database_url=database_url,
            seam="resume-completed",
            action=_resume_run,
            action_args=(run_id, str(output_path), str(payload_path), rows, True),
        ) as child:
            child.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
            child.release()
            assert child.wait_for_exit(timeout=_PROCESS_TIMEOUT_SECONDS).exitcode == 0

    assert output_path.read_bytes() == expected_a + (expected_b if second_row_valid else b"")
    if not second_row_valid:
        assert _capture_target_image(output_path) == killed_target
    with LandscapeDB.from_url(database_url, create_tables=False) as db:
        resumed_image = capture_state_engine_image(db, run_id=run_id)
        audit_status, audit_counters = derive_resume_terminal_status_from_audit(
            RecorderFactory(db, payload_store=FilesystemPayloadStore(payload_path)), run_id
        )
    resumed_effect = next(effect for effect in resumed_image.tables["sink_effects"] if effect["effect_id"] == effect_id)
    assert resumed_effect["state"] == "finalized"
    assert len(resumed_image.tables["sink_effects"]) == (2 if second_row_valid else 1)
    assert len([operation for operation in _effect_operations(resumed_image) if operation["sink_effect_id"] == effect_id]) == 1
    _assert_attempt_evidence(resumed_image, effect_id=effect_id, expected=_FINAL_ATTEMPTS[_VISIBLE_PUBLICATION_SEAM])
    outcomes = {outcome["token_id"]: outcome for outcome in resumed_image.tables["token_outcomes"] if outcome["completed"] == 1}
    assert outcomes[a_token_id]["outcome"] == "success"
    assert outcomes[a_token_id]["path"] != "unrouted"
    assert len(outcomes) == 2
    sink_node_id = resumed_effect["sink_node_id"]
    assert [
        state["status"]
        for state in resumed_image.tables["node_states"]
        if state["token_id"] == a_token_id and state["node_id"] == sink_node_id
    ] == ["completed"]
    assert [
        state["status"]
        for state in resumed_image.tables["node_states"]
        if state["token_id"] == b_token_id and state["node_id"] == sink_node_id
    ] == ["completed" if second_row_valid else "failed"]
    assert outcomes[b_token_id]["outcome"] == ("success" if second_row_valid else "failure")
    result = json.loads(output_path.with_suffix(".result.json").read_text())
    assert result == {
        "status": audit_status.value,
        "processed": audit_counters.rows_processed,
        "succeeded": audit_counters.rows_succeeded,
        "failed": audit_counters.rows_failed,
    }
    assert result["processed"] == 2
    assert result["succeeded"] == (2 if second_row_valid else 1)
    assert result["failed"] == (0 if second_row_valid else 1)
    assert resumed_image.tables["runs"][0]["status"] == result["status"]


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL exit-code oracle is POSIX-specific")
@pytest.mark.parametrize("seam_value", _PROCESS_DEATH_SEAMS)
@pytest.mark.timing_sensitive
def test_process_death_reopens_same_sink_effect_and_publishes_once(tmp_path: Path, seam_value: str) -> None:
    _exercise_process_death_seam(tmp_path, seam_value)


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL exit-code oracle is POSIX-specific")
@pytest.mark.timing_sensitive
def test_single_process_profile_covers_complete_sink_effect_process_death_matrix(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    if not request.config.pluginmanager.hasplugin("scripts.state_engine_profile_reporter"):
        pytest.skip("state-engine profile reporter is not loaded")
    reporter = cast("RuntimeProfileReporter", request.getfixturevalue("state_engine_profile"))
    for seam_value in _PROCESS_DEATH_SEAMS:
        _exercise_process_death_seam(
            tmp_path / seam_value,
            seam_value,
            observe_profile=lambda db: _observe_single_process_profile(db, reporter),
        )


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL exit-code oracle is POSIX-specific")
@pytest.mark.parametrize("seam_value", ["before_effect", "remote_commit", "after_finalize_before_response"])
@pytest.mark.timing_sensitive
def test_power_automate_process_death_reconciles_original_delivery(tmp_path: Path, seam_value: str) -> None:
    from tests.integration.pipeline.test_power_automate_effect_recovery import exercise_power_automate_process_death

    exercise_power_automate_process_death(tmp_path, seam_value)
