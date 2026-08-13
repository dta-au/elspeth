"""Task 10 process-death evidence for atomic non-resumable finalization."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from sqlalchemy import select

from elspeth.contracts import FrameworkBugError, NodeType, RunStatus
from elspeth.contracts.checkpoint import CheckpointDraft, ResumePoint
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.enums import TerminalPath
from elspeth.contracts.errors import IncompleteSourceResumeError
from elspeth.contracts.sink_effects import SinkEffectMemberCandidate
from elspeth.core.checkpoint import CheckpointManager
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.execution.sink_effect_identity import resolve_sink_effect_members
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import RunSourceLifecycleState, operations_table, run_sources_table, runs_table
from elspeth.engine.clock import MockClock
from tests.e2e.recovery.harness import _SOURCE_ROWS, _T0, _build_pipeline, _run_to_interrupted_checkpoint, spawn_database_process_at_seam
from tests.fixtures.landscape import register_test_node
from tests.helpers.state_engine import capture_state_engine_image
from tests.unit.core.landscape.test_sink_effect_reservation import _pipeline_request

if TYPE_CHECKING:
    from scripts.state_engine_profile_reporter import RuntimeProfileReporter

_PROCESS_TIMEOUT_SECONDS = 15.0
_LEADER_WORKER_ID = "worker:task10:process-leader"
_TOPOLOGY_HASH = "a" * 64


def _finalize_in_child(
    db: LandscapeDB,
    run_id: str,
    status_value: str,
    worker_id: str,
    leader_epoch: int,
) -> None:
    factory = RecorderFactory(db)
    factory.run_lifecycle.finalize_run(
        run_id,
        RunStatus(status_value),
        token=CoordinationToken(run_id=run_id, worker_id=worker_id, leader_epoch=leader_epoch),
    )


def _build_open_effect_run(
    database_url: str,
    *,
    run_id: str,
    source_state: RunSourceLifecycleState,
    with_checkpoint: bool,
) -> tuple[str, str]:
    with LandscapeDB.from_url(database_url) as db:
        factory = RecorderFactory(db)
        factory.run_lifecycle.begin_run(
            config={},
            canonical_version="v1",
            run_id=run_id,
            leader_worker_id=_LEADER_WORKER_ID,
        )
        source_id = register_test_node(
            factory.data_flow,
            run_id,
            "source",
            node_type=NodeType.SOURCE,
            plugin_name="source",
        )
        sink_id = register_test_node(
            factory.data_flow,
            run_id,
            "sink",
            node_type=NodeType.SINK,
            plugin_name="sink",
        )
        factory.run_lifecycle.record_run_source(
            run_id=run_id,
            source_node_id=source_id,
            source_name="primary",
            plugin_name="source",
            config_hash="c" * 64,
            lifecycle_state=source_state,
        )
        if with_checkpoint:
            CheckpointManager(db).create_checkpoint(
                draft=CheckpointDraft(run_id=run_id, sequence_number=0, upstream_topology_hash=_TOPOLOGY_HASH)
            )
        _row, token = factory.data_flow.create_row_with_token(
            run_id,
            source_id,
            0,
            {"value": 1},
            source_row_index=0,
            ingest_sequence=0,
        )
        factory.execution.begin_node_state(
            token_id=token.token_id,
            node_id=sink_id,
            run_id=run_id,
            step_index=0,
            input_data={"value": 1},
        )
        members = resolve_sink_effect_members(
            factory,
            (SinkEffectMemberCandidate(token_id=token.token_id, row={"value": 1}),),
        )
        effect = factory.execution.sink_effects.reserve(_pipeline_request(run_id, sink_id, members)).new_effect
        assert effect is not None
        operation = next(item for item in factory.execution.get_operations_for_run(run_id) if item.sink_effect_id == effect.effect_id)
        return token.token_id, operation.operation_id


def _observe_single_process_profile(db: LandscapeDB, request: pytest.FixtureRequest) -> None:
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


def test_non_resumable_finalization_survives_abrupt_post_commit_process_death(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'non-resumable.db'}"
    run_id = "run-task10-process-non-resumable"
    token_id, operation_id = _build_open_effect_run(
        database_url,
        run_id=run_id,
        source_state=RunSourceLifecycleState.LOADING,
        with_checkpoint=True,
    )

    with spawn_database_process_at_seam(
        database_url=database_url,
        seam="non-resumable-finalization-committed",
        action=_finalize_in_child,
        action_args=(run_id, RunStatus.FAILED.value, _LEADER_WORKER_ID, 1),
    ) as child:
        ready = child.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        assert ready.pid != os.getpid()
        assert ready.database_dialect == "sqlite"
        child.kill()
        exit_status = child.wait_for_exit(timeout=_PROCESS_TIMEOUT_SECONDS)
        assert exit_status.was_killed

    with LandscapeDB.from_url(database_url, create_tables=False) as fresh_db:
        fresh = RecorderFactory(fresh_db)
        _observe_single_process_profile(fresh_db, request)
        image = capture_state_engine_image(fresh, run_id=run_id)
        run = fresh.run_lifecycle.get_run(run_id)
        operation = fresh.execution.get_operation(operation_id)
        outcome = fresh.data_flow.get_token_outcome(token_id)

        assert run is not None and run.status is RunStatus.FAILED and run.completed_at is not None
        assert operation is not None and operation.status == "failed"
        assert operation.completed_at is not None and operation.duration_ms is not None
        assert operation.error_message == "run finalized as non-resumable before sink effect completed"
        assert outcome is not None and outcome.path is TerminalPath.ABANDONED and outcome.completed is False
        assert len(image.tables["runs"]) == 1
        assert len(image.tables["operations"]) == 1
        assert len(image.tables["token_outcomes"]) == 1


def test_resumable_finalization_preserves_open_effect_after_process_death(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'resumable.db'}"
    run_id = "run-task10-process-resumable"
    token_id, operation_id = _build_open_effect_run(
        database_url,
        run_id=run_id,
        source_state=RunSourceLifecycleState.EXHAUSTED,
        with_checkpoint=True,
    )

    with spawn_database_process_at_seam(
        database_url=database_url,
        seam="resumable-finalization-committed",
        action=_finalize_in_child,
        action_args=(run_id, RunStatus.INTERRUPTED.value, _LEADER_WORKER_ID, 1),
    ) as child:
        child.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        child.kill()
        assert child.wait_for_exit(timeout=_PROCESS_TIMEOUT_SECONDS).was_killed

    with LandscapeDB.from_url(database_url, create_tables=False) as fresh_db:
        fresh = RecorderFactory(fresh_db)
        run = fresh.run_lifecycle.get_run(run_id)
        operation = fresh.execution.get_operation(operation_id)
        outcome = fresh.data_flow.get_token_outcome(token_id)

        assert run is not None and run.status is RunStatus.INTERRUPTED
        assert operation is not None and operation.status == "open"
        assert outcome is None


def test_public_resume_refuses_non_resumable_failed_effect_without_reopening_or_mutation(tmp_path: Path) -> None:
    """The public resume boundary preserves the entire terminal stamp/image."""
    clock = MockClock(start=_T0)
    crashed = _run_to_interrupted_checkpoint(tmp_path, clock)
    clock.advance(600)
    leader_token = crashed.factory.run_coordination.acquire_run_leadership(
        run_id=crashed.run_id,
        worker_id="worker:task10:non-resumable-resume",
        now=clock.now_utc(),
        window_seconds=10**9,
    )
    with crashed.db.engine.begin() as conn:
        conn.execute(
            run_sources_table.update()
            .where(run_sources_table.c.run_id == crashed.run_id)
            .values(lifecycle_state=RunSourceLifecycleState.LOADING.value)
        )

    sink_node_id = str(crashed.graph.get_sinks()[0])
    _row, token = crashed.factory.data_flow.create_row_with_token(
        crashed.run_id,
        crashed.source_node_id,
        50_000,
        {"id": 50_000, "value": 1},
        source_row_index=50_000,
        ingest_sequence=50_000,
    )
    crashed.factory.execution.begin_node_state(
        token_id=token.token_id,
        node_id=sink_node_id,
        run_id=crashed.run_id,
        step_index=0,
        input_data={"id": 50_000, "value": 1},
    )
    members = resolve_sink_effect_members(
        crashed.factory,
        (SinkEffectMemberCandidate(token_id=token.token_id, row={"id": 50_000, "value": 1}),),
    )
    effect = crashed.factory.execution.sink_effects.reserve(_pipeline_request(crashed.run_id, sink_node_id, members)).new_effect
    assert effect is not None
    operation = next(
        item for item in crashed.factory.execution.get_operations_for_run(crashed.run_id) if item.sink_effect_id == effect.effect_id
    )

    crashed.factory.run_lifecycle.finalize_run(crashed.run_id, RunStatus.FAILED, token=leader_token)
    with crashed.db.engine.connect() as conn:
        run_before = dict(conn.execute(select(runs_table).where(runs_table.c.run_id == crashed.run_id)).mappings().one())
        operation_before = dict(
            conn.execute(select(operations_table).where(operations_table.c.operation_id == operation.operation_id)).mappings().one()
        )
    assert operation_before["status"] == "failed"

    latest = crashed.checkpoint_mgr.get_latest_checkpoint(crashed.run_id)
    assert latest is not None
    config, graph, _sink, _source = _build_pipeline(_SOURCE_ROWS)
    with pytest.raises(IncompleteSourceResumeError, match="primary=loading"):
        crashed.resume_orchestrator().resume(
            ResumePoint(checkpoint=latest, sequence_number=latest.sequence_number),
            config,
            graph,
            payload_store=crashed.payload_store,
        )
    with pytest.raises(FrameworkBugError, match="already-completed operation"):
        crashed.factory.execution.complete_operation(operation.operation_id, "completed", duration_ms=1.0)

    with crashed.db.engine.connect() as conn:
        run_after = dict(conn.execute(select(runs_table).where(runs_table.c.run_id == crashed.run_id)).mappings().one())
        operation_after = dict(
            conn.execute(select(operations_table).where(operations_table.c.operation_id == operation.operation_id)).mappings().one()
        )
    assert run_after == run_before
    assert operation_after == operation_before
