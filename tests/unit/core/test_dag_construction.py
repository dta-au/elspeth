"""Execution graph construction, accessors, identity, and node immutability tests."""

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


def test_mixed_multi_source_graph_records_direct_source_sink_edge() -> None:
    """A direct source->sink route must be visible even when other sources use transforms."""
    from elspeth.contracts import NodeType, RoutingMode
    from elspeth.core.config import SourceSettings, TransformSettings
    from elspeth.core.dag.wiring import WiredTransform
    from tests.fixtures.plugins import CollectSink, ListSource, PassTransform

    direct_source = ListSource([{"id": 1}], name="direct_source", on_success="direct_out")
    routed_source = ListSource([{"id": 2}], name="routed_source", on_success="work_in")
    transform = PassTransform(name="normalize", input_connection="work_in", on_success="processed_out")

    graph = ExecutionGraph.from_plugin_instances(
        sources={
            "direct": direct_source,
            "routed": routed_source,
        },
        source_settings_map={
            "direct": SourceSettings(plugin="direct_source", on_success="direct_out", options={}),
            "routed": SourceSettings(plugin="routed_source", on_success="work_in", options={}),
        },
        transforms=[
            WiredTransform(
                plugin=transform,
                settings=TransformSettings(
                    name="normalize",
                    plugin="normalize",
                    input="work_in",
                    on_success="processed_out",
                    on_error="discard",
                    options={},
                ),
            )
        ],
        sinks={
            "direct_out": CollectSink("direct_out"),
            "processed_out": CollectSink("processed_out"),
        },
    )

    direct_source_node = next(
        node.node_id for node in graph.get_nodes() if node.node_type is NodeType.SOURCE and node.plugin_name == "direct_source"
    )
    direct_sink_node = next(
        node.node_id for node in graph.get_nodes() if node.node_type is NodeType.SINK and node.plugin_name == "direct_out"
    )

    assert any(
        edge.from_node == direct_source_node
        and edge.to_node == direct_sink_node
        and edge.label == "on_success"
        and edge.mode is RoutingMode.MOVE
        for edge in graph.get_edges()
    )


def test_source_route_to_unconsumed_declared_queue_is_rejected() -> None:
    """A declared queue is only a valid source target when some downstream node consumes it."""
    from elspeth.core.config import QueueSettings, SourceSettings
    from elspeth.core.dag import GraphValidationError
    from tests.fixtures.plugins import CollectSink, ListSource

    with pytest.raises(
        GraphValidationError, match=r"Source 'queued' on_success 'orphan_q' references queue 'orphan_q' with no downstream consumer"
    ):
        ExecutionGraph.from_plugin_instances(
            sources={
                "queued": ListSource([{"id": 1}], name="queued_source", on_success="orphan_q"),
                "direct": ListSource([{"id": 2}], name="direct_source", on_success="out"),
            },
            source_settings_map={
                "queued": SourceSettings(plugin="queued_source", on_success="orphan_q", options={}),
                "direct": SourceSettings(plugin="direct_source", on_success="out", options={}),
            },
            sinks={"out": CollectSink("out")},
            queues={"orphan_q": QueueSettings(description="unused")},
        )


