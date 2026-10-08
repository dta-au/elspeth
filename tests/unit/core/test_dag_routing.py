"""DAG gate, connection, edge, and terminal route validation tests."""

from __future__ import annotations

import pytest

from elspeth.core.config import ElspethSettings, GateSettings
from elspeth.core.dag import ExecutionGraph
from tests.fixtures.dag import (
    _aggregation_settings,
    _gate_settings,
    _source_settings,
    _transform_settings,
    instantiate_plugins_from_config,
    instantiate_plugins_from_config_raw,
)


class TestGateConnectionRouteMaterialization:
    """Gate routes to named connections should wire explicit labeled edges."""

    def test_gate_connection_route_creates_labeled_edge(self, plugin_manager) -> None:
        """A gate route targeting a named connection creates a labeled MOVE edge."""
        from elspeth.contracts.types import GateName, NodeID
        from elspeth.core.config import (
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )
        from elspeth.core.config import (
            GateSettings as GateSettingsModel,
        )
        from elspeth.core.dag import ExecutionGraph

        config = ElspethSettings(
            sources={
                "primary": SourceSettings(
                    plugin="csv",
                    on_success="source_out",
                    options={
                        "path": "test.csv",
                        "on_validation_failure": "discard",
                        "schema": {"mode": "observed"},
                    },
                )
            },
            gates=[
                GateSettingsModel(
                    name="router",
                    input="source_out",
                    condition="True",
                    routes={"true": "checker_in", "false": "flagged"},
                ),
                GateSettingsModel(
                    name="checker",
                    input="checker_in",
                    condition="True",
                    routes={"true": "output", "false": "flagged"},
                ),
            ],
            sinks={
                "output": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "output.json", "schema": {"mode": "observed"}}
                ),
                "flagged": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "flagged.json", "schema": {"mode": "observed"}}
                ),
            },
        )

        plugins = instantiate_plugins_from_config_raw(config)
        graph = ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=list(config.gates),
        )

        router_id = graph.get_config_gate_id_map()[GateName("router")]
        checker_id = graph.get_config_gate_id_map()[GateName("checker")]

        edges = graph.get_edges()
        true_edges = [e for e in edges if NodeID(e.from_node) == router_id and e.to_node == checker_id and e.label == "true"]
        assert len(true_edges) == 1, f"Expected 1 'true' edge from router to checker, got {len(true_edges)}"
        continue_edges = [e for e in edges if NodeID(e.from_node) == router_id and e.to_node == checker_id and e.label == "continue"]
        assert len(continue_edges) == 1, f"Expected 1 'continue' edge from router to checker, got {len(continue_edges)}"

    def test_gate_converging_connection_routes_preserve_all_labels(self, plugin_manager) -> None:
        """Converging gate labels to one connection should materialize all route edges."""
        from elspeth.contracts import RouteDestinationKind
        from elspeth.contracts.types import GateName
        from elspeth.core.config import (
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )
        from elspeth.core.config import (
            GateSettings as GateSettingsModel,
        )
        from elspeth.core.dag import ExecutionGraph

        config = ElspethSettings(
            sources={
                "primary": SourceSettings(
                    plugin="csv",
                    on_success="source_out",
                    options={
                        "path": "test.csv",
                        "on_validation_failure": "discard",
                        "schema": {"mode": "observed"},
                    },
                )
            },
            gates=[
                GateSettingsModel(
                    name="router",
                    input="source_out",
                    condition="True",
                    routes={"true": "checker_in", "false": "checker_in"},
                ),
                GateSettingsModel(
                    name="checker",
                    input="checker_in",
                    condition="True",
                    routes={"true": "output", "false": "output"},
                ),
            ],
            sinks={
                "output": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "output.json", "schema": {"mode": "observed"}}
                ),
            },
        )

        plugins = instantiate_plugins_from_config_raw(config)
        graph = ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=list(config.gates),
        )

        gate_ids = graph.get_config_gate_id_map()
        router_id = gate_ids[GateName("router")]
        checker_id = gate_ids[GateName("checker")]

        edges = [edge for edge in graph.get_edges() if edge.from_node == router_id and edge.to_node == checker_id]
        assert len(edges) == 3
        assert {edge.label for edge in edges} == {"true", "false", "continue"}

        route_map = graph.get_route_resolution_map()
        for route_label in ("true", "false"):
            destination = route_map[(router_id, route_label)]
            assert destination.kind == RouteDestinationKind.PROCESSING_NODE
            assert destination.next_node_id == checker_id

    def test_gate_connection_route_preserves_route_resolution(self, plugin_manager) -> None:
        """Named-connection routes resolve to processing nodes; sink routes resolve to sinks."""
        from elspeth.contracts import RouteDestination, RouteDestinationKind
        from elspeth.contracts.types import GateName, SinkName
        from elspeth.core.config import (
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )
        from elspeth.core.config import (
            GateSettings as GateSettingsModel,
        )
        from elspeth.core.dag import ExecutionGraph

        config = ElspethSettings(
            sources={
                "primary": SourceSettings(
                    plugin="csv",
                    on_success="source_out",
                    options={
                        "path": "test.csv",
                        "on_validation_failure": "discard",
                        "schema": {"mode": "observed"},
                    },
                )
            },
            gates=[
                GateSettingsModel(
                    name="router",
                    input="source_out",
                    condition="True",
                    routes={"true": "checker_in", "false": "flagged"},
                ),
                GateSettingsModel(
                    name="checker",
                    input="checker_in",
                    condition="True",
                    routes={"true": "output", "false": "flagged"},
                ),
            ],
            sinks={
                "output": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "output.json", "schema": {"mode": "observed"}}
                ),
                "flagged": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "flagged.json", "schema": {"mode": "observed"}}
                ),
            },
        )

        plugins = instantiate_plugins_from_config_raw(config)
        graph = ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=list(config.gates),
        )

        router_id = graph.get_config_gate_id_map()[GateName("router")]
        resolution_map = graph.get_route_resolution_map()

        # "false" -> sink
        assert (router_id, "false") in resolution_map
        assert resolution_map[(router_id, "false")] == RouteDestination.sink(SinkName("flagged"))

        # "true" -> processing_node (the checker gate)
        assert (router_id, "true") in resolution_map
        assert resolution_map[(router_id, "true")].kind == RouteDestinationKind.PROCESSING_NODE

    def test_terminal_gate_unconsumed_connection_rejected(self, plugin_manager) -> None:
        """A terminal gate route to an unconsumed connection fails validation."""
        from elspeth.core.config import (
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )
        from elspeth.core.config import (
            GateSettings as GateSettingsModel,
        )
        from elspeth.core.dag import ExecutionGraph, GraphValidationError

        config = ElspethSettings(
            sources={
                "primary": SourceSettings(
                    plugin="csv",
                    on_success="source_out",
                    options={
                        "path": "test.csv",
                        "on_validation_failure": "discard",
                        "schema": {"mode": "observed"},
                    },
                )
            },
            gates=[
                GateSettingsModel(
                    name="terminal_gate",
                    input="source_out",
                    condition="True",
                    routes={"true": "output", "false": "orphan_conn"},
                ),
            ],
            sinks={
                "output": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "output.json", "schema": {"mode": "observed"}}
                ),
            },
        )

        plugins = instantiate_plugins_from_config_raw(config)
        with pytest.raises(GraphValidationError, match=r"neither a sink nor a known connection name"):
            ExecutionGraph.from_plugin_instances(
                sources=plugins.sources,
                source_settings_map=plugins.source_settings_map,
                transforms=plugins.transforms,
                sinks=plugins.sinks,
                aggregations=plugins.aggregations,
                gates=list(config.gates),
            )


