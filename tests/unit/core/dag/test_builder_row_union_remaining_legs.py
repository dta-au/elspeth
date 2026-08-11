"""Focused row-union builder coverage for config identity and graph revisits."""

from __future__ import annotations

from pathlib import Path

import pytest

from elspeth.contracts.types import GateName, RowUnionName
from elspeth.core.dag.models import GraphValidationError
from tests.unit.core.test_dag_row_union import _build_graph, _yaml

_PASSTHROUGH_AFTER_JOIN = """
transforms:
  - name: after_join
    plugin: passthrough
    input: joined
    on_success: output
    on_error: discard
    options:
      schema:
        mode: observed
"""


def test_row_union_timeout_is_retained_in_node_config(tmp_path: Path) -> None:
    row_union = """
row_unions:
  - name: variant_union
    branches: [control_branch, treatment_branch]
    on_success: union_out
    timeout_seconds: 12.5
"""

    graph = _build_graph(_yaml(tmp_path, row_unions=row_union, tail=_PASSTHROUGH_AFTER_JOIN.replace("joined", "union_out")))
    union_id = graph.get_row_union_id_map()[RowUnionName("variant_union")]

    assert graph.get_node_info(union_id).config["timeout_seconds"] == 12.5


def test_branch_cannot_be_declared_by_two_row_unions(tmp_path: Path) -> None:
    row_unions = """
row_unions:
  - name: first_union
    branches: [control_branch, treatment_branch]
    on_success: first_out
  - name: second_union
    branches: [control_branch, ghost_branch]
    on_success: second_out
"""

    with pytest.raises(GraphValidationError) as exc_info:
        _build_graph(_yaml(tmp_path, row_unions=row_unions, tail=""))

    message = str(exc_info.value)
    assert "Duplicate branch name 'control_branch'" in message
    assert "first_union" in message
    assert "second_union" in message


def test_downstream_convergent_gate_is_accepted(tmp_path: Path) -> None:
    row_union = """
row_unions:
  - name: variant_union
    branches: [control_branch, treatment_branch]
    on_success: union_out
"""
    downstream_gate = """
  - name: downstream_converger
    input: union_out
    condition: "True"
    routes:
      'true': joined
      'false': joined
"""

    graph = _build_graph(
        _yaml(
            tmp_path,
            row_unions=row_union,
            extra_gates=downstream_gate,
            tail=_PASSTHROUGH_AFTER_JOIN,
        )
    )
    gate_id = graph.get_config_gate_id_map()[GateName("downstream_converger")]
    route_map = graph.get_route_resolution_map()

    assert route_map[(gate_id, "true")] == route_map[(gate_id, "false")]


def test_convergent_upstream_gate_is_accepted_for_fork_ancestry(tmp_path: Path) -> None:
    row_union = """
row_unions:
  - name: variant_union
    branches: [control_branch, treatment_branch]
    on_success: union_out
"""
    upstream_gate = """
  - name: upstream_converger
    input: pre_routed
    condition: "True"
    routes:
      'true': routed
      'false': routed
"""
    yaml_text = _yaml(
        tmp_path,
        row_unions=row_union,
        extra_gates=upstream_gate,
        tail=_PASSTHROUGH_AFTER_JOIN.replace("joined", "union_out"),
    ).replace("on_success: routed", "on_success: pre_routed", 1)

    graph = _build_graph(yaml_text)
    gate_id = graph.get_config_gate_id_map()[GateName("upstream_converger")]
    route_map = graph.get_route_resolution_map()

    assert route_map[(gate_id, "true")] == route_map[(gate_id, "false")]
