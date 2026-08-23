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
"""

from __future__ import annotations

import json
from pathlib import Path

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
    input_csv = tmp_path / "input.csv"
    input_csv.write_text(_INPUT_CSV)
    output_path = tmp_path / "output.jsonl"

    settings_yaml = _SETTINGS_YAML_TEMPLATE.format(input_path=input_csv, output_path=output_path)
    settings = load_settings_from_yaml_string(settings_yaml)
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

    # E1a's precise crash site, isolated: build_dag_traversal_context calls
    # this eagerly, before any row is read.
    branch_first_nodes = graph.get_branch_first_nodes()
    assert set(branch_first_nodes) == {"outer_a", "outer_b", "inner_a1", "inner_a2"}

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

    result_data = result.to_dict()
    assert result_data["status"] == "completed"
    assert result_data["rows_processed"] == 3
    assert result_data["rows_succeeded"] == 3
    assert result_data["rows_failed"] == 0
    assert result_data["rows_coalesce_failed"] == 0

    output_rows = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert output_rows == list(_EXPECTED_ROWS)
