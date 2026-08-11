"""Actual CLI-hosted follower composition and runtime-context evidence."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update
from typer.testing import CliRunner

from elspeth.contracts import FrameworkBugError, RunStatus
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.scheduler import SchedulerEventType, TokenWorkStatus
from elspeth.core.config import load_settings_from_yaml_string
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import (
    node_states_table,
    run_attributions_table,
    run_workers_table,
    runs_table,
    scheduler_events_table,
)
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.orchestrator.follower import FollowerProcessor
from elspeth.plugins.transforms.passthrough import PassThrough
from tests.e2e.recovery.test_follower_join_and_drain import (
    _GUARD_LIVE_SEAT_WINDOW_SECONDS,
    _build_runtime_graph,
    _real_follower_settings_text,
    _seed_real_follower_ready_item,
    _work_item,
)


@pytest.mark.timeout(120)
def test_actual_cli_join_traverses_and_tears_down_with_full_runtime_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typer join hosts a web-attributed run with leader-equivalent facilities.

    The run-attribution row is the production web-to-Landscape identity seam.
    This test deliberately does not emit the web-hosted deployment profile:
    the leader is assembled directly rather than launched by the web service.
    """
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
    result = Orchestrator(db).run(
        config,
        graph=graph,
        settings=settings,
        payload_store=payload_store,
        initiated_by_user_id="task10-web-user",
        auth_provider_type="local",
    )
    run_id = result.run_id
    factory = RecorderFactory(db, payload_store=payload_store)
    target_node_id = graph.get_next_node(graph.get_sources()[0])
    assert target_node_id is not None
    token_id = _seed_real_follower_ready_item(
        db=db,
        factory=factory,
        run_id=run_id,
        row_data={"id": 2, "value": 20},
        target_node_id=str(target_node_id),
        target_step_index=graph.get_node_step_map()[target_node_id],
    )
    with db.engine.begin() as conn:
        conn.execute(update(runs_table).where(runs_table.c.run_id == run_id).values(status=RunStatus.RUNNING.value, completed_at=None))
    now = datetime.now(UTC)
    factory.run_coordination.acquire_run_leadership(
        run_id=run_id,
        worker_id=f"worker:{run_id}:cli-profile-leader",
        now=now,
        window_seconds=_GUARD_LIVE_SEAT_WINDOW_SECONDS,
    )

    traversed = threading.Event()
    lifecycle: list[str] = []
    contexts: list[PluginContext] = []
    real_terminal = FollowerProcessor._run_is_terminal
    real_process = PassThrough.process
    real_start = PassThrough.on_start
    real_complete = PassThrough.on_complete
    real_close = PassThrough.close
    deadline = time.monotonic() + 10.0

    def bounded_terminal(self: FollowerProcessor) -> bool:
        assert time.monotonic() < deadline, "actual CLI follower never traversed its READY work"
        return traversed.is_set() or real_terminal(self)

    def observed_start(self: PassThrough, ctx: PluginContext) -> None:
        contexts.append(ctx)
        lifecycle.append("start")
        real_start(self, ctx)

    def observed_process(self: PassThrough, row: Any, ctx: Any) -> Any:
        processed = real_process(self, row, ctx)
        traversed.set()
        return processed

    def observed_complete(self: PassThrough, ctx: PluginContext) -> None:
        lifecycle.append("complete")
        real_complete(self, ctx)

    def observed_close(self: PassThrough) -> None:
        lifecycle.append("close")
        real_close(self)

    monkeypatch.setattr(FollowerProcessor, "_run_is_terminal", bounded_terminal)
    monkeypatch.setattr(PassThrough, "on_start", observed_start)
    monkeypatch.setattr(PassThrough, "process", observed_process)
    monkeypatch.setattr(PassThrough, "on_complete", observed_complete)
    monkeypatch.setattr(PassThrough, "close", observed_close)

    before_sink = (tmp_path / "real-follower-output.jsonl").read_bytes()
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
        assert cli_result.exit_code == 0, cli_result.output
        assert traversed.is_set()
        assert lifecycle == ["start", "complete", "close"]
        assert len(contexts) == 1
        follower_ctx = contexts[0]
        assert follower_ctx.rate_limit_registry is not None
        assert follower_ctx.concurrency_config is not None
        assert follower_ctx.shutdown_event is not None

        item = _work_item(db, token_id)
        assert item["status"] == TokenWorkStatus.PENDING_SINK.value
        assert (tmp_path / "real-follower-output.jsonl").read_bytes() == before_sink

        with db.engine.connect() as conn:
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
            events = conn.execute(
                select(
                    scheduler_events_table.c.event_type,
                    scheduler_events_table.c.caller_owner,
                ).where(
                    scheduler_events_table.c.run_id == run_id,
                    scheduler_events_table.c.token_id == token_id,
                )
            ).all()
            followers = conn.execute(
                select(run_workers_table.c.worker_id, run_workers_table.c.status).where(
                    run_workers_table.c.run_id == run_id,
                    run_workers_table.c.role == "follower",
                )
            ).all()
            attribution = conn.execute(
                select(
                    run_attributions_table.c.initiated_by_user_id,
                    run_attributions_table.c.auth_provider_type,
                ).where(run_attributions_table.c.run_id == run_id)
            ).one()

        assert "completed" in states
        assert [event.event_type for event in events] == [
            SchedulerEventType.ENQUEUE.value,
            SchedulerEventType.CLAIM_READY.value,
            SchedulerEventType.MARK_PENDING_SINK.value,
        ]
        follower_ids = {worker.worker_id for worker in followers}
        assert len(follower_ids) == 1
        follower_id = next(iter(follower_ids))
        assert followers[0].status == "departed"
        assert [event.caller_owner for event in events] == [None, follower_id, follower_id]
        assert tuple(attribution) == ("task10-web-user", "local")

    finally:
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
