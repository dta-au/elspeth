"""A row_union-bound fork inside a scope is rejected at BUILD (elspeth-9db785ace7).

The ticket's crash shape: json_explode (scope opener) -> gate fork_to
[arm_a, arm_b] -> a transform per arm -> row_union fanning both arms back to
one connection -> batch_stats collector (scope closer, require_all). The
builder used to ACCEPT this and the run died with the collector's Tier-1
duplicate-member AuditIntegrityError — whose own message declares the state
build-time impossible (spec §7 rule 5) — because a row_union is a
pass-through closer (ruling 27): it releases the ORIGINAL branch tokens, so
every scope member presented one token per fork branch at the collector.
Rule 5's FORK arm now consults ``GroupBinding.closer_kind`` and rejects the
shape flat at build (`core/dag/bound_regions.py::validate_openers_bound_in_region`).

The coalesce sibling (the ticket's probe_nest4 contrast probe) is the
control: a coalesce is a MERGING closer — one released token per member — so
the identical topology with a coalesce in the row_union's position must keep
building AND running clean end to end (the run witness follows
tests/integration/pipeline/test_collector_happy_path.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts.enums import RunStatus
from elspeth.contracts.sink_effects import SinkEffectExecutionPurpose, SinkEffectInputKind
from elspeth.core.config import ElspethSettings, load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.models import GraphValidationError
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

# The ticket's repro topology, parametrized only in the fan-in block: the
# row_union variant must be REJECTED at build, the coalesce variant must
# build and run clean. Everything else is byte-identical between the two.
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
  - name: explode_pages
    plugin: json_explode
    input: rows
    on_success: page_routed
    on_error: discard
    options:
      array_field: pages
      output_field: page
      schema: {{mode: observed}}
  - name: rev_a
    plugin: value_transform
    input: arm_a
    on_success: a_out
    on_error: discard
    options:
      schema: {{mode: observed}}
      operations:
        - {{target: reviewer, expression: "'a'"}}
        - {{target: score, expression: "row['page']['page_no'] * 10"}}
  - name: rev_b
    plugin: value_transform
    input: arm_b
    on_success: b_out
    on_error: discard
    options:
      schema: {{mode: observed}}
      operations:
        - {{target: reviewer, expression: "'b'"}}
        - {{target: score, expression: "row['page']['page_no'] * 12"}}
gates:
  - name: reviewer_fork
    input: page_routed
    condition: "True"
    routes:
      'true': fork
      'false': discard
    fork_to: [arm_a, arm_b]
{fan_in_block}
collectors:
  - name: doc_verdict
    plugin: batch_stats
    input: {collector_input}
    on_success: out
    options:
      value_field: score
      schema: {{mode: observed}}
scopes:
  - name: document_pages
    opener: explode_pages
    closer: doc_verdict
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

_ROW_UNION_BLOCK = """row_unions:
  - name: reviewer_union
    branches:
      arm_a: a_out
      arm_b: b_out
    on_success: pages
"""

_COALESCE_BLOCK = """coalesce:
  - name: reviewer_merge
    branches:
      arm_a: a_out
      arm_b: b_out
    policy: require_all
    merge: union
    union_collision_policy: last_wins
"""

# Two documents -> two EXPAND groups of 2 and 3 members.
_DOCUMENTS = [
    {"id": 1, "pages": [{"page_no": 1}, {"page_no": 2}]},
    {"id": 2, "pages": [{"page_no": 1}, {"page_no": 2}, {"page_no": 3}]},
]


def _settings(tmp_path: Path, *, fan_in: str) -> ElspethSettings:
    input_path = tmp_path / "docs.jsonl"
    input_path.write_text("\n".join(json.dumps(doc) for doc in _DOCUMENTS) + "\n")
    fan_in_block, collector_input = {
        "row_union": (_ROW_UNION_BLOCK, "pages"),
        "coalesce": (_COALESCE_BLOCK, "reviewer_merge"),
    }[fan_in]
    return load_settings_from_yaml_string(
        _SETTINGS_YAML.format(
            input_path=input_path,
            output_path=tmp_path / "output.jsonl",
            fan_in_block=fan_in_block,
            collector_input=collector_input,
        )
    )


def _build_graph(settings: ElspethSettings, bundle: Any, sinks: Any) -> ExecutionGraph:
    return ExecutionGraph.from_plugin_instances(
        sources=bundle.sources,
        source_settings_map=bundle.source_settings_map,
        transforms=bundle.transforms,
        sinks=sinks,
        aggregations=bundle.aggregations,
        gates=list(settings.gates),
        queues=settings.queues,
        coalesce_settings=list(settings.coalesce) or None,
        row_union_settings=list(settings.row_unions) or None,
        collectors=bundle.collectors,
        scope_settings=list(settings.scopes),
        max_bound_region_depth=settings.max_bound_region_depth,
    )


def test_row_union_bound_fork_inside_scope_rejected_at_build(tmp_path: Path) -> None:
    """The ticket's exact shape now fails the build, naming the mechanism:
    the gate, the row_union closer, the enclosing collector, the release
    cardinality (ruling 27 vs rule 5), and the coalesce remedy."""
    settings = _settings(tmp_path, fan_in="row_union")
    bundle = instantiate_plugins_from_config(settings)
    with pytest.raises(
        GraphValidationError,
        match=r"Fork gate 'reviewer_fork' inside bound region 'doc_verdict' closes at row_union 'reviewer_union'",
    ) as excinfo:
        _build_graph(settings, bundle, bundle.sinks)
    message = str(excinfo.value)
    assert "pass-through closer (ruling 27)" in message
    assert "one-token-per-member" in message
    assert "coalesce instead" in message
    assert (excinfo.value.component_id, excinfo.value.component_type) == ("reviewer_fork", "gate")


def test_coalesce_bound_fork_inside_scope_builds_and_runs_clean(tmp_path: Path) -> None:
    """The control (probe_nest4 shape): the same topology closing the fork
    at a COALESCE builds — both bound regions present — and runs to
    COMPLETED with one collector release per document group."""
    settings = _settings(tmp_path, fan_in="coalesce")
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
    graph = _build_graph(settings, bundle, execution_sinks)
    assert {r.binding.closer_name for r in graph.get_bound_regions()} == {"doc_verdict", "reviewer_merge"}
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
    assert result.to_dict()["status"] == RunStatus.COMPLETED.value
    output_path = tmp_path / "output.jsonl"
    output_rows = [json.loads(line) for line in output_path.read_text().splitlines()]
    # One collector release per document group, each accounting every page
    # exactly once (the one-token-per-member guarantee the rejected
    # row_union variant would have violated).
    assert [row["count"] for row in output_rows] == [2, 3]
