"""Sink-effect process-death evidence for the supported SQLite worker profiles."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from concurrent.futures import Future
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock, create_autospec
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
from elspeth.contracts.scheduler import SchedulerEventType, TokenWorkStatus
from elspeth.core.checkpoint import CheckpointManager
from elspeth.core.checkpoint import manager as checkpoint_manager_module
from elspeth.core.config import load_settings_from_yaml_string
from elspeth.core.landscape import LandscapeDB, run_lifecycle_repository
from elspeth.core.landscape.data_flow import tokens as token_repository_module
from elspeth.core.landscape.scheduler import dispositions as scheduler_dispositions_module
from elspeth.core.landscape.scheduler import fencing as scheduler_fencing_module
from elspeth.core.landscape.scheduler import queue as scheduler_queue_module
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import (
    run_workers_table,
    scheduler_events_table,
    token_work_items_table,
)
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.executors.sink_effects import SinkEffectCoordinator, SinkEffectExecutionSeam
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.processor import RowProcessor
from elspeth.plugins.sinks import _local_file_effects
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution.progress import ProgressBroadcaster
from elspeth.web.execution.schemas import ValidationReadiness, ValidationResult
from elspeth.web.execution.service import ExecutionServiceImpl
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, WebPluginPolicy
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry
from elspeth.web.sessions.protocol import CompositionStateRecord, SessionServiceProtocol
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.e2e.recovery.harness import spawn_database_process_at_seam, spawn_database_process_with_pause
from tests.e2e.recovery.test_sink_effect_process_death_matrix import (
    _EXPECTED_BYTES,
    _FINAL_ATTEMPTS,
    _FINAL_EFFECT_GENERATIONS,
    _KILLED_ATTEMPTS,
    _KILLED_EFFECT_GENERATIONS,
    _PROCESS_DEATH_SEAMS,
    _PROCESS_TIMEOUT_SECONDS,
    _SCHEDULER_CALLBACK_SEAM,
    _VISIBLE_PUBLICATION_SEAM,
    _capture_target_image,
    _install_short_sink_lease,
    _wait_until_run_is_resumable,
)
from tests.helpers.state_engine import StateEngineImage, capture_state_engine_image

_PROFILE_RUN_LIVENESS_SECONDS = 5.0
_PROFILE_SCHEDULER_LEASE_SECONDS = 2

if TYPE_CHECKING:
    from scripts.state_engine_profile_reporter import RuntimeProfileReporter


def _profile_settings_text(tmp_path: Path) -> str:
    input_path = tmp_path / "input.jsonl"
    input_path.write_text(json.dumps({"id": 1, "value": 10}) + "\n", encoding="utf-8")
    return f"""
sources:
  rows:
    plugin: json
    on_success: processing
    options:
      path: {json.dumps(str(input_path))}
      format: jsonl
      on_validation_failure: discard
      schema:
        mode: observed
gates:
  - name: fork_row
    input: processing
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [branch_a, branch_b]
sinks:
  branch_a:
    plugin: json
    on_write_failure: discard
    options:
      path: {json.dumps(str(tmp_path / "output-a.jsonl"))}
      format: jsonl
      schema:
        mode: observed
  branch_b:
    plugin: json
    on_write_failure: discard
    options:
      path: {json.dumps(str(tmp_path / "output-b.jsonl"))}
      format: jsonl
      schema:
        mode: observed
landscape:
  url: sqlite:///{tmp_path / "audit.db"}
payload_store:
  backend: filesystem
  base_path: {tmp_path / "payloads"}
checkpoint:
  enabled: true
  frequency: every_row
