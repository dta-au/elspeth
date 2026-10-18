"""Cancellation preserves recovered continuations until a later resume.

The source exhausts into an EOF buffer before an expansion produces the
backlog. A downstream fault leaves real READY/FAILED work for public resume;
no test fabricates recovery rows or terminal outcomes.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from sqlalchemy import select

from elspeth.contracts import PipelineRow, RunStatus
from elspeth.contracts.errors import GracefulShutdownError
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.core.checkpoint import RecoveryManager
from elspeth.core.landscape.schema import node_states_table, runs_table, token_outcomes_table, token_work_items_table
from elspeth.engine.orchestrator import leader_drain
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.transforms.value_transform import ValueTransform
from tests.integration.pipeline.test_barrier_hold_payload import build_pipeline, resume_pipeline, run_pipeline

_BODY = """
aggregations:
  - name: eof_buffer
    plugin: test_passthrough_identity_batch
    input: buffered
    on_success: rows
    on_error: discard
    trigger: {{count: 100}}
    output_mode: passthrough
    options:
      schema: {{mode: observed}}
transforms:
EXPANSION
  - name: lift
    plugin: value_transform
    input: values
    on_success: DESTINATION
    on_error: discard
    options:
      schema: {{mode: observed}}
      operations:
        - target: n
          expression: "row['item'] * 10"
"""

_EXPANSION = """
  - name: explode
    plugin: json_explode
    input: rows
    on_success: values
    on_error: discard
    options:
      array_field: items
      output_field: item
      schema: {{mode: observed}}
"""

_HOLD = """
  - name: hold
    plugin: test_passthrough_identity_batch
    input: held
    on_success: out
    on_error: discard
    trigger: {{count: 100}}
    output_mode: passthrough
    options:
      schema: {{mode: observed}}
