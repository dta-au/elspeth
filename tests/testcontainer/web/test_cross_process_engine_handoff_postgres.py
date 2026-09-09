"""SIGKILL and ownership-replacement proofs through the PostgreSQL engine."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from tests.e2e.recovery.harness import spawn_database_process_at_seam, spawn_database_process_with_pause
from tests.fixtures.landscape import expire_leader_seat
from tests.testcontainer.web.test_cross_process_run_control_postgres import _prepare
from tests.testcontainer.web.test_cross_process_run_reconciliation_postgres import recovery_databases as recovery_databases
from tests.testcontainer.web.test_global_run_recovery_postgres import _expire_fence, _expire_instance, _register_live_instance

from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
from elspeth.contracts.enums import RunStatus
from elspeth.contracts.run_start import RunStartPermitBinding
from elspeth.contracts.sink_effects import SinkEffectExecutionPurpose, SinkEffectInputKind
from elspeth.core.checkpoint import CheckpointManager, RecoveryManager
from elspeth.core.config import CheckpointSettings, ElspethSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB, RecorderFactory
from elspeth.core.landscape.run_start_admission import RunStartAdmissionRepository, RunStartAdmissionState
from elspeth.core.landscape.schema import (
    checkpoints_table,
    node_states_table,
    rows_table,
    run_sources_table,
    run_start_admissions_table,
    runs_table,
    sink_effects_table,
    token_outcomes_table,
    token_work_items_table,
)
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.executors.sink import SinkExecutor
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.orchestrator.preflight import (
    assemble_and_validate_pipeline_config,
    execution_sink_bindings_for_runtime,
    execution_sinks_for_runtime,
    sink_effect_modes_from_runtime_bindings,
    validate_pipeline_sink_effect_capabilities,
)
from elspeth.plugins.infrastructure.runtime_factory import instantiate_plugins_from_config
from elspeth.web.coordination.contracts import SessionOperationFenceLost, SessionOperationKind
from elspeth.web.coordination.repository import PostgresSessionOperationRepository
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import session_operation_fences_table

pytestmark = [pytest.mark.testcontainer, pytest.mark.timeout(120)]


@pytest.fixture
def engine_handoff_databases(request: pytest.FixtureRequest):
    return request.getfixturevalue("recovery_databases")


def _assemble(base: Path, *, resume: bool):
    purpose = SinkEffectExecutionPurpose.RESUME if resume else SinkEffectExecutionPurpose.FRESH
    settings = ElspethSettings.model_validate(
        {
            "sources": {
                "rows": {
                    "plugin": "json",
                    "on_success": "output",
                    "options": {
                        "path": str(base / "input.jsonl"),
                        "format": "jsonl",
                        "on_validation_failure": "discard",
                        "schema": {"mode": "observed"},
                    },
                }
            },
            "sinks": {
                "output": {
                    "plugin": "json",
                    "on_write_failure": "discard",
                    "options": {"path": str(base / "output.jsonl"), "format": "jsonl", "mode": "write", "schema": {"mode": "observed"}},
                }
            },
        }
    )
    bundle = instantiate_plugins_from_config(settings, preflight_mode=True, sink_effect_purpose=purpose)
    sinks = execution_sinks_for_runtime(settings, bundle.sinks)
    if resume:
        for sink in sinks.values():
            sink.configure_for_resume()
    bindings = execution_sink_bindings_for_runtime(settings, bundle.sink_effect_bindings)
    modes = sink_effect_modes_from_runtime_bindings(
        sinks, bindings, purpose=purpose, configured_options={name: settings.sinks[name].options for name in sinks}
    )
    admission = validate_pipeline_sink_effect_capabilities(
        sinks, configured_modes=modes, required_input_kind=SinkEffectInputKind.PIPELINE_MEMBERS
    )
    graph = ExecutionGraph.from_plugin_instances(
        sources=bundle.sources,
        source_settings_map=bundle.source_settings_map,
        transforms=bundle.transforms,
        sinks=sinks,
        aggregations=bundle.aggregations,
        gates=list(settings.gates),
        queues=settings.queues,
    )
    config = assemble_and_validate_pipeline_config(
        sources=bundle.sources,
        transforms=bundle.transforms,
        sinks=sinks,
        aggregations=bundle.aggregations,
        settings=settings,
        graph=graph,
        sink_effect_modes=modes,
        sink_effect_admission=admission,
    )
    return config, graph, settings


def _orchestrator(db: LandscapeDB) -> Orchestrator:
    return Orchestrator(
        db,
        checkpoint_manager=CheckpointManager(db),
        checkpoint_config=RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row")),
    )


def _run_to_seam(
    db: LandscapeDB, pause: Callable[[], None], base_path: str, binding: RunStartPermitBinding, phase: str, session_url=None, context=None
):
    base = Path(base_path)
    config, graph, settings = _assemble(base, resume=False)
    orchestrator = _orchestrator(db)
    session_engine = create_session_engine(session_url) if session_url is not None else None
    authority = PostgresSessionOperationRepository(session_engine) if session_engine is not None else None

    def guard() -> None:
        if authority is not None:
            authority.compare_and_swap(context)

    register = orchestrator._register_graph_nodes_and_edges
    write = SinkExecutor.write

    def register_then_pause(*args, **kwargs):
        result = register(*args, **kwargs)
        pause()
        return result

    def pause_then_write(executor, *args, **kwargs):
        pause()
        return write(executor, *args, **kwargs)

    seam_patch = (
        patch.object(orchestrator, "_register_graph_nodes_and_edges", side_effect=register_then_pause)
        if phase == "prepared"
        else patch.object(SinkExecutor, "write", pause_then_write)
    )
    try:
        with seam_patch:
            orchestrator.run(
                config,
                graph,
                settings=settings,
                payload_store=FilesystemPayloadStore(base / "payloads"),
                shutdown_event=threading.Event(),
                run_start_permit=binding,
                openrouter_catalog_sha256="0" * 64,
                openrouter_catalog_source="bundled",
                pre_effect_guard=guard,
                check_coordination_latch=guard,
            )
    except SessionOperationFenceLost:
        assert phase == "stale"
    else:
        raise AssertionError("owner unexpectedly completed past its crash/refusal seam")
    finally:
        if session_engine is not None:
            session_engine.dispose()


def _recover(db: LandscapeDB, base_path: str, binding: RunStartPermitBinding) -> None:
    base = Path(base_path)
    admission = RunStartAdmissionRepository(db).observe(binding)
    assert admission is not None
    resume = admission.state is RunStartAdmissionState.EXECUTING
    config, graph, settings = _assemble(base, resume=resume)
    orchestrator = _orchestrator(db)
    store = FilesystemPayloadStore(base / "payloads")
    if resume:
        recovery = RecoveryManager(db, CheckpointManager(db))
        point = recovery.get_resume_point(binding.run_id, graph)
        assert point is not None, recovery.can_resume(binding.run_id, graph)
        result = orchestrator.resume(point, config, graph, settings=settings, payload_store=store, shutdown_event=threading.Event())
    else:
        result = orchestrator.run(
            config,
            graph,
            settings=settings,
            payload_store=store,
            shutdown_event=threading.Event(),
            run_start_permit=binding,
            openrouter_catalog_sha256="0" * 64,
            openrouter_catalog_source="bundled",
        )
    assert result.status is RunStatus.COMPLETED
    assert [json.loads(line)["id"] for line in (base / "output.jsonl").read_text().splitlines()] == [1, 2, 3]


def _initialize(landscape_url: str, base: Path) -> RunStartPermitBinding:
    (base / "input.jsonl").write_text('{"id":1}\n{"id":2}\n{"id":3}\n')
    with LandscapeDB.from_url(landscape_url):
        pass
    return RunStartPermitBinding(str(uuid4()), str(uuid4()), 1, "a" * 64)


def _effect_snapshot(db: LandscapeDB, run_id: str):
    with db.engine.connect() as conn:
        return tuple(
            frozenset(tuple(row) for row in conn.execute(select(table).where(table.c.run_id == run_id)))
            for table in (rows_table, node_states_table, sink_effects_table, token_outcomes_table, token_work_items_table)
        )


@pytest.mark.parametrize("phase", ["prepared", "checkpoint"])
def test_sigkill_owner_recovers_same_run_once_in_new_process(engine_handoff_databases, tmp_path: Path, phase: str) -> None:
    _, landscape_url = engine_handoff_databases
    binding = _initialize(landscape_url, tmp_path)
    with spawn_database_process_with_pause(
        database_url=landscape_url,
        seam=phase,
        action=_run_to_seam,
        action_args=(str(tmp_path), binding, phase),
    ) as owner:
        ready = owner.wait_until_ready(timeout=60)
        assert ready.pid != os.getpid()
        assert ready.database_dialect == "postgresql"
        owner.kill()
        assert owner.wait_for_exit(timeout=10).was_killed
    with LandscapeDB.from_url(landscape_url, create_tables=False) as db:
        admission = RunStartAdmissionRepository(db).observe(binding)
        assert admission is not None
        assert admission.state is (RunStartAdmissionState.PREPARED if phase == "prepared" else RunStartAdmissionState.EXECUTING)
        with db.engine.connect() as conn:
            assert (
                conn.execute(
                    select(func.count()).select_from(checkpoints_table).where(checkpoints_table.c.run_id == binding.run_id)
                ).scalar_one()
                >= 1
            )
            if phase == "checkpoint":
                states = (
                    conn.execute(select(run_sources_table.c.lifecycle_state).where(run_sources_table.c.run_id == binding.run_id))
                    .scalars()
                    .all()
                )
                assert states == ["exhausted"]
        expire_leader_seat(db, binding.run_id)
        with db.write_connection() as conn:
            conn.execute(
                update(token_work_items_table)
                .where(token_work_items_table.c.run_id == binding.run_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
    assert not (tmp_path / "output.jsonl").exists()
    with spawn_database_process_at_seam(
        database_url=landscape_url,
        seam="recovered",
        action=_recover,
        action_args=(str(tmp_path), binding),
    ) as recoverer:
        recoverer.wait_until_ready(timeout=60)
        recoverer.release()
        assert recoverer.wait_for_exit(timeout=10).exitcode == 0
    with LandscapeDB.from_url(landscape_url, create_tables=False) as db, db.engine.connect() as conn:
        assert conn.execute(select(runs_table.c.status).where(runs_table.c.run_id == binding.run_id)).scalar_one() == "completed"
        assert (
            conn.execute(
                select(func.count()).select_from(run_start_admissions_table).where(run_start_admissions_table.c.run_id == binding.run_id)
            ).scalar_one()
            == 1
        )
        outcomes = (
            conn.execute(select(token_outcomes_table.c.token_id).where(token_outcomes_table.c.run_id == binding.run_id)).scalars().all()
        )
        assert len(outcomes) == len(set(outcomes)) == 3


def test_replaced_web_owner_with_live_landscape_seat_cannot_start_later_sink_effect(engine_handoff_databases, tmp_path: Path) -> None:
    session_url, landscape_url = engine_handoff_databases
    binding = _initialize(landscape_url, tmp_path)
    engine, authority, context, _state_id = _prepare(session_url)
    try:
        with spawn_database_process_with_pause(
            database_url=landscape_url,
            seam="before-later-sink-effect",
            action=_run_to_seam,
            action_args=(str(tmp_path), binding, "stale", session_url, context),
        ) as owner:
            owner.wait_until_ready(timeout=60)
            with LandscapeDB.from_url(landscape_url, create_tables=False) as db:
                before = _effect_snapshot(db, binding.run_id)
                leader = RecorderFactory(db).run_coordination.live_leader(run_id=binding.run_id)
                assert leader is not None and leader.seat_live
            _expire_fence(engine, UUID(context.fence.session_id))
            with engine.connect() as conn:
                previous_owner = conn.execute(
                    select(session_operation_fences_table.c.owner_instance_id).where(
                        session_operation_fences_table.c.session_id == context.fence.session_id
                    )
                ).scalar_one()
            _expire_instance(engine, previous_owner)
            replacement = f"replacement-{uuid4()}"
            _register_live_instance(engine, replacement)
            successor = authority.acquire(
                session_id=UUID(context.fence.session_id),
                operation_kind=SessionOperationKind.EXECUTE,
                owner_instance_id=replacement,
                lease_seconds=300,
            )
            owner.release()
            assert owner.wait_for_exit(timeout=30).exitcode == 0
            authority.release(successor)
        with LandscapeDB.from_url(landscape_url, create_tables=False) as db:
            assert _effect_snapshot(db, binding.run_id) == before
            run = RecorderFactory(db).run_lifecycle.get_run(binding.run_id)
            assert run is not None and run.status is RunStatus.RUNNING
        assert not (tmp_path / "output.jsonl").exists()
    finally:
        engine.dispose()
