"""DAG construction for row_union barriers (elspeth-a5b86149d4 v1 contract).

Build-time surface: fork branches may target a row_union (identity or
per-branch transform-chain form), the released group continues on the
declared on_success connection, and the group-indivisibility guard rejects
any downstream aggregation whose trigger could fire mid-group (only the
implicit end_of_source trigger is accepted in v1).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from elspeth.cli_helpers import instantiate_plugins_from_config
from elspeth.core.config import load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.graph import GraphValidationError


def _build_graph(yaml_text: str) -> ExecutionGraph:
    settings = load_settings_from_yaml_string(yaml_text)
    bundle = instantiate_plugins_from_config(settings)
    return ExecutionGraph.from_plugin_instances(
        sources=bundle.sources,
        source_settings_map=bundle.source_settings_map,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        gates=list(settings.gates),
        coalesce_settings=list(settings.coalesce) if settings.coalesce else None,
        queues=settings.queues,
        row_union_settings=list(settings.row_unions),
    )


def _yaml(
    tmp_path: Path,
    *,
    row_unions: str,
    branch_transforms: str = "",
    tail: str,
) -> str:
    """Assemble a fork topology: source -> gate fork -> [branches] -> row_union -> tail."""
    input_path = tmp_path / "input.jsonl"
    if not input_path.exists():
        input_path.write_text('{"id": 1, "amount": 3}\n{"id": 2, "amount": 5}\n')
    return f"""
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
{branch_transforms}
{row_unions}
{tail}
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {tmp_path / "out.jsonl"}
      format: jsonl
      schema:
        mode: observed
"""


_IDENTITY_UNION = """
row_unions:
  - name: variant_union
    branches: [control_branch, treatment_branch]
    on_success: union_out
"""

_PASSTHROUGH_TAIL = """
transforms:
  - name: after_union
    plugin: passthrough
    input: union_out
    on_success: output
    on_error: discard
    options:
      schema:
        mode: observed
"""


class TestRowUnionGraphBuild:
    def test_fork_branches_may_target_row_union(self, tmp_path: Path) -> None:
        graph = _build_graph(_yaml(tmp_path, row_unions=_IDENTITY_UNION, tail=_PASSTHROUGH_TAIL))
        id_map = graph.get_row_union_id_map()
        assert set(id_map) == {"variant_union"}
        branch_map = graph.get_branch_to_row_union_map()
        assert branch_map == {
            "control_branch": "variant_union",
            "treatment_branch": "variant_union",
        }
        info = graph.get_node_info(id_map["variant_union"])
        assert info.node_type.value == "row_union"

    def test_row_union_with_transform_chains(self, tmp_path: Path) -> None:
        branch_transforms = """
transforms:
  - name: tag_control
    plugin: passthrough
    input: control_branch
    on_success: control_scored
    on_error: discard
    options:
      schema:
        mode: observed
  - name: tag_treatment
    plugin: passthrough
    input: treatment_branch
    on_success: treatment_scored
    on_error: discard
    options:
      schema:
        mode: observed
  - name: after_union
    plugin: passthrough
    input: union_out
    on_success: output
    on_error: discard
    options:
      schema:
        mode: observed
"""
        chain_union = """
row_unions:
  - name: variant_union
    branches:
      control_branch: control_scored
      treatment_branch: treatment_scored
    on_success: union_out
"""
        graph = _build_graph(_yaml(tmp_path, row_unions=chain_union, branch_transforms=branch_transforms, tail=""))
        union_id = graph.get_row_union_id_map()["variant_union"]
        # Both chain-end transforms must feed the union node.
        predecessor_plugins = {graph.get_node_info(edge.from_node).plugin_name for edge in graph.get_incoming_edges(union_id)}
        assert predecessor_plugins == {"passthrough"}

    def test_unproduced_branch_rejected(self, tmp_path: Path) -> None:
        bad_union = """
row_unions:
  - name: variant_union
    branches: [control_branch, treatment_branch, ghost_branch]
    on_success: union_out
"""
        with pytest.raises(GraphValidationError, match="no gate produces"):
            _build_graph(_yaml(tmp_path, row_unions=bad_union, tail=_PASSTHROUGH_TAIL))

    def test_no_destination_error_names_row_unions(self, tmp_path: Path) -> None:
        # third_branch has no coalesce, no row_union, and no sink: the
        # fork-destination error must now advertise row_union branches as an
        # option alongside coalesce branches and sinks.
        yaml_text = _yaml(tmp_path, row_unions=_IDENTITY_UNION, tail=_PASSTHROUGH_TAIL).replace(
            "fork_to: [control_branch, treatment_branch]",
            "fork_to: [control_branch, treatment_branch, third_branch]",
        )
        with pytest.raises(GraphValidationError, match="row_union"):
            _build_graph(yaml_text)

    def test_count_trigger_downstream_of_row_union_rejected(self, tmp_path: Path) -> None:
        agg_tail = """
aggregations:
  - name: batch_totals
    plugin: batch_stats
    input: union_out
    on_success: output
    on_error: discard
    trigger:
      count: 5
    output_mode: transform
    options:
      schema:
        mode: observed
      value_field: amount
      compute_mean: true
"""
        with pytest.raises(GraphValidationError, match="end_of_source"):
            _build_graph(_yaml(tmp_path, row_unions=_IDENTITY_UNION, tail=agg_tail))

    def test_end_of_source_trigger_downstream_accepted(self, tmp_path: Path) -> None:
        agg_tail = """
aggregations:
  - name: batch_totals
    plugin: batch_stats
    input: union_out
    on_success: output
    on_error: discard
    trigger: {}
    output_mode: transform
    options:
      schema:
        mode: observed
      value_field: amount
      compute_mean: true
"""
        graph = _build_graph(_yaml(tmp_path, row_unions=_IDENTITY_UNION, tail=agg_tail))
        assert graph.get_row_union_id_map()

    def test_on_success_naming_sink_rejected(self, tmp_path: Path) -> None:
        sink_union = """
row_unions:
  - name: variant_union
    branches: [control_branch, treatment_branch]
    on_success: output
"""
        with pytest.raises(GraphValidationError, match="sink"):
            _build_graph(_yaml(tmp_path, row_unions=sink_union, tail=""))

    def test_branch_shared_with_coalesce_rejected(self, tmp_path: Path) -> None:
        shared = """
coalesce:
  - name: merge_point
    branches: [control_branch, treatment_branch]
    policy: require_all
    merge: union
row_unions:
  - name: variant_union
    branches: [control_branch, treatment_branch]
    on_success: union_out
"""
        with pytest.raises(GraphValidationError, match="coalesce"):
            _build_graph(_yaml(tmp_path, row_unions=shared, tail=_PASSTHROUGH_TAIL))
