"""Ordinary settings/run regression for a coalesce nested in a scope and fork."""

from __future__ import annotations

import json
import threading
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import select, update

from elspeth.config_loading import load_settings_from_yaml_string
from elspeth.contracts import PipelineRow
from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
from elspeth.contracts.enums import FrameKind, NodeStateStatus, RunStatus, TerminalOutcome
from elspeth.contracts.errors import GracefulShutdownError
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.contracts.sink_effects import SinkEffectExecutionPurpose, SinkEffectInputKind
from elspeth.core.checkpoint import CheckpointManager, RecoveryManager
from elspeth.core.config import CheckpointSettings, ElspethSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import (
    coalesce_effects_table,
    group_losses_table,
    group_records_table,
    node_states_table,
    runs_table,
    token_outcomes_table,
    token_work_items_table,
    tokens_table,
)
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.clock import MockClock
from elspeth.engine.coalesce_executor import CoalesceExecutor
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.orchestrator.preflight import (
    assemble_and_validate_pipeline_config,
    execution_sink_bindings_for_runtime,
    execution_sinks_for_runtime,
    sink_effect_modes_from_runtime_bindings,
    validate_pipeline_sink_effect_capabilities,
)
from elspeth.engine.processor import RowProcessor
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.infrastructure.runtime_factory import instantiate_plugins_from_config
from elspeth.plugins.sources.json_source import JSONSource
from elspeth.plugins.transforms.batch_stats import BatchStats
from elspeth.plugins.transforms.json_explode import JSONExplode
from elspeth.plugins.transforms.llm.model_catalog import read_openrouter_catalog_snapshot_id
from elspeth.plugins.transforms.value_transform import ValueTransform

_SETTINGS_YAML = """
sources:
  docs:
    plugin: json
    on_success: outer_input
    options:
      path: {input_path}
      format: jsonl
      snapshot_for_resume: true
      on_validation_failure: discard
      schema: {{mode: observed}}
concurrency:
  max_workers: 1
gates:
  - name: outer_fork
    input: outer_input
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [scope_path, bypass]
  - name: inner_fork
    input: exploded
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [inner_left, inner_right]
transforms:
  - name: explode
    plugin: json_explode
    input: scope_path
    on_success: exploded
    on_error: discard
    options:
      array_field: items
      output_field: item
      schema: {{mode: observed}}
  - name: read_left
    plugin: value_transform
    input: inner_left
    on_success: left_done
    on_error: discard
    options:
      schema: {{mode: observed}}
      operations:
        - target: item
          expression: "row['item'] + 0"
  - name: read_right
    plugin: value_transform
    input: inner_right
    on_success: right_done
    on_error: discard
    options:
      schema: {{mode: observed}}
      operations:
        - target: item
          expression: "row['item'] + 0"
  - name: read_bypass
    plugin: value_transform
    input: bypass
    on_success: bypass_done
    on_error: discard
    options:
      schema: {{mode: observed}}
      operations:
        - target: id
          expression: "row['id'] + 0"
coalesce:
  - name: inner_merge
    branches: {{inner_left: left_done, inner_right: right_done}}
    policy: require_all
    merge: select
    select_branch: inner_left
  - name: outer_merge
    branches: {{scope_path: scope_done, bypass: bypass_done}}
    policy: require_all
    merge: select
    select_branch: scope_path
    on_success: out
collectors:
  - name: page_stitcher
    plugin: batch_stats
    input: inner_merge
    on_success: scope_done
    options:
      value_field: item
      schema: {{mode: observed}}
scopes:
  - name: document_pages
    opener: explode
    closer: page_stitcher
    policy: require_all
sinks:
  out:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      format: jsonl
      schema: {{mode: observed}}
"""

_DOCUMENTS = [{"id": 1, "items": [3, 1, 2]}, {"id": 2, "items": [5, 7]}]


