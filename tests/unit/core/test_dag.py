"""DAG validation, coalesce, and schema compatibility tests."""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.core.config import GateSettings
from elspeth.core.dag import ExecutionGraph
from tests.fixtures.dag import (
    _gate_settings,
    _source_settings,
    instantiate_plugins_from_config,
    instantiate_plugins_from_config_raw,
)


class TestDAGValidation:
    """Validation of execution graphs."""

    def test_is_valid_for_acyclic(self) -> None:
        from elspeth.contracts import NodeType
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("a", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("b", node_type=NodeType.TRANSFORM, plugin_name="x")
        graph.add_node("c", node_type=NodeType.SINK, plugin_name="csv")
        graph.add_edge("a", "b", label="continue")
        graph.add_edge("b", "c", label="continue")

        assert graph.is_acyclic() is True

    def test_is_invalid_for_cycle(self) -> None:
        from elspeth.contracts import NodeType
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("a", node_type=NodeType.TRANSFORM, plugin_name="x")
        graph.add_node("b", node_type=NodeType.TRANSFORM, plugin_name="y")
        graph.add_edge("a", "b", label="continue")
        graph.add_edge("b", "a", label="continue")  # Creates cycle!

        assert graph.is_acyclic() is False

    def test_validate_raises_on_cycle(self) -> None:
        from elspeth.contracts import NodeType
        from elspeth.core.dag import ExecutionGraph, GraphValidationError

        graph = ExecutionGraph()
        graph.add_node("a", node_type=NodeType.TRANSFORM, plugin_name="x")
        graph.add_node("b", node_type=NodeType.TRANSFORM, plugin_name="y")
        graph.add_edge("a", "b", label="continue")
        graph.add_edge("b", "a", label="continue")

        with pytest.raises(GraphValidationError, match="cycle"):
            graph.validate()

    def test_topological_order(self) -> None:
        from elspeth.contracts import NodeType
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("t1", node_type=NodeType.TRANSFORM, plugin_name="a")
        graph.add_node("t2", node_type=NodeType.TRANSFORM, plugin_name="b")
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv")

        graph.add_edge("source", "t1", label="continue")
        graph.add_edge("t1", "t2", label="continue")
        graph.add_edge("t2", "sink", label="continue")

        order = graph.topological_order()

        # Source must come first, sink must come last
        assert order[0] == "source"
        assert order[-1] == "sink"
        # t1 must come before t2
        assert order.index("t1") < order.index("t2")

    def test_validate_rejects_duplicate_outgoing_edge_labels(self) -> None:
        """Duplicate outgoing edge labels from same node must be rejected.

        The orchestrator's edge_map keys by (from_node, label), so duplicate
        labels from the same node would cause silent overwrites during
        registration - routing events would be recorded against the wrong
        edge, corrupting the audit trail.
        """
        from elspeth.contracts import NodeType
        from elspeth.core.dag import ExecutionGraph, GraphValidationError

        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("gate", node_type=NodeType.GATE, plugin_name="config_gate")
        graph.add_node("sink_a", node_type=NodeType.SINK, plugin_name="csv")
        graph.add_node("sink_b", node_type=NodeType.SINK, plugin_name="csv")

        # Gate has one "continue" edge to sink_a
        graph.add_edge("source", "gate", label="continue")
        graph.add_edge("gate", "sink_a", label="continue")
        # Add ANOTHER "continue" edge to a different sink - this is the collision
        graph.add_edge("gate", "sink_b", label="continue")

        with pytest.raises(GraphValidationError, match="duplicate outgoing edge label"):
            graph.validate()

    def test_validate_allows_same_label_from_different_nodes(self) -> None:
        """Same label from different nodes is allowed (no collision).

        The uniqueness constraint is per-node, not global. Multiple nodes
        can each have a 'continue' edge because edge_map keys by (from_node, label).
        """
        from elspeth.contracts import NodeType
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("t1", node_type=NodeType.TRANSFORM, plugin_name="a")
        graph.add_node("t2", node_type=NodeType.TRANSFORM, plugin_name="b")
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv")

        # Each node has ONE "continue" edge - no collisions
        graph.add_edge("source", "t1", label="continue")
        graph.add_edge("t1", "t2", label="continue")
        graph.add_edge("t2", "sink", label="continue")

        # Should not raise - labels are unique per source node
        graph.validate()

    def test_validate_rejects_disconnected_graph(self) -> None:
        """Graphs with unreachable nodes must be rejected.

        If a node cannot be reached from the source, it will never execute.
        This is either a configuration error or indicates orphaned nodes.
        """
        from elspeth.contracts import NodeType
        from elspeth.core.dag import ExecutionGraph, GraphValidationError

        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("transform", node_type=NodeType.TRANSFORM, plugin_name="process")
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv")
        graph.add_node("orphan", node_type=NodeType.TRANSFORM, plugin_name="abandoned")  # Not connected!

        graph.add_edge("source", "transform", label="continue")
        graph.add_edge("transform", "sink", label="continue")
        # orphan has no incoming edges - unreachable

        with pytest.raises(GraphValidationError, match=r"unreachable|disconnected"):
            graph.validate()

    def test_validate_rejects_self_loop(self) -> None:
        """Self-loops (node with edge to itself) must be rejected.

        A node cannot route to itself - this would create infinite recursion.
        Should be caught as a special case of cycle detection.
        """
        from elspeth.contracts import NodeType
        from elspeth.core.dag import ExecutionGraph, GraphValidationError

        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("transform", node_type=NodeType.TRANSFORM, plugin_name="loop")
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv")

        graph.add_edge("source", "transform", label="continue")
        graph.add_edge("transform", "transform", label="retry")  # Self-loop!
        graph.add_edge("transform", "sink", label="continue")

        with pytest.raises(GraphValidationError, match=r"cycle|self-loop"):
            graph.validate()

    def test_validate_allows_diamond_merge(self) -> None:
        """Diamond patterns (node with multiple incoming edges) should be allowed.

        A node can have multiple incoming edges as long as the graph is acyclic.
        Common pattern: fork → parallel processing → merge.
        """
        from elspeth.contracts import NodeType
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("gate", node_type=NodeType.GATE, plugin_name="fork_gate")
        graph.add_node("path_a", node_type=NodeType.TRANSFORM, plugin_name="fast")
        graph.add_node("path_b", node_type=NodeType.TRANSFORM, plugin_name="slow")
        graph.add_node("merge", node_type=NodeType.COALESCE, plugin_name="merge_results")
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv")

        graph.add_edge("source", "gate", label="continue")
        graph.add_edge("gate", "path_a", label="path_a")
        graph.add_edge("gate", "path_b", label="path_b")
        graph.add_edge("path_a", "merge", label="continue")  # First incoming edge
        graph.add_edge("path_b", "merge", label="continue")  # Second incoming edge (diamond!)
        graph.add_edge("merge", "sink", label="continue")

        # Should not raise - diamond merge is valid
        graph.validate()


class TestSchemaContractValidation:
    """Schema contract validation during DAG construction."""

    def test_unsatisfiable_contract_dependencies_detected(self) -> None:
        """Impossible contract dependencies should be detected during validation.

        Scenario: Transform B requires field from Transform A's output, but A comes
        AFTER B in the pipeline (impossible to satisfy).

        This documents the "circular contract dependencies" edge case: when contract
        requirements create a logical impossibility in a linear pipeline.
        """
        from elspeth.contracts import NodeType
        from elspeth.contracts.schema import SchemaConfig
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()

        # Build a linear pipeline: source → transform_b → transform_a → sink
        # But transform_b requires a field that only transform_a produces (impossible!)

        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node(
            "transform_b",
            node_type=NodeType.TRANSFORM,
            plugin_name="requires_a_output",
            config={"required_input_fields": ["field_from_a"]},  # Requires A's output
        )
        graph.add_node(
            "transform_a",
            node_type=NodeType.TRANSFORM,
            plugin_name="produces_output",
            output_schema_config=SchemaConfig.from_dict({"mode": "observed", "guaranteed_fields": ["field_from_a"]}),  # Produces the field
        )
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv")

        # Pipeline order means B can't see A's output
        graph.add_edge("source", "transform_b", label="continue")
        graph.add_edge("transform_b", "transform_a", label="continue")
        graph.add_edge("transform_a", "sink", label="continue")

        # Should raise during edge validation - B requires field that isn't available
        with pytest.raises(ValueError, match=r"required field|missing field|unsatisfied"):
            graph.validate_edge_compatibility()


class TestSourceSinkValidation:
    """Validation of source and sink constraints."""

    def test_validate_requires_at_least_one_source(self) -> None:
        from elspeth.contracts import NodeType
        from elspeth.core.dag import ExecutionGraph, GraphValidationError

        graph = ExecutionGraph()
        graph.add_node("t1", node_type=NodeType.TRANSFORM, plugin_name="x")
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv")
        graph.add_edge("t1", "sink", label="continue")

        with pytest.raises(GraphValidationError, match="at least one source"):
            graph.validate()

    def test_validate_requires_at_least_one_sink(self) -> None:
        from elspeth.contracts import NodeType
        from elspeth.core.dag import ExecutionGraph, GraphValidationError

        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("t1", node_type=NodeType.TRANSFORM, plugin_name="x")
        graph.add_edge("source", "t1", label="continue")

        with pytest.raises(GraphValidationError, match="at least one sink"):
            graph.validate()

    def test_validate_multiple_sinks_allowed(self) -> None:
        from elspeth.contracts import NodeType
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("gate", node_type=NodeType.GATE, plugin_name="classifier")
        graph.add_node("sink1", node_type=NodeType.SINK, plugin_name="csv")
        graph.add_node("sink2", node_type=NodeType.SINK, plugin_name="csv")

        graph.add_edge("source", "gate", label="continue")
        graph.add_edge("gate", "sink1", label="normal")
        graph.add_edge("gate", "sink2", label="flagged")

        # Should not raise
        graph.validate()

    def test_validate_catches_gate_sink_edge_without_route_label(self) -> None:
        """validate() rejects gate→sink edges missing from _route_label_map."""
        from elspeth.contracts import NodeType, RoutingMode
        from elspeth.contracts.types import NodeID, SinkName
        from elspeth.core.dag import ExecutionGraph, GraphValidationError

        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("gate", node_type=NodeType.GATE, plugin_name="config_gate")
        graph.add_node("sink_a", node_type=NodeType.SINK, plugin_name="csv")
        graph.add_node("sink_b", node_type=NodeType.SINK, plugin_name="csv")
        graph.add_edge("source", "gate", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("gate", "sink_a", label="true", mode=RoutingMode.MOVE)
        graph.add_edge("gate", "sink_b", label="false", mode=RoutingMode.MOVE)
        # Register route label for sink_a but NOT for sink_b
        graph.add_route_label_entry(NodeID("gate"), SinkName("sink_a"), "true")
        graph.set_sink_id_map({SinkName("sink_a"): NodeID("sink_a"), SinkName("sink_b"): NodeID("sink_b")})

        with pytest.raises(GraphValidationError, match=r"direct edge to sink node 'sink_b'.*no registered route label"):
            graph.validate()

    def test_validate_allows_gate_copy_edges_to_sinks_without_route_labels(self) -> None:
        """COPY edges (fork paths) from gates to sinks don't require route labels."""
        from elspeth.contracts import NodeType, RoutingMode
        from elspeth.contracts.types import NodeID, SinkName
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("gate", node_type=NodeType.GATE, plugin_name="config_gate")
        graph.add_node("sink_a", node_type=NodeType.SINK, plugin_name="csv")
        graph.add_node("sink_b", node_type=NodeType.SINK, plugin_name="csv")
        graph.add_edge("source", "gate", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("gate", "sink_a", label="branch_a", mode=RoutingMode.COPY)
        graph.add_edge("gate", "sink_b", label="branch_b", mode=RoutingMode.COPY)
        # No route labels registered — COPY edges are exempt
        graph.set_sink_id_map(
            {
                SinkName("sink_a"): NodeID("sink_a"),
                SinkName("sink_b"): NodeID("sink_b"),
            }
        )

        # Should NOT raise — COPY edges don't need route labels
        graph.validate()

    def test_validate_catches_sink_missing_from_sink_id_map(self) -> None:
        """validate() rejects sink nodes that exist in graph but not in _sink_id_map."""
        from elspeth.contracts import NodeType, RoutingMode
        from elspeth.contracts.types import NodeID, SinkName
        from elspeth.core.dag import ExecutionGraph, GraphValidationError

        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("gate", node_type=NodeType.GATE, plugin_name="config_gate")
        graph.add_node("sink_a", node_type=NodeType.SINK, plugin_name="csv")
        graph.add_node("sink_b", node_type=NodeType.SINK, plugin_name="csv")
        graph.add_edge("source", "gate", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("gate", "sink_a", label="true", mode=RoutingMode.MOVE)
        graph.add_edge("gate", "sink_b", label="false", mode=RoutingMode.MOVE)
        graph.add_route_label_entry(NodeID("gate"), SinkName("sink_a"), "true")
        # Register only sink_a in the ID map — sink_b is missing
        graph.set_sink_id_map({SinkName("sink_a"): NodeID("sink_a")})

        with pytest.raises(GraphValidationError, match=r"Sink node 'sink_b'.*not registered.*sink ID map"):
            graph.validate()

    def test_validate_allows_mixed_move_and_copy_edges_from_gate(self) -> None:
        """Gate with both MOVE (route) and COPY (fork) edges to sinks validates correctly.

        MOVE edges require route labels; COPY edges are exempt. Validates that
        the per-edge mode filtering works when the same gate has both types.
        """
        from elspeth.contracts import NodeType, RoutingMode
        from elspeth.contracts.types import NodeID, SinkName
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("gate", node_type=NodeType.GATE, plugin_name="config_gate")
        graph.add_node("sink_routed", node_type=NodeType.SINK, plugin_name="csv")
        graph.add_node("sink_forked", node_type=NodeType.SINK, plugin_name="csv")
        graph.add_edge("source", "gate", label="continue", mode=RoutingMode.MOVE)
        # MOVE edge (route decision) — needs route label
        graph.add_edge("gate", "sink_routed", label="flagged", mode=RoutingMode.MOVE)
        # COPY edge (fork fan-out) — exempt from route label requirement
        graph.add_edge("gate", "sink_forked", label="branch_copy", mode=RoutingMode.COPY)
        graph.add_route_label_entry(NodeID("gate"), SinkName("sink_routed"), "flagged")
        graph.set_sink_id_map(
            {
                SinkName("sink_routed"): NodeID("sink_routed"),
                SinkName("sink_forked"): NodeID("sink_forked"),
            }
        )

        # Should NOT raise — MOVE has label, COPY is exempt
        graph.validate()

    def test_get_source_node(self) -> None:
        from elspeth.contracts import NodeType
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("my_source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv")
        graph.add_edge("my_source", "sink", label="continue")

        assert graph.get_sources()[0] == "my_source"

    def test_get_sources_returns_empty_when_no_source(self) -> None:
        """get_sources() returns an empty list when graph has no source node."""
        from elspeth.contracts import NodeType
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv")

        assert graph.get_sources() == []

    def test_get_sources_returns_all_sources(self) -> None:
        """get_sources() returns every source node in a plural-source graph."""
        from elspeth.contracts import NodeType
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("source1", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("source2", node_type=NodeType.SOURCE, plugin_name="json")
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv")

        assert graph.get_sources() == ["source1", "source2"]

    def test_get_sink_nodes(self) -> None:
        from elspeth.contracts import NodeType
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("sink1", node_type=NodeType.SINK, plugin_name="csv")
        graph.add_node("sink2", node_type=NodeType.SINK, plugin_name="json")
        graph.add_edge("source", "sink1", label="continue")
        graph.add_edge("source", "sink2", label="continue")

        sinks = graph.get_sinks()
        assert set(sinks) == {"sink1", "sink2"}


class TestCoalesceNodes:
    """Test coalesce node creation in DAG."""

    def test_from_config_creates_coalesce_node(self, plugin_manager) -> None:
        """Coalesce config should create a coalesce node in the graph."""
        from elspeth.contracts import CoalesceName, NodeType
        from elspeth.core.config import (
            CoalesceSettings,
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )
        from elspeth.core.dag import ExecutionGraph

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
                "output": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "out.json", "schema": {"mode": "observed"}}
                ),
            },
            gates=[
                _gate_settings(
                    GateSettings,
                    name="forker",
                    condition="True",
                    routes={"true": "fork", "false": "output"},
                    fork_to=["path_a", "path_b"],
                ),
            ],
            coalesce=[
                CoalesceSettings(
                    name="merge_results",
                    branches=["path_a", "path_b"],
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

        # Use proper accessor, not string matching
        coalesce_map = graph.get_coalesce_id_map()
        assert CoalesceName("merge_results") in coalesce_map

        # Verify node type
        node_id = coalesce_map[CoalesceName("merge_results")]
        node_info = graph.get_node_info(node_id)
        assert node_info.node_type == NodeType.COALESCE
        assert node_info.plugin_name == "coalesce:merge_results"

    def test_from_config_coalesce_edges_from_fork_branches(self, plugin_manager) -> None:
        """Coalesce node should have edges from fork gate (via branches)."""
        from elspeth.contracts import CoalesceName, GateName, RoutingMode
        from elspeth.core.config import (
            CoalesceSettings,
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )
        from elspeth.core.dag import ExecutionGraph

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
                "output": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "output.json", "schema": {"mode": "observed"}}
                ),
            },
            gates=[
                _gate_settings(
                    GateSettings,
                    name="forker",
                    condition="True",
                    routes={"true": "fork", "false": "output"},
                    fork_to=["path_a", "path_b"],
                ),
            ],
            coalesce=[
                CoalesceSettings(
                    name="merge_results",
                    branches=["path_a", "path_b"],
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

        # Get node IDs
        gate_id = graph.get_config_gate_id_map()[GateName("forker")]
        coalesce_id = graph.get_coalesce_id_map()[CoalesceName("merge_results")]

        # Verify edges from fork gate to coalesce node
        edges = graph.get_edges()
        gate_to_coalesce_edges = [e for e in edges if e.from_node == gate_id and e.to_node == coalesce_id]

        # Should have two edges (path_a and path_b) with COPY mode
        assert len(gate_to_coalesce_edges) == 2
        labels = {e.label for e in gate_to_coalesce_edges}
        assert labels == {"path_a", "path_b"}
        assert all(e.mode == RoutingMode.COPY for e in gate_to_coalesce_edges)

    def test_duplicate_fork_branches_rejected_in_config_gate(self, plugin_manager) -> None:
        """Duplicate branch names in fork_to should be rejected for config gates."""
        from elspeth.core.config import ElspethSettings, SinkSettings, SourceSettings
        from elspeth.core.dag import ExecutionGraph, GraphValidationError

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
                "output": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "output.json", "schema": {"mode": "observed"}}
                ),
                "path_a": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "path_a.json", "schema": {"mode": "observed"}}
                ),
            },
            gates=[
                _gate_settings(
                    GateSettings,
                    name="forker",
                    condition="True",
                    routes={"true": "fork", "false": "output"},
                    fork_to=["path_a", "path_a"],  # Duplicate branch name
                ),
            ],
        )

        plugins = instantiate_plugins_from_config(settings)

        with pytest.raises(GraphValidationError, match=r"duplicate fork branches"):
            ExecutionGraph.from_plugin_instances(
                sources=plugins.sources,
                source_settings_map=plugins.source_settings_map,
                transforms=plugins.transforms,
                sinks=plugins.sinks,
                aggregations=plugins.aggregations,
                gates=list(settings.gates),
                coalesce_settings=settings.coalesce,
            )

    def test_partial_branch_coverage_branches_not_in_coalesce_is_mixed_closure(
        self,
        plugin_manager,
    ) -> None:
        """Whole-roster fork closure (spec §7 rule 2, ruling 23): a fork with
        one branch bound to a coalesce and a sibling branch routed direct to
        a sink is MIXED closure, rejected at build time. Pre-ruling-23 this
        subset shape silently built (branches outside the coalesce fell
        through to the sink); ruling 23 retired that interim — a fork is
        either fully bound (every declared branch closes at the SAME
        barrier) or fully unbound (pure fan-out), never a mix.
        """
        from elspeth.core.config import (
            CoalesceSettings,
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )
        from elspeth.core.dag import ExecutionGraph, GraphValidationError

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
                "output": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "output.json", "schema": {"mode": "observed"}}
                ),
                "path_c": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "path_c.json", "schema": {"mode": "observed"}}
                ),
            },
            gates=[
                _gate_settings(
                    GateSettings,
                    name="forker",
                    condition="True",
                    routes={"true": "fork", "false": "output"},
                    fork_to=["path_a", "path_b", "path_c"],  # 3 branches
                ),
            ],
            coalesce=[
                CoalesceSettings(
                    name="merge_results",
                    branches=["path_a", "path_b"],  # Only 2 branches in coalesce
                    policy="require_all",
                    merge="union",
                ),
            ],
        )

        plugins = instantiate_plugins_from_config(settings)

        with pytest.raises(GraphValidationError, match="mixed closure"):
            ExecutionGraph.from_plugin_instances(
                sources=plugins.sources,
                source_settings_map=plugins.source_settings_map,
                transforms=plugins.transforms,
                sinks=plugins.sinks,
                aggregations=plugins.aggregations,
                gates=list(settings.gates),
                coalesce_settings=settings.coalesce,
            )

    def test_get_coalesce_id_map_returns_mapping(self, plugin_manager) -> None:
        """get_coalesce_id_map should return coalesce_name -> node_id.

        Two SEQUENTIAL (not nested) fork/coalesce pairs, each whole-roster
        bound to its own single coalesce (spec §7 rule 2, ruling 23): one
        fork closing at TWO sibling coalesces is a multi-closer split and
        now build-rejected, so this fixture cannot use a single shared fork
        the way it did pre-ruling-23.
        """
        from elspeth.contracts import CoalesceName
        from elspeth.core.config import (
            CoalesceSettings,
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )
        from elspeth.core.dag import ExecutionGraph

        settings = ElspethSettings(
            sources={
                "primary": SourceSettings(
                    plugin="csv",
                    on_success="routed",
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
            },
            gates=[
                GateSettings(
                    name="forker_ab",
                    input="routed",
                    condition="True",
                    routes={"true": "fork", "false": "output"},
                    fork_to=["path_a", "path_b"],
                ),
                GateSettings(
                    name="forker_cd",
                    input="merge_ab",
                    condition="True",
                    routes={"true": "fork", "false": "output"},
                    fork_to=["path_c", "path_d"],
                ),
            ],
            coalesce=[
                CoalesceSettings(
                    name="merge_ab",
                    branches=["path_a", "path_b"],
                    policy="require_all",
                    merge="union",
                ),
                CoalesceSettings(
                    name="merge_cd",
                    branches=["path_c", "path_d"],
                    policy="require_all",
                    merge="nested",
                    on_success="output",
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
        coalesce_map = graph.get_coalesce_id_map()

        # Should have both coalesce nodes
        assert CoalesceName("merge_ab") in coalesce_map
        assert CoalesceName("merge_cd") in coalesce_map

        # Node IDs should be unique
        assert coalesce_map[CoalesceName("merge_ab")] != coalesce_map[CoalesceName("merge_cd")]

        # Verify both nodes exist in the graph
        assert graph.has_node(coalesce_map[CoalesceName("merge_ab")])
        assert graph.has_node(coalesce_map[CoalesceName("merge_cd")])

    def test_get_branch_to_coalesce_map_returns_mapping(self, plugin_manager) -> None:
        """get_branch_to_coalesce_map should return branch_name -> coalesce_name."""
        from elspeth.contracts import BranchName
        from elspeth.core.config import (
            CoalesceSettings,
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )
        from elspeth.core.dag import ExecutionGraph

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
                "output": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "output.json", "schema": {"mode": "observed"}}
                ),
            },
            gates=[
                _gate_settings(
                    GateSettings,
                    name="forker",
                    condition="True",
                    routes={"true": "fork", "false": "output"},
                    fork_to=["path_a", "path_b"],
                ),
            ],
            coalesce=[
                CoalesceSettings(
                    name="merge_results",
                    branches=["path_a", "path_b"],
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
        branch_map = graph.get_branch_to_coalesce_map()

        # Should map branches to coalesce_name (not node_id) for processor node-map lookup
        assert branch_map[BranchName("path_a")] == "merge_results"
        assert branch_map[BranchName("path_b")] == "merge_results"

    def test_branch_to_coalesce_maps_to_coalesce_name_for_node_lookup(self, plugin_manager) -> None:
        """branch_to_coalesce should map to coalesce_name (not node_id) for coalesce node lookup.

        BUG-LINEAGE-01: The processor needs to look up coalesce node position using:
            coalesce_name = branch_to_coalesce[branch_name]
            coalesce_node_id = coalesce_node_map[coalesce_name]

        But branch_to_coalesce was mapping branch_name -> node_id, causing KeyError
        when trying to look up in coalesce_node_map which expects coalesce_name keys.
        """
        from elspeth.contracts import BranchName
        from elspeth.core.config import (
            CoalesceSettings,
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )
        from elspeth.core.dag import ExecutionGraph

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
                "output": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "output.json", "schema": {"mode": "observed"}}
                ),
            },
            gates=[
                _gate_settings(
                    GateSettings,
                    name="forker",
                    condition="True",
                    routes={"true": "fork", "false": "output"},
                    fork_to=["analysis_path", "validation_path"],
                ),
            ],
            coalesce=[
                CoalesceSettings(
                    name="join_point",
                    branches=["analysis_path", "validation_path"],
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

        branch_map = graph.get_branch_to_coalesce_map()

        # CRITICAL: Must map to coalesce_name (not node_id) for processor node lookup
        # The processor does: coalesce_node_map[branch_to_coalesce[branch_name]]
        # coalesce_node_map has keys like "join_point", NOT node_ids like "coalesce_join_point_abc123"
        assert branch_map[BranchName("analysis_path")] == "join_point", (
            f"Expected coalesce_name 'join_point', got {branch_map[BranchName('analysis_path')]}"
        )
        assert branch_map[BranchName("validation_path")] == "join_point", (
            f"Expected coalesce_name 'join_point', got {branch_map[BranchName('validation_path')]}"
        )

    def test_duplicate_branch_names_across_coalesces_rejected(self, plugin_manager) -> None:
        """Duplicate branch names across coalesce settings should be rejected."""
        from elspeth.core.config import (
            CoalesceSettings,
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )
        from elspeth.core.dag import ExecutionGraph, GraphValidationError

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
                "output": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "output.json", "schema": {"mode": "observed"}}
                ),
            },
            gates=[
                _gate_settings(
                    GateSettings,
                    name="forker",
                    condition="True",
                    routes={"true": "fork", "false": "output"},
                    fork_to=["path_a", "path_b", "path_x"],
                ),
            ],
            coalesce=[
                CoalesceSettings(
                    name="merge_ab",
                    branches=["path_a", "path_b"],
                    policy="require_all",
                    merge="union",
                ),
                CoalesceSettings(
                    name="merge_xy",
                    branches=["path_a", "path_x"],  # path_a duplicated!
                    policy="require_all",
                    merge="union",
                ),
            ],
        )

        plugins = instantiate_plugins_from_config(settings)

        with pytest.raises(GraphValidationError, match="Duplicate branch name 'path_a'"):
            ExecutionGraph.from_plugin_instances(
                sources=plugins.sources,
                source_settings_map=plugins.source_settings_map,
                transforms=plugins.transforms,
                sinks=plugins.sinks,
                aggregations=plugins.aggregations,
                gates=list(settings.gates),
                coalesce_settings=settings.coalesce,
            )

    def test_empty_coalesce_branches_rejected(self, plugin_manager) -> None:
        """Coalesce with empty branches list should be rejected by Pydantic."""
        from pydantic import ValidationError

        from elspeth.core.config import (
            CoalesceSettings,
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )

        # Pydantic validates min_length=2 for branches field
        with pytest.raises(ValidationError, match="at least 2 items"):
            ElspethSettings(
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
                },
                coalesce=[
                    CoalesceSettings(
                        name="empty_merge",
                        branches=[],  # Invalid! Pydantic requires min_length=2
                        policy="require_all",
                        merge="union",
                    ),
                ],
            )

    def test_coalesce_branch_not_produced_by_any_gate_rejected(self, plugin_manager) -> None:
        """Coalesce referencing non-existent fork branches should be rejected.

        Vehicle reshaped for whole-roster fork closure (spec §7 rule 2,
        ruling 23): the original fixture routed 'path_b' direct to a sink
        alongside a coalesce-bound 'path_a', which is MIXED closure and now
        fires first — an unrelated defect in the fixture's own scaffolding,
        not what this test means to pin (same reshape Task 6 applied to its
        own row_union fallout). Both declared fork branches now close at the
        coalesce; only 'path_x' is a genuine orphan no gate produces, which
        isolates the roster-mismatch message this test checks for.

        Naming note (review F10, 2026-08-23): after the reshape, this pins
        rule 2's aggregate closer-roster-mismatch site (`builder.py` ~935,
        "…drawn from 1 fork gate(s)… no gate produces […]…"), not the
        dedicated "coalesce declares branch X but no gate produces this
        branch" check (`builder.py` ~949) the test's name and original
        fixture were written against. The dedicated check has no test
        naming it directly as of this fix round.
        """
        from elspeth.core.config import (
            CoalesceSettings,
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )
        from elspeth.core.dag import ExecutionGraph, GraphValidationError

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
                "output": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "output.json", "schema": {"mode": "observed"}}
                ),
            },
            gates=[
                _gate_settings(
                    GateSettings,
                    name="forker",
                    condition="True",
                    routes={"true": "fork", "false": "output"},
                    fork_to=["path_a", "path_b"],
                ),
            ],
            coalesce=[
                CoalesceSettings(
                    name="merge_results",
                    branches=["path_a", "path_b", "path_x"],  # path_x not in fork_to!
                    policy="require_all",
                    merge="union",
                ),
            ],
        )

        plugins = instantiate_plugins_from_config(settings)

        with pytest.raises(GraphValidationError, match=r"no gate produces.*path_x"):
            ExecutionGraph.from_plugin_instances(
                sources=plugins.sources,
                source_settings_map=plugins.source_settings_map,
                transforms=plugins.transforms,
                sinks=plugins.sinks,
                aggregations=plugins.aggregations,
                gates=list(settings.gates),
                coalesce_settings=settings.coalesce,
            )

    def test_fork_coalesce_contract_branch_map_compatible_with_node_map(self, plugin_manager) -> None:
        """Contract test: branch_to_coalesce values must be usable as coalesce_node_map keys.

        This is the CRITICAL contract between DAG builder and Processor.
        The processor does: coalesce_node_map[branch_to_coalesce[branch_name]]
        This test ensures the production path (`from_plugin_instances`) produces compatible mappings.

        Two SEQUENTIAL (not nested) fork/coalesce pairs (spec §7 rule 2,
        ruling 23): one fork closing at TWO sibling coalesces is a
        multi-closer split and now build-rejected, so this fixture cannot
        use a single shared fork the way it did pre-ruling-23 (see
        `test_get_coalesce_id_map_returns_mapping`, same reshape).
        """
        from elspeth.core.config import (
            CoalesceSettings,
            ElspethSettings,
            SinkSettings,
            SourceSettings,
            TransformSettings,
        )
        from elspeth.core.dag import ExecutionGraph

        settings = ElspethSettings(
            sources={
                "primary": SourceSettings(
                    plugin="csv",
                    on_success="to_gate_in",
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
            },
            transforms=[
                TransformSettings(
                    name="passthrough",
                    plugin="passthrough",
                    input="to_gate_in",
                    on_success="routed",
                    on_error="discard",
                    options={"schema": {"mode": "observed"}},
                ),
            ],
            gates=[
                GateSettings(
                    name="forker_ab",
                    input="routed",
                    condition="True",
                    routes={"true": "fork", "false": "output"},
                    fork_to=["path_a", "path_b"],
                ),
                GateSettings(
                    name="forker_cd",
                    input="merge_ab",
                    condition="True",
                    routes={"true": "fork", "false": "output"},
                    fork_to=["path_c", "path_d"],
                ),
            ],
            coalesce=[
                CoalesceSettings(
                    name="merge_ab",
                    branches=["path_a", "path_b"],
                    policy="require_all",
                    merge="union",
                ),
                CoalesceSettings(
                    name="merge_cd",
                    branches=["path_c", "path_d"],
                    policy="require_all",
                    merge="union",
                    on_success="output",
                ),
            ],
        )

        plugins = instantiate_plugins_from_config(settings)

        # Use production path
        graph = ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=list(settings.gates),
            coalesce_settings=settings.coalesce,
        )

        # Get the mappings that processor would use
        branch_to_coalesce = graph.get_branch_to_coalesce_map()
        coalesce_node_map = graph.get_coalesce_id_map()

        # CRITICAL CONTRACT: Every value in branch_to_coalesce must be a key in coalesce_node_map.
        for branch_name, coalesce_name in branch_to_coalesce.items():
            assert coalesce_name in coalesce_node_map, (
                f"Contract violation: branch_to_coalesce['{branch_name}'] = '{coalesce_name}', "
                f"but '{coalesce_name}' not in coalesce_node_map keys: {list(coalesce_node_map.keys())}"
            )

            # Also verify it's the coalesce_name, not a node_id
            assert not coalesce_name.startswith("coalesce_"), (
                f"Contract violation: branch_to_coalesce['{branch_name}'] = '{coalesce_name}' "
                f"looks like a node_id (starts with 'coalesce_'), should be coalesce name"
            )

    def test_coalesce_node_has_edge_to_output_sink(self, plugin_manager) -> None:
        """Coalesce node should have an edge to the output sink."""
        from elspeth.contracts import CoalesceName, RoutingMode, SinkName
        from elspeth.core.config import (
            CoalesceSettings,
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )
        from elspeth.core.dag import ExecutionGraph

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
                "output": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "output.json", "schema": {"mode": "observed"}}
                ),
            },
            gates=[
                _gate_settings(
                    GateSettings,
                    name="forker",
                    condition="True",
                    routes={"true": "fork", "false": "output"},
                    fork_to=["path_a", "path_b"],
                ),
            ],
            coalesce=[
                CoalesceSettings(
                    name="merge_results",
                    branches=["path_a", "path_b"],
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

        coalesce_id = graph.get_coalesce_id_map()[CoalesceName("merge_results")]
        output_sink_id = graph.get_sink_id_map()[SinkName("output")]

        # Verify edge from coalesce to output sink
        edges = graph.get_edges()
        coalesce_to_sink_edges = [e for e in edges if e.from_node == coalesce_id and e.to_node == output_sink_id]

        assert len(coalesce_to_sink_edges) == 1
        assert coalesce_to_sink_edges[0].label == "on_success"
        assert coalesce_to_sink_edges[0].mode == RoutingMode.MOVE

    def test_coalesce_node_connects_to_next_gate(self, plugin_manager) -> None:
        """Coalesce node should continue to the next gate when one exists."""
        from elspeth.contracts import CoalesceName, GateName, RoutingMode
        from elspeth.core.config import (
            CoalesceSettings,
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )
        from elspeth.core.dag import ExecutionGraph

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
                "output": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "output.json", "schema": {"mode": "observed"}}
                ),
            },
            gates=[
                _gate_settings(
                    GateSettings,
                    name="forker1",
                    input="source_out",
                    condition="True",
                    routes={"true": "fork", "false": "output"},
                    fork_to=["path_a", "path_b"],
                ),
                _gate_settings(
                    GateSettings,
                    name="gate2",
                    input="merge_results",
                    condition="True",
                    routes={"true": "output", "false": "output"},
                ),
            ],
            coalesce=[
                CoalesceSettings(
                    name="merge_results",
                    branches=["path_a", "path_b"],
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

        coalesce_id = graph.get_coalesce_id_map()[CoalesceName("merge_results")]
        gate2_id = graph.get_config_gate_id_map()[GateName("gate2")]

        edges = graph.get_edges()
        coalesce_to_gate_edges = [e for e in edges if e.from_node == coalesce_id and e.to_node == gate2_id]

        assert len(coalesce_to_gate_edges) == 1
        assert coalesce_to_gate_edges[0].label == "continue"
        assert coalesce_to_gate_edges[0].mode == RoutingMode.MOVE

    def test_coalesce_node_stores_config(self, plugin_manager) -> None:
        """Coalesce node should store configuration for audit trail."""
        from elspeth.contracts import CoalesceName
        from elspeth.core.config import (
            CoalesceSettings,
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )
        from elspeth.core.dag import ExecutionGraph

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
                "output": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "output.json", "schema": {"mode": "observed"}}
                ),
            },
            gates=[
                _gate_settings(
                    GateSettings,
                    name="forker",
                    condition="True",
                    routes={"true": "fork", "false": "output"},
                    fork_to=["path_a", "path_b"],
                ),
            ],
            coalesce=[
                CoalesceSettings(
                    name="merge_results",
                    branches=["path_a", "path_b"],
                    policy="quorum",
                    quorum_count=1,
                    merge="nested",
                    timeout_seconds=30.0,
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

        coalesce_id = graph.get_coalesce_id_map()[CoalesceName("merge_results")]
        node_info = graph.get_node_info(coalesce_id)

        # Verify config is stored
        assert node_info.config["branches"] == {"path_a": "path_a", "path_b": "path_b"}
        assert node_info.config["policy"] == "quorum"
        assert node_info.config["merge"] == "nested"
        assert node_info.config["timeout_seconds"] == 30.0
        assert node_info.config["quorum_count"] == 1

    def test_union_collision_policy_binds_coalesce_config_and_identity_with_default_compatibility(
        self,
        plugin_manager,
    ) -> None:
        """Behavior-changing union policies must bind audit and fail-closed resume identity."""
        from datetime import UTC, datetime

        from elspeth.contracts import Checkpoint, CoalesceName
        from elspeth.core.canonical import compute_full_topology_hash
        from elspeth.core.checkpoint.compatibility import CheckpointCompatibilityValidator
        from elspeth.core.config import (
            CoalesceSettings,
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )
        from elspeth.core.dag import ExecutionGraph

        def build(policy: str | None) -> tuple[str, dict[str, Any], ExecutionGraph]:
            coalesce_kwargs: dict[str, Any] = {
                "name": "merge_results",
                "branches": ["path_a", "path_b"],
                "policy": "require_all",
                "merge": "union",
            }
            if policy is not None:
                coalesce_kwargs["union_collision_policy"] = policy
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
                    "output": SinkSettings(
                        plugin="json",
                        on_write_failure="discard",
                        options={"path": "output.json", "schema": {"mode": "observed"}},
                    ),
                },
                gates=[
                    _gate_settings(
                        GateSettings,
                        name="forker",
                        condition="True",
                        routes={"true": "fork", "false": "output"},
                        fork_to=["path_a", "path_b"],
                    ),
                ],
                coalesce=[CoalesceSettings(**coalesce_kwargs)],
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
            coalesce_id = graph.get_coalesce_id_map()[CoalesceName("merge_results")]
            return str(coalesce_id), dict(graph.get_node_info(coalesce_id).config), graph

        default_id, default_config, default_graph = build(None)
        explicit_last_id, explicit_last_config, explicit_last_graph = build("last_wins")
        first_id, first_config, first_graph = build("first_wins")
        fail_id, fail_config, fail_graph = build("fail")

        assert default_id == explicit_last_id
        assert len({default_id, first_id, fail_id}) == 3
        assert default_config == explicit_last_config
        assert {
            policy: config["union_collision_policy"]
            for policy, config in (
                ("last_wins", default_config),
                ("first_wins", first_config),
                ("fail", fail_config),
            )
        } == {
            "last_wins": "last_wins",
            "first_wins": "first_wins",
            "fail": "fail",
        }

        default_topology_hash = compute_full_topology_hash(default_graph)
        assert compute_full_topology_hash(explicit_last_graph) == default_topology_hash
        assert (
            len(
                {
                    default_topology_hash,
                    compute_full_topology_hash(first_graph),
                    compute_full_topology_hash(fail_graph),
                }
            )
            == 3
        )

        checkpoint = Checkpoint(
            checkpoint_id="cp-coalesce-policy",
            run_id="run-coalesce-policy",
            sequence_number=1,
            created_at=datetime.now(UTC),
            upstream_topology_hash=default_topology_hash,
            format_version=Checkpoint.CURRENT_FORMAT_VERSION,
        )
        validator = CheckpointCompatibilityValidator()
        assert validator.validate(checkpoint, explicit_last_graph).can_resume is True
        for drifted_graph in (first_graph, fail_graph):
            result = validator.validate(checkpoint, drifted_graph)
            assert result.can_resume is False
            assert result.reason is not None
            assert "Pipeline configuration changed since checkpoint was created." in result.reason

    def test_select_merge_coalesce_through_production_path(self, plugin_manager) -> None:
        """Select merge coalesce builds through from_plugin_instances() with select_branch.

        Exercises the select_branch code path in the builder: config propagation,
        schema copying from the selected branch, and validation.
        """
        from elspeth.contracts import CoalesceName, NodeType
        from elspeth.core.config import (
            CoalesceSettings,
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )
        from elspeth.core.dag import ExecutionGraph

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
                "output": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "output.json", "schema": {"mode": "observed"}}
                ),
            },
            gates=[
                _gate_settings(
                    GateSettings,
                    name="forker",
                    condition="True",
                    routes={"true": "fork", "false": "output"},
                    fork_to=["path_a", "path_b"],
                ),
            ],
            coalesce=[
                CoalesceSettings(
                    name="select_winner",
                    branches=["path_a", "path_b"],
                    policy="require_all",
                    merge="select",
                    select_branch="path_a",
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

        # Verify coalesce node exists with correct config
        coalesce_map = graph.get_coalesce_id_map()
        assert CoalesceName("select_winner") in coalesce_map

        coalesce_id = coalesce_map[CoalesceName("select_winner")]
        node_info = graph.get_node_info(coalesce_id)
        assert node_info.node_type == NodeType.COALESCE
        assert node_info.config["merge"] == "select"
        assert node_info.config["select_branch"] == "path_a"

        # Graph should validate without errors
        graph.validate()


class TestSchemaValidation:
    """Tests for graph-based schema compatibility validation."""

    def test_schema_validation_catches_gate_routing_to_incompatible_sink(self) -> None:
        """Gate routes to sink before required field is added - should fail validation.

        This is the bug scenario: A gate routes rows directly to a sink from an
        intermediate point in the pipeline, but the sink requires a field that
        hasn't been added yet. The old linear validator checked all sinks against
        the "final transform output", missing this incompatibility.
        """
        from elspeth.contracts import NodeType, PluginSchema
        from elspeth.core.dag import ExecutionGraph

        class SourceSchema(PluginSchema):
            """Source provides: name, quality."""

            name: str
            quality: str

        class AddScoreSchema(PluginSchema):
            """After add_score transform: name, quality, score."""

            name: str
            quality: str
            score: int

        class RawSinkSchema(PluginSchema):
            """Raw sink requires: name, quality, score."""

            name: str
            quality: str
            score: int  # NOT PROVIDED by source or gate!

        graph = ExecutionGraph()

        # Build pipeline: Source -> Gate -> [routes to raw_sink OR continues to add_score]
        graph.add_node(
            "src",
            node_type=NodeType.SOURCE,
            plugin_name="csv",
            output_schema=SourceSchema,
        )
        graph.add_node(
            "gate",
            node_type=NodeType.GATE,
            plugin_name="quality_gate",
            input_schema=SourceSchema,
            output_schema=SourceSchema,  # Gate doesn't modify data
        )
        graph.add_node(
            "add_score",
            node_type=NodeType.TRANSFORM,
            plugin_name="add_score_transform",
            input_schema=SourceSchema,
            output_schema=AddScoreSchema,  # Adds 'score' field
        )
        graph.add_node(
            "raw_sink",
            node_type=NodeType.SINK,
            plugin_name="csv",
            input_schema=RawSinkSchema,  # Requires 'score' field!
        )
        graph.add_node(
            "processed_sink",
            node_type=NodeType.SINK,
            plugin_name="csv",
            input_schema=AddScoreSchema,
        )

        # Edges
        graph.add_edge("src", "gate", label="continue")
        graph.add_edge("gate", "raw_sink", label="raw")  # BUG: Routes BEFORE add_score!
        graph.add_edge("gate", "add_score", label="continue")
        graph.add_edge("add_score", "processed_sink", label="continue")

        # The gate routes to raw_sink with SourceSchema (no 'score' field),
        # but raw_sink requires RawSinkSchema (with 'score' field).
        # validate_edge_compatibility() raises ValueError for schema mismatches
        with pytest.raises(ValueError, match="score"):
            graph.validate_edge_compatibility()

    def test_coalesce_rejects_incompatible_branch_schemas(self) -> None:
        """Coalesce with incompatible branch schemas should fail validation.

        CRITICAL P0 BLOCKER: Coalesce incompatible schema behavior was UNDEFINED.
        Manual graph construction bypasses config schema limitation that
        doesn't support per-branch transforms.
        """
        from elspeth.contracts import NodeType, PluginSchema, RoutingMode
        from elspeth.core.dag import ExecutionGraph

        class SourceOutput(PluginSchema):
            id: int
            name: str

        class BranchAOutput(PluginSchema):
            """Branch A adds score field."""

            id: int
            name: str
            score: float

        class BranchBOutput(PluginSchema):
            """Branch B adds rank field (different from Branch A)."""

            id: int
            name: str
            rank: int

        graph = ExecutionGraph()

        # Build fork/join DAG with INCOMPATIBLE branch schemas
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv", output_schema=SourceOutput)
        graph.add_node("fork_gate", node_type=NodeType.GATE, plugin_name="fork_gate")

        # Branch A: adds score field
        graph.add_node(
            "transform_a",
            node_type=NodeType.TRANSFORM,
            plugin_name="add_score",
            input_schema=SourceOutput,
            output_schema=BranchAOutput,
        )

        # Branch B: adds rank field (incompatible with Branch A)
        graph.add_node(
            "transform_b",
            node_type=NodeType.TRANSFORM,
            plugin_name="add_rank",
            input_schema=SourceOutput,
            output_schema=BranchBOutput,
        )

        # Coalesce attempts to merge incompatible schemas
        graph.add_node(
            "coalesce",
            node_type=NodeType.COALESCE,
            plugin_name="coalesce:merge",
            config={
                "branches": ["branch_a", "branch_b"],
                "policy": "require_all",
                "merge": "union",
            },
        )

        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv")

        # Build edges
        graph.add_edge("source", "fork_gate", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("fork_gate", "transform_a", label="branch_a", mode=RoutingMode.COPY)
        graph.add_edge("fork_gate", "transform_b", label="branch_b", mode=RoutingMode.COPY)
        graph.add_edge("transform_a", "coalesce", label="branch_a", mode=RoutingMode.MOVE)
        graph.add_edge("transform_b", "coalesce", label="branch_b", mode=RoutingMode.MOVE)
        graph.add_edge("coalesce", "sink", label="continue", mode=RoutingMode.MOVE)

        # Should crash: coalesce can't merge BranchAOutput and BranchBOutput
        # validate_edge_compatibility() raises ValueError for schema mismatches
        with pytest.raises(ValueError) as exc_info:
            graph.validate_edge_compatibility()

        # Error should mention incompatible schemas
        error_msg = str(exc_info.value).lower()
        assert "schema" in error_msg or "incompatible" in error_msg

    def test_gate_routes_to_multiple_processing_connections(self, plugin_manager) -> None:
        """Gate route labels can fan out to different downstream processing nodes."""
        from elspeth.contracts import GateName, RouteDestinationKind
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
                    on_success="to_router",
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
                "flagged": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "flagged.json", "schema": {"mode": "observed"}}
                ),
            },
            gates=[
                _gate_settings(
                    GateSettings,
                    name="router",
                    input="to_router",
                    condition="row['score'] > 0.5",
                    routes={"true": "high_path", "false": "low_path"},
                ),
                _gate_settings(
                    GateSettings,
                    name="high_gate",
                    input="high_path",
                    condition="True",
                    routes={"true": "output", "false": "output"},
                ),
                _gate_settings(
                    GateSettings,
                    name="low_gate",
                    input="low_path",
                    condition="True",
                    routes={"true": "flagged", "false": "flagged"},
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

        gate_ids = graph.get_config_gate_id_map()
        router_id = gate_ids[GateName("router")]
        high_gate_id = gate_ids[GateName("high_gate")]
        low_gate_id = gate_ids[GateName("low_gate")]

        router_edges = [edge for edge in graph.get_edges() if edge.from_node == router_id]
        router_targets = {(edge.label, edge.to_node) for edge in router_edges}
        assert ("true", high_gate_id) in router_targets
        assert ("false", low_gate_id) in router_targets

        route_map = graph.get_route_resolution_map()
        true_dest = route_map[(router_id, "true")]
        false_dest = route_map[(router_id, "false")]

        assert true_dest.kind == RouteDestinationKind.PROCESSING_NODE
        assert true_dest.next_node_id == high_gate_id
        assert false_dest.kind == RouteDestinationKind.PROCESSING_NODE
        assert false_dest.next_node_id == low_gate_id

    def test_coalesce_accepts_structurally_identical_schemas(self) -> None:
        """Coalesce should accept branches with structurally identical schemas.

        BUG FIX: P2-2026-01-30-coalesce-schema-identity-check
        Previously, coalesce validation compared schema classes by identity (!=),
        rejecting structurally identical schemas that were distinct class objects.
        This happens when create_schema_from_config() is called multiple times
        with the same field definitions (e.g., per-instance LLM transforms).

        The fix uses check_compatibility() for structural comparison.
        """
        from elspeth.contracts import NodeType, PluginSchema, RoutingMode
        from elspeth.contracts.schema import FieldDefinition, SchemaConfig
        from elspeth.core.dag import ExecutionGraph
        from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config

        # Create two STRUCTURALLY IDENTICAL schemas from same config
        # These will be distinct class objects (SchemaA is not SchemaB)
        fields = (
            FieldDefinition(name="id", field_type="int"),
            FieldDefinition(name="value", field_type="str"),
        )
        config = SchemaConfig(mode="fixed", fields=fields)

        # Each call creates a NEW class object
        SchemaA = create_schema_from_config(config, "BranchASchema")
        SchemaB = create_schema_from_config(config, "BranchBSchema")

        # Verify they are distinct class objects but structurally identical
        assert SchemaA is not SchemaB, "Test requires distinct class objects"
        assert list(SchemaA.model_fields.keys()) == list(SchemaB.model_fields.keys())

        class SourceOutput(PluginSchema):
            id: int

        graph = ExecutionGraph()

        # Build fork/join DAG with COMPATIBLE (structurally identical) schemas
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv", output_schema=SourceOutput)
        graph.add_node("fork_gate", node_type=NodeType.GATE, plugin_name="fork_gate")

        # Branch A uses SchemaA
        graph.add_node(
            "transform_a",
            node_type=NodeType.TRANSFORM,
            plugin_name="enrich",
            input_schema=SourceOutput,
            output_schema=SchemaA,
        )

        # Branch B uses SchemaB (structurally identical to SchemaA!)
        graph.add_node(
            "transform_b",
            node_type=NodeType.TRANSFORM,
            plugin_name="enrich",
            input_schema=SourceOutput,
            output_schema=SchemaB,
        )

        graph.add_node(
            "coalesce",
            node_type=NodeType.COALESCE,
            plugin_name="coalesce:merge",
            config={
                "branches": ["branch_a", "branch_b"],
                "policy": "require_all",
                "merge": "union",
            },
        )

        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv")

        # Build edges
        graph.add_edge("source", "fork_gate", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("fork_gate", "transform_a", label="branch_a", mode=RoutingMode.COPY)
        graph.add_edge("fork_gate", "transform_b", label="branch_b", mode=RoutingMode.COPY)
        graph.add_edge("transform_a", "coalesce", label="branch_a", mode=RoutingMode.MOVE)
        graph.add_edge("transform_b", "coalesce", label="branch_b", mode=RoutingMode.MOVE)
        graph.add_edge("coalesce", "sink", label="continue", mode=RoutingMode.MOVE)

        # Should PASS: schemas are structurally identical
        # (This was incorrectly failing before the fix)
        graph.validate_edge_compatibility()

    def test_coalesce_accepts_dynamic_schemas_from_different_instances(self) -> None:
        """Coalesce should accept branches with observed schemas from different instances.

        BUG FIX: P2-2026-01-30-coalesce-schema-identity-check
        Observed schemas (mode="observed") are compatible with anything.
        Even if they're distinct class objects, they should pass validation.
        """
        from elspeth.contracts import NodeType, PluginSchema, RoutingMode
        from elspeth.contracts.schema import SchemaConfig
        from elspeth.core.dag import ExecutionGraph
        from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config

        # Create two observed schemas (accept anything)
        config = SchemaConfig(mode="observed", fields=None)

        DynamicA = create_schema_from_config(config, "DynamicA")
        DynamicB = create_schema_from_config(config, "DynamicB")

        # Verify they are distinct class objects
        assert DynamicA is not DynamicB, "Test requires distinct class objects"

        class SourceOutput(PluginSchema):
            id: int

        graph = ExecutionGraph()

        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv", output_schema=SourceOutput)
        graph.add_node("fork_gate", node_type=NodeType.GATE, plugin_name="fork_gate")

        # Both branches use observed schemas (distinct objects)
        graph.add_node(
            "transform_a",
            node_type=NodeType.TRANSFORM,
            plugin_name="llm",
            input_schema=SourceOutput,
            output_schema=DynamicA,
        )

        graph.add_node(
            "transform_b",
            node_type=NodeType.TRANSFORM,
            plugin_name="llm",
            input_schema=SourceOutput,
            output_schema=DynamicB,
        )

        graph.add_node(
            "coalesce",
            node_type=NodeType.COALESCE,
            plugin_name="coalesce:merge",
            config={
                "branches": ["branch_a", "branch_b"],
                "policy": "require_all",
                "merge": "union",
            },
        )

        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv")

        graph.add_edge("source", "fork_gate", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("fork_gate", "transform_a", label="branch_a", mode=RoutingMode.COPY)
        graph.add_edge("fork_gate", "transform_b", label="branch_b", mode=RoutingMode.COPY)
        graph.add_edge("transform_a", "coalesce", label="branch_a", mode=RoutingMode.MOVE)
        graph.add_edge("transform_b", "coalesce", label="branch_b", mode=RoutingMode.MOVE)
        graph.add_edge("coalesce", "sink", label="continue", mode=RoutingMode.MOVE)

        # Should PASS: observed schemas are compatible with anything
        graph.validate_edge_compatibility()

    def test_coalesce_rejects_mixed_observed_explicit_branches(self) -> None:
        """Coalesce must reject mixed observed/explicit branch schemas.

        BUG FIX: P2-2026-02-01-dynamic-branch-schema-mismatch-not-detected

        When one branch produces a observed schema and another produces an explicit
        schema, the coalesce's effective schema becomes the first branch's schema.
        This masks the mismatch: downstream consumers expect explicit fields that
        dynamic-branch rows may not have, causing runtime failures.

        Pre-run validation must detect and reject this mismatch.
        """
        from elspeth.contracts import NodeType, PluginSchema, RoutingMode
        from elspeth.contracts.schema import SchemaConfig
        from elspeth.core.dag import ExecutionGraph
        from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config

        # Create a observed schema (no fields, accepts anything)
        DynamicSchema = create_schema_from_config(
            SchemaConfig.from_dict({"mode": "observed"}),
            "DynamicSchema",
            allow_coercion=False,
        )

        # Create an explicit schema with specific fields
        ExplicitSchema = create_schema_from_config(
            SchemaConfig.from_dict({"mode": "fixed", "fields": ["value: float", "id: int"]}),
            "ExplicitSchema",
            allow_coercion=False,
        )

        class SourceOutput(PluginSchema):
            id: int

        graph = ExecutionGraph()

        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv", output_schema=SourceOutput)
        graph.add_node("fork_gate", node_type=NodeType.GATE, plugin_name="fork_gate")

        # Branch A: produces EXPLICIT schema
        graph.add_node(
            "transform_a",
            node_type=NodeType.TRANSFORM,
            plugin_name="processor",
            input_schema=SourceOutput,
            output_schema=ExplicitSchema,
        )

        # Branch B: produces DYNAMIC schema
        graph.add_node(
            "transform_b",
            node_type=NodeType.TRANSFORM,
            plugin_name="llm",
            input_schema=SourceOutput,
            output_schema=DynamicSchema,
        )

        graph.add_node(
            "coalesce",
            node_type=NodeType.COALESCE,
            plugin_name="coalesce:merge",
            config={
                "branches": ["branch_a", "branch_b"],
                "policy": "require_all",
                "merge": "union",
            },
        )

        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv")

        graph.add_edge("source", "fork_gate", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("fork_gate", "transform_a", label="branch_a", mode=RoutingMode.COPY)
        graph.add_edge("fork_gate", "transform_b", label="branch_b", mode=RoutingMode.COPY)
        graph.add_edge("transform_a", "coalesce", label="branch_a", mode=RoutingMode.MOVE)
        graph.add_edge("transform_b", "coalesce", label="branch_b", mode=RoutingMode.MOVE)
        graph.add_edge("coalesce", "sink", label="continue", mode=RoutingMode.MOVE)

        # Should FAIL: mixed observed/explicit branches are not allowed
        with pytest.raises(ValueError, match=r"mixed.*observed.*explicit|observed.*explicit.*mismatch"):
            graph.validate_edge_compatibility()

    def test_coalesce_rejects_mixed_none_explicit_branches(self) -> None:
        """Coalesce must reject mixed None/explicit branch schemas.

        BUG FIX: P2-2026-02-01-dynamic-branch-schema-mismatch-not-detected

        None (unspecified output_schema) is treated as dynamic. Mixed with explicit
        schemas, this creates the same mismatch problem as observed schema classes.
        """
        from elspeth.contracts import NodeType, PluginSchema, RoutingMode
        from elspeth.contracts.schema import SchemaConfig
        from elspeth.core.dag import ExecutionGraph
        from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config

        # Create an explicit schema with specific fields
        ExplicitSchema = create_schema_from_config(
            SchemaConfig.from_dict({"mode": "fixed", "fields": ["value: float", "id: int"]}),
            "ExplicitSchema",
            allow_coercion=False,
        )

        class SourceOutput(PluginSchema):
            id: int

        graph = ExecutionGraph()

        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv", output_schema=SourceOutput)
        graph.add_node("fork_gate", node_type=NodeType.GATE, plugin_name="fork_gate")

        # Branch A: produces EXPLICIT schema
        graph.add_node(
            "transform_a",
            node_type=NodeType.TRANSFORM,
            plugin_name="processor",
            input_schema=SourceOutput,
            output_schema=ExplicitSchema,
        )

        # Branch B: produces NONE (unspecified = observed schema)
        graph.add_node(
            "transform_b",
            node_type=NodeType.TRANSFORM,
            plugin_name="llm",
            input_schema=SourceOutput,
            output_schema=None,
        )

        graph.add_node(
            "coalesce",
            node_type=NodeType.COALESCE,
            plugin_name="coalesce:merge",
            config={
                "branches": ["branch_a", "branch_b"],
                "policy": "require_all",
                "merge": "union",
            },
        )

        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv")

        graph.add_edge("source", "fork_gate", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("fork_gate", "transform_a", label="branch_a", mode=RoutingMode.COPY)
        graph.add_edge("fork_gate", "transform_b", label="branch_b", mode=RoutingMode.COPY)
        graph.add_edge("transform_a", "coalesce", label="branch_a", mode=RoutingMode.MOVE)
        graph.add_edge("transform_b", "coalesce", label="branch_b", mode=RoutingMode.MOVE)
        graph.add_edge("coalesce", "sink", label="continue", mode=RoutingMode.MOVE)

        # Should FAIL: mixed None/explicit branches are not allowed
        with pytest.raises(ValueError, match=r"mixed.*observed.*explicit|observed.*explicit.*mismatch"):
            graph.validate_edge_compatibility()

    def test_gate_rejects_mixed_observed_explicit_incoming_branches(self) -> None:
        """Gate with multiple inputs must reject mixed observed/explicit schemas.

        BUG FIX: P2-2026-02-01-dynamic-branch-schema-mismatch-not-detected

        Gates can receive inputs from multiple sources (e.g., in complex DAGs).
        When inputs have mixed observed/explicit schemas, the same mismatch problem
        occurs as with coalesce nodes.
        """
        from elspeth.contracts import NodeType, PluginSchema, RoutingMode
        from elspeth.contracts.schema import SchemaConfig
        from elspeth.core.dag import ExecutionGraph
        from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config

        # Create a observed schema
        DynamicSchema = create_schema_from_config(
            SchemaConfig.from_dict({"mode": "observed"}),
            "DynamicSchema",
            allow_coercion=False,
        )

        # Create an explicit schema
        ExplicitSchema = create_schema_from_config(
            SchemaConfig.from_dict({"mode": "fixed", "fields": ["value: float", "id: int"]}),
            "ExplicitSchema",
            allow_coercion=False,
        )

        class SourceOutput(PluginSchema):
            id: int

        graph = ExecutionGraph()

        # Two sources feeding into one gate
        graph.add_node("source1", node_type=NodeType.SOURCE, plugin_name="csv", output_schema=SourceOutput)
        graph.add_node("source2", node_type=NodeType.SOURCE, plugin_name="csv", output_schema=SourceOutput)

        # Transform 1 produces explicit schema
        graph.add_node(
            "transform1",
            node_type=NodeType.TRANSFORM,
            plugin_name="processor",
            input_schema=SourceOutput,
            output_schema=ExplicitSchema,
        )

        # Transform 2 produces observed schema
        graph.add_node(
            "transform2",
            node_type=NodeType.TRANSFORM,
            plugin_name="llm",
            input_schema=SourceOutput,
            output_schema=DynamicSchema,
        )

        # Gate receives both (simulating a join-like pattern)
        graph.add_node("merge_gate", node_type=NodeType.GATE, plugin_name="config_gate")

        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv")

        graph.add_edge("source1", "transform1", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("source2", "transform2", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("transform1", "merge_gate", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("transform2", "merge_gate", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("merge_gate", "sink", label="continue", mode=RoutingMode.MOVE)

        # Should FAIL: mixed observed/explicit inputs to gate
        with pytest.raises(ValueError, match=r"mixed.*observed.*explicit|observed.*explicit.*mismatch"):
            graph.validate_edge_compatibility()

    def test_aggregation_schema_transition_in_topology(self) -> None:
        """Aggregation with input_schema and output_schema in single topology.

        Verifies source→agg validates against input_schema,
        and agg→sink validates against output_schema.
        """
        from elspeth.contracts import NodeType, PluginSchema
        from elspeth.core.dag import ExecutionGraph

        class SourceOutput(PluginSchema):
            value: float

        class AggregationOutput(PluginSchema):
            count: int
            sum: float

        graph = ExecutionGraph()

        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv", output_schema=SourceOutput)
        graph.add_node(
            "agg",
            node_type=NodeType.AGGREGATION,
            plugin_name="batch_stats",
            input_schema=SourceOutput,  # Incoming edge validates against this
            output_schema=AggregationOutput,  # Outgoing edge validates against this
            config={
                "trigger": {"count": 1},
                "output_mode": "transform",
                "options": {
                    "schema": {"mode": "fixed", "fields": ["value: float"]},
                    "value_field": "value",
                },
                "schema": {"mode": "fixed", "fields": ["value: float"]},
            },
        )
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv", input_schema=AggregationOutput)

        graph.add_edge("source", "agg", label="continue")
        graph.add_edge("agg", "sink", label="continue")

        # Should pass - schemas compatible at both edges
        graph.validate()

    def test_aggregation_schema_transition_incompatible_output(self) -> None:
        """Aggregation with incompatible output_schema should fail validation."""
        from elspeth.contracts import NodeType, PluginSchema
        from elspeth.core.dag import ExecutionGraph

        class SourceOutput(PluginSchema):
            value: float

        class AggregationOutput(PluginSchema):
            count: int
            sum: float

        class SinkInput(PluginSchema):
            """Sink requires 'average' field not in aggregation output."""

            count: int
            sum: float
            average: float  # NOT in AggregationOutput!

        graph = ExecutionGraph()

        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv", output_schema=SourceOutput)
        graph.add_node(
            "agg",
            node_type=NodeType.AGGREGATION,
            plugin_name="batch_stats",
            input_schema=SourceOutput,
            output_schema=AggregationOutput,
            config={
                "trigger": {"count": 1},
                "output_mode": "transform",
                "options": {
                    "schema": {"mode": "fixed", "fields": ["value: float"]},
                    "value_field": "value",
                },
                "schema": {"mode": "fixed", "fields": ["value: float"]},
            },
        )
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv", input_schema=SinkInput)

        graph.add_edge("source", "agg", label="continue")
        graph.add_edge("agg", "sink", label="continue")

        # Should crash - sink requires 'average' field
        # validate_edge_compatibility() raises ValueError for schema mismatches
        with pytest.raises(ValueError) as exc_info:
            graph.validate_edge_compatibility()

        assert "average" in str(exc_info.value).lower()

    def test_schema_validation_error_includes_diagnostic_details(self) -> None:
        """Schema validation errors include field name, producer node, consumer node."""
        from elspeth.contracts import NodeType, PluginSchema
        from elspeth.core.dag import ExecutionGraph

        class SourceOutput(PluginSchema):
            id: int
            name: str

        class SinkInput(PluginSchema):
            id: int
            name: str
            score: float  # Missing from source output

        graph = ExecutionGraph()

        graph.add_node("my_source", node_type=NodeType.SOURCE, plugin_name="csv_reader", output_schema=SourceOutput)
        graph.add_node("my_sink", node_type=NodeType.SINK, plugin_name="db_writer", input_schema=SinkInput)

        graph.add_edge("my_source", "my_sink", label="continue")

        # Capture error and verify diagnostic details
        # validate_edge_compatibility() raises ValueError for schema mismatches
        with pytest.raises(ValueError) as exc_info:
            graph.validate_edge_compatibility()

        error_msg = str(exc_info.value)

        # Should include field name
        assert "score" in error_msg.lower()

        # Should include producer node ID
        assert "my_source" in error_msg

        # Should include consumer node ID
        assert "my_sink" in error_msg


def test_from_plugin_instances_extracts_schemas():
    """Verify from_plugin_instances extracts schemas from instances."""
    import tempfile
    from pathlib import Path

    from elspeth.config_loading import load_settings
    from elspeth.core.dag import ExecutionGraph

    config_yaml = """
sources:
  primary:
    plugin: csv
    on_success: source_out
    options:
      path: test.csv
      schema:
        mode: fixed
        fields:
          - "value: float"
      on_validation_failure: discard

transforms:
  - name: passthrough_0
    plugin: passthrough
    input: source_out
    on_success: output
    on_error: discard
    options:
      schema:
        mode: fixed
        fields:
          - "value: float"

sinks:
  output:
    plugin: csv
    on_write_failure: discard
    options:
      path: output.csv
      schema:
        mode: fixed
        fields:
          - "value: float"
"""

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(config_yaml)
        config_file = Path(f.name)

    try:
        config = load_settings(config_file)
        plugins = instantiate_plugins_from_config(config)

        graph = ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=list(config.gates),
            coalesce_settings=list(config.coalesce) if config.coalesce else None,
        )

        # Verify schemas extracted
        source_id = graph.get_sources()[0]
        assert source_id is not None
        source_info = graph.get_node_info(source_id)
        assert source_info.output_schema is not None

    finally:
        config_file.unlink()


def test_from_plugin_instances_cycle_raises_graph_validation_error(monkeypatch: pytest.MonkeyPatch):
    """NetworkXUnfeasible during topo sort in from_plugin_instances yields GraphValidationError.

    Regression: nx.topological_sort at the pipeline-ordering step was unguarded,
    so a cycle raised raw NetworkXUnfeasible instead of GraphValidationError.
    The single-producer invariant prevents cycles through normal config, but
    this wrapping is defense-in-depth for future edge-building changes.
    """
    import tempfile
    from pathlib import Path

    import networkx as nx

    from elspeth.config_loading import load_settings
    from elspeth.core.dag import ExecutionGraph, GraphValidationError

    config_yaml = """
sources:
  primary:
    plugin: csv
    on_success: step_a
    options:
      path: test.csv
      schema:
        mode: observed
      on_validation_failure: discard

transforms:
  - name: passthrough_0
    plugin: passthrough
    input: step_a
    on_success: output
    on_error: discard
    options:
      schema:
        mode: observed

sinks:
  output:
    plugin: csv
    on_write_failure: discard
    options:
      path: output.csv
      schema:
        mode: observed
"""

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(config_yaml)
        config_file = Path(f.name)

    try:
        config = load_settings(config_file)
        plugins = instantiate_plugins_from_config(config)

        def raise_unfeasible(g):
            raise nx.NetworkXUnfeasible("graph contains a cycle")

        monkeypatch.setattr(nx, "topological_sort", raise_unfeasible)

        with pytest.raises(GraphValidationError, match="cycle"):
            ExecutionGraph.from_plugin_instances(
                sources=plugins.sources,
                source_settings_map=plugins.source_settings_map,
                transforms=plugins.transforms,
                sinks=plugins.sinks,
                aggregations=plugins.aggregations,
                gates=list(config.gates),
                coalesce_settings=list(config.coalesce) if config.coalesce else None,
            )
    finally:
        config_file.unlink()


def test_validate_aggregation_dual_schema():
    """Verify aggregation edges validate against correct schemas."""
    from elspeth.contracts import NodeType
    from elspeth.contracts.schema import SchemaConfig
    from elspeth.core.dag import ExecutionGraph
    from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config

    input_schema_config = {"mode": "fixed", "fields": ["value: float"]}
    InputSchema = create_schema_from_config(
        SchemaConfig.from_dict(input_schema_config),
        "InputSchema",
        allow_coercion=False,
    )

    OutputSchema = create_schema_from_config(
        SchemaConfig.from_dict({"mode": "fixed", "fields": ["count: int", "sum: float"]}),
        "OutputSchema",
        allow_coercion=False,
    )

    graph = ExecutionGraph()
    graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv", output_schema=InputSchema)
    graph.add_node(
        "agg",
        node_type=NodeType.AGGREGATION,
        plugin_name="batch_stats",
        input_schema=InputSchema,
        output_schema=OutputSchema,
        config={
            "trigger": {"count": 1},
            "output_mode": "transform",
            "options": {
                "schema": dict(input_schema_config),
                "value_field": "value",
            },
            "schema": dict(input_schema_config),
        },
    )
    graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv", input_schema=OutputSchema)

    graph.add_edge("source", "agg", label="continue")
    graph.add_edge("agg", "sink", label="continue")

    # Should pass - validate_edge_compatibility() raises ValueError on failure
    graph.validate_edge_compatibility()  # No exception means success


def test_validate_aggregation_detects_incompatibility():
    """Verify validation detects aggregation output mismatch."""
    from elspeth.contracts import NodeType
    from elspeth.contracts.schema import SchemaConfig
    from elspeth.core.dag import ExecutionGraph
    from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config

    input_schema_config = {"mode": "fixed", "fields": ["value: float"]}
    InputSchema = create_schema_from_config(
        SchemaConfig.from_dict(input_schema_config),
        "InputSchema",
        allow_coercion=False,
    )

    OutputSchema = create_schema_from_config(
        SchemaConfig.from_dict({"mode": "fixed", "fields": ["count: int"]}),  # Missing 'sum'
        "OutputSchema",
        allow_coercion=False,
    )

    SinkSchema = create_schema_from_config(
        SchemaConfig.from_dict({"mode": "fixed", "fields": ["count: int", "sum: float"]}),
        "SinkSchema",
        allow_coercion=False,
    )

    graph = ExecutionGraph()
    graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv", output_schema=InputSchema)
    graph.add_node(
        "agg",
        node_type=NodeType.AGGREGATION,
        plugin_name="batch_stats",
        input_schema=InputSchema,
        output_schema=OutputSchema,
        config={
            "trigger": {"count": 1},
            "output_mode": "transform",
            "options": {
                "schema": dict(input_schema_config),
                "value_field": "value",
            },
            "schema": dict(input_schema_config),
        },
    )
    graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv", input_schema=SinkSchema)

    graph.add_edge("source", "agg", label="continue")
    graph.add_edge("agg", "sink", label="continue")

    # Should fail - sink requires 'sum' which aggregation output doesn't provide
    with pytest.raises(ValueError) as exc_info:
        graph.validate_edge_compatibility()

    assert "sum" in str(exc_info.value).lower()


class TestDynamicSchemaDetection:
    """Tests for detecting and handling observed schemas."""

    def test_dynamic_source_to_specific_sink_should_skip_validation(self) -> None:
        """Dynamic source → specific sink should PASS (validation skipped).

        Manually constructed graph with dynamic output_schema and specific input_schema.
        Validation should be skipped for observed schemas.
        """
        from elspeth.contracts import NodeType
        from elspeth.contracts.schema import SchemaConfig
        from elspeth.core.dag import ExecutionGraph
        from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config

        # Create observed schema (no fields, extra='allow')
        DynamicSchema = create_schema_from_config(
            SchemaConfig.from_dict({"mode": "observed"}),
            "DynamicSchema",
            allow_coercion=False,
        )

        # Create specific schema (has fields, extra='forbid')
        SpecificSchema = create_schema_from_config(
            SchemaConfig.from_dict({"mode": "fixed", "fields": ["value: float", "name: str"]}),
            "SpecificSchema",
            allow_coercion=False,
        )

        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv", output_schema=DynamicSchema)
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv", input_schema=SpecificSchema)
        graph.add_edge("source", "sink", label="continue")

        # Should NOT raise - validation is skipped for observed schemas
        graph.validate()

    def test_specific_source_to_dynamic_sink_should_skip_validation(self) -> None:
        """Specific source → dynamic sink should PASS (validation skipped).

        Manually constructed graph with specific output_schema and dynamic input_schema.
        Validation should be skipped for observed schemas.
        """
        from elspeth.contracts import NodeType
        from elspeth.contracts.schema import SchemaConfig
        from elspeth.core.dag import ExecutionGraph
        from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config

        # Create specific schema
        SpecificSchema = create_schema_from_config(
            SchemaConfig.from_dict({"mode": "fixed", "fields": ["value: float"]}),
            "SpecificSchema",
            allow_coercion=False,
        )

        # Create observed schema
        DynamicSchema = create_schema_from_config(
            SchemaConfig.from_dict({"mode": "observed"}),
            "DynamicSchema",
            allow_coercion=False,
        )

        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv", output_schema=SpecificSchema)
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv", input_schema=DynamicSchema)
        graph.add_edge("source", "sink", label="continue")

        # Should NOT raise - validation is skipped for observed schemas
        graph.validate()

    def test_dynamic_schema_detection_in_validation(self) -> None:
        """Observed schema detection correctly identifies dynamic vs explicit schemas.

        Observed schemas have no fields and extra='allow', matching the detection
        logic in ExecutionGraph._get_missing_required_fields().
        """
        from elspeth.contracts import PluginSchema
        from elspeth.contracts.schema import SchemaConfig
        from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config

        # Create observed schema
        DynamicSchema = create_schema_from_config(
            SchemaConfig.from_dict({"mode": "observed"}),
            "DynamicSchema",
            allow_coercion=False,
        )

        # Create explicit schema
        ExplicitSchema = create_schema_from_config(
            SchemaConfig.from_dict({"mode": "fixed", "fields": ["value: float"]}),
            "ExplicitSchema",
            allow_coercion=False,
        )

        # Helper to check if schema is observed (matches logic in dag.py)
        def is_observed_schema(schema: type[PluginSchema] | None) -> bool:
            if schema is None:
                return True
            return len(schema.model_fields) == 0 and schema.model_config.get("extra") == "allow"

        # Test observed schema detection
        assert is_observed_schema(DynamicSchema) is True

        # Test explicit schema detection
        assert is_observed_schema(ExplicitSchema) is False

        # Test backwards compat (None = observed)
        assert is_observed_schema(None) is True


class TestCoalesceOnSuccessValidation:
    """xe91: Coalesce on_success routing validation (3 error paths).

    Three error paths in coalesce wiring (dag.py:1081-1103):
    - dag.py:1091: Terminal coalesce missing on_success
    - dag.py:1082: Non-terminal coalesce with on_success set
    - dag.py:1099: Coalesce on_success references unknown sink
    """

    def test_terminal_coalesce_missing_on_success_raises(self, plugin_manager) -> None:
        """Terminal coalesce without on_success raises GraphValidationError."""
        from elspeth.core.config import (
            CoalesceSettings,
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
                    name="forker",
                    input="to_gate",
                    condition="True",
                    routes={"true": "fork", "false": "output"},
                    fork_to=["path_a", "path_b"],
                ),
            ],
            coalesce=[
                CoalesceSettings(
                    name="merge",
                    branches=["path_a", "path_b"],
                    policy="require_all",
                    merge="union",
                    # No on_success — terminal coalesce must have it
                ),
            ],
        )

        plugins = instantiate_plugins_from_config_raw(config)
        with pytest.raises(GraphValidationError, match=r"Dangling output|no incoming branches"):
            ExecutionGraph.from_plugin_instances(
                sources=plugins.sources,
                source_settings_map=plugins.source_settings_map,
                transforms=plugins.transforms,
                sinks=plugins.sinks,
                aggregations=plugins.aggregations,
                gates=list(config.gates),
                coalesce_settings=config.coalesce,
            )

    def test_non_terminal_coalesce_with_on_success_to_sink_builds(self, plugin_manager) -> None:
        """Coalesce with on_success pointing to a sink builds successfully.

        In the new WiredTransform system, coalesce on_success creates a terminal
        edge to the named sink. This is valid wiring.
        """
        from elspeth.core.config import (
            CoalesceSettings,
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
                    name="forker",
                    input="to_gate",
                    condition="True",
                    routes={"true": "fork", "false": "output"},
                    fork_to=["path_a", "path_b"],
                ),
            ],
            coalesce=[
                CoalesceSettings(
                    name="merge",
                    branches=["path_a", "path_b"],
                    policy="require_all",
                    merge="union",
                    on_success="output",
                ),
            ],
        )

        plugins = instantiate_plugins_from_config_raw(config)
        # Coalesce on_success to a valid sink should build successfully
        graph = ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=list(config.gates),
            coalesce_settings=config.coalesce,
        )
        assert graph is not None

    def test_terminal_coalesce_unknown_sink_raises(self, plugin_manager) -> None:
        """Coalesce on_success referencing unknown sink raises GraphValidationError."""
        from elspeth.core.config import (
            CoalesceSettings,
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
                    name="forker",
                    input="to_gate",
                    condition="True",
                    routes={"true": "fork", "false": "output"},
                    fork_to=["path_a", "path_b"],
                ),
            ],
            coalesce=[
                CoalesceSettings(
                    name="merge",
                    branches=["path_a", "path_b"],
                    policy="require_all",
                    merge="union",
                    on_success="nonexistent_sink",
                ),
            ],
        )

        plugins = instantiate_plugins_from_config_raw(config)
        with pytest.raises(GraphValidationError, match=r"unknown sink 'nonexistent_sink'"):
            ExecutionGraph.from_plugin_instances(
                sources=plugins.sources,
                source_settings_map=plugins.source_settings_map,
                transforms=plugins.transforms,
                sinks=plugins.sinks,
                aggregations=plugins.aggregations,
                gates=list(config.gates),
                coalesce_settings=config.coalesce,
            )