class TestDAGBuilder:
    """Building execution graphs from configuration."""

    def test_empty_dag(self) -> None:
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        assert graph.node_count == 0
        assert graph.edge_count == 0

    def test_add_node(self) -> None:
        from elspeth.contracts import NodeType
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")

        assert graph.node_count == 1
        assert graph.has_node("source")

    def test_add_edge(self) -> None:
        from elspeth.contracts import NodeType
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("transform", node_type=NodeType.TRANSFORM, plugin_name="validate")
        graph.add_edge("source", "transform", label="continue")

        assert graph.edge_count == 1

    def test_linear_pipeline(self) -> None:
        from elspeth.contracts import NodeType
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("t1", node_type=NodeType.TRANSFORM, plugin_name="enrich")
        graph.add_node("t2", node_type=NodeType.TRANSFORM, plugin_name="classify")
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv")

        graph.add_edge("source", "t1", label="continue")
        graph.add_edge("t1", "t2", label="continue")
        graph.add_edge("t2", "sink", label="continue")

        assert graph.node_count == 4
        assert graph.edge_count == 3

    def test_pipeline_sequence_includes_route_labeled_processing_targets(self) -> None:
        """Fallback sequence discovery must include MOVE edges beyond 'continue'."""
        from elspeth.contracts import NodeType, RoutingMode
        from elspeth.contracts.types import NodeID
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("router", node_type=NodeType.GATE, plugin_name="router")
        graph.add_node("branch_t", node_type=NodeType.TRANSFORM, plugin_name="passthrough")
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="json")

        graph.add_edge("source", "router", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("router", "branch_t", label="high", mode=RoutingMode.MOVE)
        graph.add_edge("branch_t", "sink", label="on_success", mode=RoutingMode.MOVE)

        sequence = graph.get_pipeline_node_sequence()
        assert sequence == [NodeID("router"), NodeID("branch_t")]

        step_map = graph.build_step_map()
        assert step_map[NodeID("source")] == 0
        assert step_map[NodeID("router")] == 1
        assert step_map[NodeID("branch_t")] == 2


class TestExecutionGraphAccessors:
    """Access node info and edges from graph."""

    def test_get_node_info(self) -> None:
        """Get NodeInfo for a node."""
        from elspeth.contracts import NodeType
        from elspeth.core.dag import ExecutionGraph, NodeInfo

        graph = ExecutionGraph()
        graph.add_node(
            "node_1",
            node_type=NodeType.TRANSFORM,
            plugin_name="my_plugin",
            config={"key": "value"},
        )

        info = graph.get_node_info("node_1")

        assert isinstance(info, NodeInfo)
        assert info.node_id == "node_1"
        assert info.node_type == NodeType.TRANSFORM
        assert info.plugin_name == "my_plugin"
        assert info.config == {"key": "value"}

    def test_get_node_info_missing(self) -> None:
        """Get NodeInfo for missing node raises."""
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()

        with pytest.raises(KeyError):
            graph.get_node_info("nonexistent")

    def test_get_edges(self) -> None:
        """Get all edges with data."""
        from elspeth.contracts import EdgeInfo, NodeID, NodeType, RoutingMode
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("a", node_type=NodeType.SOURCE, plugin_name="src")
        graph.add_node("b", node_type=NodeType.TRANSFORM, plugin_name="tf")
        graph.add_node("c", node_type=NodeType.SINK, plugin_name="sink")
        graph.add_edge("a", "b", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("b", "c", label="output", mode=RoutingMode.COPY)

        edges = list(graph.get_edges())

        assert len(edges) == 2
        # Each edge is EdgeInfo (not tuple)
        assert EdgeInfo(from_node=NodeID("a"), to_node=NodeID("b"), label="continue", mode=RoutingMode.MOVE) in edges
        assert EdgeInfo(from_node=NodeID("b"), to_node=NodeID("c"), label="output", mode=RoutingMode.COPY) in edges

    def test_get_edges_empty_graph(self) -> None:
        """Empty graph returns empty list."""
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        edges = list(graph.get_edges())

        assert edges == []

    def test_get_incoming_edges_returns_edges_pointing_to_node(self):
        """get_incoming_edges() returns all edges with to_node matching the given node_id."""
        from elspeth.contracts import NodeType, RoutingMode
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("A", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("B", node_type=NodeType.TRANSFORM, plugin_name="mapper")
        graph.add_node("C", node_type=NodeType.SINK, plugin_name="csv")

        graph.add_edge("A", "B", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("B", "C", label="continue", mode=RoutingMode.MOVE)

        incoming = graph.get_incoming_edges("B")

        assert len(incoming) == 1
        assert incoming[0].from_node == "A"
        assert incoming[0].to_node == "B"
        assert incoming[0].label == "continue"
        assert incoming[0].mode == RoutingMode.MOVE

    def test_get_incoming_edges_returns_empty_for_source_node(self):
        """get_incoming_edges() returns empty list for nodes with no predecessors."""
        from elspeth.contracts import NodeType
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("A", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("B", node_type=NodeType.SINK, plugin_name="csv")

        incoming = graph.get_incoming_edges("A")

        assert incoming == []

    def test_get_effective_producer_schema_walks_through_gates(self) -> None:
        """_get_effective_producer_schema() recursively finds schema through gate chain."""
        from elspeth.contracts import NodeType, PluginSchema, RoutingMode
        from elspeth.core.dag import ExecutionGraph

        class OutputSchema(PluginSchema):
            value: int

        graph = ExecutionGraph()

        # Build chain: source -> gate -> sink
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv", output_schema=OutputSchema)
        graph.add_node("gate", node_type=NodeType.GATE, plugin_name="config_gate:check")  # No schema
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv")

        graph.add_edge("source", "gate", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("gate", "sink", label="flagged", mode=RoutingMode.MOVE)

        # Gate's effective producer schema should be source's output schema
        effective_schema = graph.get_effective_producer_schema("gate")

        assert effective_schema == OutputSchema

    def test_get_effective_producer_schema_crashes_on_gate_without_inputs(self):
        """_get_effective_producer_schema() crashes if gate has no incoming edges."""
        from elspeth.contracts import NodeType
        from elspeth.core.dag import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("gate", node_type=NodeType.GATE, plugin_name="config_gate:orphan")

        # Gate with no inputs is a bug in our code - should crash
        # ValueError is raised by internal validation during edge compatibility checking
        with pytest.raises(ValueError) as exc_info:
            graph.get_effective_producer_schema("gate")

        assert "no incoming edges" in str(exc_info.value).lower()
        assert "bug in graph construction" in str(exc_info.value).lower()

    def test_get_effective_producer_schema_handles_chained_gates(self) -> None:
        """_get_effective_producer_schema() recursively walks through multiple gates."""
        from elspeth.contracts import NodeType, PluginSchema, RoutingMode
        from elspeth.core.dag import ExecutionGraph

        class SourceOutput(PluginSchema):
            id: int
            name: str

        graph = ExecutionGraph()

        # Build chain: source -> gate1 -> gate2 -> sink
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv", output_schema=SourceOutput)
        graph.add_node("gate1", node_type=NodeType.GATE, plugin_name="config_gate:first")
        graph.add_node("gate2", node_type=NodeType.GATE, plugin_name="config_gate:second")
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv")

        graph.add_edge("source", "gate1", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("gate1", "gate2", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("gate2", "sink", label="approved", mode=RoutingMode.MOVE)

        # gate2's effective schema should trace back to source
        effective_schema = graph.get_effective_producer_schema("gate2")

        assert effective_schema == SourceOutput

    def test_dag_validation_only_checks_structure(self) -> None:
        """DAG validation should only check cycles and connectivity, not schemas."""
        from elspeth.contracts import NodeType, PluginSchema
        from elspeth.core.dag import ExecutionGraph

        class OutputSchema(PluginSchema):
            value: int

        class DifferentSchema(PluginSchema):
            different: str  # Incompatible!

        graph = ExecutionGraph()

        # Add incompatible schemas
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv", output_schema=OutputSchema)
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv", input_schema=DifferentSchema)
        graph.add_edge("source", "sink", label="continue")

        # OLD behavior: Would raise GraphValidationError for schema mismatch
        # NEW behavior: Only checks structural validity (no cycles)
        graph.validate()  # Should NOT raise - no structural problems

    def test_get_effective_producer_schema_returns_direct_schema_for_transform(self) -> None:
        """_get_effective_producer_schema() returns output_schema directly for transform nodes."""
        from elspeth.contracts import NodeType, PluginSchema
        from elspeth.core.dag import ExecutionGraph

        class TransformOutput(PluginSchema):
            result: str

        graph = ExecutionGraph()
        graph.add_node(
            "transform",
            node_type=NodeType.TRANSFORM,
            plugin_name="field_mapper",
            output_schema=TransformOutput,
        )

        effective_schema = graph.get_effective_producer_schema("transform")

        assert effective_schema == TransformOutput

    def test_validate_edge_schemas_uses_effective_schema_for_gates(self) -> None:
        """validate_edge_compatibility() uses effective producer schema for gate edges."""
        from elspeth.contracts import NodeType, PluginSchema, RoutingMode
        from elspeth.core.dag import ExecutionGraph

        class SourceOutput(PluginSchema):
            id: int
            name: str
            # Note: does NOT have 'score' field

        class SinkInput(PluginSchema):
            id: int
            score: float  # Required field not in source output

        graph = ExecutionGraph()

        # Pipeline: source -> gate -> sink
        # Gate has NO schemas (simulates config-driven gate from from_config())
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv", output_schema=SourceOutput)
        graph.add_node("gate", node_type=NodeType.GATE, plugin_name="config_gate:check")  # NO SCHEMA
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv", input_schema=SinkInput)

        graph.add_edge("source", "gate", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("gate", "sink", label="flagged", mode=RoutingMode.MOVE)

        # Should detect schema incompatibility on gate -> sink edge
        # validate_edge_compatibility() raises ValueError for schema mismatches
        with pytest.raises(ValueError) as exc_info:
            graph.validate_edge_compatibility()

        # Verify error mentions the missing field
        assert "score" in str(exc_info.value).lower()
        # Verify error includes node IDs (gate -> sink)
        assert "gate" in str(exc_info.value)
        assert "sink" in str(exc_info.value)

    def test_validate_edge_schemas_raises_edge_contract_error_with_structured_fields(self) -> None:
        """Tier 1.5 final hardening — _validate_edge_compatibility raises
        the EdgeContractError subclass carrying CompatibilityResult, so
        downstream layers (composer runtime preflight error formatting in
        web/execution/validation.py) can build LLM-actionable suggestions
        from structured fields rather than re-parsing prose."""
        from elspeth.contracts import NodeType, PluginSchema
        from elspeth.core.dag import ExecutionGraph
        from elspeth.core.dag.models import EdgeContractError, GraphValidationError

        class ProducerOutput(PluginSchema):
            content: str
            fetch_status: int  # producer emits as int

        class ConsumerInput(PluginSchema):
            content: str
            fetch_status: str  # consumer requires as str — type mismatch

        graph = ExecutionGraph()
        graph.add_node(
            "fetch_node",
            node_type=NodeType.SOURCE,
            plugin_name="web_scrape",
            output_schema=ProducerOutput,
        )
        graph.add_node(
            "consume_node",
            node_type=NodeType.SINK,
            plugin_name="csv",
            input_schema=ConsumerInput,
        )
        graph.add_edge("fetch_node", "consume_node", label="continue")

        with pytest.raises(EdgeContractError) as exc_info:
            graph.validate_edge_compatibility()

        exc = exc_info.value
        # Subclass relationship — existing GraphValidationError catchers
        # keep working without modification.
        assert isinstance(exc, GraphValidationError)
        # Structured fields populated with the actual node IDs and schema
        # names from the failing edge.
        assert exc.from_node_id == "fetch_node"
        assert exc.to_node_id == "consume_node"
        assert exc.producer_schema_name == "ProducerOutput"
        assert exc.consumer_schema_name == "ConsumerInput"
        # CompatibilityResult preserved end-to-end so the formatter can
        # walk per-field detail.
        assert exc.compatibility_result.compatible is False
        type_mismatch_fields = [name for name, _, _ in exc.compatibility_result.type_mismatches]
        assert "fetch_status" in type_mismatch_fields
        # component_id semantics preserved (consumer is "responsible").
        assert exc.component_id == "consume_node"

    def test_validate_edge_schemas_validates_all_fork_destinations(self) -> None:
        """Fork gates validate all destination edges against effective schema."""
        from elspeth.contracts import NodeType, PluginSchema, RoutingMode
        from elspeth.core.dag import ExecutionGraph

        class SourceOutput(PluginSchema):
            id: int
            name: str

        class SinkA(PluginSchema):
            id: int  # Compatible - only requires id

        class SinkB(PluginSchema):
            id: int
            score: float  # Incompatible - requires field not in source

        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv", output_schema=SourceOutput)
        graph.add_node("gate", node_type=NodeType.GATE, plugin_name="config_gate:fork")  # NO SCHEMA
        graph.add_node("sink_a", node_type=NodeType.SINK, plugin_name="csv_a", input_schema=SinkA)
        graph.add_node("sink_b", node_type=NodeType.SINK, plugin_name="csv_b", input_schema=SinkB)

        graph.add_edge("source", "gate", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("gate", "sink_a", label="branch_a", mode=RoutingMode.COPY)  # Fork: COPY mode
        graph.add_edge("gate", "sink_b", label="branch_b", mode=RoutingMode.COPY)  # Fork: COPY mode

        # Should detect incompatibility on gate -> sink_b edge
        # validate_edge_compatibility() raises ValueError for schema mismatches
        with pytest.raises(ValueError) as exc_info:
            graph.validate_edge_compatibility()

        assert "score" in str(exc_info.value).lower()
        assert "gate" in str(exc_info.value)


class TestExecutionGraphFromConfig:
    """Build ExecutionGraph from ElspethSettings."""

    def test_from_config_minimal(self, plugin_manager) -> None:
        """Build graph from minimal config (source -> sink only)."""
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
                )
            },
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

        # Should have: source -> output_sink
        assert graph.node_count == 2
        assert graph.edge_count == 1
        assert graph.get_sources()[0] is not None
        assert len(graph.get_sinks()) == 1

    def test_from_config_is_valid(self, plugin_manager) -> None:
        """Graph from valid config passes validation."""
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
                )
            },
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

        # Should not raise
        graph.validate()
        assert graph.is_acyclic()

    def test_from_config_with_transforms(self, plugin_manager) -> None:
        """Build graph with transform chain."""
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
                )
            },
            transforms=[
                _transform_settings(
                    TransformSettings,
                    name="passthrough_0",
                    plugin="passthrough",
                    on_success="to_field_mapper",
                    options={"schema": {"mode": "observed"}},
                ),
                _transform_settings(
                    TransformSettings,
                    name="field_mapper_0",
                    plugin="field_mapper",
                    on_success="output",
                    options={"schema": {"mode": "observed"}},
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

        # Should have: source -> passthrough -> field_mapper -> output_sink
        assert graph.node_count == 4
        assert graph.edge_count == 3

        # Topological order should be correct
        order = graph.topological_order()
        assert len(order) == 4
        # Source should be first (has "source" in node_id)
        assert "source" in order[0]
        # Sink should be last (has "sink" in node_id)
        assert "sink" in order[-1]
        # Verify transform ordering (passthrough before field_mapper)
        passthrough_idx = next(i for i, n in enumerate(order) if "passthrough" in n)
        field_mapper_idx = next(i for i, n in enumerate(order) if "field_mapper" in n)
        assert passthrough_idx < field_mapper_idx

    def test_pydantic_rejects_missing_on_success_on_transform(self, plugin_manager) -> None:
        """on_success is required on TransformSettings — omitting it is a Pydantic error."""
        from pydantic import ValidationError

        from elspeth.core.config import TransformSettings

        with pytest.raises(ValidationError, match="on_success"):
            TransformSettings(  # type: ignore[call-arg]  # intentionally missing on_success
                name="passthrough_0",
                plugin="passthrough",
                input="source_out",
                on_error="discard",
                options={"schema": {"mode": "observed"}},
            )

    def test_non_terminal_transform_with_on_success_builds_when_properly_wired(self, plugin_manager) -> None:
        """Non-terminal transforms with explicit wired connections build successfully.

        In the new WiredTransform system, every transform declares input/on_success
        connections. This is not an error - it's how connection matching works.
        """
        from elspeth.core.config import ElspethSettings, SinkSettings, SourceSettings, TransformSettings
        from elspeth.core.dag import ExecutionGraph

        config = ElspethSettings(
            sources={
                "primary": _source_settings(
                    SourceSettings,
                    plugin="csv",
                    on_success="source_out",
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
                _transform_settings(
                    TransformSettings,
                    name="passthrough_0",
                    plugin="passthrough",
                    input="source_out",
                    on_success="conn_0_1",
                    options={"schema": {"mode": "observed"}},
                ),
                _transform_settings(
                    TransformSettings,
                    name="passthrough_1",
                    plugin="passthrough",
                    input="conn_0_1",
                    on_success="output",
                    options={"schema": {"mode": "observed"}},
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

    def test_terminal_transform_on_success_unknown_sink_raises(self, plugin_manager) -> None:
        """Terminal transform on_success must reference a configured sink."""
        from elspeth.core.config import ElspethSettings, SinkSettings, SourceSettings, TransformSettings
        from elspeth.core.dag import ExecutionGraph, GraphValidationError

        config = ElspethSettings(
            sources={
                "primary": _source_settings(
                    SourceSettings,
                    plugin="csv",
                    options={
                        "path": "test.csv",
                        "on_validation_failure": "discard",
                        "on_success": "source_out",
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
                _transform_settings(
                    TransformSettings,
                    plugin="passthrough",
                    input="source_out",
                    on_success="nowhere",
                    options={"schema": {"mode": "observed"}},
                ),
            ],
        )

        plugins = instantiate_plugins_from_config_raw(config)
        with pytest.raises(GraphValidationError, match=r"on_success 'nowhere' is neither a sink nor a known connection"):
            ExecutionGraph.from_plugin_instances(
                sources=plugins.sources,
                source_settings_map=plugins.source_settings_map,
                transforms=plugins.transforms,
                sinks=plugins.sinks,
                aggregations=plugins.aggregations,
                gates=list(config.gates),
            )

    def test_terminal_transform_with_valid_on_success_passes(self, plugin_manager) -> None:
        """Terminal transform with valid on_success builds successfully."""
        from elspeth.core.config import ElspethSettings, SinkSettings, SourceSettings, TransformSettings
        from elspeth.core.dag import ExecutionGraph

        config = ElspethSettings(
            sources={
                "primary": _source_settings(
                    SourceSettings,
                    plugin="csv",
                    options={
                        "path": "test.csv",
                        "on_validation_failure": "discard",
                        "on_success": "source_out",
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
                _transform_settings(
                    TransformSettings,
                    plugin="passthrough",
                    input="source_out",
                    on_success="output",
                    options={"schema": {"mode": "observed"}},
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

    def test_non_terminal_without_on_success_and_terminal_with_on_success_passes(self, plugin_manager) -> None:
        """Non-terminal transform omitted on_success, terminal declares it."""
        from elspeth.core.config import ElspethSettings, SinkSettings, SourceSettings, TransformSettings
        from elspeth.core.dag import ExecutionGraph

        config = ElspethSettings(
            sources={
                "primary": _source_settings(
                    SourceSettings,
                    plugin="csv",
                    options={
                        "path": "test.csv",
                        "on_validation_failure": "discard",
                        "on_success": "source_out",
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
                _transform_settings(
                    TransformSettings,
                    name="passthrough_0",
                    plugin="passthrough",
                    input="source_out",
                    on_success="conn_pt_fm",
                    options={"schema": {"mode": "observed"}},
                ),
                _transform_settings(
                    TransformSettings,
                    name="field_mapper_0",
                    plugin="field_mapper",
                    input="conn_pt_fm",
                    on_success="output",
                    options={"schema": {"mode": "observed"}},
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

    def test_transform_before_aggregation_not_treated_as_terminal(self, plugin_manager) -> None:
        """A transform before an aggregation is non-terminal and must not require on_success."""
        from elspeth.core.config import (
            AggregationSettings,
            ElspethSettings,
            SinkSettings,
            SourceSettings,
            TransformSettings,
            TriggerConfig,
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
                )
            },
            transforms=[
                _transform_settings(
                    TransformSettings,
                    plugin="passthrough",
                    on_success="to_agg",
                    options={"schema": {"mode": "observed"}},
                ),
            ],
            aggregations=[
                _aggregation_settings(
                    AggregationSettings,
                    name="batch_stats",
                    plugin="batch_stats",
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

        plugins = instantiate_plugins_from_config(config)
        graph = ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=list(config.gates),
        )

        graph.validate()

    def test_from_config_with_gate_routes(self, plugin_manager) -> None:
        """Build graph with config-driven gate routing to multiple sinks."""
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
                    name="safety_gate",
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

        # Should have:
        #   source -> safety_gate -> results (via "continue")
        #                         -> flagged (via "suspicious")
        assert graph.node_count == 4  # source, config_gate, results, flagged
        # Edges: source->gate, gate->results (continue), gate->flagged (route)
        assert graph.edge_count == 3

    def test_from_config_validates_route_targets(self, plugin_manager) -> None:
        """Config gate routes must reference existing sinks."""
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
                )
            },
            gates=[
                _gate_settings(
                    GateSettings,
                    name="bad_gate",
                    condition="True",
                    routes={"true": "nonexistent_sink", "false": "output"},
                ),
            ],
        )

        with pytest.raises(GraphValidationError) as exc_info:
            plugins = instantiate_plugins_from_config(config)
            ExecutionGraph.from_plugin_instances(
                sources=plugins.sources,
                source_settings_map=plugins.source_settings_map,
                transforms=plugins.transforms,
                sinks=plugins.sinks,
                aggregations=plugins.aggregations,
                gates=list(config.gates),
            )

        assert "nonexistent_sink" in str(exc_info.value)

    def test_get_sink_id_map(self, plugin_manager) -> None:
        """Get explicit sink_name -> node_id mapping."""
        from elspeth.contracts import SinkName
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
        sink_map = graph.get_sink_id_map()

        # Explicit mapping - no substring matching
        assert SinkName("results") in sink_map
        assert SinkName("flagged") in sink_map
        assert sink_map[SinkName("results")] != sink_map[SinkName("flagged")]

    def test_get_transform_id_map(self, plugin_manager) -> None:
        """Get explicit sequence -> node_id mapping for transforms."""
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
                )
            },
            transforms=[
                _transform_settings(
                    TransformSettings,
                    name="passthrough_0",
                    plugin="passthrough",
                    on_success="to_field_mapper",
                    options={"schema": {"mode": "observed"}},
                ),
                _transform_settings(
                    TransformSettings,
                    name="field_mapper_0",
                    plugin="field_mapper",
                    on_success="output",
                    options={"schema": {"mode": "observed"}},
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
        transform_map = graph.get_transform_id_map()

        # Explicit mapping by sequence position
        assert 0 in transform_map  # passthrough
        assert 1 in transform_map  # field_mapper
        assert transform_map[0] != transform_map[1]

    def test_pipeline_traversal_methods_linear(self, plugin_manager) -> None:
        """Traversal helpers should follow continue edges over processing nodes."""
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
                )
            },
            transforms=[
                _transform_settings(
                    TransformSettings,
                    name="passthrough_0",
                    plugin="passthrough",
                    on_success="to_field_mapper",
                    options={"schema": {"mode": "observed"}},
                ),
                _transform_settings(
                    TransformSettings,
                    name="field_mapper_0",
                    plugin="field_mapper",
                    on_success="output",
                    options={"schema": {"mode": "observed"}},
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

        sequence = graph.get_pipeline_node_sequence()
        assert len(sequence) == 2
        assert graph.get_next_node(graph.get_sources()[0]) == sequence[0]
        assert graph.get_next_node(sequence[0]) == sequence[1]
        assert graph.get_next_node(sequence[1]) is None

    def test_build_step_map_matches_schema_contract(self, plugin_manager) -> None:
        """Step numbering must satisfy Landscape DB schema contract (UniqueConstraint on token_id, step_index, attempt)."""
        from elspeth.contracts.types import GateName, SinkName
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
                    name="passthrough_0",
                    plugin="passthrough",
                    on_success="pt0_out",
                    options={"schema": {"mode": "observed"}},
                ),
                _transform_settings(
                    TransformSettings,
                    name="passthrough_1",
                    plugin="passthrough",
                    on_success="pt1_out",
                    options={"schema": {"mode": "observed"}},
                ),
                _transform_settings(
                    TransformSettings,
                    name="field_mapper_0",
                    plugin="field_mapper",
                    on_success="to_gates",
                    options={"schema": {"mode": "observed"}},
                ),
            ],
            gates=[
                _gate_settings(GateSettings, name="g1", condition="True", routes={"true": "g2_in", "false": "flagged"}),
                _gate_settings(GateSettings, name="g2", input="g2_in", condition="True", routes={"true": "output", "false": "output"}),
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

        plugins = instantiate_plugins_from_config(config)
        graph = ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=list(config.gates),
        )

        step_map = graph.build_step_map()
        source_id = graph.get_sources()[0]
        assert source_id is not None
        assert step_map[source_id] == 0

        transform_map = graph.get_transform_id_map()
        assert step_map[transform_map[0]] == 1
        assert step_map[transform_map[1]] == 2
        assert step_map[transform_map[2]] == 3

        config_gate_map = graph.get_config_gate_id_map()
        assert step_map[config_gate_map[GateName("g1")]] == 4
        assert step_map[config_gate_map[GateName("g2")]] == 5

        expected_step_map = {
            source_id: 0,
            transform_map[0]: 1,
            transform_map[1]: 2,
            transform_map[2]: 3,
            config_gate_map[GateName("g1")]: 4,
            config_gate_map[GateName("g2")]: 5,
        }
        assert step_map == expected_step_map

        sink_id = graph.get_sink_id_map()[SinkName("output")]
        assert graph.is_sink_node(sink_id) is True
        assert graph.is_sink_node(transform_map[0]) is False
        from elspeth.contracts.types import NodeID

        with pytest.raises(KeyError, match="Node not found"):
            graph.is_sink_node(NodeID("does_not_exist"))

    def test_get_terminal_sink_map_for_source_only(self, plugin_manager) -> None:
        """Source-only graph exposes terminal sink mapping (no test-time injection)."""
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
                    options={
                        "path": "test.csv",
                        "on_validation_failure": "discard",
                        "on_success": "results",
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

        terminal_map = graph.get_terminal_sink_map()
        source_node = graph.get_sources()[0]
        assert source_node is not None
        assert terminal_map[source_node] == "results"

        bad_config = ElspethSettings(
            sources={
                "primary": _source_settings(
                    SourceSettings,
                    plugin="csv",
                    options={
                        "path": "test.csv",
                        "on_validation_failure": "discard",
                        "on_success": "missing_sink",
                        "schema": {"mode": "observed"},
                    },
                )
            },
            sinks={
                "results": SinkSettings(
                    plugin="json", on_write_failure="discard", options={"path": "results.json", "schema": {"mode": "observed"}}
                ),
            },
        )
        bad_plugins = instantiate_plugins_from_config_raw(bad_config)
        with pytest.raises(
            GraphValidationError, match=r"Source 'primary' on_success 'missing_sink' is neither a sink nor a known connection"
        ):
            ExecutionGraph.from_plugin_instances(
                sources=bad_plugins.sources,
                source_settings_map=bad_plugins.source_settings_map,
                transforms=bad_plugins.transforms,
                sinks=bad_plugins.sinks,
                aggregations=bad_plugins.aggregations,
                gates=list(bad_config.gates),
            )


class TestDeterministicNodeIDs:
    """Tests for deterministic node ID generation."""

    def test_node_ids_are_deterministic_for_same_config(self) -> None:
        """Node IDs must be deterministic for checkpoint/resume compatibility."""
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
                    on_success="out",
                    options={"schema": {"mode": "observed"}},
                )
            ],
            sinks={
                "out": SinkSettings(plugin="json", on_write_failure="discard", options={"path": "out.json", "schema": {"mode": "observed"}})
            },
        )

        # Build graph twice with same config
        plugins1 = instantiate_plugins_from_config(config)
        graph1 = ExecutionGraph.from_plugin_instances(
            sources=plugins1.sources,
            source_settings_map=plugins1.source_settings_map,
            transforms=plugins1.transforms,
            sinks=plugins1.sinks,
            aggregations=plugins1.aggregations,
            gates=list(config.gates),
        )

        plugins2 = instantiate_plugins_from_config(config)
        graph2 = ExecutionGraph.from_plugin_instances(
            sources=plugins2.sources,
            source_settings_map=plugins2.source_settings_map,
            transforms=plugins2.transforms,
            sinks=plugins2.sinks,
            aggregations=plugins2.aggregations,
            gates=list(config.gates),
        )

        # Node IDs must be identical
        nodes1 = sorted(graph1._graph.nodes())
        nodes2 = sorted(graph2._graph.nodes())

        assert nodes1 == nodes2, "Node IDs must be deterministic for checkpoint compatibility"

    def test_node_ids_change_when_config_changes(self) -> None:
        """Node IDs should change if plugin config changes."""
        from elspeth.core.config import (
            ElspethSettings,
            SinkSettings,
            SourceSettings,
        )
        from elspeth.core.dag import ExecutionGraph

        config1 = ElspethSettings(
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
            transforms=[],
            sinks={
                "out": SinkSettings(plugin="json", on_write_failure="discard", options={"path": "out.json", "schema": {"mode": "observed"}})
            },
        )

        config2 = ElspethSettings(
            sources={
                "primary": _source_settings(
                    SourceSettings,
                    plugin="csv",
                    options={
                        "path": "test.csv",
                        "on_validation_failure": "discard",
                        "schema": {"mode": "fixed", "fields": ["id: int"]},  # Different!
                    },
                )
            },
            transforms=[],
            sinks={
                "out": SinkSettings(plugin="json", on_write_failure="discard", options={"path": "out.json", "schema": {"mode": "observed"}})
            },
        )

        plugins1 = instantiate_plugins_from_config(config1)
        graph1 = ExecutionGraph.from_plugin_instances(
            sources=plugins1.sources,
            source_settings_map=plugins1.source_settings_map,
            transforms=plugins1.transforms,
            sinks=plugins1.sinks,
            aggregations=plugins1.aggregations,
            gates=list(config1.gates),
        )

        plugins2 = instantiate_plugins_from_config(config2)
        graph2 = ExecutionGraph.from_plugin_instances(
            sources=plugins2.sources,
            source_settings_map=plugins2.source_settings_map,
            transforms=plugins2.transforms,
            sinks=plugins2.sinks,
            aggregations=plugins2.aggregations,
            gates=list(config2.gates),
        )

        # Source node IDs should differ (different config)
        source_id_1 = next(n for n in graph1._graph.nodes() if n.startswith("source_"))
        source_id_2 = next(n for n in graph2._graph.nodes() if n.startswith("source_"))

        assert source_id_1 != source_id_2

    def test_overlong_node_name_is_rejected_at_settings_level(self) -> None:
        """Overlong transform names fail fast at settings validation.

        The _MAX_NODE_NAME_LENGTH=38 constraint on TransformSettings ensures
        generated node IDs stay within the 64-char landscape column limit.
        This is the primary protection layer; the DAG-level node_id length
        check is defense-in-depth that is unreachable with valid settings.
        """
        from pydantic import ValidationError

        from elspeth.core.config import TransformSettings

        long_name = "x" * 39  # Exceeds _MAX_NODE_NAME_LENGTH (38)
        with pytest.raises(ValidationError, match=r"exceeds max length 38"):
            TransformSettings(
                name=long_name,
                plugin="passthrough",
                input="source_out",
                on_success="out",
                on_error="discard",
                options={"schema": {"mode": "observed"}},
            )


class TestNodeInfoImmutability:
    """Tests for NodeInfo config immutability after graph construction."""

    def test_config_frozen_after_from_plugin_instances(self, plugin_manager) -> None:
        """NodeInfo.config should be MappingProxyType after from_plugin_instances.

        The DAG builder freezes all NodeInfo configs to MappingProxyType after
        construction to prevent accidental mutation of node configs. Attempting
        to set a key should raise TypeError.
        """
        from types import MappingProxyType

        from elspeth.core.config import SinkSettings, SourceSettings
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
                    plugin="json", on_write_failure="discard", options={"path": "out.json", "schema": {"mode": "observed"}}
                ),
            },
        )
        plugins = instantiate_plugins_from_config(config)
        graph = ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=list(config.gates),
            coalesce_settings=config.coalesce,
        )
        frozen_count = 0
        for info in graph.get_nodes():
            if info.config:
                # config is typed as dict[str, Any] but frozen to MappingProxyType
                # by the builder at runtime. Mypy considers this unreachable because
                # dict and MappingProxyType are disjoint final types.
                assert isinstance(info.config, MappingProxyType), (  # type: ignore[unreachable]
                    f"Node '{info.node_id}' config should be MappingProxyType after construction, got {type(info.config).__name__}"
                )
                with pytest.raises(TypeError):  # type: ignore[unreachable]
                    info.config["injected_key"] = "should_fail"
                frozen_count += 1
        assert frozen_count > 0, "Expected at least one node with non-empty config"
