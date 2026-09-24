"""A barrier's durable hold is the token AS IT ARRIVED (elspeth-5887fb7928 AC-R4).

A claim runs every node it can reach before it blocks, so a token that crosses
a transform on its way to a barrier arrives carrying that transform's output,
not the row it was enqueued with. The BLOCKED journal row is what every
barrier restore rebuilds the member from (resume, takeover, follower
hand-off). It used to keep the READY-time row, so a resumed flush was handed
rows without the transform's field. The member then failed the batch's own
input preflight ("required input field(s) ['n'] absent"), and the plugin was
never re-invoked. That turned a transient fault into a permanent, wrong verdict.

The shape is a transform that adds ``n``, then a batch_stats barrier over ``n``,
whose FIRST ``process`` call raises an ordinary exception (a transient fault:
the run aborts, as any non-contract plugin exception at a flush does) and
whose later calls work. The collector arm leaves its members' accept-time holds
OPEN; resume requires that state (``CollectorJournalRestorer`` refuses a live
member without an OPEN hold) and re-flushes the group. The aggregation arm marks
the batch FAILED without a verdict, and resume retries it. Either way the
resumed flush must see exactly the rows the first call saw, and the run must end
exactly as a run with no fault does.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from elspeth.config_loading import load_settings_from_yaml_string
from elspeth.contracts import Determinism, PipelineRow
from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
from elspeth.contracts.enums import NodeStateStatus, RunStatus
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.contracts.sink_effects import SinkEffectExecutionPurpose, SinkEffectInputKind
from elspeth.core.checkpoint import CheckpointManager, RecoveryManager
from elspeth.core.config import CheckpointSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import node_states_table, runs_table, token_outcomes_table, token_work_items_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.orchestrator.preflight import (
    assemble_and_validate_pipeline_config,
    execution_sink_bindings_for_runtime,
    execution_sinks_for_runtime,
    sink_effect_modes_from_runtime_bindings,
    validate_pipeline_sink_effect_capabilities,
)
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.infrastructure.runtime_factory import instantiate_plugins_from_config
from elspeth.plugins.transforms.batch_stats import BatchStats
from elspeth.plugins.transforms.llm.model_catalog import read_openrouter_catalog_snapshot_id

_SOURCE_AND_SINK = """
sources:
  docs:
    plugin: json
    on_success: buffered
    options:
      path: {input_path}
      format: jsonl
      on_validation_failure: discard
      schema: {{mode: observed}}
concurrency:
  max_workers: 1
sinks:
  out:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      format: jsonl
      schema: {{mode: observed}}
"""

# The EOF buffer runs the scope's flushes after the source is exhausted, which
# is what makes the aborted run resumable (resume refuses an incomplete source).
_COLLECTOR_ARM = """
aggregations:
  - name: eof_buffer
    plugin: batch_replicate
    input: buffered
    on_success: rows
    on_error: discard
    trigger: {{count: 100}}
    output_mode: passthrough
    options:
      include_copy_index: false
      schema: {{mode: observed}}
transforms:
  - name: explode
    plugin: json_explode
    input: rows
    on_success: page_in
    on_error: discard
    options:
      array_field: items
      output_field: item
      schema: {{mode: observed}}
  - name: lift
    plugin: value_transform
    input: page_in
    on_success: pages
    on_error: discard
    options:
      schema: {{mode: observed}}
      operations:
        - target: n
          expression: "row['item'] * 10"
collectors:
  - name: stitch
    plugin: batch_stats
    input: pages
    on_success: out
    options:
      value_field: n
      schema: {{mode: observed}}
scopes:
  - name: document_pages
    opener: explode
    closer: stitch
    policy: require_all
"""

_AGGREGATION_ARM = """
transforms:
  - name: lift
    plugin: value_transform
    input: buffered
    on_success: lifted
    on_error: discard
    options:
      schema: {{mode: observed}}
      operations:
        - target: n
          expression: "row['v'] * 10"
