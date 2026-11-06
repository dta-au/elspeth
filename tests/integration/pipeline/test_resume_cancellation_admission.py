"""Real recovery admission and intake boundaries under cooperative cancellation."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from elspeth.contracts import PipelineRow, RunStatus
from elspeth.contracts.errors import GracefulShutdownError
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.core.checkpoint import RecoveryManager
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import node_states_table, runs_table, token_outcomes_table, token_work_items_table
from elspeth.engine.executors.gate import GateExecutor
from elspeth.engine.orchestrator import source_iteration
from elspeth.engine.processor import RowProcessor
from elspeth.engine.retry import RetryManager
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.transforms.value_transform import ValueTransform
from tests.fixtures.passthrough_batch_plugins import PassthroughIdentityBatch
from tests.integration.pipeline.test_barrier_hold_payload import build_pipeline, resume_pipeline, run_pipeline
from tests.integration.pipeline.test_resume_cancellation_backlog import _BODY, _EXPANSION, _BacklogFault


@pytest.mark.parametrize("boundary", ["claim", "first-attempt", "without-retry"])
def test_cancellation_before_first_operation_keeps_claim_undecided_for_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    body = _BODY.replace("EXPANSION", _EXPANSION).replace("DESTINATION", "out")
    control = run_pipeline(build_pipeline(tmp_path / "control", body, [{"id": 1, "items": [1, 2, 3]}]))
    env = build_pipeline(tmp_path, body, [{"id": 1, "items": [1, 2, 3]}])

    def fault(self: ValueTransform, row: PipelineRow, ctx: PluginContext) -> TransformResult:
        raise _BacklogFault("fault after expansion")

    with monkeypatch.context() as fault_patch:
        fault_patch.setattr(ValueTransform, "process", fault)
        with pytest.raises(_BacklogFault):
            run_pipeline(env)
    with env["db"].connection() as conn:
        run_id = conn.execute(select(runs_table.c.run_id)).scalar_one()
    recovery = RecoveryManager(env["db"], env["checkpoints"])
    event = threading.Event()
    original_claim = TokenSchedulerRepository.claim_ready
    original_retry = RetryManager.execute_with_retry
    original_process = ValueTransform.process
    original_execute = RowProcessor._execute_transform_with_retry
    calls: list[int] = []

    def cancel_claim(self: TokenSchedulerRepository, **kwargs: Any):
        claim = original_claim(self, **kwargs)
        if claim is not None:
            event.set()
        return claim

    def cancel_attempt(self: RetryManager, operation, *args: Any, **kwargs: Any):
        if operation.__name__ == "execute_attempt":
            event.set()
        return original_retry(self, operation, *args, **kwargs)

    def record(self: ValueTransform, row: PipelineRow, ctx: PluginContext) -> TransformResult:
        calls.append(row["item"])
        return original_process(self, row, ctx)

    def cancel_without_retry(self: RowProcessor, *args: Any, **kwargs: Any):
        self._retry_manager = None
        event.set()
        return original_execute(self, *args, **kwargs)

    with monkeypatch.context() as cancel_patch:
        cancel_patch.setattr(ValueTransform, "process", record)
        if boundary == "claim":
            cancel_patch.setattr(TokenSchedulerRepository, "claim_ready", cancel_claim)
        elif boundary == "first-attempt":
            cancel_patch.setattr(RetryManager, "execute_with_retry", cancel_attempt)
        else:
            cancel_patch.setattr(RowProcessor, "_execute_transform_with_retry", cancel_without_retry)
        with pytest.raises(GracefulShutdownError):
            env["orchestrator"].resume(
                resume_point=recovery.get_resume_point(run_id, env["graph"]),
                config=env["config"],
                graph=env["graph"],
                settings=env["settings"],
                payload_store=env["payload_store"],
                shutdown_event=event,
            )
    assert calls == []
    with env["db"].connection() as conn:
        assert conn.execute(select(token_outcomes_table.c.token_id).where(token_outcomes_table.c.outcome == "failure")).all() == []
        assert (
            conn.execute(select(node_states_table.c.state_id).where(node_states_table.c.error_json.like("%shutdown_requested%"))).all()
            == []
        )
        active = conn.execute(
            select(token_work_items_table.c.status, token_work_items_table.c.row_payload_json).where(
                token_work_items_table.c.status != TokenWorkStatus.TERMINAL.value
            )
        ).all()
    assert sorted(status for status, _ in active) == ["failed", "ready", "ready"]
    assert all(json.loads(payload)["row"]["data"] for _, payload in active)

    with monkeypatch.context() as finish_patch:
        finish_patch.setattr(ValueTransform, "process", record)
        result = resume_pipeline(env)
    assert result.status is RunStatus.COMPLETED
    expected_counters = control.to_dict()
    actual_counters = result.to_dict()
    expected_counters.pop("run_id")
    actual_counters.pop("run_id")
    assert actual_counters == expected_counters
    assert sorted(calls) == [1, 2, 3]
    output = [json.loads(line) for line in env["output_path"].read_text().splitlines()]
    assert sorted(row["n"] for row in output) == [10, 20, 30]


_COUNT_BATCH = """
aggregations:
  - name: counted
    plugin: test_passthrough_identity_batch
    input: buffered
    on_success: out
    on_error: discard
    trigger: {{count: 1}}
    output_mode: passthrough
    options:
      schema: {{mode: observed}}
