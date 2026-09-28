"""Graph-tier expression types survive gates and stamped LLM producers."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.cli_helpers import instantiate_plugins_from_config
from elspeth.config_loading import load_settings, load_settings_from_yaml_string
from elspeth.core.dag.graph import ExecutionGraph


def _graph(settings):
    plugins = instantiate_plugins_from_config(settings, preflight_mode=True)
    return ExecutionGraph.from_plugin_instances(
        sources=plugins.sources,
        source_settings_map=plugins.source_settings_map,
        transforms=plugins.transforms,
        sinks=plugins.sinks,
        aggregations=plugins.aggregations,
        gates=settings.gates,
        coalesce_settings=settings.coalesce,
        queues=settings.queues,
        row_union_settings=settings.row_unions,
        collectors=plugins.collectors,
        scope_settings=settings.scopes,
    )


def test_fork_identity_gate_recopies_bound_type_and_delivers(tmp_path: Path, monkeypatch) -> None:
    """T13: a gate downstream of value_transform must copy its graph-bound int."""
    source = tmp_path / "input.csv"
    source.write_text("id,a\n1,2\n2,3\n3,4\n")
    output = tmp_path / "output.jsonl"
    settings_data = {
        "landscape": {"url": f"sqlite:///{tmp_path / 'audit.db'}"},
        "payload_store": {"backend": "filesystem", "base_path": str(tmp_path / "payloads")},
        "sources": {
            "src": {
                "plugin": "csv",
                "on_success": "raw",
                "options": {
                    "path": str(source),
                    "on_validation_failure": "discard",
                    "schema": {"mode": "fixed", "fields": ["id: int", "a: int"]},
                },
            }
        },
        "transforms": [
            {
                "name": "vt1",
                "plugin": "value_transform",
                "input": "raw",
                "on_success": "calculated",
                "on_error": "discard",
                "options": {
                    "schema": {"mode": "flexible", "fields": ["id: int"]},
                    "operations": [{"target": "t", "expression": "row['a'] + 1"}],
                },
            },
            {
                "name": "typed_branch",
                "plugin": "passthrough",
                "input": "path_b",
                "on_success": "out_b",
                "on_error": "discard",
                "options": {"schema": {"mode": "flexible", "fields": ["id: int", "t: int"]}},
            },
        ],
        "gates": [
            {
                "name": "split_after_vt",
                "input": "calculated",
                "condition": "True",
                "routes": {"true": "fork", "false": "output"},
                "fork_to": ["path_a", "path_b"],
            }
        ],
        "coalesce": [
            {
                "name": "merge",
                "branches": {"path_a": "path_a", "path_b": "out_b"},
                "policy": "require_all",
                "merge": "union",
                "on_success": "output",
            }
        ],
        "sinks": {
            "output": {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {"path": str(output), "format": "jsonl", "schema": {"mode": "observed"}},
            }
        },
    }
    settings_text = yaml.safe_dump(settings_data, sort_keys=False)
    graph = _graph(load_settings_from_yaml_string(settings_text))
    vt_info = graph.get_node_info(graph.get_transform_name_id_map()["vt1"])
    gate_info = graph.get_node_info(graph.get_config_gate_id_map()["split_after_vt"])
    assert vt_info.output_field_declarations["t"].field_type == "int"
    assert gate_info.output_schema_config is not None
    assert gate_info.output_schema_config.fields is not None
    assert next(field for field in gate_info.output_schema_config.fields if field.name == "t").field_type == "int"

    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(settings_text)
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "t13-gate-recopy-control")
    result = CliRunner().invoke(app, ["--no-dotenv", "run", "-s", str(settings_path), "--execute"])
    assert result.exit_code == 0, result.output
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert len(rows) == 3
    assert sorted(row["t"] for row in rows) == [3, 4, 5]


def test_llm_stamp_is_visible_to_graph_bound_value_transform() -> None:
    """T14: both ab_llm_experiment arms publish score:float from LLM stamps."""
    settings = load_settings(Path("examples/ab_llm_experiment/settings.yaml"))
    graph = _graph(settings)
    for name in ("tag_a", "tag_b"):
        info = graph.get_node_info(graph.get_transform_name_id_map()[name])
        assert info.output_field_declarations["score"].field_type == "float"