def _build_pipeline(tmp_path: Path, *, settings: ElspethSettings | None = None, db: LandscapeDB | None = None) -> dict[str, Any]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    output_path = tmp_path / "output.jsonl"
    if settings is None:
        input_path = tmp_path / "docs.jsonl"
        input_path.write_text("\n".join(json.dumps(doc) for doc in _DOCUMENTS) + "\n")
        settings = load_settings_from_yaml_string(_SETTINGS_YAML.format(input_path=input_path, output_path=output_path))
    bundle = instantiate_plugins_from_config(settings, preflight_mode=True, sink_effect_purpose=SinkEffectExecutionPurpose.FRESH)
    sinks = execution_sinks_for_runtime(settings, bundle.sinks)
    bindings = execution_sink_bindings_for_runtime(settings, bundle.sink_effect_bindings)
    modes = sink_effect_modes_from_runtime_bindings(
        sinks,
        bindings,
        purpose=SinkEffectExecutionPurpose.FRESH,
        configured_options={name: settings.sinks[name].options for name in sinks},
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
        coalesce_settings=list(settings.coalesce),
        collectors=bundle.collectors,
        scope_settings=list(settings.scopes),
        max_bound_region_depth=settings.max_bound_region_depth,
    )
    graph.validate()
    graph.validate_edge_compatibility()
    assert {(r.binding.opener_name, r.binding.closer_name, r.depth) for r in graph.get_bound_regions()} == {
        ("outer_fork", "outer_merge", 1),
        ("explode", "page_stitcher", 2),
        ("inner_fork", "inner_merge", 3),
    }
    config = assemble_and_validate_pipeline_config(
        sources=bundle.sources,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        settings=settings,
        graph=graph,
        sink_effect_modes=modes,
        sink_effect_admission=admission,
    )
    if db is None:
        db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    checkpoints = CheckpointManager(db)
    return {
        "config": config,
        "graph": graph,
        "settings": settings,
        "db": db,
        "checkpoints": checkpoints,
        "orchestrator": Orchestrator(
            db,
            checkpoint_manager=checkpoints,
            checkpoint_config=RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row")),
        ),
        "payload_store": FilesystemPayloadStore(tmp_path / "payloads"),
        "output_path": output_path,
    }


def _run(env: dict[str, Any], *, shutdown_event: threading.Event | None = None) -> Any:
    catalog_hash, catalog_source = read_openrouter_catalog_snapshot_id()
    return env["orchestrator"].run(
        env["config"],
        graph=env["graph"],
        settings=env["settings"],
        payload_store=env["payload_store"],
        openrouter_catalog_sha256=catalog_hash,
        openrouter_catalog_source=catalog_source,
        shutdown_event=shutdown_event,
    )


def _inner_continuations(env: dict[str, Any]) -> list[Any]:
    node = next(b.closer_node_id for b in env["graph"].get_group_bindings().bindings if b.closer_name == "inner_merge")
    with env["db"].connection() as conn:
        return list(
            conn.execute(
                select(token_work_items_table)
                .join(coalesce_effects_table, coalesce_effects_table.c.result_token_id == token_work_items_table.c.token_id)
                .where(coalesce_effects_table.c.coalesce_node_id == str(node))
            ).all()
        )


def _assert_inner_cursors(rows: list[Any]) -> None:
    assert rows
    for row in rows:
        assert (row.collector_name, row.coalesce_name, row.row_union_name, row.coalesce_node_id) == ("page_stitcher", None, None, None)
        assert [f[0] for f in json.loads(row.lineage_path_json)] == ["fork", "expand"]


def _assert_completed(env: dict[str, Any], result: Any, *, expected_status: RunStatus = RunStatus.COMPLETED) -> list[dict[str, Any]]:
    assert result.status == expected_status
    rows = [json.loads(line) for line in env["output_path"].read_text().splitlines()]
    assert [(row["count"], row["sum"]) for row in rows] == [(3, 6), (2, 12)]
    continuations = _inner_continuations(env)
    assert len(continuations) == sum(len(doc["items"]) for doc in _DOCUMENTS)
    _assert_inner_cursors(continuations)
    with env["db"].connection() as conn:
        active = conn.execute(
            select(token_work_items_table.c.work_item_id).where(
                token_work_items_table.c.status.in_(
                    [
                        s.value
                        for s in (TokenWorkStatus.READY, TokenWorkStatus.LEASED, TokenWorkStatus.BLOCKED, TokenWorkStatus.PENDING_SINK)
                    ]
                )
            )
        ).all()
        assert active == []
        assert (
            conn.execute(
                select(node_states_table.c.state_id).where(node_states_table.c.status.in_([NodeStateStatus.OPEN, NodeStateStatus.PENDING]))
            ).all()
            == []
        )
        minted = set(conn.execute(select(tokens_table.c.token_id)).scalars())
        completed = set(conn.execute(select(token_outcomes_table.c.token_id).where(token_outcomes_table.c.completed == 1)).scalars())
        assert minted == completed
        groups = conn.execute(select(group_records_table).where(group_records_table.c.kind == FrameKind.EXPAND.value)).all()
        scopes = {g.group_id for g in groups if g.closes_group_id is None}
        closed = {g.closes_group_id for g in groups if g.closes_group_id is not None}
        assert len(scopes) == len(_DOCUMENTS)
        assert scopes == closed
    return rows