"""


def _install_profile_leader_hooks(db: LandscapeDB, pause: Any, seam_value: str) -> None:
    real_claim_ready = TokenSchedulerRepository.claim_ready

    def wait_for_follower_handoff(
        self: TokenSchedulerRepository,
        *,
        run_id: str,
        lease_owner: str,
        lease_seconds: int,
        now: datetime,
    ) -> Any:
        with db.engine.connect() as conn:
            role = conn.execute(
                select(run_workers_table.c.role).where(
                    run_workers_table.c.run_id == run_id,
                    run_workers_table.c.worker_id == lease_owner,
                )
            ).scalar_one_or_none()
            ready_exists = (
                conn.execute(
                    select(token_work_items_table.c.work_item_id).where(
                        token_work_items_table.c.run_id == run_id,
                        token_work_items_table.c.status == TokenWorkStatus.READY.value,
                    )
                ).first()
                is not None
            )
        if role == "leader" and ready_exists:
            deadline = time.monotonic() + _PROCESS_TIMEOUT_SECONDS
            while True:
                with db.engine.connect() as conn:
                    handoff_exists = (
                        conn.execute(
                            select(scheduler_events_table.c.event_id).where(
                                scheduler_events_table.c.run_id == run_id,
                                scheduler_events_table.c.event_type == SchedulerEventType.MARK_PENDING_SINK.value,
                                scheduler_events_table.c.caller_owner != lease_owner,
                            )
                        ).first()
                        is not None
                    )
                if handoff_exists:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("claim-only follower did not produce a pending-sink handoff")
                time.sleep(0.01)
        return real_claim_ready(
            self,
            run_id=run_id,
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
            now=now,
        )

    TokenSchedulerRepository.claim_ready = wait_for_follower_handoff  # type: ignore[method-assign]
    if seam_value == _VISIBLE_PUBLICATION_SEAM:
        _local_file_effects._after_replace = lambda _target: pause()
    elif seam_value == _SCHEDULER_CALLBACK_SEAM:
        real_terminalize = RowProcessor.mark_sink_bound_scheduler_terminal_many

        def pause_before_scheduler_callback(self: RowProcessor, token_ids: tuple[str, ...]) -> None:
            pause()
            real_terminalize(self, token_ids)

        RowProcessor.mark_sink_bound_scheduler_terminal_many = pause_before_scheduler_callback  # type: ignore[method-assign]
    else:
        target_seam = SinkEffectExecutionSeam(seam_value)
        real_fault = SinkEffectCoordinator._fault

        def pause_at_target(self: SinkEffectCoordinator, seam: SinkEffectExecutionSeam) -> None:
            if seam is target_seam:
                pause()
            real_fault(self, seam)

        SinkEffectCoordinator._fault = pause_at_target  # type: ignore[method-assign]


def _install_profile_run_liveness() -> None:
    run_lifecycle_repository.DEFAULT_RUN_LIVENESS_WINDOW_SECONDS = _PROFILE_RUN_LIVENESS_SECONDS  # type: ignore[attr-defined]
    checkpoint_manager_module.DEFAULT_RUN_LIVENESS_WINDOW_SECONDS = _PROFILE_RUN_LIVENESS_SECONDS  # type: ignore[attr-defined]
    token_repository_module.DEFAULT_RUN_LIVENESS_WINDOW_SECONDS = _PROFILE_RUN_LIVENESS_SECONDS  # type: ignore[attr-defined]
    scheduler_dispositions_module.DEFAULT_RUN_LIVENESS_WINDOW_SECONDS = _PROFILE_RUN_LIVENESS_SECONDS  # type: ignore[attr-defined]
    scheduler_fencing_module.DEFAULT_RUN_LIVENESS_WINDOW_SECONDS = _PROFILE_RUN_LIVENESS_SECONDS  # type: ignore[attr-defined]
    scheduler_queue_module.DEFAULT_RUN_LIVENESS_WINDOW_SECONDS = _PROFILE_RUN_LIVENESS_SECONDS  # type: ignore[attr-defined]


def _install_short_scheduler_lease() -> None:
    from elspeth.engine.orchestrator import processor_factory as processor_factory_module

    real_build_row_processor = processor_factory_module.build_row_processor

    def short_build_row_processor(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("scheduler_lease_seconds", _PROFILE_SCHEDULER_LEASE_SECONDS)
        kwargs.setdefault("scheduler_heartbeat_seconds", 1)
        return real_build_row_processor(*args, **kwargs)

    processor_factory_module.build_row_processor = short_build_row_processor


def _run_same_host_leader_to_sink_seam(
    db: LandscapeDB,
    pause: Any,
    run_id: str,
    settings_path: str,
    seam_value: str,
) -> None:
    from elspeth.cli import _instantiate_plugins_for_runtime_preflight, _preflight_execution_sinks
    from elspeth.core.dag import ExecutionGraph
    from elspeth.engine.orchestrator.preflight import assemble_and_validate_pipeline_config

    _install_short_sink_lease()
    _install_profile_run_liveness()
    _install_short_scheduler_lease()
    _install_profile_leader_hooks(db, pause, seam_value)
    settings = load_settings_from_yaml_string(Path(settings_path).read_text(encoding="utf-8"))
    plugins = _instantiate_plugins_for_runtime_preflight(settings)
    execution_sinks, execution_modes, admission = _preflight_execution_sinks(settings, plugins)
    graph = ExecutionGraph.from_plugin_instances(
        sources=plugins.sources,
        source_settings_map=plugins.source_settings_map,
        transforms=plugins.transforms,
        sinks=execution_sinks,
        aggregations=plugins.aggregations,
        gates=list(settings.gates),
        queues=settings.queues,
    )
    graph.validate()
    config = assemble_and_validate_pipeline_config(
        sources=plugins.sources,
        transforms=plugins.transforms,
        sinks=execution_sinks,
        aggregations=plugins.aggregations,
        settings=settings,
        graph=graph,
        sink_effect_modes=execution_modes,
        sink_effect_admission=admission,
    )
    checkpoint_config = RuntimeCheckpointConfig.from_settings(settings.checkpoint)
    Orchestrator(
        db,
        checkpoint_manager=CheckpointManager(db),
        checkpoint_config=checkpoint_config,
    ).run(
        config,
        graph=graph,
        settings=settings,
        payload_store=FilesystemPayloadStore(settings.payload_store.base_path),
        run_id=run_id,
        openrouter_catalog_sha256="0" * 64,
        openrouter_catalog_source="bundled",
    )


def _web_composition_state(*, session_id: UUID, state_id: UUID) -> CompositionStateRecord:
    return CompositionStateRecord(
        id=state_id,
        session_id=session_id,
        version=1,
        sources={
            "rows": {
                "plugin": "json",
                "on_success": "processing",
                "on_validation_failure": "discard",
                "options": {"schema": {"mode": "observed"}},
            }
        },
        source=None,
        nodes=[
            {
                "id": "fork_row",
                "node_type": "gate",
                "plugin": None,
                "input": "processing",
                "on_success": None,
                "on_error": None,
                "options": {},
                "condition": "True",
                "routes": {"true": "fork", "false": "discard"},
                "fork_to": ["branch_a", "branch_b"],
            }
        ],
        edges=[],
        outputs=[
            {
                "name": "branch_a",
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {"format": "jsonl", "schema": {"mode": "observed"}},
            },
            {
                "name": "branch_b",
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {"format": "jsonl", "schema": {"mode": "observed"}},
            },
        ],
        metadata_={"name": "Task 9 web sink recovery profile", "description": ""},
        is_valid=True,
        validation_errors=None,
        created_at=datetime.now(UTC),
        derived_from_state_id=None,
        composer_meta=None,
    )


async def _execute_web_leader(run_id: str, settings_path: str) -> None:
    settings = load_settings_from_yaml_string(Path(settings_path).read_text(encoding="utf-8"))
    session_id = uuid4()
    state_record = _web_composition_state(session_id=session_id, state_id=uuid4())
    run_uuid = UUID(run_id)
    session_service = create_autospec(SessionServiceProtocol, instance=True)
    session_service.get_active_run.return_value = None
    session_service.get_current_state.return_value = state_record
    session_service.create_run.return_value = SimpleNamespace(id=run_uuid)
    session_service.get_run.return_value = SimpleNamespace(status="running", session_id=session_id)
    session_service.update_run_status.return_value = None
    event_sequence = 0

    async def append_run_event(**_kwargs: Any) -> SimpleNamespace:
        nonlocal event_sequence
        event_sequence += 1
        return SimpleNamespace(sequence=event_sequence)

    session_service.append_run_event.side_effect = append_run_event
    tmp_path = Path(settings_path).parent
    web_settings = SimpleNamespace(
        deployment_target="default",
        deployment_state_mode="sqlite-single",
        landscape_url=settings.landscape.url,
        landscape_passphrase=None,
        payload_store_path=settings.payload_store.base_path,
        data_dir=tmp_path,
        get_landscape_url=lambda: settings.landscape.url,
        get_payload_store_path=lambda: settings.payload_store.base_path,
        get_session_db_url=lambda: f"sqlite:///{tmp_path / 'sessions.db'}",
    )
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    web_policy = WebPluginPolicy(
        schema_version=1,
        required=snapshot.available,
        configured_optional=frozenset(),
        authorized=snapshot.available,
        preferences=(),
        control_modes=snapshot.control_modes,
        plugin_code_identities=(),
        policy_hash=snapshot.policy_hash,
    )
    loop = asyncio.get_running_loop()
    service = ExecutionServiceImpl(
        loop=loop,
        broadcaster=ProgressBroadcaster(loop),
        settings=cast(Any, web_settings),
        session_service=session_service,
        yaml_generator=SimpleNamespace(generate_yaml=lambda _state: Path(settings_path).read_text(encoding="utf-8")),
        telemetry=build_sessions_telemetry(),
        blob_service=None,
        secret_service=None,
        plugin_snapshot_factory=lambda _user_id: snapshot,
        operator_profile_registry=MagicMock(spec=OperatorProfileRegistry),
        web_plugin_policy=web_policy,
        catalog=catalog,
    )
    service.set_openrouter_catalog_snapshot(sha256="0" * 64, source="bundled")
    valid_preflight = ValidationResult(
        is_valid=True,
        checks=[],
        errors=[],
        readiness=ValidationReadiness(
            authoring_valid=True,
            execution_ready=True,
            completion_ready=True,
            blockers=[],
        ),
    )

    async def accept_state_preflight(*_args: Any, **_kwargs: Any) -> ValidationResult:
        return valid_preflight

    service._authoritative_state_preflight = accept_state_preflight  # type: ignore[method-assign]
    submitted: list[Future[Any]] = []
    real_submit = service._executor.submit

    def capture_submit(*args: Any, **kwargs: Any) -> Future[Any]:
        future = real_submit(*args, **kwargs)
        submitted.append(future)
        return future

    service._executor.submit = capture_submit  # type: ignore[method-assign]
    try:
        launched = await service.execute(
            session_id,
            user_id="task9-web-user",
            auth_provider_type="local",
        )
        assert launched == run_uuid
        assert len(submitted) == 1
        await asyncio.wrap_future(submitted[0])
    finally:
        await service.shutdown()


def _run_web_leader_to_sink_seam(
    db: LandscapeDB,
    pause: Any,
    run_id: str,
    settings_path: str,
    seam_value: str,
) -> None:
    _install_short_sink_lease()
    _install_profile_run_liveness()
    _install_short_scheduler_lease()
    _install_profile_leader_hooks(db, pause, seam_value)
    asyncio.run(_execute_web_leader(run_id, settings_path))


def _resume_profile_via_cli(
    _db: LandscapeDB,
    run_id: str,
    settings_path: str,
    database_path: str,
) -> None:
    from elspeth.cli import app

    _install_short_sink_lease()
    _install_profile_run_liveness()
    _install_short_scheduler_lease()
    result = CliRunner().invoke(
        app,
        [
            "resume",
            run_id,
            "--settings",
            settings_path,
            "--database",
            database_path,
            "--execute",
            "--format",
            "json",
        ],
    )
    if result.exit_code != 0:
        raise AssertionError(f"CLI resume failed with exit {result.exit_code}: {result.output}") from result.exception


def _wait_for_fork_ready(database_url: str, run_id: str, child_alive: Any) -> None:
    deadline = time.monotonic() + _PROCESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        assert child_alive(), "leader exited before producing follower-claimable work"
        try:
            with LandscapeDB.from_url(database_url, create_tables=False) as db, db.engine.connect() as conn:
                ready = conn.execute(
                    select(token_work_items_table.c.work_item_id).where(
                        token_work_items_table.c.run_id == run_id,
                        token_work_items_table.c.status == TokenWorkStatus.READY.value,
                    )
                ).first()
                leader = conn.execute(
                    select(run_workers_table.c.worker_id).where(
                        run_workers_table.c.run_id == run_id,
                        run_workers_table.c.role == "leader",
                        run_workers_table.c.status == "active",
                    )
                ).first()
            if ready is not None and leader is not None:
                return
        except OperationalError:
            pass
        time.sleep(0.01)
    raise AssertionError("leader did not produce follower-claimable work")


def _run_cli_follower_until_seat_dead(
    _db: LandscapeDB,
    run_id: str,
    settings_path: str,
    database_path: str,
) -> None:
    from elspeth.cli import app

    result = CliRunner().invoke(
        app,
        [
            "join",
            run_id,
            "--settings",
            settings_path,
            "--database",
            database_path,
            "--format",
            "json",
        ],
    )
    if result.exit_code != 2:
        raise AssertionError(f"CLI follower exited {result.exit_code}, expected seat-dead exit 2: {result.output}") from result.exception
    if '"event": "seat_dead"' not in result.output or "Use `elspeth resume" not in result.output:
        raise AssertionError(f"CLI follower omitted its seat-dead recovery evidence: {result.output}")


def _attempts_for(image: StateEngineImage, effect_id: object) -> tuple[tuple[str, str, int], ...]:
    attempts: list[tuple[str, str, int]] = []
    for row in image.tables["sink_effect_attempts"]:
        if row["effect_id"] != effect_id:
            continue
        generation = row["generation"]
        assert type(generation) is int
        attempts.append((str(row["action"]), str(row["state"]), generation))
    return tuple(sorted(attempts))


def _expected_killed_effect_state(seam_value: str) -> str:
    return {
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


def _observe_profile(db: LandscapeDB, request: pytest.FixtureRequest, *, deployment: str) -> None:
    if not request.config.pluginmanager.hasplugin("scripts.state_engine_profile_reporter"):
        return
    reporter = cast("RuntimeProfileReporter", request.getfixturevalue("state_engine_profile"))
    raw = db.engine.raw_connection()
    try:
        connection = raw.driver_connection
        assert isinstance(connection, sqlite3.Connection)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        reporter.observe_sqlite(connection, deployment=deployment)
    finally:
        raw.close()


def _exercise_worker_profile(
    tmp_path: Path,
    seam_value: str,
    *,
    leader_action: Any,
    web_attributed: bool,
) -> LandscapeDB:
    tmp_path.mkdir(parents=True, exist_ok=True)
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(_profile_settings_text(tmp_path), encoding="utf-8")
    database_path = tmp_path / "audit.db"
    database_url = f"sqlite:///{database_path}"
    run_id = str(uuid4())
    with LandscapeDB(database_url):
        pass

    follower_child = None
    try:
        with spawn_database_process_with_pause(
            database_url=database_url,
            seam=seam_value,
            action=leader_action,
            action_args=(run_id, str(settings_path), seam_value),
        ) as child:
            _wait_for_fork_ready(database_url, run_id, lambda: child.is_alive)
            follower_child = spawn_database_process_at_seam(
                database_url=database_url,
                seam="follower-seat-dead",
                action=_run_cli_follower_until_seat_dead,
                action_args=(run_id, str(settings_path), str(database_path)),
            )
            ready = child.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
            assert ready.pid != os.getpid()
            child.kill()
            assert child.wait_for_exit(timeout=_PROCESS_TIMEOUT_SECONDS).was_killed

        assert follower_child is not None
        follower_child.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        follower_child.release()
        assert follower_child.wait_for_exit(timeout=_PROCESS_TIMEOUT_SECONDS).exitcode == 0
        follower_child.close()
        follower_child = None

        output_paths = (tmp_path / "output-a.jsonl", tmp_path / "output-b.jsonl")
        killed_targets = {path: _capture_target_image(path) for path in output_paths}
        visible_at_kill = tuple(path for path, image in killed_targets.items() if image.exists)
        publication_visible = seam_value in {
            _VISIBLE_PUBLICATION_SEAM,
            SinkEffectExecutionSeam.AFTER_EFFECT_BEFORE_RETURN.value,
            SinkEffectExecutionSeam.AFTER_RETURN_BEFORE_FINALIZE.value,
            SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE.value,
            _SCHEDULER_CALLBACK_SEAM,
        }
        assert len(visible_at_kill) == int(publication_visible)
        if publication_visible:
            assert visible_at_kill[0].read_bytes() == _EXPECTED_BYTES
        with LandscapeDB.from_url(database_url, create_tables=False) as killed_db:
            killed_image = capture_state_engine_image(killed_db, run_id=run_id)
        killed_effect: Any | None
        if seam_value == SinkEffectExecutionSeam.BEFORE_RESERVATION.value:
            assert killed_image.tables["sink_effects"] == ()
            killed_effect = None
        else:
            assert len(killed_image.tables["sink_effects"]) == 1
            killed_effect = killed_image.tables["sink_effects"][0]
            killed_generation = killed_effect["generation"]
            assert type(killed_generation) is int
            assert killed_generation == _KILLED_EFFECT_GENERATIONS[seam_value]
            assert killed_effect["state"] == _expected_killed_effect_state(seam_value)
            assert _attempts_for(killed_image, killed_effect["effect_id"]) == tuple(sorted(_KILLED_ATTEMPTS[seam_value]))
        effect_operations = tuple(row for row in killed_image.tables["operations"] if row["sink_effect_id"] is not None)
        assert len(effect_operations) == int(killed_effect is not None)
        if killed_effect is not None:
            expected_operation_status = (
                "completed"
                if seam_value
                in {
                    SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE.value,
                    _SCHEDULER_CALLBACK_SEAM,
                }
                else "open"
            )
            assert effect_operations[0]["status"] == expected_operation_status
        expected_killed_artifacts = int(
            seam_value
            in {
                SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE.value,
                _SCHEDULER_CALLBACK_SEAM,
            }
        )
        assert len(killed_image.tables["artifacts"]) == expected_killed_artifacts

        _wait_until_run_is_resumable(database_url, run_id)
        with spawn_database_process_at_seam(
            database_url=database_url,
            seam="profile-resume-completed",
            action=_resume_profile_via_cli,
            action_args=(run_id, str(settings_path), str(database_path)),
        ) as child:
            child.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
            child.release()
            assert child.wait_for_exit(timeout=_PROCESS_TIMEOUT_SECONDS).exitcode == 0

        assert all(path.read_bytes() == _EXPECTED_BYTES for path in output_paths)
        resumed_targets = {path: _capture_target_image(path) for path in output_paths}
        for path in visible_at_kill:
            assert resumed_targets[path] == killed_targets[path]

        resumed_db = LandscapeDB.from_url(database_url, create_tables=False)
        resumed_image = capture_state_engine_image(resumed_db, run_id=run_id)
        assert len(resumed_image.tables["sink_effects"]) == 2
        assert all(row["state"] == "finalized" for row in resumed_image.tables["sink_effects"])
        if killed_effect is not None:
            resumed_effect = next(row for row in resumed_image.tables["sink_effects"] if row["effect_id"] == killed_effect["effect_id"])
            resumed_generation = resumed_effect["generation"]
            assert type(resumed_generation) is int
            assert resumed_generation == _FINAL_EFFECT_GENERATIONS[seam_value]
            assert _attempts_for(resumed_image, resumed_effect["effect_id"]) == tuple(sorted(_FINAL_ATTEMPTS[seam_value]))
        assert len(resumed_image.tables["artifacts"]) == 2
        final_effect_operations = tuple(row for row in resumed_image.tables["operations"] if row["sink_effect_id"] is not None)
        assert len(final_effect_operations) == 2
        assert all(row["status"] == "completed" for row in final_effect_operations)
        assert len(resumed_image.tables["token_outcomes"]) == 3
        terminal_events = tuple(
            row
            for row in resumed_image.tables["scheduler_events"]
            if row["event_type"] == SchedulerEventType.MARK_PENDING_SINK_TERMINAL.value
        )
        assert len(terminal_events) == 2
        assert len({row["caller_owner"] for row in terminal_events}) == 1
        assert any(row["role"] == "follower" for row in resumed_image.tables["run_workers"])
        if web_attributed:
            assert len(resumed_image.tables["run_attributions"]) == 1
            attribution = resumed_image.tables["run_attributions"][0]
            assert attribution["initiated_by_user_id"] == "task9-web-user"
            assert attribution["auth_provider_type"] == "local"
        else:
            assert resumed_image.tables["run_attributions"] == ()
        return resumed_db
    finally:
        if follower_child is not None:
            follower_child.close()


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL exit-code oracle is POSIX-specific")
def test_same_host_leader_plus_claim_only_followers_profile_covers_sink_effect_process_death_matrix(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    for seam_value in _PROCESS_DEATH_SEAMS:
        db = _exercise_worker_profile(
            tmp_path / seam_value,
            seam_value,
            leader_action=_run_same_host_leader_to_sink_seam,
            web_attributed=False,
        )
        try:
            _observe_profile(
                db,
                request,
                deployment="same-host-leader-plus-claim-only-followers",
            )
        finally:
            db.close()


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL exit-code oracle is POSIX-specific")
def test_web_hosted_leader_plus_same_host_cli_followers_profile_covers_sink_effect_process_death_matrix(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    for seam_value in _PROCESS_DEATH_SEAMS:
        db = _exercise_worker_profile(
            tmp_path / seam_value,
            seam_value,
            leader_action=_run_web_leader_to_sink_seam,
            web_attributed=True,
        )
        try:
            _observe_profile(
                db,
                request,
                deployment="web-hosted-leader-plus-same-host-cli-followers",
            )
        finally:
            db.close()
