"""Regression test for Phase 0 fix #2: GraphValidationError suppressed.

Bug: In get_effective_producer_schema(), when processing a select-merge
coalesce with a transform branch, _trace_branch_endpoints was called
inside a try/except that caught GraphValidationError and returned None
instead of propagating the error. This silently hid graph construction
bugs by falling back to "dynamic schema" instead of raising.

Fix: Removed the try/except so GraphValidationError from
_trace_branch_endpoints propagates up to the caller.
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from elspeth.contracts import RouteDestination
from elspeth.contracts.enums import NodeType, RoutingMode
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.types import BranchName, CoalesceName, NodeID, RowUnionName, SinkName
from elspeth.core.dag.graph import ExecutionGraph
from elspeth.core.dag.models import BranchInfo, GraphValidationError


class TestSelectMergeCoalesceRaisesOnBrokenBranch:
    """Verify GraphValidationError propagates from get_effective_producer_schema
    for select-merge coalesce with untraceable branches.
    """

    def test_untraceable_branch_raises_graph_validation_error(self) -> None:
        """When _trace_branch_endpoints fails for a select-merge coalesce,
        get_effective_producer_schema must raise GraphValidationError,
        not return None.

        Before the fix, a try/except caught the error and returned None,
        silently treating the coalesce as dynamic schema.
        """
        graph = ExecutionGraph()

        # Build a minimal graph with a select-merge coalesce
        # that has a transform branch which cannot be traced
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node(
            "gate",
            node_type=NodeType.GATE,
            plugin_name="fork_gate",
            config={"routes": {"true": "fork"}},
        )
        graph.add_node(
            "coalesce",
            node_type=NodeType.COALESCE,
            plugin_name="coalesce",
            config={
                "merge": "select",
                "select_branch": "branch_a",
            },
        )
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv_sink")

        # Wire edges: source -> gate, gate -> coalesce (COPY for branch_b),
        # but branch_a is a MOVE edge that has no proper chain
        graph.add_edge("source", "gate", label="continue")
        # branch_b is identity (COPY)
        graph.add_edge("gate", "coalesce", label="branch_b", mode=RoutingMode.COPY)
        # branch_a has a MOVE edge from gate directly to coalesce
        # but with no intermediate transform — the trace should find gate as
        # the fork producer, but we deliberately set up a broken trace by
        # NOT populating the _branch_gate_map for this branch
        graph.add_edge("gate", "coalesce", label="branch_a", mode=RoutingMode.MOVE)
        graph.add_edge("coalesce", "sink", label="on_success")

        # Set the branch_info to point to a nonexistent gate for branch_a
        # This simulates a graph construction bug
        graph.set_branch_info(
            {
                BranchName("branch_a"): BranchInfo(
                    coalesce_name=CoalesceName("coalesce"),
                    gate_node_id=NodeID("nonexistent_gate"),
                ),
            }
        )

        # get_effective_producer_schema for coalesce with select-merge
        # should raise GraphValidationError, NOT return None
        with pytest.raises((GraphValidationError, KeyError)):
            graph.get_effective_producer_schema("coalesce")

    def test_valid_select_merge_still_works(self) -> None:
        """Sanity check: properly constructed select-merge coalesce works."""
        graph = ExecutionGraph()

        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node(
            "gate",
            node_type=NodeType.GATE,
            plugin_name="fork_gate",
            config={"routes": {"true": "fork"}},
        )
        graph.add_node(
            "coalesce",
            node_type=NodeType.COALESCE,
            plugin_name="coalesce",
            config={
                "merge": "select",
                "select_branch": "branch_b",
            },
        )
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv_sink")

        graph.add_edge("source", "gate", label="continue")
        # branch_b is identity (COPY) — select picks this branch
        graph.add_edge("gate", "coalesce", label="branch_b", mode=RoutingMode.COPY)
        graph.add_edge("gate", "coalesce", label="branch_a", mode=RoutingMode.MOVE)
        graph.add_edge("coalesce", "sink", label="on_success")

        graph.set_branch_info(
            {
                BranchName("branch_a"): BranchInfo(
                    coalesce_name=CoalesceName("coalesce"),
                    gate_node_id=NodeID("gate"),
                ),
            }
        )

        # Identity branch (COPY): should trace through to gate's schema
        # This should NOT raise
        result = graph.get_effective_producer_schema("coalesce")
        # Returns None because gate has no output_schema — that's fine,
        # the important thing is it didn't raise an error
        assert result is None


class TestExecutionGraphConstructionApi:
    def test_add_node_wraps_invalid_raw_schema_as_graph_validation_error(self) -> None:
        graph = ExecutionGraph()

        with pytest.raises(GraphValidationError, match="Invalid schema config") as exc_info:
            graph.add_node(
                "source",
                node_type=NodeType.SOURCE,
                plugin_name="csv",
                config={"schema": {"mode": "invalid"}},
            )

        assert exc_info.value.component_id == "source"
        assert exc_info.value.component_type == "source"
        assert isinstance(exc_info.value.__cause__, ValueError)
        assert "Invalid schema mode 'invalid'" in str(exc_info.value.__cause__)

    def test_set_node_output_schema_replaces_node_info_without_mutating_existing_instance(self) -> None:
        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        original_info = graph.get_node_info("source")

        schema = SchemaConfig(mode="observed", fields=None)

        graph.set_node_output_schema("source", schema)

        updated_info = graph.get_node_info("source")
        assert updated_info is not original_info
        assert original_info.output_schema_config is None
        assert updated_info.output_schema_config is schema

    def test_finalize_node_configs_replaces_node_info_without_mutating_existing_instance(self) -> None:
        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv", config={"schema": {"mode": "observed"}})
        original_info = graph.get_node_info("source")

        graph.finalize_node_configs()

        updated_info = graph.get_node_info("source")
        assert updated_info is not original_info
        assert isinstance(updated_info.config, MappingProxyType)
        assert isinstance(original_info.config, dict)

    def test_set_node_output_schema_updates_node_info_through_graph_api(self) -> None:
        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")

        schema = SchemaConfig(mode="observed", fields=None)

        graph.set_node_output_schema("source", schema)

        assert graph.get_node_info("source").output_schema_config is schema

    def test_topological_processing_order_filters_to_processing_nodes(self) -> None:
        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("transform", node_type=NodeType.TRANSFORM, plugin_name="classifier")
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv")
        graph.add_edge("source", "transform", label="continue")
        graph.add_edge("transform", "sink", label="continue")

        order = graph.topological_processing_order({NodeID("transform")})

        assert order == [NodeID("transform")]

    def test_topological_processing_order_preserves_cycle_error_contract(self) -> None:
        graph = ExecutionGraph()
        graph.add_node("first", node_type=NodeType.TRANSFORM, plugin_name="a")
        graph.add_node("second", node_type=NodeType.TRANSFORM, plugin_name="b")
        graph.add_edge("first", "second", label="forward")
        graph.add_edge("second", "first", label="back")

        with pytest.raises(GraphValidationError, match="Pipeline contains a cycle"):
            graph.topological_processing_order({NodeID("first"), NodeID("second")})

    def test_finalize_node_configs_deep_freezes_node_config(self) -> None:
        graph = ExecutionGraph()
        graph.add_node(
            "source",
            node_type=NodeType.SOURCE,
            plugin_name="csv",
            config={"options": {"columns": ["name"]}},
        )

        graph.finalize_node_configs()

        config = graph.get_node_info("source").config
        assert config["options"]["columns"] == ("name",)
        with pytest.raises(TypeError):
            config["new"] = "value"

    def test_set_route_resolution_map_copies_caller_mapping(self) -> None:
        graph = ExecutionGraph()
        key = (NodeID("gate"), "drop")
        route_map = {key: RouteDestination.discard()}

        graph.set_route_resolution_map(route_map)
        route_map.clear()

        assert graph.get_route_resolution_map() == {key: RouteDestination.discard()}

    def test_set_route_label_map_copies_caller_mapping(self) -> None:
        graph = ExecutionGraph()
        key = (NodeID("gate"), SinkName("output"))
        route_map = {key: "selected"}

        graph.set_route_label_map(route_map)
        route_map.clear()

        assert graph.get_route_label("gate", SinkName("output")) == "selected"


class TestExecutionGraphTraversal:
    def test_get_next_node_ignores_continue_copy_edge(self) -> None:
        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("copy_target", node_type=NodeType.TRANSFORM, plugin_name="copy")
        graph.add_edge("source", "copy_target", label="continue", mode=RoutingMode.COPY)

        assert graph.get_next_node(NodeID("source")) is None

    def test_get_next_node_treats_sink_as_terminal(self) -> None:
        graph = ExecutionGraph()
        graph.add_node("transform", node_type=NodeType.TRANSFORM, plugin_name="map")
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="json")
        graph.add_edge("transform", "sink", label="continue", mode=RoutingMode.MOVE)

        assert graph.get_next_node(NodeID("transform")) is None

    def test_get_next_node_rejects_multiple_continue_processing_edges(self) -> None:
        graph = ExecutionGraph()
        graph.add_node("gate", node_type=NodeType.GATE, plugin_name="expression")
        graph.add_node("left", node_type=NodeType.TRANSFORM, plugin_name="map")
        graph.add_node("right", node_type=NodeType.TRANSFORM, plugin_name="map")
        graph.add_edge("gate", "left", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("gate", "right", label="continue", mode=RoutingMode.MOVE)

        with pytest.raises(GraphValidationError, match="multiple continue MOVE edges") as exc_info:
            graph.get_next_node(NodeID("gate"))

        assert exc_info.value.component_id == "gate"
        assert exc_info.value.component_type == "gate"

    def test_pipeline_node_sequence_is_empty_without_sources(self) -> None:
        graph = ExecutionGraph()
        graph.add_node("transform", node_type=NodeType.TRANSFORM, plugin_name="map")

        assert graph.get_pipeline_node_sequence() == []

    def test_pipeline_node_sequence_checks_every_source(self) -> None:
        graph = ExecutionGraph()
        graph.add_node("terminal_source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("active_source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("transform", node_type=NodeType.TRANSFORM, plugin_name="map")
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="json")
        graph.add_edge("terminal_source", "sink", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("active_source", "transform", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("transform", "sink", label="continue", mode=RoutingMode.MOVE)

        assert graph.get_pipeline_node_sequence() == [NodeID("transform")]

    def test_pipeline_node_sequence_deduplicates_a_converged_node(self) -> None:
        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("gate", node_type=NodeType.GATE, plugin_name="expression")
        graph.add_node("left", node_type=NodeType.TRANSFORM, plugin_name="map")
        graph.add_node("right", node_type=NodeType.TRANSFORM, plugin_name="map")
        graph.add_node("queue", node_type=NodeType.QUEUE, plugin_name="queue")
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="json")
        graph.add_edge("source", "gate", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("gate", "left", label="left", mode=RoutingMode.MOVE)
        graph.add_edge("gate", "right", label="right", mode=RoutingMode.MOVE)
        graph.add_edge("left", "queue", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("right", "queue", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("queue", "sink", label="continue", mode=RoutingMode.MOVE)

        sequence = graph.get_pipeline_node_sequence()

        assert sequence.count(NodeID("queue")) == 1
        assert set(sequence) == {
            NodeID("gate"),
            NodeID("left"),
            NodeID("right"),
            NodeID("queue"),
        }

    def test_pipeline_node_sequence_ignores_copy_targets(self) -> None:
        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("gate", node_type=NodeType.GATE, plugin_name="expression")
        graph.add_node("copy_target", node_type=NodeType.COALESCE, plugin_name="coalesce")
        graph.add_node("move_target", node_type=NodeType.TRANSFORM, plugin_name="map")
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="json")
        graph.add_edge("source", "gate", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("gate", "copy_target", label="identity", mode=RoutingMode.COPY)
        graph.add_edge("gate", "move_target", label="selected", mode=RoutingMode.MOVE)
        graph.add_edge("copy_target", "sink", label="on_success", mode=RoutingMode.MOVE)
        graph.add_edge("move_target", "sink", label="on_success", mode=RoutingMode.MOVE)

        assert graph.get_pipeline_node_sequence() == [NodeID("gate"), NodeID("move_target")]

    def test_row_union_branch_first_nodes_cover_identity_and_transform_arms(self) -> None:
        graph = ExecutionGraph()
        graph.add_node("gate", node_type=NodeType.GATE, plugin_name="fork")
        graph.add_node("transform", node_type=NodeType.TRANSFORM, plugin_name="map")
        graph.add_node("union", node_type=NodeType.ROW_UNION, plugin_name="row_union")
        graph.add_edge("gate", "union", label="identity", mode=RoutingMode.COPY)
        graph.add_edge("gate", "transform", label="changed", mode=RoutingMode.MOVE)
        graph.add_edge("transform", "union", label="continue", mode=RoutingMode.MOVE)
        graph.set_row_union_id_map({RowUnionName("merge"): NodeID("union")})
        graph.set_branch_to_row_union_map(
            {
                BranchName("identity"): RowUnionName("merge"),
                BranchName("changed"): RowUnionName("merge"),
            }
        )
        graph.set_row_union_branch_gates(
            {
                BranchName("identity"): NodeID("gate"),
                BranchName("changed"): NodeID("gate"),
            }
        )

        assert graph.get_branch_first_nodes() == {
            BranchName("identity"): NodeID("union"),
            BranchName("changed"): NodeID("transform"),
        }

    def test_branch_and_terminal_sink_maps_filter_by_edge_contract(self) -> None:
        graph = ExecutionGraph()
        graph.add_node("gate", node_type=NodeType.GATE, plugin_name="fork")
        graph.add_node("transform", node_type=NodeType.TRANSFORM, plugin_name="map")
        graph.add_node("branch_sink", node_type=NodeType.SINK, plugin_name="json")
        graph.add_node("terminal_sink", node_type=NodeType.SINK, plugin_name="json")
        graph.set_sink_id_map(
            {
                SinkName("branch_output"): NodeID("branch_sink"),
                SinkName("terminal_output"): NodeID("terminal_sink"),
            }
        )
        graph.add_edge("gate", "branch_sink", label="selected", mode=RoutingMode.COPY)
        graph.add_edge("transform", "terminal_sink", label="on_success", mode=RoutingMode.MOVE)

        assert graph.get_branch_to_sink_map() == {BranchName("selected"): SinkName("branch_output")}
        assert graph.get_terminal_sink_map() == {NodeID("transform"): SinkName("terminal_output")}