aggregations:
  - name: summarise
    plugin: batch_stats
    input: lifted
    on_success: out
    on_error: discard
    trigger: {{count: 100}}
    options:
      value_field: n
      schema: {{mode: observed}}
"""

_ARMS = {
    "collector": (_COLLECTOR_ARM, [{"id": 1, "items": [3, 1, 2]}], "collector_stitch"),
    "aggregation": (_AGGREGATION_ARM, [{"id": i, "v": i} for i in (3, 1, 2)], "aggregation_summarise"),
}


# Captured once, so a second patch in the same test wraps the real plugin, not the first wrapper.
_REAL_BATCH_STATS_PROCESS = BatchStats.process


class _TransientFlushFault(RuntimeError):
    """An ordinary plugin exception: not Tier-1, not a contract violation, so the flush aborts the run."""


def build_pipeline(tmp_path: Path, body_yaml: str, docs: list[dict[str, Any]], *, db: LandscapeDB | None = None) -> dict[str, Any]:
    """Build a checkpointed json-source/json-sink pipeline through the production build path.

    ``body_yaml`` is the processing section (a format string over nothing but
    literal braces). The audit database is SQLite under ``tmp_path`` unless
    ``db`` (a PostgreSQL proof) is given. Shared with
    ``test_collector_failure_verdict.py`` and its PostgreSQL twin.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    input_path = tmp_path / "docs.jsonl"
    input_path.write_text("\n".join(json.dumps(doc) for doc in docs) + "\n")
    output_path = tmp_path / "out.jsonl"
    settings = load_settings_from_yaml_string((_SOURCE_AND_SINK + body_yaml).format(input_path=input_path, output_path=output_path))
    bundle = instantiate_plugins_from_config(settings, preflight_mode=True, sink_effect_purpose=SinkEffectExecutionPurpose.FRESH)
    sinks = execution_sinks_for_runtime(settings, bundle.sinks)
    modes = sink_effect_modes_from_runtime_bindings(
        sinks,
        execution_sink_bindings_for_runtime(settings, bundle.sink_effect_bindings),
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
        collectors=bundle.collectors,
        scope_settings=list(settings.scopes),
        max_bound_region_depth=settings.max_bound_region_depth,
    )
    graph.validate()
    graph.validate_edge_compatibility()
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
    catalog_sha256, catalog_source = read_openrouter_catalog_snapshot_id()
    return {
        "db": db,
        "config": config,
        "graph": graph,
        "settings": settings,
        "checkpoints": checkpoints,
        "orchestrator": Orchestrator(
            db,
            checkpoint_manager=checkpoints,
            checkpoint_config=RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row")),
        ),
        "payload_store": FilesystemPayloadStore(tmp_path / "payloads"),
        "catalog": (catalog_sha256, catalog_source),
        "output_path": output_path,
    }


def _build(tmp_path: Path, arm: str) -> dict[str, Any]:
    arm_yaml, docs, _prefix = _ARMS[arm]
    return build_pipeline(tmp_path, arm_yaml, docs)


def run_pipeline(env: dict[str, Any]) -> Any:
    catalog_sha256, catalog_source = env["catalog"]
    return env["orchestrator"].run(
        env["config"],
        graph=env["graph"],
        settings=env["settings"],
        payload_store=env["payload_store"],
        openrouter_catalog_sha256=catalog_sha256,
        openrouter_catalog_source=catalog_source,
    )


def resume_pipeline(env: dict[str, Any]) -> Any:
    """Resume the run in ``env`` through RecoveryManager's admission and the production resume path."""
    with env["db"].connection() as conn:
        run_id = conn.execute(select(runs_table.c.run_id)).scalar_one()
    recovery = RecoveryManager(env["db"], env["checkpoints"])
    check = recovery.can_resume(run_id, env["graph"])
    assert check.can_resume, check.reason
    return env["orchestrator"].resume(
        resume_point=recovery.get_resume_point(run_id, env["graph"]),
        config=env["config"],
        graph=env["graph"],
        settings=env["settings"],
        payload_store=env["payload_store"],
    )