"""


@pytest.mark.parametrize("boundary", ["between-batches", "after-adoption"])
def test_one_intake_preserves_later_batches_after_first_batch_requests_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    env = build_pipeline(tmp_path, _COUNT_BATCH, [{"id": i} for i in (1, 2, 3)])

    def hold_arrivals(self: RowProcessor, ctx: PluginContext):
        return [], []

    def fault_before_eof(**kwargs: Any):
        raise _BacklogFault("source exhausted before intake")

    with monkeypatch.context() as fault_patch:
        fault_patch.setattr(RowProcessor, "_run_barrier_intake_pass", hold_arrivals)
        fault_patch.setattr(source_iteration, "run_end_of_input_barrier_flush", fault_before_eof)
        with pytest.raises(_BacklogFault):
            run_pipeline(env)
    with env["db"].connection() as conn:
        run_id = conn.execute(select(runs_table.c.run_id)).scalar_one()
        assert conn.execute(select(token_work_items_table.c.barrier_adopted_epoch)).scalars().all() == [None, None, None]
    recovery = RecoveryManager(env["db"], env["checkpoints"])
    event = threading.Event()
    original_process = PassthroughIdentityBatch.process
    original_adopt = TokenSchedulerRepository.adopt_blocked_barrier_item
    batches: list[list[int]] = []

    def cancel(self: PassthroughIdentityBatch, rows: list[PipelineRow], ctx: PluginContext) -> TransformResult:
        batches.append([row["id"] for row in rows])
        result = original_process(self, rows, ctx)
        if boundary == "between-batches":
            event.set()
        return result

    def cancel_adopt(self: TokenSchedulerRepository, **kwargs: Any):
        adoption = original_adopt(self, **kwargs)
        event.set()
        return adoption

    with monkeypatch.context() as cancel_patch:
        cancel_patch.setattr(PassthroughIdentityBatch, "process", cancel)
        if boundary == "after-adoption":
            cancel_patch.setattr(TokenSchedulerRepository, "adopt_blocked_barrier_item", cancel_adopt)
        with pytest.raises(GracefulShutdownError):
            env["orchestrator"].resume(
                resume_point=recovery.get_resume_point(run_id, env["graph"]),
                config=env["config"],
                graph=env["graph"],
                settings=env["settings"],
                payload_store=env["payload_store"],
                shutdown_event=event,
            )
    assert batches == ([[1]] if boundary == "between-batches" else [])
    with env["db"].connection() as conn:
        assert sorted(conn.execute(select(token_work_items_table.c.status)).scalars()) == (
            ["blocked", "blocked", "terminal"] if boundary == "between-batches" else ["blocked"] * 3
        )
        pending = conn.execute(
            select(token_work_items_table.c.token_id, token_work_items_table.c.barrier_adopted_epoch).where(
                token_work_items_table.c.status == "blocked"
            )
        ).all()
        unadopted = [token_id for token_id, epoch in pending if epoch is None]
        assert len(unadopted) == 2
        assert (
            conn.execute(
                select(token_outcomes_table.c.token_id).where(
                    token_outcomes_table.c.token_id.in_(unadopted), token_outcomes_table.c.outcome == "buffered"
                )
            ).all()
            == []
        )
    if boundary == "between-batches":
        assert [json.loads(line)["id"] for line in env["output_path"].read_text().splitlines()] == [1]
    else:
        assert not env["output_path"].exists()

    def record(self: PassthroughIdentityBatch, rows: list[PipelineRow], ctx: PluginContext) -> TransformResult:
        batches.append([row["id"] for row in rows])
        return original_process(self, rows, ctx)

    with monkeypatch.context() as finish_patch:
        finish_patch.setattr(PassthroughIdentityBatch, "process", record)
        result = resume_pipeline(env)
    assert result.status is RunStatus.COMPLETED
    assert batches == [[1], [2], [3]]
    assert sorted(json.loads(line)["id"] for line in env["output_path"].read_text().splitlines()) == [1, 2, 3]


@pytest.mark.parametrize("earlier_operation", ["transform", "gate"])
def test_later_unstarted_transform_does_not_requeue_an_already_started_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, earlier_operation: str
) -> None:
    body = _BODY.replace("EXPANSION", "").replace("on_success: rows", "on_success: values")
    if earlier_operation == "transform":
        body = body.replace("DESTINATION", "later")
        body += """
  - name: final
    plugin: value_transform
    input: later
    on_success: out
    on_error: discard
    options:
      schema: {{mode: observed}}
      operations:
        - target: m
          expression: "row['n'] + 1"