class TestExecutionGraphRouteMapping:
    """Test route label <-> sink name mapping for edge lookup."""

    def test_get_route_label_for_sink(self, plugin_manager) -> None:
        """Get route label that leads to a sink from a config gate."""
        from elspeth.contracts import GateName, SinkName
        from elspeth.core.config import (
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )
        from elspeth.core.dag import ExecutionGraph

        config = ElspethSettings(
            sources={
                "primary": _source_settings(
                    SourceSettings,
                    plugin="csv",
                    options={
                        "path": "test.csv",
                        "on_validation_failure": "discard",
                        "schema": {"mode": "observed"},
                    },
                )
            },
            sinks={
                "results": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "results.json", "schema": {"mode": "observed"}}
                ),
                "flagged": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "flagged.json", "schema": {"mode": "observed"}}
                ),
            },
            gates=[
                _gate_settings(
                    GateSettings,
                    name="classifier",
                    condition="row['suspicious'] == True",
                    routes={"true": "flagged", "false": "results"},
                ),
            ],
        )

        plugins = instantiate_plugins_from_config(config)
        graph = ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=list(config.gates),
        )

        # Get the config gate's node_id
        gate_node_id = graph.get_config_gate_id_map()[GateName("classifier")]

        # Given gate node and sink name, get the route label
        route_label = graph.get_route_label(gate_node_id, SinkName("flagged"))

        assert route_label == "true"

    def test_get_route_label_for_continue(self, plugin_manager) -> None:
        """Non-terminal continue routes return 'continue' as label."""
        from elspeth.contracts import GateName, SinkName
        from elspeth.core.config import (
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )
        from elspeth.core.dag import ExecutionGraph

        config = ElspethSettings(
            sources={
                "primary": _source_settings(
                    SourceSettings,
                    plugin="csv",
                    options={
                        "path": "test.csv",
                        "on_validation_failure": "discard",
                        "schema": {"mode": "observed"},
                    },
                )
            },
            sinks={
                "results": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "results.json", "schema": {"mode": "observed"}}
                ),
                "flagged": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "flagged.json", "schema": {"mode": "observed"}}
                ),
            },
            gates=[
                _gate_settings(
                    GateSettings,
                    name="gate1",
                    condition="True",
                    routes={"true": "gate1_pass", "false": "flagged"},
                ),
                _gate_settings(
                    GateSettings,
                    input="gate1_pass",
                    name="gate2",
                    condition="True",
                    routes={"true": "results", "false": "results"},
                ),
            ],
        )

        plugins = instantiate_plugins_from_config(config)
        graph = ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=list(config.gates),
        )
        gate_node_id = graph.get_config_gate_id_map()[GateName("gate1")]

        # gate1 reaches results via a continue edge to gate2
        route_label = graph.get_route_label(gate_node_id, SinkName("results"))
        assert route_label == "continue"

    def test_hyphenated_sink_names_work_in_dag(self, plugin_manager) -> None:
        """Gate routing to hyphenated sink names works correctly.

        Regression test for gate-route-destination-name-validation-mismatch bug.
        Sink names don't need to match identifier pattern - they're just dict keys.
        """
        from elspeth.contracts import GateName, SinkName
        from elspeth.core.config import (
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )
        from elspeth.core.dag import ExecutionGraph

        config = ElspethSettings(
            sources={
                "primary": _source_settings(
                    SourceSettings,
                    plugin="csv",
                    options={
                        "path": "test.csv",
                        "on_validation_failure": "discard",
                        "schema": {"mode": "observed"},
                    },
                )
            },
            sinks={
                "output-sink": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "output.json", "schema": {"mode": "observed"}}
                ),
                "quarantine-bucket": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "quarantine.json", "schema": {"mode": "observed"}}
                ),
            },
            gates=[
                _gate_settings(
                    GateSettings,
                    name="quality_check",
                    condition="row['score'] >= 0.5",
                    routes={"true": "output-sink", "false": "quarantine-bucket"},
                ),
            ],
        )

        # DAG compilation should succeed with hyphenated sink names
        plugins = instantiate_plugins_from_config(config)
        graph = ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=list(config.gates),
        )

        # Verify both hyphenated sinks exist
        sink_ids = graph.get_sink_id_map()
        assert SinkName("output-sink") in sink_ids
        assert SinkName("quarantine-bucket") in sink_ids

        # Verify gate routes to the hyphenated sinks
        gate_node_id = graph.get_config_gate_id_map()[GateName("quality_check")]
        assert graph.get_route_label(gate_node_id, SinkName("quarantine-bucket")) == "false"

    def test_get_route_label_returns_continue_for_missing_label(self) -> None:
        """get_route_label() returns 'continue' for unregistered gate→sink pairs.

        The primary enforcement is in validate() — get_route_label() is a
        simple map lookup with 'continue' fallback.
        """
        from elspeth.contracts import NodeType, RoutingMode
        from elspeth.contracts.types import NodeID, SinkName
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("gate", node_type=NodeType.GATE, plugin_name="config_gate")
        graph.add_node("sink_a", node_type=NodeType.SINK, plugin_name="csv")
        graph.add_node("sink_b", node_type=NodeType.SINK, plugin_name="csv")
        graph.add_edge("source", "gate", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("gate", "sink_a", label="true", mode=RoutingMode.MOVE)
        graph.add_edge("gate", "sink_b", label="false", mode=RoutingMode.MOVE)
        graph.add_route_label_entry(NodeID("gate"), SinkName("sink_a"), "true")
        graph.set_sink_id_map({SinkName("sink_a"): NodeID("sink_a"), SinkName("sink_b"): NodeID("sink_b")})

        # Registered label returns the label
        assert graph.get_route_label("gate", SinkName("sink_a")) == "true"
        # Unregistered pair returns "continue" (validate() catches the real bug)
        assert graph.get_route_label("gate", SinkName("sink_b")) == "continue"

    def test_get_route_label_returns_continue_for_non_gate(self) -> None:
        """Non-gate nodes (transforms) return 'continue' for unregistered sinks."""
        from elspeth.contracts import NodeType, RoutingMode
        from elspeth.contracts.types import SinkName
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("transform", node_type=NodeType.TRANSFORM, plugin_name="passthrough")
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv")
        graph.add_edge("source", "transform", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("transform", "sink", label="continue", mode=RoutingMode.MOVE)

        # Non-gate nodes still return "continue" for default path
        assert graph.get_route_label("transform", SinkName("sink")) == "continue"

    def test_route_labels_registered_through_production_path(self, plugin_manager) -> None:
        """Route labels are correctly registered when building via from_plugin_instances().

        Guards against BUG-LINEAGE-01 pattern: manual graph construction in tests
        passes but production path has different (wrong) mapping. This test verifies
        the builder registers route labels that survive validate().
        """
        from elspeth.contracts import GateName, SinkName
        from elspeth.core.config import (
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )
        from elspeth.core.dag import ExecutionGraph

        config = ElspethSettings(
            sources={
                "primary": _source_settings(
                    SourceSettings,
                    plugin="csv",
                    options={
                        "path": "test.csv",
                        "on_validation_failure": "discard",
                        "schema": {"mode": "observed"},
                    },
                )
            },
            sinks={
                "clean": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "clean.json", "schema": {"mode": "observed"}}
                ),
                "quarantine": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "quarantine.json", "schema": {"mode": "observed"}}
                ),
            },
            gates=[
                _gate_settings(
                    GateSettings,
                    name="router",
                    condition="row.get('valid') is not None",
                    routes={"true": "clean", "false": "quarantine"},
                ),
            ],
        )

        plugins = instantiate_plugins_from_config(config)
        graph = ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=list(config.gates),
            coalesce_settings=None,
        )

        # validate() should pass — all gate→sink MOVE edges have route labels
        graph.validate()

        # Verify the labels are actually registered correctly
        gate_id = graph.get_config_gate_id_map()[GateName("router")]
        assert graph.get_route_label(gate_id, SinkName("clean")) == "true"
        assert graph.get_route_label(gate_id, SinkName("quarantine")) == "false"


