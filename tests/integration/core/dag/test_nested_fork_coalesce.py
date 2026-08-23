"""Nested fork-in-fork (coalesce-feeds-coalesce) runs end-to-end (elspeth-0bd2cde19a, E1).

Deliberately test-owned, NOT under tests/fixtures/dag_scenario_corpus/v1/: that
directory's contract test (tests/unit/architecture/test_dag_scenario_corpus_contract.py)
carries exact-inventory pins keyed to the manifest, and wiring the corpus's full
witness (schema.py EXPECTED_SCENARIOS, manifest.yaml evidence, SHA rotations) is
WS1a Task 8a's own ledger item, not this ticket's. This test proves the two
underlying engine defects the walker/coalesce-completion fix (graph.py's
_is_nested_barrier_branch/_resolve_nested_branch_first_node,
work_items.py's resolve_merged_branch_barrier) closes: the fixture and expected
output are the verbatim shape from Task 8a's preserved fixtures
(.superpowers/sdd/2026-08-21-unified-lineage-ws1a-model-core/task-8a-fixtures/).

Round-2 review (elspeth-0bd2cde19a, ws2-e1-review, 2026-08-23) added two more
end-to-end cases in this file: a LOSS-triggered nested merge (F1 — a THIRD
merge-release path the original fix missed, distinct from the arrival-fire
path the first test above exercises) and a nested region inside an unbound
consumer-fed outer branch (F3 — resolve_merged_branch_barrier's ``(None,
None)`` arm, reachable since E2 made consumer-fed outer branches authorable).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts.sink_effects import SinkEffectExecutionPurpose, SinkEffectInputKind
from elspeth.core.config import load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.orchestrator.preflight import (
    assemble_and_validate_pipeline_config,
    execution_sink_bindings_for_runtime,
    execution_sinks_for_runtime,
    sink_effect_modes_from_runtime_bindings,
    validate_pipeline_sink_effect_capabilities,
)
from elspeth.plugins.infrastructure.runtime_factory import instantiate_plugins_from_config
from elspeth.plugins.transforms.llm.model_catalog import read_openrouter_catalog_snapshot_id

_INPUT_CSV = "id,value\n1,10\n2,20\n3,30\n"


def _build_and_run(
    settings_yaml: str, tmp_path: Path, *, input_csv: str = _INPUT_CSV
) -> tuple[ExecutionGraph, dict[str, Any], list[dict[str, Any]]]:
    """Build+run a settings YAML through the real production path.

    Shared by every case in this file: build via ExecutionGraph.from_plugin_instances,
    run via the real Orchestrator, return (graph, result_dict, output_rows).
    """
    input_csv_path = tmp_path / "input.csv"
    input_csv_path.write_text(input_csv)
    output_path = tmp_path / "output.jsonl"

    formatted = settings_yaml.format(input_path=input_csv_path, output_path=output_path)
    settings = load_settings_from_yaml_string(formatted)
    bundle = instantiate_plugins_from_config(settings, preflight_mode=True, sink_effect_purpose=SinkEffectExecutionPurpose.FRESH)

    execution_sinks = execution_sinks_for_runtime(settings, bundle.sinks)
    execution_bindings = execution_sink_bindings_for_runtime(settings, bundle.sink_effect_bindings)
    sink_effect_modes = sink_effect_modes_from_runtime_bindings(
        execution_sinks,
        execution_bindings,
        purpose=SinkEffectExecutionPurpose.FRESH,
        configured_options={name: settings.sinks[name].options for name in execution_sinks},
    )
    sink_effect_admission = validate_pipeline_sink_effect_capabilities(
        execution_sinks,
        configured_modes=sink_effect_modes,
        required_input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
    )

    graph = ExecutionGraph.from_plugin_instances(
        sources=bundle.sources,
        source_settings_map=bundle.source_settings_map,
        transforms=bundle.transforms,
        sinks=execution_sinks,
        aggregations=bundle.aggregations,
        gates=list(settings.gates),
        coalesce_settings=list(settings.coalesce) if settings.coalesce else None,
        row_union_settings=list(settings.row_unions) if settings.row_unions else None,
        queues=settings.queues,
        collectors=bundle.collectors,
        scope_settings=list(settings.scopes) if settings.scopes else None,
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
        sink_effect_modes=sink_effect_modes,
        sink_effect_admission=sink_effect_admission,
    )

    catalog_sha256, catalog_source = read_openrouter_catalog_snapshot_id()
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    payload_store = FilesystemPayloadStore(tmp_path / "payloads")
    try:
        result = Orchestrator(db).run(
            config,
            graph=graph,
            settings=settings,
            payload_store=payload_store,
            openrouter_catalog_sha256=catalog_sha256,
            openrouter_catalog_source=catalog_source,
        )
    finally:
        db.close()

    output_rows = [json.loads(line) for line in output_path.read_text().splitlines()] if output_path.exists() else []
    return graph, result.to_dict(), output_rows


# Verbatim topology from the preserved WS1a Task 8a fixture
# (task-8a-fixtures/nested-fork-in-fork/depth2-require-all.yaml): outer_fork
# fans out to {outer_a, outer_b}; outer_a immediately feeds a SECOND, nested
# fork/coalesce region (inner_fork -> merge_inner) whose release is itself
# one of merge_outer's declared branches — the coalesce-feeds-coalesce shape
# E1a's walker fix and E1b's coalesce-completion routing fix both close.
_SETTINGS_YAML_TEMPLATE = """
sources:
  primary:
    plugin: csv
    on_success: outer_fork_input
    options:
      path: {input_path}
      on_validation_failure: discard
      schema: {{mode: fixed, fields: ["id: int", "value: int"]}}