"""
    else:
        body = body.replace("input: values", "input: later").replace("DESTINATION", "out")
        body += """
gates:
  - name: check
    input: values
    condition: "'keep'"
    routes:
      keep: later
"""
    env = build_pipeline(tmp_path, body, [{"id": i, "item": i} for i in (1, 2, 3)])
    first_node = (
        env["graph"].get_transform_name_id_map()["lift"]
        if earlier_operation == "transform"
        else env["graph"].get_config_gate_id_map()["check"]
    )
    original_traversal = RowProcessor._process_single_token

    def fault(self: RowProcessor, *args: Any, **kwargs: Any):
        if kwargs["current_node_id"] == first_node:
            raise _BacklogFault("fault before the first executable node")
        return original_traversal(self, *args, **kwargs)

    with monkeypatch.context() as fault_patch:
        fault_patch.setattr(RowProcessor, "_process_single_token", fault)
        with pytest.raises(_BacklogFault):
            run_pipeline(env)
    with env["db"].connection() as conn:
        run_id = conn.execute(select(runs_table.c.run_id)).scalar_one()
    recovery = RecoveryManager(env["db"], env["checkpoints"])
    event = threading.Event()
    prior_calls: list[int] = []
    later_calls: list[int] = []
    original_process = ValueTransform.process
    original_gate = GateExecutor.execute_config_gate

    def record_transform(self: ValueTransform, row: PipelineRow, ctx: PluginContext) -> TransformResult:
        if earlier_operation == "transform" and self.on_success == "later":
            prior_calls.append(row["id"])
            result = original_process(self, row, ctx)
            if ctx.shutdown_event is event:
                event.set()
            return result
        later_calls.append(row["id"])
        return original_process(self, row, ctx)

    def record_gate(self: GateExecutor, gate_config, node_id, token, ctx, *args: Any, **kwargs: Any):
        result = original_gate(self, gate_config, node_id, token, ctx, *args, **kwargs)
        prior_calls.append(token.row_data["id"])
        if ctx.shutdown_event is event:
            event.set()
        return result

    with monkeypatch.context() as record_patch:
        record_patch.setattr(ValueTransform, "process", record_transform)
        record_patch.setattr(GateExecutor, "execute_config_gate", record_gate)
        with pytest.raises(GracefulShutdownError):
            env["orchestrator"].resume(
                resume_point=recovery.get_resume_point(run_id, env["graph"]),
                config=env["config"],
                graph=env["graph"],
                settings=env["settings"],
                payload_store=env["payload_store"],
                shutdown_event=event,
            )
        assert len(prior_calls) == 1
        assert later_calls == []
        result = resume_pipeline(env)
    # The current claim retains its established interruption disposition; it
    # must never be mistaken for an unstarted claim and replay the prior node.
    assert result.status is RunStatus.COMPLETED_WITH_FAILURES
    assert sorted(prior_calls) == [1, 2, 3]
    assert len(later_calls) == 2
    assert result.rows_failed == 1


def test_shutdown_during_retry_backoff_does_not_requeue_a_started_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A claim whose first transform attempt already ran is not "unstarted".

    The first executable transform may preserve its claim as unstarted only
    while no attempt has been invoked. Here attempt 1 runs (its effect is out),
    requests shutdown and fails retryably, so RetryManager stops in backoff.
    That claim keeps the established interruption disposition and must not be
    requeued for a later resume to run the plugin a second time.
    """
    body = _BODY.replace("EXPANSION", "").replace("on_success: rows", "on_success: values").replace("DESTINATION", "out")
    env = build_pipeline(tmp_path, body, [{"id": i, "item": i} for i in (1, 2, 3)])
    lift = env["graph"].get_transform_name_id_map()["lift"]
    original_traversal = RowProcessor._process_single_token

    def fault(self: RowProcessor, *args: Any, **kwargs: Any):
        if kwargs["current_node_id"] == lift:
            raise _BacklogFault("fault before the first executable node")
        return original_traversal(self, *args, **kwargs)

    with monkeypatch.context() as fault_patch:
        fault_patch.setattr(RowProcessor, "_process_single_token", fault)
        with pytest.raises(_BacklogFault):
            run_pipeline(env)
    with env["db"].connection() as conn:
        run_id = conn.execute(select(runs_table.c.run_id)).scalar_one()
    recovery = RecoveryManager(env["db"], env["checkpoints"])
    event = threading.Event()
    calls: list[int] = []
    original_process = ValueTransform.process

    def started_then_backoff(self: ValueTransform, row: PipelineRow, ctx: PluginContext) -> TransformResult:
        calls.append(row["id"])
        if len(calls) == 1:
            event.set()
            raise ConnectionError("attempt 1 ran; shutdown requested before its retry")
        return original_process(self, row, ctx)

    with monkeypatch.context() as record_patch:
        record_patch.setattr(ValueTransform, "process", started_then_backoff)
        with pytest.raises(GracefulShutdownError):
            env["orchestrator"].resume(
                resume_point=recovery.get_resume_point(run_id, env["graph"]),
                config=env["config"],
                graph=env["graph"],
                settings=env["settings"],
                payload_store=env["payload_store"],
                shutdown_event=event,
            )
        assert len(calls) == 1
        started_id = calls[0]
        result = resume_pipeline(env)

    # The started row ran exactly once: resume did not replay its plugin.
    assert calls.count(started_id) == 1
    assert sorted(calls) == [1, 2, 3]
    assert result.status is RunStatus.COMPLETED_WITH_FAILURES
    assert result.rows_failed == 1
