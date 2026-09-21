"""Keep corpus graph evidence aligned with production node kinds."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import MappingProxyType
from typing import get_args

import pytest
from pydantic import ValidationError
from tests.fixtures.dag_scenario_corpus.harness import RenderedScenario, build_scenario
from tests.fixtures.dag_scenario_corpus.schema import GraphEvidence, GraphNodeType, GraphNodeTypeCount

from elspeth.config_loading import load_settings_from_yaml_string
from elspeth.contracts.enums import NodeType


def test_corpus_graph_node_types_match_production_node_types() -> None:
    assert set(get_args(GraphNodeType)) == {node_type.value for node_type in NodeType}


def test_corpus_graph_node_type_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError, match="node_type"):
        GraphNodeTypeCount.model_validate({"node_type": "unknown", "count": 1})


def test_corpus_build_records_collector_node_type(tmp_path: Path) -> None:
    input_path = tmp_path / "input.jsonl"
    input_path.write_text('{"items":[3,1,2]}\n')
    output_path = tmp_path / "output.jsonl"
    settings_yaml = f"""
sources:
  docs:
    plugin: json
    on_success: rows
    options:
      path: {input_path}
      format: jsonl
      on_validation_failure: discard
      schema: {{mode: observed}}
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
  - name: page_stitcher
    plugin: batch_stats
    input: pages
    on_success: out
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
    settings_digest = hashlib.sha256(settings_yaml.encode()).hexdigest()
    rendered = RenderedScenario(
        settings=load_settings_from_yaml_string(settings_yaml),
        settings_yaml=settings_yaml,
        settings_sha256=settings_digest,
        fixture_sha256=settings_digest,
        input_paths=MappingProxyType({"docs": input_path}),
        output_paths=MappingProxyType({"out": output_path}),
        output_expectations=MappingProxyType({}),
        fault_marker=tmp_path / "fault.marker",
    )

    built = build_scenario(rendered)

    collector_nodes = [node for node in built.graph.get_nodes() if node.node_type is NodeType.COLLECTOR]
    assert len(collector_nodes) == 1
    evidence = built.graph_evidence
    assert evidence.accepted is True
    assert evidence.node_type_counts is not None
    assert {entry.node_type: entry.count for entry in evidence.node_type_counts} == {
        "collector": 1,
        "sink": 1,
        "source": 1,
        "transform": 1,
    }
    assert GraphEvidence.model_validate_json(evidence.model_dump_json()) == evidence