class TestMultiEdgeSupport:
    """Tests for MultiDiGraph multi-edge support."""

    def test_multiple_edges_same_node_pair(self) -> None:
        """MultiDiGraph allows multiple labeled edges between same nodes."""
        from elspeth.contracts import NodeType, RoutingMode
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("gate", node_type=NodeType.GATE, plugin_name="fork_gate")
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="output")

        # Add two edges with different labels to SAME destination
        graph.add_edge("gate", "sink", label="path_a", mode=RoutingMode.COPY)
        graph.add_edge("gate", "sink", label="path_b", mode=RoutingMode.COPY)

        # Both edges should exist (DiGraph would show 1, MultiDiGraph shows 2)
        assert graph.edge_count == 2

        edges = graph.get_edges()
        labels = {e.label for e in edges}
        assert labels == {"path_a", "path_b"}

    def test_multi_edge_graph_is_acyclic(self) -> None:
        """Verify is_acyclic() works correctly with MultiDiGraph parallel edges."""
        from elspeth.contracts import NodeType, RoutingMode
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("gate", node_type=NodeType.GATE, plugin_name="classifier")
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv")

        graph.add_edge("source", "gate", label="continue", mode=RoutingMode.MOVE)
        # Multiple parallel edges to same sink - still acyclic
        graph.add_edge("gate", "sink", label="high", mode=RoutingMode.MOVE)
        graph.add_edge("gate", "sink", label="medium", mode=RoutingMode.MOVE)
        graph.add_edge("gate", "sink", label="low", mode=RoutingMode.MOVE)

        # Graph with parallel edges should still be detected as acyclic
        assert graph.is_acyclic() is True
        # Full validation should also pass
        graph.validate()


