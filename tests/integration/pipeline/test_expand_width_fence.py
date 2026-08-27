"""Expand-width fence, end to end (elspeth-258bd49d81).

`settings.max_expand_group_width` caps how many members one expansion may
mint. The refusal fires at the opener, BEFORE the eager mint transaction, and
the parent row leaves through the ordinary transform error channel: quarantine
under `on_error: discard`, with the loss ledger carrying the explicit
`expand_width_exceeded` reason for any enclosing bound group. Rows at or under
the ceiling are untouched — the run continues.

Two graph shapes, because the bound one is where a hang would hide: a refused
BOUND scope opener mints no group at all, so the collector must simply never
hear of it (nothing waits, the run finishes) rather than deadlock on a group
that will never arrive.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select

from elspeth.contracts.enums import RunStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.sink_effects import SinkEffectExecutionPurpose, SinkEffectInputKind
from elspeth.core.config import load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import token_outcomes_table
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

_UNBOUND_YAML = """
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
max_expand_group_width: 2
transforms:
  - name: explode
    plugin: json_explode
    input: rows
    on_success: out
    on_error: discard
    options:
      array_field: items
      output_field: item
      schema: {{mode: observed}}
sinks:
  out:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      format: jsonl
      schema: {{mode: observed}}
"""

_BOUND_YAML = """
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
max_expand_group_width: 2
transforms:
  - name: explode
    plugin: json_explode
    input: rows
    on_success: pages
    on_error: discard
    options:
      array_field: items
      output_field: item
      schema: {{mode: observed}}
collectors:
  - name: stitcher
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
    closer: stitcher
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

# Doc 1 fits the width-2 ceiling; doc 2 would mint 3 members and is refused.
_DOCUMENTS = [{"id": 1, "items": [5, 7]}, {"id": 2, "items": [3, 1, 2]}]


def _build_and_run(tmp_path: Path, settings_yaml: str) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    input_path = tmp_path / "docs.jsonl"
    input_path.write_text("\n".join(json.dumps(doc) for doc in _DOCUMENTS) + "\n")
    output_path = tmp_path / "output.jsonl"
    settings = load_settings_from_yaml_string(settings_yaml.format(input_path=input_path, output_path=output_path))
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
    db_path = tmp_path / "audit.db"
    db = LandscapeDB(f"sqlite:///{db_path}")
    try:
        result = Orchestrator(db).run(
            config,
            graph=graph,
            settings=settings,
            payload_store=FilesystemPayloadStore(tmp_path / "payloads"),
        )
    finally:
        db.close()
    output_rows = [json.loads(line) for line in output_path.read_text().splitlines()] if output_path.exists() else []
    return db_path, result.to_dict(), output_rows


def _quarantined_token_count(db_path: Path) -> int:
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        outcomes = conn.execute(select(token_outcomes_table.c.outcome, token_outcomes_table.c.path)).all()
    return sum(1 for o in outcomes if o.outcome == TerminalOutcome.FAILURE.value and o.path == TerminalPath.QUARANTINED_AT_SOURCE.value)


def test_unbound_expansion_over_ceiling_is_refused_row_level(tmp_path: Path) -> None:
    db_path, result_data, output_rows = _build_and_run(tmp_path, _UNBOUND_YAML)

    # The wide document is refused; the narrow one expands and reaches the sink.
    assert result_data["status"] == RunStatus.COMPLETED_WITH_FAILURES.value
    assert sorted(row["item"] for row in output_rows) == [5, 7]
    assert _quarantined_token_count(db_path) == 1


def test_bound_scope_opener_over_ceiling_refuses_without_hanging_the_collector(tmp_path: Path) -> None:
    """The refused opener mints NO group, so the collector never waits on one —
    the run must finish (not deadlock) with the narrow document's stats row."""
    db_path, result_data, output_rows = _build_and_run(tmp_path, _BOUND_YAML)

    assert result_data["status"] == RunStatus.COMPLETED_WITH_FAILURES.value
    # One stats row: the narrow document's group of 2 (sum 12).
    assert [(row["count"], row["sum"]) for row in output_rows] == [(2, 12)]
    assert _quarantined_token_count(db_path) == 1