concurrency:
  max_workers: 1
gates:
  - name: outer_fork
    input: outer_fork_input
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [outer_a, outer_b]
  - name: inner_fork
    input: outer_a
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [inner_a1, inner_a2]
coalesce:
  - name: merge_inner
    branches: {{inner_a1: inner_a1, inner_a2: inner_a2}}
    policy: require_all
    merge: nested
  - name: merge_outer
    branches: {{outer_a: merge_inner, outer_b: outer_b}}
    policy: require_all
    merge: nested
    on_success: output
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      format: jsonl
      schema: {{mode: observed}}
"""

_EXPECTED_ROWS = (
    {"outer_a": {"inner_a1": {"id": 1, "value": 10}, "inner_a2": {"id": 1, "value": 10}}, "outer_b": {"id": 1, "value": 10}},
    {"outer_a": {"inner_a1": {"id": 2, "value": 20}, "inner_a2": {"id": 2, "value": 20}}, "outer_b": {"id": 2, "value": 20}},
    {"outer_a": {"inner_a1": {"id": 3, "value": 30}, "inner_a2": {"id": 3, "value": 30}}, "outer_b": {"id": 3, "value": 30}},
)


def test_nested_fork_in_fork_builds_and_runs_to_completion(tmp_path: Path) -> None:
    """Coalesce-feeds-coalesce builds, runs to completion, and emits the exact merged rows.

    Regression (elspeth-0bd2cde19a):
    - E1a: merge_outer's 'outer_a' branch names merge_inner (another coalesce),
      not a literal transform — ExecutionGraph.get_branch_first_nodes() used
      to raise GraphValidationError at build_dag_traversal_context time,
      before any row was processed.
    - E1b: past E1a, merge_inner's completion released a continuation that
      re-held at merge_inner's own barrier instead of advancing to
      merge_outer (OrchestrationInvariantError: 'Token branch ... not in
      expected branches').
    """
    graph, result_data, output_rows = _build_and_run(_SETTINGS_YAML_TEMPLATE, tmp_path)

    # E1a's precise crash site, isolated: build_dag_traversal_context calls
    # this eagerly, before any row is read.
    branch_first_nodes = graph.get_branch_first_nodes()
    assert set(branch_first_nodes) == {"outer_a", "outer_b", "inner_a1", "inner_a2"}

    assert result_data["status"] == "completed"
    assert result_data["rows_processed"] == 3
    assert result_data["rows_succeeded"] == 3
    assert result_data["rows_failed"] == 0
    assert result_data["rows_coalesce_failed"] == 0
    assert output_rows == list(_EXPECTED_ROWS)


# ===== F1: loss-triggered nested merge (THIRD merge-release path) =====
#
# _notify_coalesce_of_lost_branch is a merge-release path distinct from
# _fire_coalesce_merge (live arrival) and complete_coalesce_merge
# (timeout/EOF sweep): under a partial-merge policy (quorum/best_effort/
# first), a branch LOSS can itself trigger CoalesceAction.MERGE, not only a
# branch's normal arrival. The round-1 fix left this path building the
# merged continuation with no barrier context at all, so a nested inner
# coalesce's loss-triggered release sailed past merge_outer instead of
# holding there — the ticket's original outcomes.py:80 diagnosis, verbatim,
# for THIS path (round-2 F1; the round-1 report's "Probe B is
# non-representative" correction covered the arrival-fire path above, not
# this one).
_LOSS_MERGE_SETTINGS_YAML_TEMPLATE = """
sources:
  primary:
    plugin: csv
    on_success: outer_fork_input
    options:
      path: {input_path}
      on_validation_failure: discard
      schema: {{mode: fixed, fields: ["id: int", "value: int"]}}
concurrency:
  max_workers: 1
gates:
  - name: outer_fork
    input: outer_fork_input
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [outer_a, outer_b]
  - name: inner_fork
    input: outer_a
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [inner_a1, inner_a2]
transforms:
  - name: mark_a1
    plugin: value_transform
    input: inner_a1
    on_success: in_a1
    on_error: discard
    options:
      schema: {{mode: observed}}
      operations: [{{target: branch_marker, expression: "'a1'"}}]
  - name: lose_a2
    plugin: dag_corpus_branch_loss
    input: inner_a2
    on_success: in_a2
    on_error: discard
    options:
      schema: {{mode: observed}}
coalesce:
  - name: merge_inner
    branches: {{inner_a1: in_a1, inner_a2: in_a2}}
    policy: best_effort
    timeout_seconds: 30
    merge: nested
  - name: merge_outer
    branches: {{outer_a: merge_inner, outer_b: outer_b}}
    policy: require_all
    merge: nested
    on_success: output
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      format: jsonl
      schema: {{mode: observed}}
"""


def test_loss_triggered_nested_merge_holds_at_the_enclosing_barrier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A branch-loss-triggered nested merge advances to merge_outer, not past it.

    Regression (elspeth-0bd2cde19a round-2 F1): merge_inner is best_effort;
    losing inner_a2 with inner_a1 already arrived is itself a merge trigger
    (RowProcessor._notify_coalesce_of_lost_branch -> CoalesceExecutor's
    quorum/best_effort/first evaluation can emit CoalesceAction.MERGE on a
    LOSS, not only an arrival). Before the fix this released continuation
    carried no barrier context and sailed past merge_outer, terminating
    against the source's own connection name
    (OrchestrationInvariantError: "Sink 'outer_fork_input' not in
    configured sinks"). After the fix it holds at merge_outer and merges
    correctly with outer_b once that arrives too.
    """
    from tests.fixtures.dag_scenario_corpus.plugins import install_corpus_plugin_manager

    install_corpus_plugin_manager(monkeypatch)

    _graph, result_data, output_rows = _build_and_run(_LOSS_MERGE_SETTINGS_YAML_TEMPLATE, tmp_path)

    assert result_data["status"] == "completed_with_failures"
    assert result_data["rows_processed"] == 3
    assert result_data["rows_succeeded"] == 3
    assert result_data["rows_failed"] == 3  # the lost inner_a2 branch, once per row
    assert result_data["rows_coalesce_failed"] == 0

    assert len(output_rows) == 3
    for row in output_rows:
        # inner_a2 was lost — merge_inner's nested output carries only inner_a1's
        # contribution, and that continuation correctly held at (and merged
        # with) merge_outer's outer_b branch, not the pre-fix bypass.
        assert set(row["outer_a"]) == {"inner_a1"}
        assert "outer_b" in row
        assert row["outer_a"]["inner_a1"]["branch_marker"] == "a1"


# ===== F3: nested region inside an unbound consumer-fed outer branch =====
#
# resolve_merged_branch_barrier's (None, None) arm — reachable since E2
# (elspeth-a01889580f's sibling ticket) made consumer-fed outer branches
# authorable: a fork branch bound to a coalesce whose merged output feeds an
# ORDINARY transform (spec §7 E2), not another barrier. The continuation
# carries no barrier context by design — distinct from both the flat-default
# return and a resolved barrier name.
_UNBOUND_OUTER_BRANCH_SETTINGS_YAML_TEMPLATE = """
sources:
  primary:
    plugin: csv
    on_success: outer_fork_input
    options:
      path: {input_path}
      on_validation_failure: discard
      schema: {{mode: fixed, fields: ["id: int", "value: int"]}}
concurrency:
  max_workers: 1
gates:
  - name: outer_fork
    input: outer_fork_input
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [outer_a, outer_b]
  - name: inner_fork
    input: outer_a
    condition: "True"
    routes: {{"true": fork, "false": discard}}
    fork_to: [inner_a1, inner_a2]
transforms:
  - name: after_inner
    plugin: value_transform
    input: merge_inner
    on_success: output
    on_error: discard
    options:
      schema: {{mode: observed}}
      operations: [{{target: tail, expression: "'inner'"}}]
  - name: tail_b
    plugin: value_transform
    input: outer_b
    on_success: output
    on_error: discard
    options:
      schema: {{mode: observed}}
      operations: [{{target: tail, expression: "'b'"}}]
coalesce:
  - name: merge_inner
    branches: {{inner_a1: inner_a1, inner_a2: inner_a2}}
    policy: require_all
    merge: nested
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      format: jsonl
      schema: {{mode: observed}}
"""


def test_nested_region_inside_unbound_outer_branch_completes(tmp_path: Path) -> None:
    """merge_inner's release feeds an ordinary transform, not an enclosing barrier.

    Regression (elspeth-0bd2cde19a round-2 F3): outer_a is a fork branch
    bound to merge_inner (a coalesce), but merge_inner's own output feeds
    'after_inner' — an ordinary transform, not another coalesce/row_union.
    resolve_merged_branch_barrier resolves (None, None) for this shape: no
    enclosing barrier to carry. Both outer_a's merged rows (via after_inner)
    and outer_b's rows (via tail_b) must reach the sink.
    """
    _graph, result_data, output_rows = _build_and_run(_UNBOUND_OUTER_BRANCH_SETTINGS_YAML_TEMPLATE, tmp_path)

    assert result_data["status"] == "completed"
    assert result_data["rows_processed"] == 3
    assert result_data["rows_succeeded"] == 6  # each row emits BOTH an outer_a and an outer_b tail row
    assert result_data["rows_failed"] == 0
    assert result_data["rows_coalesce_failed"] == 0

    inner_rows = [row for row in output_rows if row.get("tail") == "inner"]
    b_rows = [row for row in output_rows if row.get("tail") == "b"]
    assert len(inner_rows) == 3
    assert len(b_rows) == 3
    assert {row["inner_a1"]["id"] for row in inner_rows} == {1, 2, 3}
