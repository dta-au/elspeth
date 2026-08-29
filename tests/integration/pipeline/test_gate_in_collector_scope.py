"""Regression: a gate inside a collector-bound scope, closer terminal to a sink.

elspeth-b6a0a85a15 — this composer-authorable shape passed `elspeth validate`
and then fatally aborted at run: ``DagNavigator.resolve_jump_target_sink``'s
terminal arm recognised COALESCE only, so a gate route jump landing upstream
of a TERMINAL collector (its ``on_success`` names a sink) walked to the
barrier, found no next node and no coalesce, and raised
OrchestrationInvariantError on the first clean document. A collector is the
only barrier kind that is both terminal-capable and was missing from that arm
(a row_union's on_success must be a processing connection — builder-enforced).

The graph here is the ticket's repro: json_explode (scope opener) -> gate ->
value_transform -> batch_stats collector (scope closer, require_all) -> sink.
The run must COMPLETE with one stats row per document. The non-terminal
variant (collector feeding a downstream transform) always ran clean and is
covered by the control table on the ticket.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select

from elspeth.contracts.enums import RunStatus
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.contracts.sink_effects import SinkEffectExecutionPurpose, SinkEffectInputKind
from elspeth.core.config import load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import token_work_items_table
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

_SETTINGS_YAML = """
sources:
  docs:
    plugin: json
    on_success: rows
    options:
      path: {input_path}
      format: jsonl
      on_validation_failure: discard
      schema: {{mode: observed}}
concurrency:
  max_workers: 1
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
  - name: read_value
    plugin: value_transform
    input: gated
    on_success: pages
    on_error: discard
    options:
      schema: {{mode: observed}}
      operations:
        - target: item
          expression: "row['item'] + 0"
gates:
  - name: page_gate
    input: page_in
    condition: "'keep'"
    routes:
      keep: gated
collectors:
  - name: page_stitcher
    plugin: batch_stats
    input: pages
    on_success: out
    on_error: discard
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


def _build_and_run(tmp_path: Path) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    input_path = tmp_path / "docs.jsonl"
    input_path.write_text("\n".join(json.dumps(doc) for doc in _DOCUMENTS) + "\n")
    output_path = tmp_path / "output.jsonl"
    settings = load_settings_from_yaml_string(_SETTINGS_YAML.format(input_path=input_path, output_path=output_path))
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
        sink_effect_modes=sink_effect_modes,
        sink_effect_admission=sink_effect_admission,
    )
    catalog_sha256, catalog_source = read_openrouter_catalog_snapshot_id()
    db_path = tmp_path / "audit.db"
    db = LandscapeDB(f"sqlite:///{db_path}")
    try:
        result = Orchestrator(db).run(
            config,
            graph=graph,
            settings=settings,
            payload_store=FilesystemPayloadStore(tmp_path / "payloads"),
            openrouter_catalog_sha256=catalog_sha256,
            openrouter_catalog_source=catalog_source,
        )
    finally:
        db.close()
    output_rows = [json.loads(line) for line in output_path.read_text().splitlines()] if output_path.exists() else []
    return db_path, result.to_dict(), output_rows


def test_gate_inside_collector_scope_terminal_closer_runs_clean(tmp_path: Path) -> None:
    """validate accepts this graph, so run must too — the pair IS the claim."""
    db_path, result_data, output_rows = _build_and_run(tmp_path)

    assert result_data["status"] == RunStatus.COMPLETED.value

    # One stats row per document group, values proving both groups flushed
    # through the gate-guarded path.
    assert [(row["count"], row["sum"]) for row in output_rows] == [(3, 6), (2, 12)]

    # No BLOCKED journal residue: every member hold was adopted and released.
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        journal = conn.execute(select(token_work_items_table.c.status)).all()
    assert [j for j in journal if j.status == TokenWorkStatus.BLOCKED.value] == []
