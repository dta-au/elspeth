"""Actual CLI-hosted follower composition and runtime-context evidence."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock, create_autospec
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from typer.testing import CliRunner

from elspeth.contracts import FrameworkBugError, RunStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.scheduler import SchedulerEventType, TokenWorkStatus
from elspeth.core.config import load_settings_from_yaml_string
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import (
    checkpoints_table,
    node_states_table,
    run_attributions_table,
    run_coordination_table,
    run_sources_table,
    run_workers_table,
    runs_table,
    scheduler_events_table,
    token_outcomes_table,
    token_work_items_table,
)
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator
from elspeth.plugins.sinks.json_sink import JSONSink
from elspeth.plugins.transforms.passthrough import PassThrough
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution.progress import ProgressBroadcaster
from elspeth.web.execution.schemas import ValidationReadiness, ValidationResult
from elspeth.web.execution.service import ExecutionServiceImpl
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, WebPluginPolicy
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry
from elspeth.web.sessions.protocol import CompositionStateRecord, SessionServiceProtocol
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.e2e.recovery.test_follower_join_and_drain import (
    _GUARD_LIVE_SEAT_WINDOW_SECONDS,
    _build_runtime_graph,
    _real_follower_settings_text,
    _seed_real_follower_ready_item,
    _work_item,
)

if TYPE_CHECKING:
    from scripts.state_engine_profile_reporter import RuntimeProfileReporter


async def _wait_for(predicate: Any, *, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, message
        await asyncio.sleep(0.01)


@pytest.mark.timeout(120)
@pytest.mark.asyncio
async def test_web_execution_service_leader_and_actual_cli_follower_share_full_runtime_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """A web service leader and real CLI follower compose one fenced run."""
    from elspeth.cli import app

    processing_yaml = """
gates:
  - name: fork_row
    input: processing
    condition: "True"
    routes: {"true": fork, "false": discard}
    fork_to: [branch_a, branch_b]
"""
    input_path = tmp_path / "real-follower-input.jsonl"
    input_path.write_text(json.dumps({"id": 1, "value": 10}) + "\n", encoding="utf-8")
    output_a_path = tmp_path / "real-follower-output-a.jsonl"
    output_b_path = tmp_path / "real-follower-output-b.jsonl"
    settings_text = f"""
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
{processing_yaml}
sinks:
  branch_a:
    plugin: json
    on_write_failure: discard
    options:
      path: {json.dumps(str(output_a_path))}
      format: jsonl
      schema:
        mode: observed
  branch_b:
    plugin: json
    on_write_failure: discard
    options:
      path: {json.dumps(str(output_b_path))}
      format: jsonl
      schema:
        mode: observed
landscape:
  url: sqlite:///{tmp_path / "real-follower-audit.db"}
payload_store:
  backend: filesystem
  base_path: {tmp_path / "real-follower-payloads"}