class TestEdgeInfoIntegration:
    """Tests for typed edge returns."""

    def test_get_edges_returns_edge_info(self) -> None:
        """get_edges() returns list of EdgeInfo, not tuples."""
        from elspeth.contracts import EdgeInfo, NodeType, RoutingMode
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("source-1", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("sink-1", node_type=NodeType.SINK, plugin_name="csv")
        graph.add_edge("source-1", "sink-1", label="continue", mode=RoutingMode.MOVE)

        edges = graph.get_edges()

        assert len(edges) == 1
        assert isinstance(edges[0], EdgeInfo)
        assert edges[0].from_node == "source-1"
        assert edges[0].to_node == "sink-1"
        assert edges[0].label == "continue"
        assert edges[0].mode == RoutingMode.MOVE

    def test_add_edge_accepts_routing_mode_enum(self) -> None:
        """add_edge() accepts RoutingMode enum, not string."""
        from elspeth.contracts import NodeType, RoutingMode
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("n1", node_type=NodeType.TRANSFORM, plugin_name="test")
        graph.add_node("n2", node_type=NodeType.SINK, plugin_name="test")

        # Should accept enum directly
        graph.add_edge("n1", "n2", label="route", mode=RoutingMode.COPY)

        edges = graph.get_edges()
        assert edges[0].mode == RoutingMode.COPY


class TestMultiEdgeScenarios:
    """Tests for scenarios requiring multiple edges between same nodes."""

    def test_fork_gate_config_parses_into_valid_graph(self, plugin_manager) -> None:
        """Fork gate configuration parses into valid graph structure.

        Note: This tests config parsing, not the multi-edge bug. Fork routes
        with target="fork" don't create edges to sinks - they create child tokens.
        The multi-edge bug is tested by test_gate_multiple_routes_same_sink.
        """
        from elspeth.core.config import (
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )
        from elspeth.core.dag import ExecutionGraph

        config = ElspethSettings(
            sources={
                "primary": _source_settings(
                    SourceSettings,
                    plugin="csv",
                    options={
                        "path": "test.csv",
                        "on_validation_failure": "discard",
                        "schema": {"mode": "observed"},
                    },
                )
            },
            sinks={
                "output": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "output.json", "schema": {"mode": "observed"}}
                ),
                "path_a": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "path_a.json", "schema": {"mode": "observed"}}
                ),
                "path_b": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "path_b.json", "schema": {"mode": "observed"}}
                ),
            },
            gates=[
                _gate_settings(
                    GateSettings,
                    name="fork_gate",
                    condition="True",  # Always forks
                    routes={"true": "fork", "false": "output"},
                    fork_to=["path_a", "path_b"],
                ),
            ],
        )

        plugins = instantiate_plugins_from_config(config)
        graph = ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=list(config.gates),
        )

        # Validate graph is still valid (DAG, has source and sink)
        graph.validate()

        # The gate should have edges - at minimum the continue edge to output sink
        edges = graph.get_edges()
        gate_edges = [e for e in edges if "config_gate" in e.from_node]

        # Should have at least the continue edge to output sink
        assert len(gate_edges) >= 1

    def test_gate_multiple_routes_same_sink(self) -> None:
        """CRITICAL: Gate with multiple route labels to same sink preserves all labels.

        This is the core bug scenario: {"high": "alerts", "medium": "alerts", "low": "alerts"}
        With DiGraph, only "low" survives. With MultiDiGraph, all three edges exist.
        """
        from elspeth.contracts import NodeType, RoutingMode
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("gate", node_type=NodeType.GATE, plugin_name="classifier")
        graph.add_node("alerts", node_type=NodeType.SINK, plugin_name="csv")

        graph.add_edge("source", "gate", label="continue", mode=RoutingMode.MOVE)
        # Multiple severity levels all route to same alerts sink
        graph.add_edge("gate", "alerts", label="high", mode=RoutingMode.MOVE)
        graph.add_edge("gate", "alerts", label="medium", mode=RoutingMode.MOVE)
        graph.add_edge("gate", "alerts", label="low", mode=RoutingMode.MOVE)

        # All three edges should exist
        edges = graph.get_edges()
        alert_edges = [e for e in edges if e.to_node == "alerts"]
        assert len(alert_edges) == 3

        labels = {e.label for e in alert_edges}
        assert labels == {"high", "medium", "low"}