def test_inner_merge_closes_scope_before_outer_fork(tmp_path: Path) -> None:
    env = _build_pipeline(tmp_path)
    try:
        _assert_completed(env, _run(env))
    finally:
        env["db"].close()


def test_cold_public_resume_preserves_nearest_cursor_before_any_collector_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_complete = TokenSchedulerRepository.complete_barrier
    real_value = ValueTransform.process
    real_explode = JSONExplode.process
    real_batch = BatchStats.process
    real_load = JSONSource.load
    real_rederive = RowProcessor._rederive_expand_binding

    def execute(root: Path, cancel: bool) -> dict[str, Any]:
        env = _build_pipeline(root)
        event = threading.Event()
        interrupted = False
        phase = "run"
        calls: list[tuple[str, str, int, int | None]] = []
        states: list[str] = []
        batches: list[list[int]] = []
        rederived: list[tuple[str, str | None]] = []

        def complete(self: TokenSchedulerRepository, **kwargs: Any):
            nonlocal interrupted
            result = real_complete(self, **kwargs)
            if cancel and not interrupted and kwargs["barrier_key"] == "inner_merge" and kwargs["emitted_ready"]:
                interrupted = True
                event.set()
            return result

        def record_row(kind: str, row: PipelineRow, ctx: PluginContext) -> None:
            assert ctx.node_id is not None and ctx.state_id is not None
            calls.append((kind, ctx.node_id, row["id"], row["item"] if "item" in row else None))
            states.append(ctx.state_id)

        def value(self: ValueTransform, row: PipelineRow, ctx: PluginContext) -> TransformResult:
            record_row("value", row, ctx)
            return real_value(self, row, ctx)

        def explode(self: JSONExplode, row: PipelineRow, ctx: PluginContext) -> TransformResult:
            record_row("explode", row, ctx)
            return real_explode(self, row, ctx)

        def batch(self: BatchStats, rows: list[PipelineRow], ctx: PluginContext) -> TransformResult:
            batches.append([row["item"] for row in rows])
            return real_batch(self, rows, ctx)

        def load(self: JSONSource, ctx: PluginContext):
            assert phase != "resume", "public resume must restore the sealed source snapshot"
            return real_load(self, ctx)

        def rederive(self: RowProcessor, frame: LineageFrame):
            result = real_rederive(self, frame)
            if phase == "resume":
                rederived.append((frame.group_id, result.closer_name if result is not None else None))
            return result

        try:
            with monkeypatch.context() as patcher:
                patcher.setattr(TokenSchedulerRepository, "complete_barrier", complete)
                patcher.setattr(ValueTransform, "process", value)
                patcher.setattr(JSONExplode, "process", explode)
                patcher.setattr(BatchStats, "process", batch)
                patcher.setattr(JSONSource, "load", load)
                patcher.setattr(RowProcessor, "_rederive_expand_binding", rederive)
                if cancel:
                    with pytest.raises(GracefulShutdownError):
                        _run(env, shutdown_event=event)
                    assert interrupted and batches == []
                    published = _inner_continuations(env)
                    _assert_inner_cursors(published)
                    scope = json.loads(published[0].lineage_path_json)[1]
                    frame = LineageFrame(kind=FrameKind.EXPAND, group_id=scope[1], member_key=scope[2])
                    with env["db"].connection() as conn:
                        assert (
                            conn.execute(
                                select(group_records_table.c.group_id).where(group_records_table.c.closes_group_id.is_not(None))
                            ).all()
                            == []
                        )
                    phase = "resume"
                    rebuilt = _build_pipeline(root, settings=env["settings"], db=env["db"])
                    assert rebuilt["graph"].get_group_bindings().binding_for(frame) is None
                    with env["db"].connection() as conn:
                        run_id = conn.execute(select(runs_table.c.run_id)).scalar_one()
                    recovery = RecoveryManager(env["db"], rebuilt["checkpoints"])
                    check = recovery.can_resume(run_id, rebuilt["graph"])
                    assert check.can_resume, check.reason
                    result = rebuilt["orchestrator"].resume(
                        resume_point=recovery.get_resume_point(run_id, rebuilt["graph"]),
                        config=rebuilt["config"],
                        graph=rebuilt["graph"],
                        settings=rebuilt["settings"],
                        payload_store=rebuilt["payload_store"],
                    )
                    assert (frame.group_id, "page_stitcher") in rederived
                    env = rebuilt
                else:
                    result = _run(env)
            rows = _assert_completed(env, result)
            assert len(states) == len(set(states)), "a completed transform state was executed twice"
            return {
                "result": {k: v for k, v in result.to_dict().items() if k != "run_id"},
                "rows": rows,
                "calls": Counter(calls),
                "batches": batches,
            }
        finally:
            env["db"].close()

    assert execute(tmp_path / "resumed", True) == execute(tmp_path / "uninterrupted", False)