"""
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(settings_text, encoding="utf-8")
    settings = load_settings_from_yaml_string(settings_text)

    session_id = uuid4()
    state_id = uuid4()
    run_uuid = uuid4()
    state_record = CompositionStateRecord(
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
        metadata_={"name": "Task 10 web leader profile", "description": ""},
        is_valid=True,
        validation_errors=None,
        created_at=datetime.now(UTC),
        derived_from_state_id=None,
        composer_meta=None,
    )

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
        yaml_generator=SimpleNamespace(generate_yaml=lambda _state: settings_text),
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

    monkeypatch.setattr(service, "_authoritative_state_preflight", accept_state_preflight)

    leader_blocked = threading.Event()
    leader_release = threading.Event()
    lifecycle: dict[int, list[str]] = {}
    contexts: dict[int, PluginContext] = {}
    leader_thread_id: int | None = None
    commit_thread_ids: list[int] = []
    real_start = JSONSink.on_start
    real_complete = JSONSink.on_complete
    real_close = JSONSink.close
    real_commit = JSONSink.commit_effect

    def observed_start(self: JSONSink, ctx: Any) -> None:
        lifecycle[id(self)] = ["start"]
        contexts[id(self)] = ctx
        real_start(self, ctx)

    def observed_complete(self: JSONSink, ctx: Any) -> None:
        lifecycle[id(self)].append("complete")
        real_complete(self, ctx)

    def observed_close(self: JSONSink) -> None:
        if id(self) in lifecycle:
            lifecycle[id(self)].append("close")
        real_close(self)

    def observed_commit(self: JSONSink, plan: Any, ctx: Any) -> Any:
        commit_thread_ids.append(threading.get_ident())
        return real_commit(self, plan, ctx)

    monkeypatch.setattr(JSONSink, "on_start", observed_start)
    monkeypatch.setattr(JSONSink, "on_complete", observed_complete)
    monkeypatch.setattr(JSONSink, "close", observed_close)
    monkeypatch.setattr(JSONSink, "commit_effect", observed_commit)

    real_claim_ready = TokenSchedulerRepository.claim_ready

    def pause_leader_before_second_claim(
        self: TokenSchedulerRepository,
        *,
        run_id: str,
        lease_owner: str,
        lease_seconds: int,
        now: datetime,
    ) -> Any:
        nonlocal leader_thread_id
        with self._engine.connect() as conn:
            role = conn.execute(
                select(run_workers_table.c.role).where(
                    run_workers_table.c.run_id == run_id,
                    run_workers_table.c.worker_id == lease_owner,
                )
            ).scalar_one_or_none()
            fork_branch_ready = (
                conn.execute(
                    select(token_work_items_table.c.work_item_id).where(
                        token_work_items_table.c.run_id == run_id,
                        token_work_items_table.c.status == TokenWorkStatus.READY.value,
                    )
                ).first()
                is not None
            )
        if role == "leader" and fork_branch_ready:
            leader_thread_id = threading.get_ident()
            leader_blocked.set()
            assert leader_release.wait(30), "web leader was never released after follower hand-off"
        return real_claim_ready(
            self,
            run_id=run_id,
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
            now=now,
        )

    monkeypatch.setattr(TokenSchedulerRepository, "claim_ready", pause_leader_before_second_claim)

    submitted: list[Any] = []
    real_submit = service._executor.submit

    def capture_submit(*args: Any, **kwargs: Any) -> Any:
        future = real_submit(*args, **kwargs)
        submitted.append(future)
        return future

    monkeypatch.setattr(service._executor, "submit", capture_submit)

    launched_run_id = await service.execute(
        session_id,
        user_id="task10-web-user",
        auth_provider_type="local",
    )
    run_id = str(launched_run_id)
    assert launched_run_id == run_uuid
    assert len(submitted) == 1
    assert await asyncio.to_thread(leader_blocked.wait, 10), "ExecutionService leader never paused before a fork-branch claim"

    db = LandscapeDB.from_url(settings.landscape.url)

    try:
        with db.engine.connect() as conn:
            live_run = conn.execute(select(runs_table.c.status).where(runs_table.c.run_id == run_id)).scalar_one()
            live_source = conn.execute(select(run_sources_table.c.lifecycle_state).where(run_sources_table.c.run_id == run_id)).scalar_one()
            live_seat = conn.execute(select(run_coordination_table).where(run_coordination_table.c.run_id == run_id)).mappings().one()
            live_workers = conn.execute(select(run_workers_table).where(run_workers_table.c.run_id == run_id)).mappings().all()
            live_checkpoints = (
                conn.execute(select(checkpoints_table.c.sequence_number).where(checkpoints_table.c.run_id == run_id)).scalars().all()
            )

        assert live_run == RunStatus.RUNNING.value
        assert live_source == "loading"
        assert live_seat["leader_worker_id"] is not None
        leader_worker_id = str(live_seat["leader_worker_id"])
        assert live_seat["leader_epoch"] == 1
        assert live_seat["leader_heartbeat_expires_at"] is not None
        assert [(row["worker_id"], row["role"], row["status"]) for row in live_workers] == [(leader_worker_id, "leader", "active")]
        assert live_checkpoints == [0]

        before_sinks = {
            output_a_path: output_a_path.read_bytes() if output_a_path.exists() else b"",
            output_b_path: output_b_path.read_bytes() if output_b_path.exists() else b"",
        }
        cli_task = asyncio.create_task(
            asyncio.to_thread(
                CliRunner().invoke,
                app,
                [
                    "join",
                    run_id,
                    "--settings",
                    str(settings_path),
                    "--database",
                    str(tmp_path / "real-follower-audit.db"),
                    "--format",
                    "json",
                ],
            )
        )

        def follower_handoff_committed() -> bool:
            with db.engine.connect() as conn:
                return (
                    conn.execute(
                        select(scheduler_events_table.c.event_id).where(
                            scheduler_events_table.c.run_id == run_id,
                            scheduler_events_table.c.event_type == SchedulerEventType.MARK_PENDING_SINK.value,
                            scheduler_events_table.c.caller_owner != leader_worker_id,
                        )
                    ).first()
                    is not None
                )

        await _wait_for(
            follower_handoff_committed,
            timeout=15,
            message="actual CLI follower never committed its leader-only sink hand-off",
        )
        with db.engine.connect() as conn:
            follower_token_id = (
                conn.execute(
                    select(scheduler_events_table.c.token_id)
                    .where(
                        scheduler_events_table.c.run_id == run_id,
                        scheduler_events_table.c.event_type == SchedulerEventType.MARK_PENDING_SINK.value,
                        scheduler_events_table.c.caller_owner != leader_worker_id,
                    )
                    .order_by(scheduler_events_table.c.token_id)
                )
                .scalars()
                .first()
            )
            assert follower_token_id is not None
            token_id = str(follower_token_id)
        item_at_handoff = _work_item(db, token_id)
        assert item_at_handoff["status"] == TokenWorkStatus.PENDING_SINK.value
        assert {
            output_a_path: output_a_path.read_bytes() if output_a_path.exists() else b"",
            output_b_path: output_b_path.read_bytes() if output_b_path.exists() else b"",
        } == before_sinks

        with db.engine.connect() as conn:
            workers_at_handoff = conn.execute(
                select(run_workers_table.c.worker_id, run_workers_table.c.role, run_workers_table.c.status).where(
                    run_workers_table.c.run_id == run_id
                )
            ).all()
        assert len(workers_at_handoff) == 2
        follower_worker_id = next(row.worker_id for row in workers_at_handoff if row.role == "follower")
        assert {(row.role, row.status) for row in workers_at_handoff} == {("leader", "active"), ("follower", "active")}

        leader_release.set()
        cli_result = await asyncio.wait_for(cli_task, timeout=30)
        await asyncio.wait_for(asyncio.wrap_future(submitted[0]), timeout=30)

        assert cli_result.exit_code == 0, cli_result.output
        assert len(lifecycle) == 4
        assert all(events == ["start", "complete", "close"] for events in lifecycle.values())
        for plugin_ctx in contexts.values():
            assert plugin_ctx.rate_limit_registry is not None
            assert plugin_ctx.concurrency_config is not None
            assert plugin_ctx.shutdown_event is not None
        assert leader_thread_id is not None
        assert commit_thread_ids == [leader_thread_id, leader_thread_id]

        item = _work_item(db, token_id)
        assert item["status"] == TokenWorkStatus.TERMINAL.value

        with db.engine.connect() as conn:
            finished_run = conn.execute(select(runs_table).where(runs_table.c.run_id == run_id)).mappings().one()
            finished_source = conn.execute(
                select(run_sources_table.c.lifecycle_state).where(run_sources_table.c.run_id == run_id)
            ).scalar_one()
            finished_seat = conn.execute(select(run_coordination_table).where(run_coordination_table.c.run_id == run_id)).mappings().one()
            states = (
                conn.execute(
                    select(node_states_table.c.status).where(
                        node_states_table.c.run_id == run_id,
                        node_states_table.c.token_id == token_id,
                    )
                )
                .scalars()
                .all()
            )
            outcome = conn.execute(
                select(
                    token_outcomes_table.c.outcome,
                    token_outcomes_table.c.path,
                    token_outcomes_table.c.completed,
                    token_outcomes_table.c.sink_name,
                ).where(
                    token_outcomes_table.c.run_id == run_id,
                    token_outcomes_table.c.token_id == token_id,
                )
            ).one()
            all_outcomes = conn.execute(
                select(
                    token_outcomes_table.c.outcome,
                    token_outcomes_table.c.path,
                    token_outcomes_table.c.completed,
                    token_outcomes_table.c.sink_name,
                )
                .where(token_outcomes_table.c.run_id == run_id)
                .order_by(token_outcomes_table.c.sink_name)
            ).all()
            events = conn.execute(
                select(
                    scheduler_events_table.c.event_type,
                    scheduler_events_table.c.caller_owner,
                )
                .where(
                    scheduler_events_table.c.run_id == run_id,
                    scheduler_events_table.c.token_id == token_id,
                )
                .order_by(scheduler_events_table.c.recorded_at, scheduler_events_table.c.event_id)
            ).all()
            followers = conn.execute(
                select(run_workers_table.c.worker_id, run_workers_table.c.status).where(
                    run_workers_table.c.run_id == run_id,
                    run_workers_table.c.role == "follower",
                )
            ).all()
            all_workers = conn.execute(
                select(run_workers_table.c.worker_id, run_workers_table.c.role, run_workers_table.c.status).where(
                    run_workers_table.c.run_id == run_id
                )
            ).all()
            remaining_checkpoints = (
                conn.execute(select(checkpoints_table.c.checkpoint_id).where(checkpoints_table.c.run_id == run_id)).scalars().all()
            )
            attribution = conn.execute(
                select(
                    run_attributions_table.c.initiated_by_user_id,
                    run_attributions_table.c.auth_provider_type,
                ).where(run_attributions_table.c.run_id == run_id)
            ).one()

        assert finished_run["status"] == RunStatus.COMPLETED.value
        assert finished_source == "exhausted"
        assert finished_seat["leader_worker_id"] is None
        assert finished_seat["leader_heartbeat_expires_at"] is None
        assert finished_seat["leader_epoch"] == 1
        assert sorted((row.worker_id, row.role, row.status) for row in all_workers) == sorted(
            [
                (leader_worker_id, "leader", "departed"),
                (follower_worker_id, "follower", "departed"),
            ]
        )
        assert remaining_checkpoints == []
        assert "completed" in states
        assert tuple(outcome) in {
            (TerminalOutcome.SUCCESS.value, TerminalPath.DEFAULT_FLOW.value, 1, "branch_a"),
            (TerminalOutcome.SUCCESS.value, TerminalPath.DEFAULT_FLOW.value, 1, "branch_b"),
        }
        assert [tuple(row) for row in all_outcomes] == [
            (TerminalOutcome.TRANSIENT.value, TerminalPath.FORK_PARENT.value, 1, None),
            (TerminalOutcome.SUCCESS.value, TerminalPath.DEFAULT_FLOW.value, 1, "branch_a"),
            (TerminalOutcome.SUCCESS.value, TerminalPath.DEFAULT_FLOW.value, 1, "branch_b"),
        ]
        assert [event.event_type for event in events] == [
            SchedulerEventType.ENQUEUE.value,
            SchedulerEventType.CLAIM_READY.value,
            SchedulerEventType.MARK_PENDING_SINK.value,
            SchedulerEventType.CLAIM_PENDING_SINK.value,
            SchedulerEventType.MARK_PENDING_SINK_TERMINAL.value,
        ]
        assert [(row.worker_id, row.status) for row in followers] == [(follower_worker_id, "departed")]
        assert [event.caller_owner for event in events] == [
            None,
            follower_worker_id,
            follower_worker_id,
            leader_worker_id,
            leader_worker_id,
        ]
        assert tuple(attribution) == ("task10-web-user", "local")
        assert [json.loads(line) for line in output_a_path.read_text(encoding="utf-8").splitlines()] == [{"id": 1, "value": 10}]
        assert [json.loads(line) for line in output_b_path.read_text(encoding="utf-8").splitlines()] == [{"id": 1, "value": 10}]

        status_calls = [call.kwargs for call in session_service.update_run_status.await_args_list]
        assert [call["status"] for call in status_calls] == ["running", "completed"]
        assert status_calls[-1] == {
            "status": "completed",
            "error": None,
            "rows_processed": 1,
            "rows_succeeded": 2,
            "rows_failed": 0,
            "rows_routed_success": 0,
            "rows_routed_failure": 0,
            "rows_quarantined": 0,
        }

        if request.config.pluginmanager.hasplugin("scripts.state_engine_profile_reporter"):
            reporter = cast("RuntimeProfileReporter", request.getfixturevalue("state_engine_profile"))
            raw = db.engine.raw_connection()
            try:
                connection = raw.driver_connection
                assert isinstance(connection, sqlite3.Connection)
                assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
                reporter.observe_sqlite(
                    connection,
                    deployment="web-hosted-leader-plus-same-host-cli-followers",
                )
            finally:
                raw.close()

    finally:
        leader_release.set()
        await service.shutdown()
        db.close()


@pytest.mark.timeout(120)
def test_actual_cli_join_exceptional_traversal_departs_and_cleans_plugins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Tier-1 traversal failure still tears down lifecycle and membership."""
    from elspeth.cli import app

    processing_yaml = """
transforms:
  - name: process_row
    plugin: passthrough
    input: processing
    on_success: output
    on_error: discard
    options:
      schema:
        mode: observed
"""
    settings_text = _real_follower_settings_text(tmp_path, processing_yaml=processing_yaml)
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(settings_text, encoding="utf-8")
    settings = load_settings_from_yaml_string(settings_text)

    _plugins, graph, config = _build_runtime_graph(settings)
    db = LandscapeDB.from_url(settings.landscape.url)
    payload_store = FilesystemPayloadStore(settings.payload_store.base_path)
    result = Orchestrator(db).run(config, graph=graph, settings=settings, payload_store=payload_store)
    run_id = result.run_id
    factory = RecorderFactory(db, payload_store=payload_store)
    target_node_id = graph.get_next_node(graph.get_sources()[0])
    assert target_node_id is not None
    token_id = _seed_real_follower_ready_item(
        db=db,
        factory=factory,
        run_id=run_id,
        row_data={"id": 5, "value": 50},
        target_node_id=str(target_node_id),
        target_step_index=graph.get_node_step_map()[target_node_id],
    )
    with db.engine.begin() as conn:
        conn.execute(update(runs_table).where(runs_table.c.run_id == run_id).values(status=RunStatus.RUNNING.value, completed_at=None))
    factory.run_coordination.acquire_run_leadership(
        run_id=run_id,
        worker_id=f"worker:{run_id}:exceptional-leader",
        now=datetime.now(UTC),
        window_seconds=_GUARD_LIVE_SEAT_WINDOW_SECONDS,
    )

    lifecycle: list[str] = []
    real_start = PassThrough.on_start
    real_complete = PassThrough.on_complete
    real_close = PassThrough.close

    def observed_start(self: PassThrough, ctx: PluginContext) -> None:
        lifecycle.append("start")
        real_start(self, ctx)

    def fail_process(self: PassThrough, row: Any, ctx: Any) -> Any:
        del self, row, ctx
        raise FrameworkBugError("injected follower traversal integrity failure")

    def observed_complete(self: PassThrough, ctx: PluginContext) -> None:
        lifecycle.append("complete")
        real_complete(self, ctx)

    def observed_close(self: PassThrough) -> None:
        lifecycle.append("close")
        real_close(self)

    monkeypatch.setattr(PassThrough, "on_start", observed_start)
    monkeypatch.setattr(PassThrough, "process", fail_process)
    monkeypatch.setattr(PassThrough, "on_complete", observed_complete)
    monkeypatch.setattr(PassThrough, "close", observed_close)

    cli_result = CliRunner().invoke(
        app,
        [
            "join",
            run_id,
            "--settings",
            str(settings_path),
            "--database",
            str(tmp_path / "real-follower-audit.db"),
            "--format",
            "json",
        ],
    )

    try:
        assert cli_result.exit_code == 4, cli_result.output
        assert "FrameworkBugError" in cli_result.output
        assert lifecycle == ["start", "complete", "close"]
        item = _work_item(db, token_id)
        assert item["status"] == TokenWorkStatus.FAILED.value
        with db.engine.connect() as conn:
            followers = (
                conn.execute(
                    select(run_workers_table.c.status).where(
                        run_workers_table.c.run_id == run_id,
                        run_workers_table.c.role == "follower",
                    )
                )
                .scalars()
                .all()
            )
        assert followers == ["departed"]
    finally:
        db.close()