class TestBranchGateMap:
    """Test branch_gate_map exposure from ExecutionGraph."""

    def test_get_branch_gate_map_returns_copy(self) -> None:
        """Getter should return a copy to prevent external mutation."""
        from elspeth.contracts.types import BranchName, NodeID
        from elspeth.core.config import (
            CoalesceSettings,
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )
        from elspeth.core.dag import ExecutionGraph

        settings = ElspethSettings(
            sources={"primary": _source_settings(SourceSettings, plugin="null")},
            gates=[
                _gate_settings(
                    GateSettings,
                    name="fork_gate",
                    condition="True",
                    routes={"true": "fork", "false": "output"},
                    fork_to=["branch_a", "branch_b"],
                ),
            ],
            sinks={
                "output": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "/tmp/test.json", "schema": {"mode": "observed"}}
                )
            },
            coalesce=[
                CoalesceSettings(
                    name="merge_branches",
                    branches=["branch_a", "branch_b"],
                    policy="require_all",
                    merge="union",
                ),
            ],
        )

        plugins = instantiate_plugins_from_config(settings)
        graph = ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=list(settings.gates),
            coalesce_settings=settings.coalesce,
        )

        # Get the map
        branch_map = graph.get_branch_gate_map()

        # Verify it contains expected mappings (each branch maps to a NodeID)
        assert BranchName("branch_a") in branch_map
        assert BranchName("branch_b") in branch_map

        # Verify it's a copy (mutation doesn't affect internal state)
        original_value = branch_map[BranchName("branch_a")]
        branch_map[BranchName("branch_a")] = NodeID("fake")

        fresh_map = graph.get_branch_gate_map()
        assert fresh_map[BranchName("branch_a")] == original_value

    def test_get_branch_gate_map_empty_when_no_coalesce(self) -> None:
        """Getter returns empty dict when no coalesce configured."""
        from elspeth.core.config import ElspethSettings, SinkSettings, SourceSettings
        from elspeth.core.dag import ExecutionGraph

        settings = ElspethSettings(
            sources={"primary": _source_settings(SourceSettings, plugin="null")},
            sinks={
                "output": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "/tmp/test.json", "schema": {"mode": "observed"}}
                )
            },
        )

        plugins = instantiate_plugins_from_config(settings)
        graph = ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=list(settings.gates),
            coalesce_settings=settings.coalesce,
        )

        branch_map = graph.get_branch_gate_map()
        assert branch_map == {}