def test_live_loss_merge_closes_scope_before_outer_fork(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    input_path = tmp_path / "docs.jsonl"
    input_path.write_text("\n".join(json.dumps(doc) for doc in _DOCUMENTS) + "\n")
    raw = yaml.safe_load(_SETTINGS_YAML.format(input_path=input_path, output_path=tmp_path / "output.jsonl"))
    raw["coalesce"][0].update(policy="best_effort", timeout_seconds=60)
    right = next(t for t in raw["transforms"] if t["name"] == "read_right")
    right["options"]["operations"][0]["expression"] = "row['item'] / 0"
    settings = load_settings_from_yaml_string(yaml.safe_dump(raw))
    env = _build_pipeline(tmp_path, settings=settings)
    env["orchestrator"] = Orchestrator(
        env["db"],
        checkpoint_manager=env["checkpoints"],
        checkpoint_config=RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row")),
        clock=MockClock(start=1_750_000_000.0),
    )
    real_claim = TokenSchedulerRepository.claim_ready
    real_loss = CoalesceExecutor.notify_branch_lost
    merged: list[str] = []
    right_members: set[tuple[str, str]] = set()
    later = datetime.now(UTC) + timedelta(days=1)

    def claim(self: TokenSchedulerRepository, **kwargs: Any):
        # Control the tie order of genuine READY work so accepted left
        # arrivals are held before the real right-branch expression errors.
        with env["db"].engine.begin() as conn:
            ready = conn.execute(
                select(
                    token_work_items_table.c.work_item_id, token_work_items_table.c.token_id, token_work_items_table.c.lineage_path_json
                ).where(token_work_items_table.c.status == "ready")
            ).all()
            for work_id, token_id, lineage in ready:
                for kind, group, member in json.loads(lineage):
                    if kind == "fork" and member == "inner_right":
                        right_members.add((group, token_id))
                        conn.execute(
                            update(token_work_items_table).where(token_work_items_table.c.work_item_id == work_id).values(created_at=later)
                        )
        return real_claim(self, **kwargs)

    def lost(self: CoalesceExecutor, *args: Any, **kwargs: Any):
        outcome = real_loss(self, *args, **kwargs)
        if outcome is not None and outcome.merged_token is not None:
            merged.append(outcome.merged_token.token_id)
        return outcome

    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(TokenSchedulerRepository, "claim_ready", claim)
            patcher.setattr(CoalesceExecutor, "notify_branch_lost", lost)
            result = _run(env)
        _assert_completed(env, result, expected_status=RunStatus.COMPLETED_WITH_FAILURES)
        assert len(merged) == len(set(merged)) == sum(len(doc["items"]) for doc in _DOCUMENTS)
        with env["db"].connection() as conn:
            losses = conn.execute(select(group_losses_table)).all()
            effects = conn.execute(
                select(coalesce_effects_table).where(coalesce_effects_table.c.group_id.in_([g for g, _t in right_members]))
            ).all()
            failed = set(
                conn.execute(
                    select(token_outcomes_table.c.token_id).where(token_outcomes_table.c.outcome == TerminalOutcome.FAILURE.value)
                ).scalars()
            )
        assert [(r.closer_name, r.member_key) for r in losses] == [("inner_merge", "inner_right")] * len(merged)
        assert {(r.group_id, r.token_id) for r in losses} == right_members
        assert {e.result_token_id for e in effects} == set(merged)
        assert failed == {t for _g, t in right_members}
        assert result.rows_failed == len(right_members)
    finally:
        env["db"].close()
