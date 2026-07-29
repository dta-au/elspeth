"""End-to-end acceptance for row_union fork-based A/B statistics.

The exact scenario from elspeth-a5b86149d4 (web-eval-20260710, product
decision 2026-07-16): fork each row into control/treatment branches, tag
each branch with a long-format (prompt_variant, score) shape, release both
branches through a row_union barrier as one indivisible group, and feed the
unioned long-format stream into batch_experiment_compare /
batch_paired_preference. Before row_union existed, no fork destination
could express this (fork branches had to end at a coalesce or a sink).
"""

from __future__ import annotations

import json
from pathlib import Path

from elspeth.cli_helpers import instantiate_plugins_from_config
from elspeth.core.config import load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.orchestrator.preflight import assemble_and_validate_pipeline_config


def _run_pipeline(tmp_path: Path, aggregation_yaml: str) -> tuple[object, Path]:
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "out.jsonl"
    input_path.write_text('{"id": 1, "amount": 10}\n{"id": 2, "amount": 20}\n{"id": 3, "amount": 30}\n{"id": 4, "amount": 40}\n')
    settings = load_settings_from_yaml_string(
        f"""
sources:
  rows:
    plugin: json
    on_success: routed
    options:
      path: {input_path}
      format: jsonl
      on_validation_failure: discard
      schema:
        mode: observed
gates:
  - name: variant_fork
    input: routed
    condition: "True"
    routes:
      'true': fork
      'false': output
    fork_to: [control_branch, treatment_branch]
transforms:
  - name: tag_control
    plugin: value_transform
    input: control_branch
    on_success: control_scored
    on_error: discard
    options:
      schema:
        mode: observed
      operations:
        - target: prompt_variant
          expression: "'control'"
        - target: score
          expression: "row['amount'] * 1.0"
  - name: tag_treatment
    plugin: value_transform
    input: treatment_branch
    on_success: treatment_scored
    on_error: discard
    options:
      schema:
        mode: observed
      operations:
        - target: prompt_variant
          expression: "'treatment'"
        - target: score
          expression: "row['amount'] * 1.5"
row_unions:
  - name: variant_union
    branches:
      control_branch: control_scored
      treatment_branch: treatment_scored
    on_success: experiment_in
{aggregation_yaml}
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      format: jsonl
      schema:
        mode: observed
"""
    )
    bundle = instantiate_plugins_from_config(settings)
    graph = ExecutionGraph.from_plugin_instances(
        sources=bundle.sources,
        source_settings_map=bundle.source_settings_map,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        gates=list(settings.gates),
        queues=settings.queues,
        row_union_settings=list(settings.row_unions),
    )
    config = assemble_and_validate_pipeline_config(
        sources=bundle.sources,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        settings=settings,
        graph=graph,
    )
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    result = Orchestrator(db).run(
        config,
        graph=graph,
        settings=settings,
        payload_store=FilesystemPayloadStore(tmp_path / "payloads"),
    )
    return result, output_path


_COMPARE_AGG = """
aggregations:
  - name: prompt_experiment
    plugin: batch_experiment_compare
    input: experiment_in
    on_success: output
    on_error: discard
    trigger: {}
    output_mode: transform
    options:
      schema:
        mode: observed
      variant_field: prompt_variant
      score_field: score
      baseline_variant: control
"""

_PAIRED_AGG = """
aggregations:
  - name: paired_preference
    plugin: batch_paired_preference
    input: experiment_in
    on_success: output
    on_error: discard
    trigger: {}
    output_mode: transform
    options:
      schema:
        mode: observed
      pair_field: id
      variant_field: prompt_variant
      score_field: score
      baseline_variant: control
"""


def test_two_variant_fork_unions_into_experiment_compare(tmp_path: Path) -> None:
    result, output_path = _run_pipeline(tmp_path, _COMPARE_AGG)

    assert result.rows_processed == 4

    output_rows = [json.loads(line) for line in output_path.read_text().splitlines() if line.strip()]
    assert len(output_rows) == 1, f"expected one comparison row, got: {output_rows}"
    comparison = output_rows[0]
    assert comparison["baseline_variant"] == "control"
    assert comparison["variant"] == "treatment"
    # amounts 10..40: control mean 25.0, treatment mean 37.5 (x1.5)
    assert comparison["baseline_mean"] == 25.0
    assert comparison["variant_mean"] == 37.5
    assert comparison["mean_delta"] == 12.5


def test_two_variant_fork_unions_into_paired_preference(tmp_path: Path) -> None:
    result, output_path = _run_pipeline(tmp_path, _PAIRED_AGG)

    assert result.rows_processed == 4

    output_rows = [json.loads(line) for line in output_path.read_text().splitlines() if line.strip()]
    assert len(output_rows) == 1, f"expected one preference row, got: {output_rows}"
    preference = output_rows[0]
    assert preference["variant"] == "treatment"
    # treatment strictly beats control in every pair (x1.5 on positive amounts)
    assert preference["wins"] == 4
    assert preference["losses"] == 0