class TestDivertEdges:
    """Tests for quarantine and error divert edges in graph construction.

    These tests use the production factory path (instantiate_plugins_from_config)
    per the Test Path Integrity rule. Divert edges make quarantine/error sinks
    reachable in the graph without participating in normal DAG traversal.
    """

    def _build_graph(self, settings: ElspethSettings) -> ExecutionGraph:
        """Build ExecutionGraph via production factory path."""
        from elspeth.core.dag import ExecutionGraph

        plugins = instantiate_plugins_from_config(settings)
        return ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=list(settings.gates),
        )

    def test_source_quarantine_edge_created(self, plugin_manager) -> None:
        """Source with on_validation_failure creates a divert edge to quarantine sink."""
        from elspeth.contracts import RoutingMode
        from elspeth.core.config import ElspethSettings, SinkSettings, SourceSettings

        settings = ElspethSettings(
            sources={
                "primary": _source_settings(
                    SourceSettings,
                    plugin="csv",
                    options={
                        "path": "test.csv",
                        "on_validation_failure": "quarantine",
                        "schema": {"mode": "observed"},
                    },
                )
            },
            sinks={
                "default": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "out.json", "schema": {"mode": "observed"}}
                ),
                "quarantine": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "quar.json", "schema": {"mode": "observed"}}
                ),
            },
        )
        graph = self._build_graph(settings)
        graph.validate()

        edges = graph.get_edges()
        divert_edges = [e for e in edges if e.mode == RoutingMode.DIVERT]
        assert len(divert_edges) == 1
        assert divert_edges[0].label == "__quarantine__"

    def test_source_discard_no_divert_edge(self, plugin_manager) -> None:
        """Source with on_validation_failure='discard' creates no divert edge."""
        from elspeth.contracts import RoutingMode
        from elspeth.core.config import ElspethSettings, SinkSettings, SourceSettings

        settings = ElspethSettings(
            sources={
                "primary": _source_settings(
                    SourceSettings,
                    plugin="csv",
                    options={
                        "path": "test.csv",
                        "on_validation_failure": "discard",
                        "schema": {"mode": "observed"},
                    },
                )
            },
            sinks={
                "default": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "out.json", "schema": {"mode": "observed"}}
                )
            },
        )
        graph = self._build_graph(settings)
        graph.validate()

        edges = graph.get_edges()
        divert_edges = [e for e in edges if e.mode == RoutingMode.DIVERT]
        assert len(divert_edges) == 0

    def test_transform_error_edge_created(self, plugin_manager) -> None:
        """Transform with on_error creates a divert edge to error sink."""
        from elspeth.contracts import RoutingMode
        from elspeth.core.config import (
            ElspethSettings,
            SinkSettings,
            SourceSettings,
            TransformSettings,
        )

        settings = ElspethSettings(
            sources={
                "primary": _source_settings(
                    SourceSettings,
                    plugin="csv",
                    options={
                        "path": "test.csv",
                        "on_validation_failure": "discard",
                        "schema": {"mode": "observed"},
                    },
                )
            },
            transforms=[
                _transform_settings(
                    TransformSettings,
                    plugin="passthrough",
                    on_success="default",
                    on_error="errors",
                    options={"schema": {"mode": "observed"}},
                ),
            ],
            sinks={
                "default": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "out.json", "schema": {"mode": "observed"}}
                ),
                "errors": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "err.json", "schema": {"mode": "observed"}}
                ),
            },
        )
        graph = self._build_graph(settings)
        graph.validate()

        edges = graph.get_edges()
        divert_edges = [e for e in edges if e.mode == RoutingMode.DIVERT]
        assert len(divert_edges) == 1
        assert divert_edges[0].label.startswith("__error_") and divert_edges[0].label.endswith("__")

    def test_quarantine_and_error_both_present(self, plugin_manager) -> None:
        """Both quarantine and error divert edges coexist."""
        from elspeth.contracts import RoutingMode
        from elspeth.core.config import (
            ElspethSettings,
            SinkSettings,
            SourceSettings,
            TransformSettings,
        )

        settings = ElspethSettings(
            sources={
                "primary": _source_settings(
                    SourceSettings,
                    plugin="csv",
                    options={
                        "path": "test.csv",
                        "on_validation_failure": "quarantine",
                        "schema": {"mode": "observed"},
                    },
                )
            },
            transforms=[
                _transform_settings(
                    TransformSettings,
                    plugin="passthrough",
                    on_success="default",
                    on_error="errors",
                    options={"schema": {"mode": "observed"}},
                ),
            ],
            sinks={
                "default": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "out.json", "schema": {"mode": "observed"}}
                ),
                "quarantine": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "quar.json", "schema": {"mode": "observed"}}
                ),
                "errors": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "err.json", "schema": {"mode": "observed"}}
                ),
            },
        )
        graph = self._build_graph(settings)
        graph.validate()

        edges = graph.get_edges()
        divert_edges = [e for e in edges if e.mode == RoutingMode.DIVERT]
        assert len(divert_edges) == 2

    def test_quarantine_to_default_sink_creates_divert_edge(self, plugin_manager) -> None:
        """If quarantine destination is default sink, divert edge still created.

        Verifies that schema validation still runs for the normal continue
        edge to default sink (the DIVERT skip is per-edge, not per-node-pair).
        """
        from elspeth.contracts import RoutingMode
        from elspeth.core.config import ElspethSettings, SinkSettings, SourceSettings

        settings = ElspethSettings(
            sources={
                "primary": _source_settings(
                    SourceSettings,
                    plugin="csv",
                    options={
                        "path": "test.csv",
                        "on_validation_failure": "default",
                        "schema": {"mode": "observed"},
                    },
                )
            },
            sinks={
                "default": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "out.json", "schema": {"mode": "observed"}}
                )
            },
        )
        graph = self._build_graph(settings)
        graph.validate()

        edges = graph.get_edges()
        divert_edges = [e for e in edges if e.mode == RoutingMode.DIVERT]
        assert len(divert_edges) == 1
        # Normal continue edge should also exist
        normal_edges = [e for e in edges if e.mode != RoutingMode.DIVERT]
        assert any(e.label == "on_success" for e in normal_edges)

    def test_multiple_transforms_share_error_sink(self, plugin_manager) -> None:
        """Multiple transforms can route errors to the same sink."""
        from elspeth.contracts import RoutingMode
        from elspeth.core.config import (
            ElspethSettings,
            SinkSettings,
            SourceSettings,
            TransformSettings,
        )

        settings = ElspethSettings(
            sources={
                "primary": _source_settings(
                    SourceSettings,
                    plugin="csv",
                    options={
                        "path": "test.csv",
                        "on_validation_failure": "discard",
                        "schema": {"mode": "observed"},
                    },
                )
            },
            transforms=[
                _transform_settings(
                    TransformSettings,
                    name="pt_a",
                    plugin="passthrough",
                    on_success="pt_a_out",
                    on_error="errors",
                    options={"schema": {"mode": "observed"}},
                ),
                _transform_settings(
                    TransformSettings,
                    name="pt_b",
                    plugin="passthrough",
                    on_success="default",
                    on_error="errors",
                    options={"schema": {"mode": "observed"}},
                ),
            ],
            sinks={
                "default": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "out.json", "schema": {"mode": "observed"}}
                ),
                "errors": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "err.json", "schema": {"mode": "observed"}}
                ),
            },
        )
        graph = self._build_graph(settings)
        graph.validate()

        edges = graph.get_edges()
        divert_edges = [e for e in edges if e.mode == RoutingMode.DIVERT]
        error_edges = [e for e in divert_edges if e.label.startswith("__error_")]
        assert len(error_edges) == 2
        # With auto-generated names, error labels include the transform name hash
        assert len({e.label for e in error_edges}) == 2
        assert all(e.label.startswith("__error_") and e.label.endswith("__") for e in error_edges)


