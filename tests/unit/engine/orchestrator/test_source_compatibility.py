"""Replay compatibility must reject drift before plugin lifecycle."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from elspeth.contracts.enums import Determinism, NodeType, RoutingMode
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.engine.orchestrator.source_compatibility import admit_registered_graph, admit_source_configuration


def _run_record() -> SimpleNamespace:
    settings = {"sources": {"primary": {"plugin": "csv"}}, "sinks": {"output": {"plugin": "json"}}}
    return SimpleNamespace(settings_json=canonical_json(settings), config_hash=stable_hash(settings), canonical_version="v1")


def test_invocation_fields_are_excluded_but_execution_drift_is_refused() -> None:
    factory = Mock()
    factory.run_lifecycle.get_run.return_value = _run_record()
    config = SimpleNamespace(
        config={
            "sources": {"primary": {"plugin": "csv"}},
            "sinks": {"output": {"plugin": "json"}},
            "run_mode": "replay",
            "replay_from": "source",
        }
    )
    admit_source_configuration(factory, source_run_id="source", config=config, canonical_version="v1")

    config.config["sinks"]["output"]["plugin"] = "sqlite"
    with pytest.raises(AuditIntegrityError, match="execution settings differ"):
        admit_source_configuration(factory, source_run_id="source", config=config, canonical_version="v1")


def test_source_settings_hash_and_canonical_version_are_checked() -> None:
    factory = Mock()
    source = _run_record()
    factory.run_lifecycle.get_run.return_value = source
    config = SimpleNamespace(config={"sources": {"primary": {"plugin": "csv"}}, "sinks": {"output": {"plugin": "json"}}})
    source.config_hash = "0" * 64
    with pytest.raises(AuditIntegrityError, match="settings hash"):
        admit_source_configuration(factory, source_run_id="source", config=config, canonical_version="v1")
    source.config_hash = stable_hash(config.config)
    with pytest.raises(AuditIntegrityError, match="canonical version"):
        admit_source_configuration(factory, source_run_id="source", config=config, canonical_version="v2")


def test_registered_plugin_implementation_and_route_drift_are_refused() -> None:
    factory = Mock()
    source_node = SimpleNamespace(
        node_id="source-node",
        node_type=NodeType.SOURCE,
        plugin_name="csv",
        plugin_version="1",
        source_file_hash="sha256:0123456789abcdef",
        config_hash="1" * 64,
        determinism=Determinism.IO_READ,
    )
    current_node = SimpleNamespace(**vars(source_node))
    source_edge = SimpleNamespace(from_node_id="source-node", to_node_id="sink-node", label="continue", default_mode=RoutingMode.MOVE)
    current_edge = SimpleNamespace(**vars(source_edge))
    factory.data_flow.get_nodes.side_effect = [[source_node], [current_node]]
    factory.data_flow.get_edges.side_effect = [[source_edge], [current_edge]]
    admit_registered_graph(factory, source_run_id="source", current_run_id="current")

    current_node.source_file_hash = "sha256:fedcba9876543210"
    factory.data_flow.get_nodes.side_effect = [[source_node], [current_node]]
    with pytest.raises(AuditIntegrityError, match="plugin implementation"):
        admit_registered_graph(factory, source_run_id="source", current_run_id="current")

    current_node.source_file_hash = source_node.source_file_hash
    current_edge.label = "divert"
    factory.data_flow.get_nodes.side_effect = [[source_node], [current_node]]
    factory.data_flow.get_edges.side_effect = [[source_edge], [current_edge]]
    with pytest.raises(AuditIntegrityError, match="route edges"):
        admit_registered_graph(factory, source_run_id="source", current_run_id="current")
