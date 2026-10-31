"""Collector cancellation retains adopted arrivals and durable member losses."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

from elspeth.contracts import PipelineRow, RunStatus
from elspeth.contracts.errors import GracefulShutdownError
from elspeth.contracts.plugin_context import PluginContext
from elspeth.core.checkpoint import RecoveryManager
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import (
    collector_group_failures_table,
    group_losses_table,
    runs_table,
    token_outcomes_table,
    token_work_items_table,
)
from elspeth.engine.barrier_coordination import BarrierIntakeCoordinator
from elspeth.engine.executors.collector import CollectorExecutor, CollectorOutcome
from elspeth.engine.processor import RowProcessor
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.transforms.batch_stats import BatchStats
from elspeth.plugins.transforms.value_transform import ValueTransform
from tests.integration.pipeline.test_barrier_hold_payload import _COLLECTOR_ARM, build_pipeline, resume_pipeline, run_pipeline
from tests.integration.pipeline.test_resume_cancellation_backlog import _BacklogFault


def _resume_with_event(env: dict[str, Any], event: threading.Event) -> None:
    with env["db"].connection() as conn:
        run_id = conn.execute(select(runs_table.c.run_id)).scalar_one()
    recovery = RecoveryManager(env["db"], env["checkpoints"])
    check = recovery.can_resume(run_id, env["graph"])
    assert check.can_resume, check.reason
    env["orchestrator"].resume(
        resume_point=recovery.get_resume_point(run_id, env["graph"]),
        config=env["config"],
        graph=env["graph"],
        settings=env["settings"],
        payload_store=env["payload_store"],
        shutdown_event=event,
    )


def test_organic_loss_cancellation_preserves_ready_survivor_and_full_counter_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _COLLECTOR_ARM.replace("policy: require_all", "policy: best_effort")
    docs = [{"id": 1, "items": [10, 11]}, {"id": 2, "items": [21, 20]}]
    original_transform = ValueTransform.process
    original_batch = BatchStats.process
    original_claim = TokenSchedulerRepository.claim_ready
    original_adopt = TokenSchedulerRepository.adopt_group_losses

    def execute(root: Path, cancel: bool) -> dict[str, Any]:
        env = build_pipeline(root, body, docs)
        event = threading.Event()
        batches: list[list[int]] = []
        marks = 0

        def survivor_last(self: TokenSchedulerRepository, **kwargs: Any):
            # Control only the tie order of genuine READY children. Normal
            # loss replay and collector closing remain active throughout.
            with env["db"].engine.begin() as conn:
                ready = conn.execute(
                    select(token_work_items_table.c.work_item_id, token_work_items_table.c.row_payload_json).where(
                        token_work_items_table.c.status == "ready"
                    )
                ).all()
                for work_id, payload in ready:
                    if json.loads(payload)["row"]["data"].get("item") == 20:
                        conn.execute(
                            update(token_work_items_table)
                            .where(token_work_items_table.c.work_item_id == work_id)
                            .values(created_at=datetime.now(UTC) + timedelta(days=1))
                        )
            return original_claim(self, **kwargs)

        def fail_members(self: ValueTransform, row: PipelineRow, ctx: PluginContext) -> TransformResult:
            if row["item"] in {11, 21}:
                return TransformResult.error({"reason": "deliberate_failure"}, retryable=False)
            return original_transform(self, row, ctx)

        def record(self: BatchStats, rows: list[PipelineRow], ctx: PluginContext) -> TransformResult:
            batches.append([row["item"] for row in rows])
            return original_batch(self, rows, ctx)

        def cancel_second_loss(self: TokenSchedulerRepository, **kwargs: Any):
            nonlocal marks
            result = original_adopt(self, **kwargs)
            marks += 1
            if cancel and marks == 2:
                event.set()
            return result

        with monkeypatch.context() as patcher:
            patcher.setattr(ValueTransform, "process", fail_members)
            patcher.setattr(BatchStats, "process", record)
            patcher.setattr(TokenSchedulerRepository, "claim_ready", survivor_last)
            patcher.setattr(TokenSchedulerRepository, "adopt_group_losses", cancel_second_loss)
            if cancel:
                catalog_sha, catalog_source = env["catalog"]
                with pytest.raises(GracefulShutdownError):
                    env["orchestrator"].run(
                        env["config"],
                        graph=env["graph"],
                        settings=env["settings"],
                        payload_store=env["payload_store"],
                        shutdown_event=event,
                        openrouter_catalog_sha256=catalog_sha,
                        openrouter_catalog_source=catalog_source,
                    )
                assert batches == [[10]]
                result = resume_pipeline(env)
            else:
                result = run_pipeline(env)
        assert result.status is RunStatus.COMPLETED_WITH_FAILURES
        assert batches == [[10], [20]]
        assert marks == 2
        return result.to_dict()

    control = execute(tmp_path / "control", False)
    interrupted = execute(tmp_path / "cancelled", True)
    control.pop("run_id")
    interrupted.pop("run_id")
    assert interrupted == control


def test_collector_adoption_finishes_hold_without_starting_cancelled_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = build_pipeline(tmp_path, _COLLECTOR_ARM, [{"id": 1, "items": [1, 2, 3]}])
    original_intake = RowProcessor._run_barrier_intake_pass

    def hold_collector(self: RowProcessor, ctx: PluginContext):
        # The EOF aggregation must still release real expansion work. Delay
        # only collector intake so all three arrivals are pending in the DB.
        if self._collector_executor.buffered_member_count() == 0:
            with env["db"].connection() as conn:
                collector_rows = conn.execute(
                    select(token_work_items_table.c.work_item_id).where(token_work_items_table.c.collector_name == "stitch")
                ).all()
            if collector_rows:
                if len(collector_rows) == 3:
                    raise _BacklogFault("three real collector arrivals are pending")
                return [], []
        return original_intake(self, ctx)

    with monkeypatch.context() as fault_patch:
        fault_patch.setattr(RowProcessor, "_run_barrier_intake_pass", hold_collector)
        with pytest.raises(_BacklogFault):
            run_pipeline(env)
    event = threading.Event()
    original_adopt = TokenSchedulerRepository.adopt_blocked_barrier_item
    original_process = BatchStats.process
    calls: list[list[int]] = []
    adopted = 0

    def cancel_last_adoption(self: TokenSchedulerRepository, **kwargs: Any):
        nonlocal adopted
        outcome = original_adopt(self, **kwargs)
        adopted += 1
        if adopted == 3:
            event.set()
        return outcome

    def record(self: BatchStats, rows: list[PipelineRow], ctx: PluginContext) -> TransformResult:
        calls.append([row["item"] for row in rows])
        return original_process(self, rows, ctx)

    with monkeypatch.context() as cancel_patch:
        cancel_patch.setattr(TokenSchedulerRepository, "adopt_blocked_barrier_item", cancel_last_adoption)
        cancel_patch.setattr(BatchStats, "process", record)
        with pytest.raises(GracefulShutdownError):
            _resume_with_event(env, event)
    assert adopted == 3
    assert calls == []
    assert not env["output_path"].exists()
    with monkeypatch.context() as finish_patch:
        finish_patch.setattr(BatchStats, "process", record)
        result = resume_pipeline(env)
    assert result.status is RunStatus.COMPLETED
    assert calls == [[1, 2, 3]]
    assert len(env["output_path"].read_text().splitlines()) == 1


@pytest.mark.parametrize("boundary", ["first-plugin", "loss-adoption"])
def test_collector_loss_with_ready_survivor_is_retained_across_cancelled_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    body = _COLLECTOR_ARM.replace("policy: require_all", "policy: best_effort")
    env = build_pipeline(tmp_path, body, [{"id": 1, "items": [10, 11]}, {"id": 2, "items": [21, 20]}])
    original_transform = ValueTransform.process
    original_process = BatchStats.process
    original_claim = TokenSchedulerRepository.claim_ready

    def survivor_last(self: TokenSchedulerRepository, **kwargs: Any):
        # Children share a publication timestamp and their final tiebreaker
        # is a hashed work-item id. Control only that scheduling timestamp
        # in this private test DB so both real losses precede the fault.
        # Recovery rows, payloads and outcomes are all produced by run().
        with env["db"].engine.begin() as conn:
            ready = conn.execute(
                select(token_work_items_table.c.work_item_id, token_work_items_table.c.row_payload_json).where(
                    token_work_items_table.c.status == "ready"
                )
            ).all()
            for work_id, payload in ready:
                if json.loads(payload)["row"]["data"].get("item") == 20:
                    conn.execute(
                        update(token_work_items_table)
                        .where(token_work_items_table.c.work_item_id == work_id)
                        .values(created_at=datetime.now(UTC) + timedelta(days=1))
                    )
        return original_claim(self, **kwargs)

    def lose_or_fault(self: ValueTransform, row: PipelineRow, ctx: PluginContext) -> TransformResult:
        if row["item"] in {11, 21}:
            return TransformResult.error({"reason": "deliberate_failure"}, retryable=False)
        if row["item"] == 20:
            raise _BacklogFault("second group's surviving member is still upstream")
        return original_transform(self, row, ctx)

    with monkeypatch.context() as fault_patch:
        fault_patch.setattr(ValueTransform, "process", lose_or_fault)
        fault_patch.setattr(TokenSchedulerRepository, "claim_ready", survivor_last)
        fault_patch.setattr(BarrierIntakeCoordinator, "_replay_group_losses", lambda self, ctx=None: [])
        fault_patch.setattr(
            CollectorExecutor,
            "_close_group",
            lambda self, name, key, pending, ctx: CollectorOutcome(held=True, collector_name=name, group_id=key[1]),
        )
        with pytest.raises(_BacklogFault):
            run_pipeline(env)
    with env["db"].connection() as conn:
        assert conn.execute(select(group_losses_table.c.adopted_epoch)).scalars().all() == [None, None]
        held = (
            conn.execute(select(token_work_items_table.c.row_payload_json).where(token_work_items_table.c.status == "blocked"))
            .scalars()
            .all()
        )
    assert [json.loads(payload)["row"]["data"]["item"] for payload in held] == [10]
    event = threading.Event()
    original_adopt_losses = TokenSchedulerRepository.adopt_group_losses
    calls: list[list[int]] = []
    marks = 0

    def cancel_adoption(self: TokenSchedulerRepository, **kwargs: Any):
        nonlocal marks
        outcome = original_adopt_losses(self, **kwargs)
        marks += 1
        if marks == 2:
            event.set()
        return outcome

    def record(self: BatchStats, rows: list[PipelineRow], ctx: PluginContext) -> TransformResult:
        calls.append([row["item"] for row in rows])
        outcome = original_process(self, rows, ctx)
        if boundary == "first-plugin" and ctx.shutdown_event is event:
            event.set()
        return outcome

    with monkeypatch.context() as cancel_patch:
        cancel_patch.setattr(BatchStats, "process", record)
        if boundary == "loss-adoption":
            cancel_patch.setattr(TokenSchedulerRepository, "adopt_group_losses", cancel_adoption)
        with pytest.raises(GracefulShutdownError):
            _resume_with_event(env, event)
    assert calls == ([[10]] if boundary == "first-plugin" else [])
    with monkeypatch.context() as finish_patch:
        finish_patch.setattr(BatchStats, "process", record)
        result = resume_pipeline(env)
    assert result.status is RunStatus.COMPLETED_WITH_FAILURES
    assert calls == [[10], [20]]
    assert len(env["output_path"].read_text().splitlines()) == 2
    with env["db"].connection() as conn:
        assert (
            conn.execute(
                select(token_work_items_table.c.work_item_id).where(
                    token_work_items_table.c.status.in_(["ready", "leased", "blocked", "pending_sink"])
                )
            ).all()
            == []
        )
        failed = conn.execute(select(token_work_items_table.c.token_id).where(token_work_items_table.c.status == "failed")).scalars().all()
        outcomes = (
            conn.execute(
                select(token_outcomes_table.c.token_id).where(
                    token_outcomes_table.c.outcome == "failure", token_outcomes_table.c.completed == 1
                )
            )
            .scalars()
            .all()
        )
        assert len(failed) == 2
        assert set(outcomes) == set(failed)


@pytest.mark.parametrize("policy", ["require_all", "best_effort"])
@pytest.mark.parametrize("member_result", ["error", "filtered"])
@pytest.mark.parametrize("boundary", ["member-result", "loss-adoption"])
def test_loss_only_public_resume_finishes_collector_verdict_without_active_scheduler_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str, member_result: str, boundary: str
) -> None:
    event = threading.Event()
    calls: list[str] = []
    notifications: list[str] = []
    original_notify = CollectorExecutor.notify_member_lost

    def lose_member(self: ValueTransform, row: PipelineRow, ctx: PluginContext) -> TransformResult:
        if boundary == "member-result" and ctx.shutdown_event is event:
            event.set()
        if member_result == "filtered":
            return TransformResult.success_empty(success_reason={"action": "transformed"})
        return TransformResult.error({"reason": "deliberate_failure"}, retryable=False)

    def collector_must_not_run(self: BatchStats, rows: list[PipelineRow], ctx: PluginContext) -> TransformResult:
        calls.append(ctx.run_id)
        pytest.fail("a loss-only collector group must close without a plugin")

    def record_notification(self: CollectorExecutor, name: str, group_id: str, member_key: str, reason: str, ctx: PluginContext):
        notifications.append(ctx.run_id)
        return original_notify(self, name, group_id, member_key, reason, ctx)

    # Model a conforming filtering transform for the successful-drop control.
    # Both runs use the same declared plugin capabilities and real loss writer.
    monkeypatch.setattr(ValueTransform, "can_drop_rows", member_result == "filtered")
    monkeypatch.setattr(ValueTransform, "process", lose_member)
    monkeypatch.setattr(BatchStats, "process", collector_must_not_run)
    monkeypatch.setattr(CollectorExecutor, "notify_member_lost", record_notification)
    body = _COLLECTOR_ARM.replace("policy: require_all", f"policy: {policy}")
    control = run_pipeline(build_pipeline(tmp_path / "control", body, [{"id": 1, "items": [1]}]))
    env = build_pipeline(tmp_path / "cancelled", body, [{"id": 1, "items": [1]}])
    if boundary == "loss-adoption":
        original_adopt = TokenSchedulerRepository.adopt_group_losses

        def cancel_adoption(self: TokenSchedulerRepository, **kwargs: Any):
            outcome = original_adopt(self, **kwargs)
            event.set()
            return outcome

        monkeypatch.setattr(TokenSchedulerRepository, "adopt_group_losses", cancel_adoption)
    catalog_sha256, catalog_source = env["catalog"]
    with pytest.raises(GracefulShutdownError):
        env["orchestrator"].run(
            env["config"],
            graph=env["graph"],
            settings=env["settings"],
            payload_store=env["payload_store"],
            openrouter_catalog_sha256=catalog_sha256,
            openrouter_catalog_source=catalog_source,
            shutdown_event=event,
        )
    with env["db"].connection() as conn:
        run_id = conn.execute(select(runs_table.c.run_id)).scalar_one()
        assert conn.execute(select(runs_table.c.status)).scalar_one() == "interrupted"
        loss_epoch = conn.execute(select(group_losses_table.c.adopted_epoch)).scalar_one()
        assert (loss_epoch is None) is (boundary == "member-result")
        failures = conn.execute(select(collector_group_failures_table.c.failure_reason)).scalars().all()
        assert failures == (["collector_missing_members"] if boundary == "loss-adoption" and policy == "require_all" else [])
        assert (
            conn.execute(
                select(token_work_items_table.c.work_item_id).where(
                    token_work_items_table.c.status.in_(["ready", "leased", "blocked", "pending_sink"])
                )
            ).all()
            == []
        )

    resumed = resume_pipeline(env)
    expected = control.to_dict()
    actual = resumed.to_dict()
    expected.pop("run_id")
    actual.pop("run_id")
    assert actual == expected
    assert resumed.collector_groups_failed == int(policy == "require_all")
    assert calls == []
    assert notifications.count(run_id) == 1
    with env["db"].connection() as conn:
        assert conn.execute(select(group_losses_table.c.adopted_epoch).where(group_losses_table.c.adopted_epoch.is_(None))).all() == []
        assert conn.execute(select(collector_group_failures_table.c.failure_reason)).scalars().all() == (
            ["collector_missing_members"] if policy == "require_all" else []
        )