class TestTerminalGateRouteValidation:
    """Terminal gates must not emit unconsumed connection outputs."""

    def test_terminal_gate_with_unconsumed_connection_raises(self, plugin_manager) -> None:
        """Terminal gate route to an orphan connection raises GraphValidationError."""
        from elspeth.core.config import (
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )
        from elspeth.core.dag import ExecutionGraph, GraphValidationError

        config = ElspethSettings(
            sources={
                "primary": _source_settings(
                    SourceSettings,
                    plugin="csv",
                    on_success="to_gate",
                    options={
                        "path": "test.csv",
                        "on_validation_failure": "discard",
                        "schema": {"mode": "observed"},
                    },
                )
            },
            sinks={
                "output": SinkSettings(
                    plugin="json",
                    on_write_failure="discard",
                    options={"path": "output.json", "schema": {"mode": "observed"}},
                ),
            },
            gates=[
                _gate_settings(
                    GateSettings,
                    name="threshold",
                    input="to_gate",
                    condition="row['score'] > 0.5",
                    routes={"true": "output", "false": "orphan_conn"},
                ),
            ],
        )

        plugins = instantiate_plugins_from_config_raw(config)
        with pytest.raises(GraphValidationError, match=r"neither a sink nor a known connection name"):
            ExecutionGraph.from_plugin_instances(
                sources=plugins.sources,
                source_settings_map=plugins.source_settings_map,
                transforms=plugins.transforms,
                sinks=plugins.sinks,
                aggregations=plugins.aggregations,
                gates=list(config.gates),
            )

    def test_non_terminal_gate_with_connection_route_passes(self, plugin_manager) -> None:
        """Non-terminal gate with explicit connection route builds successfully.

        Config gates are positioned AFTER transforms in the pipeline. A gate is
        non-terminal when another config gate follows it. The first gate routes
        to the second through a named connection.
        """
        from elspeth.core.config import (
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )
        from elspeth.core.dag import ExecutionGraph

        config = ElspethSettings(
            sources={
                "primary": _source_settings(
                    SourceSettings,
                    plugin="csv",
                    on_success="to_gates",
                    options={
                        "path": "test.csv",
                        "on_validation_failure": "discard",
                        "schema": {"mode": "observed"},
                    },
                )
            },
            sinks={
                "output": SinkSettings(
                    plugin="json",
                    on_write_failure="discard",
                    options={"path": "output.json", "schema": {"mode": "observed"}},
                ),
                "flagged": SinkSettings(
                    plugin="json",
                    on_write_failure="discard",
                    options={"path": "flagged.json", "schema": {"mode": "observed"}},
                ),
            },
            gates=[
                _gate_settings(
                    GateSettings,
                    name="first_gate",
                    input="to_gates",
                    condition="row['score'] > 0.5",
                    routes={"true": "flagged", "false": "to_second"},
                ),
                _gate_settings(
                    GateSettings,
                    name="second_gate",
                    input="to_second",
                    condition="True",
                    routes={"true": "output", "false": "output"},
                ),
            ],
        )

        plugins = instantiate_plugins_from_config_raw(config)
        graph = ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=list(config.gates),
        )
        graph.validate()

    def test_gate_route_to_transform_connection_passes(self, plugin_manager) -> None:
        """Gate routes can target downstream transforms via connection names."""
        from elspeth.contracts import RoutingMode
        from elspeth.contracts.types import GateName
        from elspeth.core.config import (
            ElspethSettings,
            SinkSettings,
            SourceSettings,
            TransformSettings,
        )
        from elspeth.core.dag import ExecutionGraph

        config = ElspethSettings(
            sources={
                "primary": _source_settings(
                    SourceSettings,
                    plugin="csv",
                    on_success="to_gate",
                    options={
                        "path": "test.csv",
                        "on_validation_failure": "discard",
                        "schema": {"mode": "observed"},
                    },
                )
            },
            transforms=[
                _transform_settings(
                    TransformSettings,
                    name="after_gate_transform",
                    plugin="passthrough",
                    input="after_gate",
                    on_success="output",
                    options={"schema": {"mode": "observed"}},
                ),
            ],
            sinks={
                "output": SinkSettings(
                    plugin="json",
                    on_write_failure="discard",
                    options={"path": "output.json", "schema": {"mode": "observed"}},
                ),
            },
            gates=[
                _gate_settings(
                    GateSettings,
                    name="router",
                    input="to_gate",
                    condition="row['score'] > 0.5",
                    routes={"true": "output", "false": "after_gate"},
                ),
            ],
        )

        plugins = instantiate_plugins_from_config_raw(config)
        graph = ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=list(config.gates),
        )
        graph.validate()

        gate_id = graph.get_config_gate_id_map()[GateName("router")]
        transform_id = graph.get_transform_id_map()[0]
        assert any(
            edge.from_node == gate_id and edge.to_node == transform_id and edge.label == "false" and edge.mode == RoutingMode.MOVE
            for edge in graph.get_edges()
        )