def terminal_counts(db: LandscapeDB) -> list[tuple[str, str, int]]:
    with db.connection() as conn:
        rows = conn.execute(
            select(token_outcomes_table.c.outcome, token_outcomes_table.c.path).where(token_outcomes_table.c.completed == 1)
        ).all()
    counts: dict[tuple[str, str], int] = {}
    for outcome, path in rows:
        counts[(outcome, path)] = counts.get((outcome, path), 0) + 1
    return sorted((outcome, path, n) for (outcome, path), n in counts.items())


def _recording_batch_stats(monkeypatch: pytest.MonkeyPatch, *, fail_first_call: bool) -> list[list[dict[str, Any]]]:
    """Record every flush's rows; optionally raise on the first call only."""
    # The fault makes the plugin's behaviour call-dependent, so it is declared so:
    # resume re-invokes it by design rather than by accident.
    monkeypatch.setattr(BatchStats, "determinism", Determinism.NON_DETERMINISTIC)
    calls: list[list[dict[str, Any]]] = []

    def process(self: BatchStats, rows: list[PipelineRow], ctx: PluginContext) -> TransformResult:
        calls.append([row.to_dict() for row in rows])
        if fail_first_call and len(calls) == 1:
            raise _TransientFlushFault("transient fault in the barrier plugin")
        return _REAL_BATCH_STATS_PROCESS(self, rows, ctx)

    monkeypatch.setattr(BatchStats, "process", process)
    return calls


@pytest.mark.parametrize("arm", sorted(_ARMS))
def test_resume_after_a_flush_fault_hands_the_barrier_the_rows_it_received(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str
) -> None:
    control_env = _build(tmp_path / "control", arm)
    control_calls = _recording_batch_stats(monkeypatch, fail_first_call=False)
    control = run_pipeline(control_env)
    control_output = control_env["output_path"].read_text()
    assert control.status is RunStatus.COMPLETED
    assert [row["n"] for row in control_calls[0]] == [30, 10, 20]  # the transform's field reached the barrier

    env = _build(tmp_path / "faulted", arm)
    calls = _recording_batch_stats(monkeypatch, fail_first_call=True)
    with pytest.raises(_TransientFlushFault):
        run_pipeline(env)

    # The aborted state: every held member's durable row is the row that
    # reached the barrier, including the transform's field.
    with env["db"].connection() as conn:
        held_rows = [
            json.loads(payload)["row"]["data"]
            for (payload,) in conn.execute(
                select(token_work_items_table.c.row_payload_json).where(token_work_items_table.c.status == TokenWorkStatus.BLOCKED.value)
            ).all()
        ]
        barrier_state_statuses = sorted(
            status
            for (status,) in conn.execute(
                select(node_states_table.c.status).where(node_states_table.c.node_id.like(f"{_ARMS[arm][2]}%"))
            ).all()
        )
    assert sorted(held_rows, key=lambda row: row["n"]) == sorted(calls[0], key=lambda row: row["n"])
    if arm == "collector":
        # The flush guard failed; the three accept-time holds stay OPEN for resume.
        assert barrier_state_statuses == [NodeStateStatus.FAILED.value] + [NodeStateStatus.OPEN.value] * 3
    else:
        assert barrier_state_statuses == [NodeStateStatus.FAILED.value]

    resumed = resume_pipeline(env)

    assert len(calls) == 2
    assert calls[1] == calls[0], "the resumed flush must see exactly the rows the first flush saw"
    assert resumed.status is RunStatus.COMPLETED
    assert env["output_path"].read_text() == control_output
    assert terminal_counts(env["db"]) == terminal_counts(control_env["db"])
