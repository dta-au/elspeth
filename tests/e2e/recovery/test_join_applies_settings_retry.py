"""``elspeth join`` retries a transient failure under the run's ``settings.retry``.

RULINGS 2026-09-26 Q2 (one retry authority) and review-E1-follower-retry-r1
F1. The follower that ``elspeth join`` builds must take its retry policy from
the run's ``settings.retry``, the same authority as the leader. The review
showed two mutants surviving: the FOLLOWER arm of ``build_row_processor``
hard-coding a policy, and ``cli.py join`` passing a policy that is not the
settings one. Both survived because every test used ``max_attempts: 3`` (the
default) and no test drove ``elspeth join`` through a transient.

Here the real ``elspeth join`` command (CliRunner, in process) joins a real
run built from the same settings file, whose ``retry.max_attempts`` is 4 (not
the default), and drains a READY row whose transform raises a retryable
``ConnectionError`` three times: the fourth attempt succeeds. Either mutant
spends a different number of attempts and routes the row instead.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update
from typer.testing import CliRunner

from elspeth.contracts import RunStatus
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.core.landscape.schema import node_states_table, runs_table, token_work_items_table, transform_errors_table
from elspeth.plugins.transforms.passthrough import PassThrough
from tests.e2e.recovery.test_follower_join_and_drain import (
    _GUARD_LIVE_SEAT_WINDOW_SECONDS,
    _build_runtime_graph,
    _real_follower_settings_text,
    _seed_real_follower_ready_item,
)

_PROCESSING_YAML = """
transforms:
  - name: flaky
    plugin: passthrough
    input: processing
    on_success: output
    on_error: discard
    options:
      schema:
        mode: observed
retry:
  max_attempts: 4
  initial_delay_seconds: 0.01
  max_delay_seconds: 0.1
"""

_TRANSIENT_CALLS = 3


@pytest.mark.timeout(120)
def test_elspeth_join_retries_a_transient_under_the_runs_retry_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import elspeth.engine.orchestrator.follower as follower_module
    from elspeth.cli import app
    from elspeth.config_loading import load_settings
    from elspeth.core.landscape import LandscapeDB
    from elspeth.core.landscape.factory import RecorderFactory
    from elspeth.core.payload_store import FilesystemPayloadStore
    from elspeth.engine.orchestrator import Orchestrator

    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(_real_follower_settings_text(tmp_path, processing_yaml=_PROCESSING_YAML), encoding="utf-8")
    settings = load_settings(settings_path)
    assert settings.retry.max_attempts == 4  # not the RetrySettings default, so a default policy cannot pass

    # A real run from the same settings file (so `join`'s config_hash admits),
    # re-opened as RUNNING under a live leader seat with one READY row.
    _plugins, graph, config = _build_runtime_graph(settings)
    db = LandscapeDB.from_url(settings.landscape.url)
    payload_store = FilesystemPayloadStore(settings.payload_store.base_path)
    run_id = Orchestrator(db).run(config, graph=graph, settings=settings, payload_store=payload_store).run_id
    factory = RecorderFactory(db, payload_store=payload_store)
    with db.engine.begin() as conn:
        conn.execute(update(runs_table).where(runs_table.c.run_id == run_id).values(status=RunStatus.RUNNING.value, completed_at=None))
    factory.run_coordination.acquire_run_leadership(
        run_id=run_id, worker_id=f"worker:{run_id}:real-leader", window_seconds=_GUARD_LIVE_SEAT_WINDOW_SECONDS
    )
    source_node_id = graph.get_sources()[0]
    target_node_id = graph.get_next_node(source_node_id)
    assert target_node_id is not None
    token_id = _seed_real_follower_ready_item(
        db=db,
        factory=factory,
        run_id=run_id,
        row_data={"id": 2, "value": 20},
        target_node_id=str(target_node_id),
        target_step_index=graph.get_node_step_map()[target_node_id],
    )

    calls: list[int] = []
    original_process = PassThrough.process

    def flaky_process(self: PassThrough, row: Any, ctx: Any) -> Any:
        calls.append(len(calls))
        if len(calls) <= _TRANSIENT_CALLS:
            raise ConnectionError("transient failure before the retry budget is spent")
        return original_process(self, row, ctx)

    real_build = follower_module.build_follower_processor

    def build_then_stop_after_idle(**kwargs: Any) -> Any:
        follower = real_build(**kwargs)

        def stop_after_idle(_seconds: float) -> None:
            # The follower drained what it could claim; end the run so it departs (exit 0).
            with db.engine.begin() as conn:
                conn.execute(update(runs_table).where(runs_table.c.run_id == run_id).values(status=RunStatus.FAILED.value))

        follower._wait_fn = stop_after_idle
        return follower

    monkeypatch.setattr(PassThrough, "process", flaky_process)
    monkeypatch.setattr(follower_module, "build_follower_processor", build_then_stop_after_idle)

    try:
        result = CliRunner().invoke(app, ["join", run_id, "--settings", str(settings_path)])

        assert result.exit_code == 0, result.output
        assert len(calls) == _TRANSIENT_CALLS + 1
        with db.engine.connect() as conn:
            states = conn.execute(
                select(node_states_table.c.attempt, node_states_table.c.status)
                .where(node_states_table.c.token_id == token_id)
                .where(node_states_table.c.node_id == str(target_node_id))
                .order_by(node_states_table.c.attempt)
            ).all()
            item = conn.execute(
                select(token_work_items_table.c.status, token_work_items_table.c.pending_outcome).where(
                    token_work_items_table.c.token_id == token_id
                )
            ).one()
            errors = conn.execute(
                select(transform_errors_table.c.error_details_json).where(transform_errors_table.c.token_id == token_id)
            ).all()
        assert [(int(row.attempt), str(row.status)) for row in states] == [(0, "failed"), (1, "failed"), (2, "failed"), (3, "completed")]
        assert (item.status, item.pending_outcome) == (TokenWorkStatus.PENDING_SINK.value, "success")
        assert [json.loads(row.error_details_json) for row in errors] == []
    finally:
        db.close()
