"""The web runtime graph factory must forward declared row_union barriers.

``build_runtime_graph`` is the single web-side graph-construction site. It did
not pass ``settings.row_unions`` to ``ExecutionGraph.from_plugin_instances``,
so a composition declaring a row_union barrier could never build: fork branches
targeting the barrier were reported as unwired ("Available row_union branches:
[]"), blaming the operator's composition for a missing production argument.
"""

from __future__ import annotations

from pathlib import Path

from elspeth.core.config import load_settings_from_yaml_string
from elspeth.plugins.infrastructure.runtime_factory import instantiate_plugins_from_config
from elspeth.web.execution.preflight import build_runtime_graph


def _row_union_yaml(tmp_path: Path) -> str:
    input_path = tmp_path / "input.jsonl"
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

row_unions:
  - name: variant_union
    branches: [control_branch, treatment_branch]
    on_success: union_out

transforms:
  - name: after_union
    plugin: passthrough
    input: union_out
    on_success: output
    on_error: discard
    options:
      schema:
        mode: observed

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


def test_build_runtime_graph_wires_declared_row_unions(tmp_path: Path) -> None:
    settings = load_settings_from_yaml_string(_row_union_yaml(tmp_path))
    bundle = instantiate_plugins_from_config(settings, preflight_mode=True)

    graph = build_runtime_graph(settings, bundle)
    graph.validate()

    id_map = graph.get_row_union_id_map()
    assert set(id_map) == {"variant_union"}
    assert graph.get_branch_to_row_union_map() == {
        "control_branch": "variant_union",
        "treatment_branch": "variant_union",
    }
    assert graph.get_node_info(id_map["variant_union"]).node_type.value == "row_union"