class TestAggregationOnSuccessValidation:
    """wp68: Aggregation on_success routing validation (3 error paths).

    Three error paths in _validate_aggregation_on_success_routing:
    - dag.py:210: Terminal aggregation missing on_success
    - dag.py:217: Aggregation on_success references unknown sink
    - dag.py:228: Non-terminal aggregation with on_success set
    """

    def test_terminal_aggregation_missing_on_success_raises(self, plugin_manager) -> None:
        """Terminal aggregation without on_success raises GraphValidationError."""
        from elspeth.core.config import (
            AggregationSettings,
            ElspethSettings,
            SinkSettings,
            SourceSettings,
            TriggerConfig,
        )
        from elspeth.core.dag import ExecutionGraph, GraphValidationError

        config = ElspethSettings(
            sources={
                "primary": _source_settings(
                    SourceSettings,
                    plugin="csv",
                    on_success="to_agg",
                    options={
                        "path": "test.csv",
                        "on_validation_failure": "discard",
                        "schema": {"mode": "observed"},
                    },
                )
            },
            sinks={
                "output": SinkSettings(
                    plugin="json",
                    on_write_failure="discard",
                    options={"path": "output.json", "schema": {"mode": "observed"}},
                ),
            },
            aggregations=[
                _aggregation_settings(
                    AggregationSettings,
                    name="batch_stats",
                    plugin="batch_stats",
                    input="to_agg",
                    trigger=TriggerConfig(count=10),
                    output_mode="transform",
                    options={
                        "value_field": "value",
                        "schema": {"mode": "observed"},
                        # No on_success — terminal aggregation must have it
                    },
                ),
            ],
        )

        plugins = instantiate_plugins_from_config_raw(config)
        with pytest.raises(GraphValidationError, match=r"Dangling output connections"):
            ExecutionGraph.from_plugin_instances(
                sources=plugins.sources,
                source_settings_map=plugins.source_settings_map,
                transforms=plugins.transforms,
                sinks=plugins.sinks,
                aggregations=plugins.aggregations,
                gates=list(config.gates),
            )

    def test_terminal_aggregation_unknown_sink_raises(self, plugin_manager) -> None:
        """Terminal aggregation on_success referencing unknown sink raises GraphValidationError."""
        from elspeth.core.config import (
            AggregationSettings,
            ElspethSettings,
            SinkSettings,
            SourceSettings,
            TriggerConfig,
        )
        from elspeth.core.dag import ExecutionGraph, GraphValidationError

        config = ElspethSettings(
            sources={
                "primary": _source_settings(
                    SourceSettings,
                    plugin="csv",
                    on_success="to_agg",
                    options={
                        "path": "test.csv",
                        "on_validation_failure": "discard",
                        "schema": {"mode": "observed"},
                    },
                )
            },
            sinks={
                "output": SinkSettings(
                    plugin="json",
                    on_write_failure="discard",
                    options={"path": "output.json", "schema": {"mode": "observed"}},
                ),
            },
            aggregations=[
                _aggregation_settings(
                    AggregationSettings,
                    name="batch_stats",
                    plugin="batch_stats",
                    input="to_agg",
                    trigger=TriggerConfig(count=10),
                    output_mode="transform",
                    options={
                        "value_field": "value",
                        "schema": {"mode": "observed"},
                        "on_success": "nonexistent_sink",
                    },
                ),
            ],
        )

        plugins = instantiate_plugins_from_config_raw(config)
        with pytest.raises(GraphValidationError, match=r"on_success 'nonexistent_sink' is neither a sink nor a known connection"):
            ExecutionGraph.from_plugin_instances(
                sources=plugins.sources,
                source_settings_map=plugins.source_settings_map,
                transforms=plugins.transforms,
                sinks=plugins.sinks,
                aggregations=plugins.aggregations,
                gates=list(config.gates),
            )

    def test_non_terminal_aggregation_with_on_success_routes_to_sink(self, plugin_manager) -> None:
        """Aggregation with on_success pointing to a sink routes correctly.

        In the new WiredTransform system, aggregation on_success creates a terminal
        edge to the sink. The gate separately consumes from its own input connection.
        """
        from elspeth.core.config import (
            AggregationSettings,
            ElspethSettings,
            SinkSettings,
            SourceSettings,
            TriggerConfig,
        )
        from elspeth.core.dag import ExecutionGraph

        config = ElspethSettings(
            sources={
                "primary": _source_settings(
                    SourceSettings,
                    plugin="csv",
                    on_success="to_agg",
                    options={
                        "path": "test.csv",
                        "on_validation_failure": "discard",
                        "schema": {"mode": "observed"},
                    },
                )
            },
            sinks={
                "output": SinkSettings(
                    plugin="json",
                    on_write_failure="discard",
                    options={"path": "output.json", "schema": {"mode": "observed"}},
                ),
            },
            aggregations=[
                _aggregation_settings(
                    AggregationSettings,
                    name="batch_stats",
                    plugin="batch_stats",
                    input="to_agg",
                    trigger=TriggerConfig(count=10),
                    output_mode="transform",
                    options={
                        "value_field": "value",
                        "schema": {"mode": "observed"},
                        "on_success": "output",
                    },
                ),
            ],
        )

        plugins = instantiate_plugins_from_config_raw(config)
        graph = ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=list(config.gates),
        )
        graph.validate()


class TestConnectionNamespaceValidation:
    """Unit tests for _validate_connection_namespaces invariants."""

    def test_sink_and_connection_name_collision_raises(self) -> None:
        """Connection namespace must be disjoint from sink namespace."""
        from elspeth.contracts.types import NodeID
        from elspeth.core.dag import ExecutionGraph, GraphValidationError

        with pytest.raises(GraphValidationError, match="overlap with sink names"):
            ExecutionGraph._validate_connection_namespaces(
                producers={"shared_name": (NodeID("transform-1"), "continue")},
                consumers={"shared_name": NodeID("transform-2")},
                consumer_claims=[("shared_name", NodeID("transform-2"), "transform 'downstream'")],
                sink_names={"shared_name"},
                check_dangling=False,
            )