"""


class _BacklogFault(RuntimeError):
    """Stop after expansion has published the child continuations."""


@pytest.mark.parametrize("buffer_result", [False, True], ids=["pending-sink", "blocked-hold"])
@pytest.mark.parametrize("preset", [False, True], ids=["during-backlog", "before-backlog"])
@pytest.mark.parametrize("expansion", [False, True], ids=["source-rows", "expansion-children"])
def test_cancelled_resume_preserves_unstarted_backlog_and_later_completes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, buffer_result: bool, preset: bool, expansion: bool
) -> None:
    body = _BODY.replace("DESTINATION", "held" if buffer_result else "out").replace("EXPANSION", _EXPANSION if expansion else "")
    if not expansion:
        body = body.replace("on_success: rows", "on_success: values")
    if buffer_result:
        body = body.replace("transforms:", _HOLD + "transforms:")
    docs = [{"id": 1, "items": [1, 2, 3]}] if expansion else [{"id": i, "item": i} for i in (1, 2, 3)]
    env = build_pipeline(tmp_path, body, docs)
    original_process = ValueTransform.process

    def fail(self: ValueTransform, row: PipelineRow, ctx: PluginContext) -> TransformResult:
        raise _BacklogFault("fault before the first child finishes")

    with monkeypatch.context() as fault_patch:
        fault_patch.setattr(ValueTransform, "process", fail)
        with pytest.raises(_BacklogFault):
            run_pipeline(env)

    db = env["db"]
    with db.connection() as conn:
        run_id = conn.execute(select(runs_table.c.run_id)).scalar_one()
        before = (
            conn.execute(
                select(token_work_items_table.c.status).where(
                    token_work_items_table.c.status.in_([TokenWorkStatus.READY.value, TokenWorkStatus.FAILED.value])
                )
            )
            .scalars()
            .all()
        )
    assert sorted(before) == [TokenWorkStatus.FAILED.value, TokenWorkStatus.READY.value, TokenWorkStatus.READY.value]

    event = threading.Event()
    if preset:
        event.set()
    calls: list[int] = []

    def cancel(self: ValueTransform, row: PipelineRow, ctx: PluginContext) -> TransformResult:
        calls.append(row["item"])
        result = original_process(self, row, ctx)
        event.set()
        return result

    recovery = RecoveryManager(db, env["checkpoints"])

    def resume(shutdown_event: threading.Event | None = None):
        check = recovery.can_resume(run_id, env["graph"])
        assert check.can_resume, check.reason
        return env["orchestrator"].resume(
            resume_point=recovery.get_resume_point(run_id, env["graph"]),
            config=env["config"],
            graph=env["graph"],
            settings=env["settings"],
            payload_store=env["payload_store"],
            shutdown_event=shutdown_event,
        )

    first_point = recovery.get_resume_point(run_id, env["graph"])
    with monkeypatch.context() as cancel_patch:
        cancel_patch.setattr(ValueTransform, "process", cancel)
        with pytest.raises(GracefulShutdownError):
            resume(event)

    assert len(calls) == (0 if preset else 1)
    with db.connection() as conn:
        assert conn.execute(select(runs_table.c.status)).scalar_one() == RunStatus.INTERRUPTED.value
        active = (
            conn.execute(select(token_work_items_table.c.status).where(token_work_items_table.c.status != TokenWorkStatus.TERMINAL.value))
            .scalars()
            .all()
        )
        assert sorted(active) == sorted(
            [TokenWorkStatus.READY.value] * (3 if preset else 2) + ([TokenWorkStatus.BLOCKED.value] if buffer_result and not preset else [])
        )
        assert conn.execute(select(token_outcomes_table.c.token_id).where(token_outcomes_table.c.outcome == "failure")).all() == []
        assert (
            conn.execute(select(node_states_table.c.state_id).where(node_states_table.c.error_json.like("%shutdown_requested%"))).all()
            == []
        )
    second_point = recovery.get_resume_point(run_id, env["graph"])
    assert second_point.sequence_number > first_point.sequence_number
    assert env["output_path"].exists() is (not buffer_result and not preset)

    def record(self: ValueTransform, row: PipelineRow, ctx: PluginContext) -> TransformResult:
        calls.append(row["item"])
        return original_process(self, row, ctx)

    with monkeypatch.context() as finish_patch:
        finish_patch.setattr(ValueTransform, "process", record)
        result = resume()
    assert result.status is RunStatus.COMPLETED
    assert sorted(calls) == [1, 2, 3]
    output = [json.loads(line) for line in env["output_path"].read_text().splitlines()]
    assert sorted(row["n"] for row in output) == [10, 20, 30]
    with db.connection() as conn:
        assert set(conn.execute(select(token_work_items_table.c.status)).scalars()) == {TokenWorkStatus.TERMINAL.value}


@pytest.mark.parametrize("resuming", [False, True], ids=["fresh-run", "resumed-run"])
def test_cancellation_during_eof_release_preserves_downstream_hold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resuming: bool) -> None:
    body = _BODY.replace("EXPANSION", "").replace("on_success: rows", "on_success: values")
    body = body.replace("DESTINATION", "held").replace("transforms:", _HOLD + "transforms:")
    env = build_pipeline(tmp_path, body, [{"id": i, "item": i} for i in (1, 2, 3)])

    def fail_flush(**kwargs):
        raise _BacklogFault("fault before EOF flush")

    if resuming:
        with monkeypatch.context() as fault_patch:
            fault_patch.setattr(leader_drain, "flush_remaining_aggregation_buffers", fail_flush)
            with pytest.raises(_BacklogFault):
                run_pipeline(env)
    recovery = RecoveryManager(env["db"], env["checkpoints"])
    event = threading.Event()
    calls: list[int] = []
    original_process = ValueTransform.process

    def cancel(self: ValueTransform, row: PipelineRow, ctx: PluginContext) -> TransformResult:
        calls.append(row["item"])
        result = original_process(self, row, ctx)
        event.set()
        return result

    with monkeypatch.context() as cancel_patch:
        cancel_patch.setattr(ValueTransform, "process", cancel)
        with pytest.raises(GracefulShutdownError):
            if resuming:
                with env["db"].connection() as conn:
                    run_id = conn.execute(select(runs_table.c.run_id)).scalar_one()
                env["orchestrator"].resume(
                    resume_point=recovery.get_resume_point(run_id, env["graph"]),
                    config=env["config"],
                    graph=env["graph"],
                    settings=env["settings"],
                    payload_store=env["payload_store"],
                    shutdown_event=event,
                )
            else:
                catalog_sha256, catalog_source = env["catalog"]
                env["orchestrator"].run(
                    env["config"],
                    graph=env["graph"],
                    settings=env["settings"],
                    payload_store=env["payload_store"],
                    openrouter_catalog_sha256=catalog_sha256,
                    openrouter_catalog_source=catalog_source,
                    shutdown_event=event,
                )
    assert calls == [1]
    with env["db"].connection() as conn:
        active = (
            conn.execute(select(token_work_items_table.c.status).where(token_work_items_table.c.status != TokenWorkStatus.TERMINAL.value))
            .scalars()
            .all()
        )
    assert sorted(active) == [TokenWorkStatus.BLOCKED.value, TokenWorkStatus.READY.value, TokenWorkStatus.READY.value]
    assert not env["output_path"].exists()
    result = resume_pipeline(env)
    assert result.status is RunStatus.COMPLETED
    output = [json.loads(line) for line in env["output_path"].read_text().splitlines()]
    assert sorted(row["n"] for row in output) == [10, 20, 30]
