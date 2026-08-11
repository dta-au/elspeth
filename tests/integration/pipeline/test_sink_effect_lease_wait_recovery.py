"""Public resume proof for bounded sink-effect lease waiting."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select

from elspeth.contracts import RunStatus, SinkProtocol, SourceProtocol
from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
from elspeth.contracts.sink_effects import SinkEffectState
from elspeth.core.checkpoint import CheckpointManager, RecoveryManager
from elspeth.core.config import CheckpointSettings, SourceSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.execution import sink_effect_finalization, sink_effect_lifecycle
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import runs_table, token_work_items_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.clock import MockClock
from elspeth.engine.executors.sink_effects import (
    SinkEffectCoordinator,
    SinkEffectExecutionSeam,
    SinkEffectInjectedFault,
)
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.plugins.sinks.json_sink import JSONSink
from tests.fixtures.base_classes import as_sink, as_source, inject_write_failure
from tests.fixtures.plugins import ListSource


def _pipeline(output: Path) -> tuple[PipelineConfig, ExecutionGraph]:
    source = ListSource([{"id": 1, "value": "once"}], on_success="output")
    sink_options = {
        "path": str(output),
        "format": "jsonl",
        "mode": "write",
        "schema": {"mode": "observed"},
    }
    sink = inject_write_failure(JSONSink(sink_options), "discard")
    graph = ExecutionGraph.from_plugin_instances(
        sources={"primary": cast(SourceProtocol, source)},
        source_settings_map={
            "primary": SourceSettings(
                plugin=source.name,
                on_success="output",
                options={},
            )
        },
        transforms=[],
        sinks={"output": cast(SinkProtocol, sink)},
        aggregations={},
        gates=[],
    )
    return (
        PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[],
            sinks={"output": as_sink(sink)},
            sink_effect_modes={"output": "write"},
        ),
        graph,
    )


def test_fresh_database_reopen_public_resume_waits_for_effect_lease_and_preserves_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-process injected response loss resumes once after DB reopen."""
    database_path = tmp_path / "audit.db"
    payload_path = tmp_path / "payloads"
    output = tmp_path / "output.jsonl"
    database_url = f"sqlite:///{database_path}"
    lease_ttl = timedelta(seconds=2)
    clock = MockClock(start=datetime.now(UTC).timestamp())
    poll_sleeps: list[float] = []

    def advance_clock(seconds: float) -> None:
        assert seconds > 0.0
        poll_sleeps.append(seconds)
        clock.advance(seconds)

    original_init = SinkEffectCoordinator.__init__

    def initialize_with_deterministic_lease(self: SinkEffectCoordinator, *args: Any, **kwargs: Any) -> None:
        kwargs.update(
            lease_ttl=lease_ttl,
            clock=clock,
            sleep=advance_clock,
            poll_interval=0.25,
        )
        original_init(self, *args, **kwargs)

    lost_response = False
    original_fault = SinkEffectCoordinator._fault

    def lose_first_publication_response(
        self: SinkEffectCoordinator,
        seam: SinkEffectExecutionSeam,
    ) -> None:
        nonlocal lost_response
        if seam is SinkEffectExecutionSeam.AFTER_EFFECT_BEFORE_RETURN and not lost_response:
            lost_response = True
            raise SinkEffectInjectedFault(seam)
        original_fault(self, seam)

    monkeypatch.setattr(sink_effect_lifecycle, "now", clock.now_utc)
    monkeypatch.setattr(sink_effect_finalization, "now", clock.now_utc)
    monkeypatch.setattr(SinkEffectCoordinator, "__init__", initialize_with_deterministic_lease)
    monkeypatch.setattr(SinkEffectCoordinator, "_fault", lose_first_publication_response)

    checkpoint_config = RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row"))
    db = LandscapeDB(database_url)
    checkpoint_manager = CheckpointManager(db)
    config, graph = _pipeline(output)
    try:
        with pytest.raises(SinkEffectInjectedFault) as captured:
            Orchestrator(
                db,
                checkpoint_manager=checkpoint_manager,
                checkpoint_config=checkpoint_config,
            ).run(
                config,
                graph=graph,
                payload_store=FilesystemPayloadStore(payload_path),
            )
        assert captured.value.seam is SinkEffectExecutionSeam.AFTER_EFFECT_BEFORE_RETURN

        with db.engine.connect() as connection:
            run_id = str(connection.scalar(select(runs_table.c.run_id)))
            scheduler_before = tuple(
                connection.scalars(select(token_work_items_table.c.status).where(token_work_items_table.c.run_id == run_id))
            )
        factory = RecorderFactory(db, payload_store=FilesystemPayloadStore(payload_path))
        (effect_before,) = factory.execution.sink_effects.get_effects_for_run(run_id)
        (operation_before,) = tuple(
            operation for operation in factory.execution.get_operations_for_run(run_id) if operation.sink_effect_id is not None
        )
        assert effect_before.state is SinkEffectState.IN_FLIGHT
        assert effect_before.lease_expires_at is not None
        assert effect_before.lease_expires_at.replace(tzinfo=UTC) > clock.now_utc()
        assert operation_before.sink_effect_id == effect_before.effect_id
        assert operation_before.status == "open"
        assert operation_before.completed_at is None
        assert scheduler_before == ("pending_sink",)
        assert checkpoint_manager.get_latest_checkpoint(run_id) is not None
        assert [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()] == [{"id": 1, "value": "once"}]
    finally:
        db.close()

    reopened = LandscapeDB(database_url)
    reopened_checkpoints = CheckpointManager(reopened)
    resumed_config, resumed_graph = _pipeline(output)
    try:
        recovery = RecoveryManager(reopened, reopened_checkpoints)
        check = recovery.can_resume(run_id, resumed_graph)
        assert check.can_resume, check.reason
        resume_point = recovery.get_resume_point(run_id, resumed_graph)
        assert resume_point is not None

        result = Orchestrator(
            reopened,
            checkpoint_manager=reopened_checkpoints,
            checkpoint_config=checkpoint_config,
        ).resume(
            resume_point,
            resumed_config,
            resumed_graph,
            payload_store=FilesystemPayloadStore(payload_path),
        )

        assert result.status is RunStatus.COMPLETED
        factory = RecorderFactory(reopened, payload_store=FilesystemPayloadStore(payload_path))
        (effect_after,) = factory.execution.sink_effects.get_effects_for_run(run_id)
        (operation_after,) = tuple(
            operation for operation in factory.execution.get_operations_for_run(run_id) if operation.sink_effect_id is not None
        )
        assert effect_after.state is SinkEffectState.FINALIZED
        assert effect_after.effect_id == effect_before.effect_id
        assert effect_after.artifact_id == effect_before.artifact_id
        assert effect_after.generation > effect_before.generation
        assert operation_after.operation_id == operation_before.operation_id
        assert operation_after.sink_effect_id == effect_after.effect_id
        assert operation_after.status == "completed"
        assert operation_after.completed_at is not None
        (artifact,) = factory.execution.get_artifacts(run_id)
        assert artifact.artifact_id == effect_before.artifact_id
        assert artifact.sink_effect_id == effect_before.effect_id

        with reopened.engine.connect() as connection:
            scheduler_after = tuple(
                connection.scalars(select(token_work_items_table.c.status).where(token_work_items_table.c.run_id == run_id))
            )
        assert scheduler_after == ("terminal",)
        assert reopened_checkpoints.get_latest_checkpoint(run_id) is None
        assert poll_sleeps
        assert all(0.0 < seconds <= 0.25 for seconds in poll_sleeps)
        assert sum(poll_sleeps) <= lease_ttl.total_seconds() + 0.25
        assert [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()] == [{"id": 1, "value": "once"}]
    finally:
        reopened.close()
