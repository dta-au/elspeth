"""Tests for ExecutionGraph validation error paths and NodeInfo guards.

Exercises rejection paths in graph.py and models.py that are only
implicitly (or never) tested through the builder. Each test constructs
the minimal graph state needed to trigger the specific error path.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from elspeth.contracts import EdgeInfo, PluginSchema
from elspeth.contracts.enums import NodeType, RoutingMode
from elspeth.contracts.schema import FieldDefinition, SchemaConfig
from elspeth.contracts.types import NodeID, SinkName
from elspeth.core.dag import schema_validation
from elspeth.core.dag.graph import ExecutionGraph
from elspeth.core.dag.guarantees import walk_effective_guarantee_vote
from elspeth.core.dag.models import GraphValidationError, NodeInfo

# ---------------------------------------------------------------------------
# Gap 2: _validate_route_resolution_map_complete — all labels missing
# ---------------------------------------------------------------------------


class TestRouteResolutionMapCompleteAllMissing:
    """validate() must reject a gate with MOVE edges to sinks but no route labels.

    The existing test suite covers partial incompleteness (some labels present,
    some missing). This tests the "completely unwired gate" case where NO
    route labels are registered at all.
    """

    def test_gate_with_move_edge_but_no_route_label_raises(self) -> None:
        """Gate has MOVE edge to a sink registered in sink_id_map, but zero route labels."""
        graph = ExecutionGraph()

        # Minimal valid topology: source -> gate -> sink
        graph.add_node("src", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("gate_1", node_type=NodeType.GATE, plugin_name="expression")
        graph.add_node("sink_1", node_type=NodeType.SINK, plugin_name="json")

        graph.add_edge("src", "gate_1", label="continue")
        graph.add_edge("gate_1", "sink_1", label="route_true", mode=RoutingMode.MOVE)

        # Register the sink so the route-label check doesn't early-return
        graph.set_sink_id_map({SinkName("output"): NodeID("sink_1")})

        # Deliberately do NOT add any route label entries
        with pytest.raises(GraphValidationError, match="no registered route label"):
            graph.validate()

    def test_gate_with_multiple_unwired_move_edges_raises(self) -> None:
        """Gate with two MOVE edges to different sinks, neither wired — first triggers error."""
        graph = ExecutionGraph()

        graph.add_node("src", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("gate_1", node_type=NodeType.GATE, plugin_name="expression")
        graph.add_node("sink_a", node_type=NodeType.SINK, plugin_name="json")
        graph.add_node("sink_b", node_type=NodeType.SINK, plugin_name="json")

        graph.add_edge("src", "gate_1", label="continue")
        graph.add_edge("gate_1", "sink_a", label="route_true", mode=RoutingMode.MOVE)
        graph.add_edge("gate_1", "sink_b", label="route_false", mode=RoutingMode.MOVE)

        graph.set_sink_id_map(
            {
                SinkName("output_a"): NodeID("sink_a"),
                SinkName("output_b"): NodeID("sink_b"),
            }
        )

        with pytest.raises(GraphValidationError, match="no registered route label"):
            graph.validate()


class TestTypedEdgeContracts:
    """ExecutionGraph edge query APIs must return EdgeInfo contracts."""

    def test_get_edges_and_incoming_edges_preserve_routing_mode_enum(self) -> None:
        graph = ExecutionGraph()
        graph.add_node("src", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("gate", node_type=NodeType.GATE, plugin_name="expression")
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="json")

        graph.add_edge("src", "gate", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("gate", "sink", label="flagged", mode=RoutingMode.COPY)

        edges = graph.get_edges()
        assert all(isinstance(edge, EdgeInfo) for edge in edges)
        assert {edge.label: edge.mode for edge in edges} == {
            "continue": RoutingMode.MOVE,
            "flagged": RoutingMode.COPY,
        }
        assert all(isinstance(edge.mode, RoutingMode) for edge in edges)

        incoming = graph.get_incoming_edges("sink")
        assert incoming == [
            EdgeInfo(
                from_node=NodeID("gate"),
                to_node=NodeID("sink"),
                label="flagged",
                mode=RoutingMode.COPY,
            )
        ]


class TestMultiProducerFanInValidation:
    """Multi-producer fan-in policy distinguishes terminal sinks from processing nodes."""

    def test_multi_source_direct_sink_fan_in_is_valid(self) -> None:
        """Direct multi-source sink fan-in is terminal write policy, not QUEUE bypass."""
        graph = ExecutionGraph()
        graph.add_node("orders", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("refunds", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("audit_sink", node_type=NodeType.SINK, plugin_name="json")

        graph.add_edge("orders", "audit_sink", label="orders_out", mode=RoutingMode.MOVE)
        graph.add_edge("refunds", "audit_sink", label="refunds_out", mode=RoutingMode.MOVE)

        graph.validate()

    def test_multi_source_processing_node_fan_in_requires_queue(self) -> None:
        """Ordinary processing nodes still require explicit QUEUE fan-in."""
        graph = ExecutionGraph()
        graph.add_node("orders", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("refunds", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("normalize", node_type=NodeType.TRANSFORM, plugin_name="mapper")
        graph.add_node("audit_sink", node_type=NodeType.SINK, plugin_name="json")

        graph.add_edge("orders", "normalize", label="orders_out", mode=RoutingMode.MOVE)
        graph.add_edge("refunds", "normalize", label="refunds_out", mode=RoutingMode.MOVE)
        graph.add_edge("normalize", "audit_sink", label="normalized", mode=RoutingMode.MOVE)

        with pytest.raises(GraphValidationError, match="fan-in from multiple producers without a queue"):
            graph.validate()


class TestRowUnionSchemaCompatibility:
    """row_union is UNION ALL: known fixed branch rows need one long-format contract."""

    @staticmethod
    def _graph(
        left_schema: type[PluginSchema] | None,
        right_schema: type[PluginSchema] | None,
        *,
        left_mode: RoutingMode = RoutingMode.MOVE,
        left_schema_config: SchemaConfig | None = None,
        right_schema_config: SchemaConfig | None = None,
    ) -> ExecutionGraph:
        graph = ExecutionGraph()
        graph.add_node(
            "left",
            node_type=NodeType.TRANSFORM,
            plugin_name="left",
            output_schema=left_schema,
            output_schema_config=left_schema_config,
        )
        graph.add_node(
            "right",
            node_type=NodeType.TRANSFORM,
            plugin_name="right",
            output_schema=right_schema,
            output_schema_config=right_schema_config,
        )
        graph.add_node(
            "union",
            node_type=NodeType.ROW_UNION,
            plugin_name="row_union:union",
            config={
                "branches": {
                    "left_branch": "left_out",
                    "right_branch": "right_out",
                },
                "on_success": "union_out",
            },
        )
        graph.add_edge("left", "union", label="continue", mode=left_mode)
        graph.add_edge("right", "union", label="continue", mode=RoutingMode.MOVE)
        return graph

    def test_compatible_fixed_branch_schemas_are_accepted(self) -> None:
        class LeftSchema(PluginSchema):
            id: str
            score: float

        class RightSchema(PluginSchema):
            id: str
            score: float

        self._graph(LeftSchema, RightSchema).validate_edge_compatibility()

    def test_incompatible_fixed_branch_schemas_are_rejected(self) -> None:
        class ScoredRow(PluginSchema):
            id: str
            score: float

        class LabelledRow(PluginSchema):
            id: str
            label: str

        with pytest.raises(GraphValidationError, match=r"row_union 'union'.*incompatible schemas"):
            self._graph(ScoredRow, LabelledRow).validate_edge_compatibility()

    def test_observed_branch_abstains_against_fixed_branch(self) -> None:
        class FixedRow(PluginSchema):
            id: str
            score: float

        self._graph(None, FixedRow).validate_edge_compatibility()

    def test_divert_branch_abstains_from_declared_output_schema(self) -> None:
        class SuccessSchema(PluginSchema):
            id: str
            score: float

        class OtherSuccessSchema(PluginSchema):
            id: str
            label: str

        self._graph(
            SuccessSchema,
            OtherSuccessSchema,
            left_mode=RoutingMode.DIVERT,
        ).validate_edge_compatibility()

    def test_disjoint_flexible_branch_declarations_are_compatible(self) -> None:
        class ScoredRow(PluginSchema):
            score: float

        class LabelledRow(PluginSchema):
            label: str

        self._graph(
            ScoredRow,
            LabelledRow,
            left_schema_config=SchemaConfig(
                mode="flexible",
                fields=(FieldDefinition("score", "float"),),
            ),
            right_schema_config=SchemaConfig(
                mode="flexible",
                fields=(FieldDefinition("label", "str"),),
            ),
        ).validate_edge_compatibility()

    def test_flexible_branches_reject_conflicting_shared_field_types(self) -> None:
        class StringIdRow(PluginSchema):
            id: str

        class IntegerIdRow(PluginSchema):
            id: int

        with pytest.raises(GraphValidationError, match=r"row_union 'union'.*incompatible schemas.*id"):
            self._graph(
                StringIdRow,
                IntegerIdRow,
                left_schema_config=SchemaConfig(
                    mode="flexible",
                    fields=(FieldDefinition("id", "str"),),
                ),
                right_schema_config=SchemaConfig(
                    mode="flexible",
                    fields=(FieldDefinition("id", "int"),),
                ),
            ).validate_edge_compatibility()


class TestEffectiveProducerSchemaConfig:
    """Pass-through gates expose a config only when every live input agrees."""

    def test_mixed_known_and_unknown_inputs_abstain(self) -> None:
        known_config = SchemaConfig(
            mode="fixed",
            fields=(FieldDefinition("id", "int"),),
        )
        graph = ExecutionGraph()
        graph.add_node(
            "known",
            node_type=NodeType.TRANSFORM,
            plugin_name="known",
            output_schema_config=known_config,
        )
        graph.add_node(
            "unknown",
            node_type=NodeType.TRANSFORM,
            plugin_name="unknown",
        )
        graph.add_node("gate", node_type=NodeType.GATE, plugin_name="gate")
        graph.add_edge("known", "gate", label="known", mode=RoutingMode.MOVE)
        graph.add_edge("unknown", "gate", label="unknown", mode=RoutingMode.MOVE)

        assert schema_validation.get_effective_producer_schema_config(graph, "gate") is None


# ---------------------------------------------------------------------------
# Gap 3: NodeInfo.__post_init__ node_id length validation
# ---------------------------------------------------------------------------


class TestNodeInfoNodeIdLengthValidation:
    """NodeInfo must reject node_id exceeding the column length limit.

    The Pydantic layer validates this too, but __post_init__ is the
    defense-in-depth guard that fires regardless of construction path.
    """

    def test_node_id_at_limit_accepted(self) -> None:
        """64-character node_id is exactly at the limit — must not raise."""
        node_id = "x" * 64
        info = NodeInfo(
            node_id=NodeID(node_id),
            node_type=NodeType.TRANSFORM,
            plugin_name="passthrough",
        )
        assert info.node_id == NodeID(node_id)

    def test_node_id_over_limit_raises(self) -> None:
        """65-character node_id exceeds the column limit — must raise."""
        node_id = "x" * 65
        with pytest.raises(GraphValidationError, match="node_id exceeds"):
            NodeInfo(
                node_id=NodeID(node_id),
                node_type=NodeType.TRANSFORM,
                plugin_name="passthrough",
            )

    def test_node_id_way_over_limit_raises(self) -> None:
        """200-character node_id — error message includes actual length."""
        node_id = "a" * 200
        with pytest.raises(GraphValidationError, match="length=200"):
            NodeInfo(
                node_id=NodeID(node_id),
                node_type=NodeType.TRANSFORM,
                plugin_name="passthrough",
            )


class TestNodeInfoIdentifierValidation:
    """NodeInfo must reject blank identifiers before they reach the graph/audit path."""

    def test_node_id_length_contract_is_owned_by_contracts_not_landscape_schema(self) -> None:
        import elspeth.core.dag.models as dag_models
        from elspeth.contracts import types as contract_types
        from elspeth.core.landscape import schema as landscape_schema

        assert hasattr(contract_types, "NODE_ID_MAX_LENGTH")
        assert dag_models._NODE_ID_MAX_LENGTH == contract_types.NODE_ID_MAX_LENGTH
        assert landscape_schema.NODE_ID_COLUMN_LENGTH == contract_types.NODE_ID_MAX_LENGTH
        assert landscape_schema.nodes_table.c.node_id.type.length == contract_types.NODE_ID_MAX_LENGTH
        node_id_storage_columns = {("nodes", "node_id")}
        for table in landscape_schema.metadata.tables.values():
            for constraint in table.foreign_key_constraints:
                for element in constraint.elements:
                    if element.column.table.name == "nodes" and element.column.name == "node_id":
                        node_id_storage_columns.add((table.name, element.parent.name))
                        assert element.parent.type.length == contract_types.NODE_ID_MAX_LENGTH

        dag_models_source = Path(dag_models.__file__).read_text(encoding="utf-8")
        tree = ast.parse(dag_models_source)
        forbidden_landscape_schema_imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                forbidden_landscape_schema_imports.extend(
                    alias.name for alias in node.names if alias.name == "elspeth.core.landscape.schema"
                )
            if isinstance(node, ast.ImportFrom):
                if node.module == "elspeth.core.landscape.schema":
                    forbidden_landscape_schema_imports.append(node.module)
                if node.module == "elspeth.core.landscape":
                    forbidden_landscape_schema_imports.extend(alias.name for alias in node.names if alias.name == "schema")
        assert forbidden_landscape_schema_imports == []

        schema_source = Path(landscape_schema.__file__).read_text(encoding="utf-8")
        schema_tree = ast.parse(schema_source)
        active_table: str | None = None
        hardcoded_node_width_columns: list[str] = []
        for node in ast.walk(schema_tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            if not isinstance(node.value.func, ast.Name) or node.value.func.id != "Table":
                continue
            if not node.value.args or not isinstance(node.value.args[0], ast.Constant):
                continue
            table_name = node.value.args[0].value
            if not isinstance(table_name, str):
                continue
            active_table = table_name
            for arg in node.value.args[2:]:
                if not isinstance(arg, ast.Call) or not isinstance(arg.func, ast.Name) or arg.func.id != "Column":
                    continue
                if len(arg.args) < 2 or not isinstance(arg.args[0], ast.Constant):
                    continue
                column_name = arg.args[0].value
                if (active_table, column_name) not in node_id_storage_columns:
                    continue
                type_arg = arg.args[1]
                if (
                    isinstance(type_arg, ast.Call)
                    and isinstance(type_arg.func, ast.Name)
                    and type_arg.func.id == "String"
                    and type_arg.args
                    and isinstance(type_arg.args[0], ast.Constant)
                    and type_arg.args[0].value == 64
                ):
                    hardcoded_node_width_columns.append(f"{active_table}.{column_name}")
        assert hardcoded_node_width_columns == []

    def test_node_info_rejects_empty_node_id(self) -> None:
        with pytest.raises(GraphValidationError, match="node_id must not be empty"):
            NodeInfo(
                node_id=NodeID(""),
                node_type=NodeType.TRANSFORM,
                plugin_name="passthrough",
            )

    def test_node_info_rejects_empty_plugin_name(self) -> None:
        with pytest.raises(GraphValidationError, match="plugin_name must not be empty"):
            NodeInfo(
                node_id=NodeID("node_1"),
                node_type=NodeType.TRANSFORM,
                plugin_name="",
            )

    def test_node_info_rejects_whitespace_only_plugin_name(self) -> None:
        with pytest.raises(GraphValidationError, match="plugin_name must not be empty"):
            NodeInfo(
                node_id=NodeID("node_1"),
                node_type=NodeType.TRANSFORM,
                plugin_name="   ",
            )

    def test_execution_graph_add_node_rejects_blank_source_identifiers(self) -> None:
        graph = ExecutionGraph()

        with pytest.raises(GraphValidationError, match="node_id must not be empty"):
            graph.add_node("", node_type=NodeType.SOURCE, plugin_name="")


# ---------------------------------------------------------------------------
# Gap 3b: NodeInfo.declared_required_fields sink-only invariant
# ---------------------------------------------------------------------------


class TestNodeInfoDeclaredRequiredFieldsSinkOnly:
    """NodeInfo.declared_required_fields is meaningful only for SINK nodes.

    Offensive-programming invariant added during schema-contract reconciliation:
    stray declared_required_fields on a non-sink node would sit unused until a
    future validator widens its scope and produces mysterious errors. Catch the
    misuse at construction time instead.
    """

    def test_node_info_sink_allows_declared_required_fields(self) -> None:
        """SINK nodes are the legitimate consumer of declared_required_fields."""
        info = NodeInfo(
            node_id=NodeID("my_sink"),
            node_type=NodeType.SINK,
            plugin_name="csv",
            declared_required_fields=frozenset({"id", "name"}),
        )
        assert info.declared_required_fields == frozenset({"id", "name"})

    def test_node_info_rejects_declared_required_fields_on_non_sink(self) -> None:
        """Offensive-programming invariant: declared_required_fields is sink-specific.

        Catches the misuse at construction time rather than letting stray data
        sit on a non-sink node until a future validator widens its scope and
        produces mysterious errors.
        """
        for non_sink_type in [
            NodeType.SOURCE,
            NodeType.TRANSFORM,
            NodeType.GATE,
            NodeType.AGGREGATION,
            NodeType.COALESCE,
        ]:
            with pytest.raises(GraphValidationError, match=r"only meaningful for SINK"):
                NodeInfo(
                    node_id=NodeID("bad_node"),
                    node_type=non_sink_type,
                    plugin_name="something",
                    declared_required_fields=frozenset({"x"}),
                )


# ---------------------------------------------------------------------------
# Gap 4: topological_order() cycle detection
# ---------------------------------------------------------------------------


class TestTopologicalOrderCycleDetection:
    """topological_order() must wrap NetworkXUnfeasible into GraphValidationError.

    The builder's validate() also checks for cycles, but topological_order()
    has its own independent guard. This tests it directly.
    """

    def test_cycle_raises_graph_validation_error(self) -> None:
        """Two-node cycle must raise GraphValidationError, not NetworkXUnfeasible."""
        graph = ExecutionGraph()

        graph.add_node("a", node_type=NodeType.TRANSFORM, plugin_name="passthrough")
        graph.add_node("b", node_type=NodeType.TRANSFORM, plugin_name="passthrough")

        graph.add_edge("a", "b", label="forward")
        graph.add_edge("b", "a", label="backward")

        with pytest.raises(GraphValidationError, match="Cannot sort graph"):
            graph.topological_order()

    def test_self_loop_raises_graph_validation_error(self) -> None:
        """Self-loop is a trivial cycle — must still raise GraphValidationError."""
        graph = ExecutionGraph()

        graph.add_node("a", node_type=NodeType.TRANSFORM, plugin_name="passthrough")
        graph.add_edge("a", "a", label="loop")

        with pytest.raises(GraphValidationError, match="Cannot sort graph"):
            graph.topological_order()


# ---------------------------------------------------------------------------
# Gap 5: get_sources() with zero and multiple sources
# ---------------------------------------------------------------------------


class TestGetSourceErrorPaths:
    """get_sources() exposes the current multi-source graph contract."""

    def test_no_sources_raises(self) -> None:
        """Graph validation rejects a graph with no source nodes."""
        graph = ExecutionGraph()
        graph.add_node("t1", node_type=NodeType.TRANSFORM, plugin_name="passthrough")
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="json")
        graph.add_edge("t1", "sink", label="out", mode=RoutingMode.MOVE)

        assert graph.get_sources() == []
        with pytest.raises(GraphValidationError, match="Graph must have at least one source"):
            graph.validate()

    def test_multiple_sources_are_returned(self) -> None:
        """Graph with two source nodes is valid in the multi-source model."""
        graph = ExecutionGraph()
        graph.add_node("src_a", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("src_b", node_type=NodeType.SOURCE, plugin_name="csv")
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="json")
        graph.add_edge("src_a", "sink", label="src_a_out", mode=RoutingMode.MOVE)
        graph.add_edge("src_b", "sink", label="src_b_out", mode=RoutingMode.MOVE)

        assert graph.get_sources() == [NodeID("src_a"), NodeID("src_b")]
        graph.validate()

    def test_empty_graph_raises(self) -> None:
        """Completely empty graph has zero sources and fails validation."""
        graph = ExecutionGraph()

        assert graph.get_sources() == []
        with pytest.raises(GraphValidationError, match="Graph must have at least one source"):
            graph.validate()


# ---------------------------------------------------------------------------
# validate_transform_output_field_collisions — build-time detection of the
# in-place rewrite the TransformExecutor preflight otherwise raises per-row
# (elspeth-cfcd333f83)
# ---------------------------------------------------------------------------


def _collision_graph(
    *,
    source_schema: dict[str, object],
    declared_output_fields: frozenset[str],
) -> ExecutionGraph:
    """source -> transform -> sink, with the transform's declaration under test."""
    graph = ExecutionGraph()
    graph.add_node("src", node_type=NodeType.SOURCE, plugin_name="text", config={"schema": source_schema})
    graph.add_node(
        "t1",
        node_type=NodeType.TRANSFORM,
        plugin_name="llm",
        config={"schema": {"mode": "observed"}},
        declared_output_fields=declared_output_fields,
    )
    graph.add_node("sink", node_type=NodeType.SINK, plugin_name="text", config={"schema": {"mode": "observed"}})
    graph.add_edge("src", "t1", label="continue", mode=RoutingMode.MOVE)
    graph.add_edge("t1", "sink", label="out", mode=RoutingMode.MOVE)
    return graph


class TestTransformOutputFieldCollisions:
    """A transform must not declare an output field its input already carries.

    The runtime equivalent is TransformExecutor._run_preflight raising
    PluginContractViolation ("would overwrite existing input fields") on the
    first row. That is a pipeline CONFIGURATION error, so it belongs on the
    build-time surface `elspeth run` and the web POST /validate both reach.
    """

    def test_upstream_guaranteed_field_is_rejected(self) -> None:
        """Ticket shape: the transform's output field is guaranteed upstream."""
        graph = _collision_graph(
            source_schema={"mode": "observed", "guaranteed_fields": ["headline"]},
            declared_output_fields=frozenset({"headline", "headline_model"}),
        )

        with pytest.raises(GraphValidationError, match="headline") as exc_info:
            schema_validation.validate_transform_output_field_collisions(graph)

        assert exc_info.value.component_id == "t1"
        assert exc_info.value.component_type == "transform"
        # Only the colliding field is named — the transform's other declared
        # outputs are legitimate additions.
        assert "headline_model" not in str(exc_info.value)

    def test_reached_through_validate_edge_compatibility(self) -> None:
        """The check is wired into the surface /validate and `elspeth run` reach."""
        graph = _collision_graph(
            source_schema={"mode": "observed", "guaranteed_fields": ["headline"]},
            declared_output_fields=frozenset({"headline"}),
        )

        with pytest.raises(GraphValidationError, match="headline"):
            graph.validate_edge_compatibility()

    def test_non_colliding_output_field_builds(self) -> None:
        """Negative control: a fresh output field must remain buildable."""
        graph = _collision_graph(
            source_schema={"mode": "observed", "guaranteed_fields": ["headline"]},
            declared_output_fields=frozenset({"title_cased"}),
        )

        schema_validation.validate_transform_output_field_collisions(graph)

    def test_abstaining_upstream_is_not_rejected(self) -> None:
        """Abstention control: never reject on a guess.

        The declared output field is the SAME name the rejection test uses, so
        the only difference is that this upstream makes no guarantee at all.
        An observed source may or may not carry 'headline'; that stays enforced
        per-row by the executor preflight.
        """
        graph = _collision_graph(
            source_schema={"mode": "observed"},
            declared_output_fields=frozenset({"headline"}),
        )

        # Pin the precondition: a change to participates_in_propagation that
        # made this upstream vote must break this test loudly rather than
        # silently voiding what it controls for.
        vote = walk_effective_guarantee_vote(graph, "src", {})
        assert vote.participated is False
        assert vote.fields == frozenset()

        schema_validation.validate_transform_output_field_collisions(graph)

    def test_multi_hop_guarantee_through_pass_through_transform_is_rejected(self) -> None:
        """source -> pass-through transform -> transform.

        The colliding field is guaranteed by the SOURCE, not by the transform's
        direct predecessor's own declaration — it only reaches the second
        transform because ADR-007 propagation carries it through the
        pass-through node. Rejecting here proves the check consults the
        effective-guarantee walk, not the predecessor's raw schema.
        """
        graph = ExecutionGraph()
        graph.add_node(
            "src",
            node_type=NodeType.SOURCE,
            plugin_name="text",
            config={"schema": {"mode": "observed", "guaranteed_fields": ["headline"]}},
        )
        graph.add_node(
            "passthru",
            node_type=NodeType.TRANSFORM,
            plugin_name="enrich",
            config={"schema": {"mode": "observed", "guaranteed_fields": ["extra"]}},
            declared_output_fields=frozenset({"extra"}),
            passes_through_input=True,
        )
        graph.add_node(
            "rewriter",
            node_type=NodeType.TRANSFORM,
            plugin_name="llm",
            config={"schema": {"mode": "observed"}},
            declared_output_fields=frozenset({"headline"}),
        )
        graph.add_edge("src", "passthru", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("passthru", "rewriter", label="continue", mode=RoutingMode.MOVE)

        # The direct predecessor's OWN declaration does not name 'headline';
        # only the propagated vote does.
        assert graph.get_guaranteed_fields("passthru") == frozenset({"extra"})
        assert walk_effective_guarantee_vote(graph, "passthru", {}).fields == frozenset({"extra", "headline"})

        with pytest.raises(GraphValidationError, match="headline") as exc_info:
            schema_validation.validate_transform_output_field_collisions(graph)
        assert exc_info.value.component_id == "rewriter"

    def test_divert_only_predecessor_is_not_rejected(self) -> None:
        """DIVERT edges are structural markers, not live inbound paths.

        Rows reach a DIVERT destination through exception handling carrying an
        error envelope, not by traversing the edge with the producer's declared
        output. Inheriting a guarantee across one would reject a runnable
        pipeline.
        """
        graph = ExecutionGraph()
        graph.add_node(
            "src",
            node_type=NodeType.SOURCE,
            plugin_name="text",
            config={"schema": {"mode": "observed", "guaranteed_fields": ["headline"]}},
        )
        graph.add_node(
            "quarantine",
            node_type=NodeType.TRANSFORM,
            plugin_name="llm",
            config={"schema": {"mode": "observed"}},
            declared_output_fields=frozenset({"headline"}),
        )
        graph.add_edge("src", "quarantine", label="on_error", mode=RoutingMode.DIVERT)

        schema_validation.validate_transform_output_field_collisions(graph)

    def test_live_edge_alongside_divert_edge_is_still_rejected(self) -> None:
        """A predecessor with BOTH a MOVE and a DIVERT edge stays live.

        Guards the regrouping in _live_predecessors: filtering edge-wise
        without collapsing by predecessor would let the DIVERT edge mask a
        real MOVE path from the same producer.
        """
        graph = ExecutionGraph()
        graph.add_node(
            "src",
            node_type=NodeType.SOURCE,
            plugin_name="text",
            config={"schema": {"mode": "observed", "guaranteed_fields": ["headline"]}},
        )
        graph.add_node(
            "t1",
            node_type=NodeType.TRANSFORM,
            plugin_name="llm",
            config={"schema": {"mode": "observed"}},
            declared_output_fields=frozenset({"headline"}),
        )
        graph.add_edge("src", "t1", label="on_error", mode=RoutingMode.DIVERT)
        graph.add_edge("src", "t1", label="continue", mode=RoutingMode.MOVE)

        with pytest.raises(GraphValidationError, match="headline"):
            schema_validation.validate_transform_output_field_collisions(graph)

    def test_divert_mode_stored_as_plain_string_is_still_skipped(self) -> None:
        """``add_edge`` does not coerce ``mode``, so the string form must skip too.

        Pins the ``==`` comparison in ``_live_predecessors``: switching it to an
        identity check (``is``) would treat a plain ``"divert"`` edge as live and
        reject a runnable pipeline.
        """
        graph = ExecutionGraph()
        graph.add_node(
            "src",
            node_type=NodeType.SOURCE,
            plugin_name="text",
            config={"schema": {"mode": "observed", "guaranteed_fields": ["headline"]}},
        )
        graph.add_node(
            "t1",
            node_type=NodeType.TRANSFORM,
            plugin_name="llm",
            config={"schema": {"mode": "observed"}},
            declared_output_fields=frozenset({"headline"}),
        )
        graph.add_edge("src", "t1", label="on_error", mode="divert")

        schema_validation.validate_transform_output_field_collisions(graph)

    def test_transform_declaring_nothing_is_skipped(self) -> None:
        """No declaration means no fields added, so no collision is possible."""
        graph = _collision_graph(
            source_schema={"mode": "observed", "guaranteed_fields": ["headline"]},
            declared_output_fields=frozenset(),
        )

        schema_validation.validate_transform_output_field_collisions(graph)


# ---------------------------------------------------------------------------
# Build-time Rule A mirror: guaranteed extras vs locked consumer input
# (elspeth-9615d6c75a)
# ---------------------------------------------------------------------------


def _locked_input_model(*, optional_a: bool = False) -> type[PluginSchema]:
    """The consumer half of the seam: a mode:fixed input model admitting [url]."""
    from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config

    fields = [FieldDefinition("url", "str")]
    if optional_a:
        fields.append(FieldDefinition("a", "str", required=False))
    return create_schema_from_config(
        SchemaConfig(mode="fixed", fields=tuple(fields)),
        "LockedUrlInput",
        allow_coercion=False,
    )


def _locked_consumer_graph(
    *,
    source_schema: dict[str, object],
    consumer_input_schema: type[PluginSchema] | None,
    consumer_node_type: NodeType = NodeType.TRANSFORM,
) -> ExecutionGraph:
    """source -> locked consumer (-> sink when the consumer is a transform)."""
    graph = ExecutionGraph()
    graph.add_node(
        "src",
        node_type=NodeType.SOURCE,
        plugin_name="csv",
        config={"schema": source_schema},
    )
    if consumer_node_type == NodeType.SINK:
        graph.add_node(
            "t1",
            node_type=NodeType.SINK,
            plugin_name="json",
            config={"schema": {"mode": "fixed", "fields": ["url: str"]}},
            input_schema=consumer_input_schema,
        )
        graph.add_edge("src", "t1", label="continue", mode=RoutingMode.MOVE)
        return graph
    graph.add_node(
        "t1",
        node_type=NodeType.TRANSFORM,
        plugin_name="web_scrape",
        config={"schema": {"mode": "fixed", "fields": ["url: str"]}},
        input_schema=consumer_input_schema,
    )
    graph.add_node(
        "sink",
        node_type=NodeType.SINK,
        plugin_name="json",
        config={"schema": {"mode": "observed"}},
    )
    graph.add_edge("src", "t1", label="continue", mode=RoutingMode.MOVE)
    graph.add_edge("t1", "sink", label="out", mode=RoutingMode.MOVE)
    return graph


class TestLockedConsumerGuaranteedExtras:
    """Build-time mirror of composer Rule A (locked_input_extras).

    A producer guarantee means every row WILL carry the field; a locked
    (mode: fixed, extra=forbid) consumer that does not admit it therefore
    kills every row at the executor input preflight
    (engine/executors/transform.py model_validate). That certainty makes
    build-time rejection sound. The composer already rejects this shape
    (Rule A, web/composer/state.py locked_input_extras); these tests close
    the YAML/DAG half of the seam (elspeth-9615d6c75a).
    """

    def test_guaranteed_extra_against_locked_consumer_is_rejected(self) -> None:
        """Ticket shape: observed source guarantees {a,url}; locked consumer admits [url]."""
        from elspeth.core.dag.models import EdgeContractError

        graph = _locked_consumer_graph(
            source_schema={"mode": "observed", "guaranteed_fields": ["a", "url"]},
            consumer_input_schema=_locked_input_model(),
        )

        with pytest.raises(EdgeContractError, match="a") as exc_info:
            graph.validate_edge_compatibility()

        assert exc_info.value.from_node_id == "src"
        assert exc_info.value.to_node_id == "t1"
        result = exc_info.value.compatibility_result
        assert result is not None
        # Only the un-admitted field is an extra; the admitted one must not be named.
        assert result.extra_fields == ("a",)

    def test_abstaining_observed_producer_is_accepted(self) -> None:
        """Abstention control: no guarantee, no certainty, no build rejection.

        Enforcement stays per-row at the executor preflight — same doctrine as
        the neighbouring guarantee-based validators.
        """
        graph = _locked_consumer_graph(
            source_schema={"mode": "observed"},
            consumer_input_schema=_locked_input_model(),
        )

        vote = walk_effective_guarantee_vote(graph, "src", {})
        assert vote.participated is False
        assert vote.fields == frozenset()

        graph.validate_edge_compatibility()

    def test_guaranteed_field_declared_optional_is_admitted(self) -> None:
        """An optional declared field is admitted input, not an extra.

        Mirrors TestExtrasFirewallDirection's boundary: rejecting here would
        trade an unreachable false accept for a live false reject.
        """
        graph = _locked_consumer_graph(
            source_schema={"mode": "observed", "guaranteed_fields": ["a", "url"]},
            consumer_input_schema=_locked_input_model(optional_a=True),
        )

        graph.validate_edge_compatibility()

    def test_flexible_consumer_admits_extras(self) -> None:
        """mode: flexible is extra='allow' — not locked, nothing to reject."""
        from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config

        flexible = create_schema_from_config(
            SchemaConfig(mode="flexible", fields=(FieldDefinition("url", "str"),)),
            "FlexibleUrlInput",
            allow_coercion=False,
        )
        graph = _locked_consumer_graph(
            source_schema={"mode": "observed", "guaranteed_fields": ["a", "url"]},
            consumer_input_schema=flexible,
        )

        graph.validate_edge_compatibility()

    def test_dynamic_consumer_schema_is_skipped(self) -> None:
        """No input model (gates, dynamic transforms) means no lock to enforce."""
        graph = _locked_consumer_graph(
            source_schema={"mode": "observed", "guaranteed_fields": ["a", "url"]},
            consumer_input_schema=None,
        )

        graph.validate_edge_compatibility()

    def test_multi_hop_guarantee_through_pass_through_is_rejected(self) -> None:
        """The check consults the effective-guarantee walk, not the raw predecessor.

        The extra field is guaranteed by the SOURCE and only reaches the locked
        consumer's edge through ADR-007 pass-through propagation.
        """
        from elspeth.core.dag.models import EdgeContractError

        graph = ExecutionGraph()
        graph.add_node(
            "src",
            node_type=NodeType.SOURCE,
            plugin_name="csv",
            config={"schema": {"mode": "observed", "guaranteed_fields": ["a", "url"]}},
        )
        graph.add_node(
            "passthru",
            node_type=NodeType.TRANSFORM,
            plugin_name="enrich",
            config={"schema": {"mode": "observed"}},
            passes_through_input=True,
        )
        graph.add_node(
            "t1",
            node_type=NodeType.TRANSFORM,
            plugin_name="web_scrape",
            config={"schema": {"mode": "fixed", "fields": ["url: str"]}},
            input_schema=_locked_input_model(),
        )
        graph.add_node(
            "sink",
            node_type=NodeType.SINK,
            plugin_name="json",
            config={"schema": {"mode": "observed"}},
        )
        graph.add_edge("src", "passthru", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("passthru", "t1", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("t1", "sink", label="out", mode=RoutingMode.MOVE)

        with pytest.raises(EdgeContractError, match="a") as exc_info:
            graph.validate_edge_compatibility()

        assert exc_info.value.to_node_id == "t1"

    def test_locked_sink_guaranteed_extras_rejected(self) -> None:
        """The sink half (composer Rule B): same certainty, same rejection."""
        from elspeth.core.dag.models import EdgeContractError

        graph = _locked_consumer_graph(
            source_schema={"mode": "observed", "guaranteed_fields": ["a", "url"]},
            consumer_input_schema=_locked_input_model(),
            consumer_node_type=NodeType.SINK,
        )

        with pytest.raises(EdgeContractError, match="a"):
            graph.validate_edge_compatibility()

    def test_multi_input_gate_into_a_nested_coalesce_still_builds(self) -> None:
        """No validation walk may resolve a mixed-schema gate for a barrier consumer.

        A ``nested`` merge has no cross-branch schema constraint at all (each
        branch is keyed separately in the output), so this topology is
        legitimate and must build cleanly. The hazard it guards is that
        ``get_effective_producer_schema`` RAISES on a gate with mixed
        observed/explicit branches, so any pass that resolves this producer
        rejects a buildable graph.

        WHAT THIS CAN AND CANNOT PIN — read before "verifying" it by mutation.
        An earlier version of this docstring claimed that deleting the
        COALESCE/ROW_UNION skip in ``validate_typed_producer_guaranteed_extras``
        makes the graph raise "Gate 'gate' has mixed observed/explicit schemas".
        That was true when written and is FALSE now: ``df50ea3c3`` moved the
        cheap consumer-schema guard AHEAD of the producer resolution, and a
        COALESCE node's ``input_schema`` is ``None``, so the loop now ``continue``s
        before it can reach the raiser. That one line is currently SUBSUMED.

        What this test does still catch, and why it earns its place: REORDERING
        the guards so the producer resolves eagerly (raises), and removing the
        matching host skip in ``validate_single_edge`` (also raises). It pins the
        OUTCOME — this graph builds — which is the property that actually
        matters and which survives whichever guard happens to deliver it.
        """
        from elspeth.contracts import PluginSchema

        class _FixedBranch(PluginSchema):
            a: str

        graph = ExecutionGraph()
        graph.add_node("src_obs", node_type=NodeType.SOURCE, plugin_name="csv", config={"schema": {"mode": "observed"}})
        graph.add_node("src_fixed", node_type=NodeType.SOURCE, plugin_name="csv", output_schema=_FixedBranch)
        graph.add_node("gate", node_type=NodeType.GATE, plugin_name="fork")
        graph.add_node(
            "coalesce",
            node_type=NodeType.COALESCE,
            plugin_name="coalesce:merge",
            config={"branches": {"path_a": "path_a", "path_b": "path_b"}, "policy": "require_all", "merge": "nested"},
        )
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="json")
        graph.add_edge("src_obs", "gate", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("src_fixed", "gate", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("gate", "coalesce", label="path_a", mode=RoutingMode.COPY)
        graph.add_edge("gate", "coalesce", label="path_b", mode=RoutingMode.COPY)
        graph.add_edge("coalesce", "sink", label="continue", mode=RoutingMode.MOVE)

        graph.validate_edge_compatibility()


def _locked_input_pipeline_settings(*, guaranteed_fields: list[str]) -> object:
    """The ticket's repro via the production path: observed source -> mode:fixed web_scrape."""
    from elspeth.core.config import (
        ElspethSettings,
        SinkSettings,
        SourceSettings,
        TransformSettings,
    )

    return ElspethSettings(
        sources={
            "primary": SourceSettings(
                plugin="csv",
                on_success="urls",
                options={
                    "path": "input.csv",
                    "on_validation_failure": "discard",
                    "schema": {"mode": "observed", "guaranteed_fields": guaranteed_fields},
                },
            )
        },
        transforms=[
            TransformSettings(
                name="scraper",
                plugin="web_scrape",
                input="urls",
                on_success="output",
                on_error="discard",
                options={
                    "url_field": "url",
                    "content_field": "page_content",
                    "fingerprint_field": "page_fingerprint",
                    "http": {
                        "abuse_contact": "test@example.com",
                        "scraping_reason": "contract validation test",
                        "allowed_hosts": ["127.0.0.0/8"],
                    },
                    "schema": {"mode": "fixed", "fields": ["url: str"]},
                },
            )
        ],
        sinks={
            "output": SinkSettings(
                plugin="json",
                on_write_failure="discard",
                options={"path": "out.jsonl", "format": "jsonl", "schema": {"mode": "observed"}},
            )
        },
    )


class TestLockedInputProductionBuildPath:
    """The builder must wire input_schema so the Rule A mirror reaches `elspeth run`."""

    def test_guaranteed_extra_is_rejected_at_build(self, plugin_manager: object) -> None:
        """At HEAD this built green and then killed every row at the preflight."""
        from elspeth.cli_helpers import instantiate_plugins_from_config

        plugins = instantiate_plugins_from_config(_locked_input_pipeline_settings(guaranteed_fields=["a", "url"]))

        with pytest.raises(GraphValidationError, match="a"):
            ExecutionGraph.from_plugin_instances(
                sources=plugins.sources,
                source_settings_map=plugins.source_settings_map,
                transforms=plugins.transforms,
                sinks=plugins.sinks,
                aggregations=plugins.aggregations,
            )

    def test_admitted_guarantee_builds(self, plugin_manager: object) -> None:
        """Baseline: guaranteeing only admitted fields must keep building."""
        from elspeth.cli_helpers import instantiate_plugins_from_config

        plugins = instantiate_plugins_from_config(_locked_input_pipeline_settings(guaranteed_fields=["url"]))

        ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
        )


# ---------------------------------------------------------------------------
# Union coalesce: guaranteed fields the merged schema does not declare
# (elspeth-1451ff385f)
# ---------------------------------------------------------------------------

_COALESCE_PIPELINE = """sources:
  primary:
    plugin: csv
    on_success: raw
    options:
      path: examples/fork_coalesce/input.csv
      schema:
        mode: fixed
        fields:
        - 'id: {source_id_type}'
        - 'product: str'
        - 'price: int'
        - 'category: str'
        - 'description: str'
      on_validation_failure: discard

gates:
- name: fork_gate
  input: raw
  condition: "True"
  routes:
    'true': fork
    'false': discard
  fork_to:
    - path_a
    - path_b

transforms:
- name: truncate_branch_a
  plugin: truncate
  input: path_a
  on_success: trunc_a
  on_error: discard
  options:
    fields:
      description: 20
    suffix: "..."
{branch_schema}
- name: truncate_branch_b
  plugin: truncate
  input: path_b
  on_success: trunc_b
  on_error: discard
  options:
    fields:
      description: 50
    suffix: "..."
{branch_schema}
{extra_transforms}
coalesce:
- name: merge_results
  branches:
    path_a: trunc_a
    path_b: trunc_b
  policy: {policy}
  merge: union
  union_collision_policy: last_wins
{coalesce_out}
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: out.jsonl
      format: jsonl
      schema:
        mode: {sink_mode}
{sink_fields}"""

# A pass-through branch that declares only the field it rewrites. The fields it
# carries untouched live in guaranteed_fields alone — the channel the merged
# schema does not declare.
_BRANCH_UNDERDECLARED = """    schema:
      mode: flexible
      fields:
      - 'description: str'"""

# The same branch declaring everything it actually passes through.
_BRANCH_FULLY_DECLARED = """    schema:
      mode: flexible
      fields:
      - 'id: int'
      - 'product: str'
      - 'price: int'
      - 'category: str'
      - 'description: str'"""

_SINK_ADMITS_DESCRIPTION = """        fields:
        - 'description: str'
"""

_SINK_ADMITS_ALL = """        fields:
        - 'id: int'
        - 'product: str'
        - 'price: int'
        - 'category: str'
        - 'description: str'
"""

# The same admitting sink with ONE type flipped against the source's id: int.
_SINK_ADMITS_ALL_WRONG_TYPE = """        fields:
        - 'id: str'
        - 'product: str'
        - 'price: int'
        - 'category: str'
        - 'description: str'
"""

# A locked TRANSFORM consumer: mode: fixed makes its input extra='forbid', so
# it rejects the phantom-guaranteed fields exactly as a locked sink does.
_LOCKED_TRANSFORM = """- name: lock_it
  plugin: truncate
  input: merge_results
  on_success: output
  on_error: discard
  options:
    fields:
      description: 10
    suffix: "..."
    schema:
      mode: fixed
      fields:
      - 'description: str'
"""

# A two-transform chain after the coalesce: type_coerce genuinely re-types id
# (input declares 'id: int' — what arrives — and the conversion derives an
# output config declaring 'id: str'), then a truncate under-declares it back
# into the guarantee channel. The final producer's model never carries id, so
# the ancestor-type walk must recurse — and must stop at the NEAREST
# declaration (retype_id's derived 'id: str'), not the farther source
# ('id: int'), because the nearer node rewrote the value the farther
# declaration describes.
_RETYPE_THEN_PASS = """- name: retype_id
  plugin: type_coerce
  input: merge_results
  on_success: retyped
  on_error: discard
  options:
    conversions:
      - field: id
        to: str
    schema:
      mode: flexible
      fields:
      - 'id: int'
      - 'description: str'
- name: pass_after_retype
  plugin: truncate
  input: retyped
  on_success: output
  on_error: discard
  options:
    fields:
      description: 5
    suffix: "..."
    schema:
      mode: flexible
      fields:
      - 'description: str'
"""

# field_mapper select_only drops the extras — remedy 3 of the rejection message.
_SELECT_ONLY_MAPPER = """- name: drop_extras
  plugin: field_mapper
  input: merge_results
  on_success: output
  on_error: discard
  options:
    select_only: true
    mapping:
      description: description
    schema:
      mode: flexible
      fields:
      - 'description: str'
"""


def _build_coalesce_graph(
    *,
    branch_schema: str = _BRANCH_UNDERDECLARED,
    sink_fields: str = _SINK_ADMITS_DESCRIPTION,
    sink_mode: str = "fixed",
    policy: str = "require_all",
    extra_transforms: str = "",
    coalesce_out: str = "\n  on_success: output",
    source_id_type: str = "int",
) -> ExecutionGraph:
    """Build fork -> two pass-through branches -> union coalesce -> locked sink."""
    from elspeth.cli_helpers import instantiate_plugins_from_config
    from elspeth.core.config import load_settings_from_yaml_string

    settings = load_settings_from_yaml_string(
        _COALESCE_PIPELINE.format(
            branch_schema=branch_schema,
            sink_mode=sink_mode,
            policy=policy,
            extra_transforms=extra_transforms,
            sink_fields=sink_fields,
            coalesce_out=coalesce_out,
            source_id_type=source_id_type,
        )
    )
    plugins = instantiate_plugins_from_config(settings)
    return ExecutionGraph.from_plugin_instances(
        sources=plugins.sources,
        source_settings_map=plugins.source_settings_map,
        transforms=plugins.transforms,
        sinks=plugins.sinks,
        aggregations=plugins.aggregations,
        gates=settings.gates,
        coalesce_settings=settings.coalesce,
    )


class TestUnionCoalesceGuaranteedExtras:
    """A union coalesce guarantees fields its own merged schema never declares.

    The builder computes a coalesce's typed ``fields`` from each branch's
    CONSTRUCTION-time schema but its ``guaranteed_fields`` from a separately
    graph-walked effective guarantee (elspeth-0b14977817), so a pass-through
    branch that declares only the field it rewrites yields a merged schema
    whose guarantees exceed its declared fields. Those fields never enter the
    pydantic model, so ``check_compatibility``'s extras arm — which compares
    ``model_fields`` — cannot see them, and a locked consumer that rejects
    every one of them was accepted at build (elspeth-1451ff385f).

    The two channels are deliberately decoupled: ``fields`` is what the node
    typed, ``guaranteed_fields`` is what the graph proves will be present. The
    fix is therefore to check the guarantee channel against a locked consumer
    on the typed path too, not to force the channels to agree.
    """

    def test_phantom_guarantee_against_locked_sink_is_rejected(self) -> None:
        """At HEAD this built green and killed every row at the sink preflight."""
        from elspeth.core.dag.models import EdgeContractError

        with pytest.raises(EdgeContractError) as exc_info:
            _build_coalesce_graph()

        result = exc_info.value.compatibility_result
        assert result is not None
        assert result.extra_fields == ("category", "id", "price", "product")

    def test_declared_branches_reach_the_same_verdict(self) -> None:
        """Control: the SAME pipeline with branches declaring what they carry.

        Semantically identical — rows carry five fields, the sink admits one —
        and already rejected today by check_compatibility's extras arm. Pins
        that under-declaring a branch cannot change the verdict, only which
        arm reports it.
        """
        from elspeth.core.dag.models import EdgeContractError

        with pytest.raises(EdgeContractError, match="Extra fields forbidden"):
            _build_coalesce_graph(branch_schema=_BRANCH_FULLY_DECLARED)

    def test_fully_declared_branches_against_admitting_sink_still_build(self) -> None:
        """A locked sink admitting every field must stay green.

        Names what this actually covers: with fully-declared branches the
        guarantees are a SUBSET of the declared fields, so the merged schema
        has no phantoms and ``check_compatibility`` alone decides this edge —
        the new pass is not the discriminating arm. Keep it as the guard on
        the declared channel; the guard on the NEW check is the flexible-
        consumer test below, which is the case where guarantees exceed the
        declared fields and the check must still decline to fire.
        """
        _build_coalesce_graph(branch_schema=_BRANCH_FULLY_DECLARED, sink_fields=_SINK_ADMITS_ALL)

    def test_flexible_consumer_admits_the_guaranteed_extras(self) -> None:
        """False-reject guard for the NEW check: phantoms + a consumer that admits extras.

        Same under-declared branches as the defect test, so the coalesce
        guarantees four fields it never declares. Since df50ea3c3 the new
        pass DECLINES this edge at its consumer-extras-policy guard
        (schema_validation.py, ``extra != "forbid"`` -> continue) before ever
        resolving the producer; the helper's own decline is the second,
        now-unreached arm. A `mode: flexible` consumer is `extra='allow'`,
        so no row can die at its preflight and the pass must stay silent.
        This test goes red only if BOTH arms stop consulting the consumer's
        extras policy — it is a guard on the outcome, not on which arm
        delivers it (verified by instrumentation: the helper is never
        invoked on this edge).
        """
        _build_coalesce_graph(sink_mode="flexible")

    def test_sink_admitting_every_guaranteed_field_builds(self) -> None:
        """The missing arm now reads the guarantee channel (elspeth-7d68b04878).

        Formerly a strict xfail pinning the inverse defect: the coalesce
        declares ['description'], so a sink admitting every GUARANTEED field
        was rejected with 'Missing fields' even though the rows carry them and
        the pipeline runs — which made the extras rejection's first remedy
        ("add the extra fields to the consumer") a dead end. The forgiveness
        is firewall-gated: a producer that FORBIDS extras keeps its missing
        verdict (TestReductiveProducerExtrasFirewall's missing-arm sibling),
        and a required field the guarantee does not cover keeps failing
        (TestDualViolationSinkDiagnosisPrecedence's model-required pin).
        """
        _build_coalesce_graph(sink_fields=_SINK_ADMITS_ALL)

    def test_forgiven_field_with_conflicting_ancestor_type_is_rejected(self) -> None:
        """A forgiven field's ancestor type IS consulted (elspeth-85e8afa2f5).

        Formerly a strict xfail pinning the type-axis residue of
        elspeth-7d68b04878: the source types id as int and this sink demands
        id: str; with under-declared branches the missing arm forgives id on
        presence alone, the build was GREEN, and every row died typed at the
        sink preflight. ``validate_forgiven_field_ancestor_types`` now walks
        the guarantee topology for the nearest ancestor declaration and
        restores the declared-control verdict — the same mismatch the
        declared A/B control below reports, attributed to the declaring
        SOURCE (not the gate that inherited its config by copy).
        """
        from elspeth.core.dag.models import EdgeContractError

        with pytest.raises(EdgeContractError) as exc_info:
            _build_coalesce_graph(sink_fields=_SINK_ADMITS_ALL_WRONG_TYPE)

        result = exc_info.value.compatibility_result
        assert result is not None
        assert result.type_mismatches == (("id", "str", "int"),)
        assert exc_info.value.from_node_id is not None and exc_info.value.from_node_id.startswith("coalesce_merge_results")
        assert exc_info.value.to_node_id is not None and exc_info.value.to_node_id.startswith("sink_output")
        assert "declared by: source_primary" in str(exc_info.value)

    def test_declared_branches_expose_the_conflicting_type(self) -> None:
        """The A/B control for the xfail above: declaration restores the verdict.

        Fully-declared branches put id: int on the merged schema, so the sink
        demanding id: str is rejected by the type-mismatch arm — which the
        guarantee channel never overrides (a declared field is never
        forgiven). Guards the declared path against any drift while
        elspeth-85e8afa2f5 stays open.
        """
        from elspeth.core.dag.models import EdgeContractError

        with pytest.raises(EdgeContractError, match="Type mismatches"):
            _build_coalesce_graph(branch_schema=_BRANCH_FULLY_DECLARED, sink_fields=_SINK_ADMITS_ALL_WRONG_TYPE)

    def test_ancestor_declaring_any_abstains_and_builds(self) -> None:
        """An ``any`` ancestor declaration states no type — the walk abstains.

        False-reject guard on the new pass: the same wrong-type sink as the
        rejection test, but the source declares ``id: any``. There is no
        declared type to enforce, so the forgiven field keeps the per-row
        posture and the build stays green. Guards against the ``Any``
        asymmetry in ``_types_compatible`` (universal on the expected side
        only): letting ``any`` through as the actual type would reject a
        pipeline for a type nobody stated.
        """
        _build_coalesce_graph(sink_fields=_SINK_ADMITS_ALL_WRONG_TYPE, source_id_type="any")

    def test_nearest_declaration_wins_through_a_retyping_chain(self) -> None:
        """A nearer re-typing declaration governs over the farther source.

        ``retype_id`` declares ``id: str`` mid-chain and ``pass_after_retype``
        under-declares it back into the guarantee channel, so the sink's
        producer never types id. The sink demands ``id: str`` — compatible
        with the NEAREST declaration and conflicting with the source's
        ``id: int``. Green proves the walk stops at the nearest declaration;
        taking the farther source type would reject this runnable pipeline.
        """
        _build_coalesce_graph(
            sink_fields=_SINK_ADMITS_ALL_WRONG_TYPE,
            extra_transforms=_RETYPE_THEN_PASS,
            coalesce_out="",
        )

    def test_nearest_declaration_rejects_against_the_farther_matching_type(self) -> None:
        """The rejecting direction of the nearest-wins pair above.

        Same chain, but the sink demands ``id: int`` — matching the FARTHER
        source declaration and conflicting with the nearest (``retype_id``'s
        ``id: str``). Red with the mismatch attributed to ``retype_id`` proves
        the walk enforces the declaration closest to the consumer in both
        directions, not whichever ancestor happens to agree.
        """
        from elspeth.core.dag.models import EdgeContractError

        with pytest.raises(EdgeContractError) as exc_info:
            _build_coalesce_graph(
                sink_fields=_SINK_ADMITS_ALL,
                extra_transforms=_RETYPE_THEN_PASS,
                coalesce_out="",
            )

        result = exc_info.value.compatibility_result
        assert result is not None
        assert result.type_mismatches == (("id", "int", "str"),)
        assert "declared by: transform_retype_id" in str(exc_info.value)

    def test_select_only_field_mapper_clears_the_rejection(self) -> None:
        """The rejection message's remedy 3 must actually clear the rejection.

        Asserts BOTH halves in one test so the claim is falsifiable: the same
        topology WITHOUT the field_mapper is rejected, and inserting one clears
        it. Checking only the second half would pass even if the rejection had
        never been live — the coalesce -> field_mapper edge is not itself
        eligible for this check (field_mapper's own input contract is
        extra='allow'), so it is the mapper doing the work, not the check
        declining to fire.

        The guarantee assertion is the mechanism: select_only narrows the walk
        to 'description', so an error whose own remedy did not clear it would
        mean the walk over-attributes through field-dropping nodes.
        """
        from elspeth.core.dag.models import EdgeContractError

        with pytest.raises(EdgeContractError):
            _build_coalesce_graph()

        graph = _build_coalesce_graph(extra_transforms=_SELECT_ONLY_MAPPER, coalesce_out="")

        mapper = next(n for n in graph.get_nodes() if n.node_id.startswith("transform_drop_extras"))
        assert schema_validation.get_effective_guaranteed_fields(graph, mapper.node_id) == frozenset({"description"})

    def test_phantom_guarantee_is_rejected_under_intersection_policy(self) -> None:
        """`best_effort` merges guarantees by INTERSECTION, a distinct code path.

        `merge_guaranteed_fields` unions under require_all and intersects
        otherwise. Every other fixture in this class is require_all, so without
        this the intersection arm is unexercised — and it reaches the same
        phantom today only because the branches are structurally identical. A
        change narrowing that arm would silently reopen the defect on
        non-require_all pipelines with nothing going red.
        """
        from elspeth.core.dag.models import EdgeContractError

        with pytest.raises(EdgeContractError) as exc_info:
            _build_coalesce_graph(policy="best_effort\n  timeout_seconds: 5")

        result = exc_info.value.compatibility_result
        assert result is not None
        assert result.extra_fields == ("category", "id", "price", "product")

    def test_phantom_guarantee_is_rejected_at_a_locked_transform_consumer(self) -> None:
        """The check governs every edge, not only sink edges.

        `validate_typed_producer_guaranteed_extras` excludes only COALESCE and
        ROW_UNION consumers, so a locked TRANSFORM is equally in scope and its
        rows die at the same input preflight. Pinned because every other case
        in this class happens to terminate at a sink.
        """
        from elspeth.core.dag.models import EdgeContractError

        with pytest.raises(EdgeContractError) as exc_info:
            _build_coalesce_graph(sink_mode="observed", sink_fields="", extra_transforms=_LOCKED_TRANSFORM, coalesce_out="")

        assert exc_info.value.to_node_id.startswith("transform_lock_it")
        result = exc_info.value.compatibility_result
        assert result is not None
        assert result.extra_fields == ("category", "id", "price", "product")


# Branches that declare nothing: the coalesce's effective schema resolves to
# DYNAMIC (None), so the sink edge takes validate_single_edge's bypass arm —
# the path where _validate_locked_consumer_guaranteed_extras itself must
# assemble the verdict.
_BRANCH_OBSERVED = """    schema:
      mode: observed"""


class TestResolveGuaranteedFieldType:
    """Unit tests for the ancestor-type walk arms the builder fixtures above
    cannot reach: every ``_build_coalesce_graph`` branch shares ONE source, so
    cross-branch disagreement, multi-node attribution, partial abstention,
    fan-in recursion, and DIVERT abstention need hand-built graphs. These are
    the walk's false-reject guards: each abstention below is a pipeline the
    new pass must keep GREEN.
    """

    @staticmethod
    def _config(fields: list[str], mode: str = "fixed") -> SchemaConfig:
        return SchemaConfig.from_dict({"mode": mode, "fields": fields})

    def _two_branch_coalesce(self, field_a: str, field_b: str) -> ExecutionGraph:
        from elspeth.core.dag.graph import ExecutionGraph

        graph = ExecutionGraph()
        graph.add_node("src_a", node_type=NodeType.SOURCE, plugin_name="csv", output_schema_config=self._config([field_a]))
        graph.add_node("src_b", node_type=NodeType.SOURCE, plugin_name="csv", output_schema_config=self._config([field_b]))
        graph.add_node("coal", node_type=NodeType.COALESCE, plugin_name="coalesce")
        graph.add_edge("src_a", "coal", label="a")
        graph.add_edge("src_b", "coal", label="b")
        return graph

    def test_cross_branch_disagreement_abstains(self) -> None:
        """int vs str across branches: the runtime type depends on which
        branch wins the collision, so no unanimous type exists and the walk
        must abstain rather than pick a side."""
        from elspeth.core.dag.guarantees import resolve_guaranteed_field_type

        graph = self._two_branch_coalesce("id: int", "id: str")
        assert resolve_guaranteed_field_type(graph, "coal", "id") is None

    def test_cross_branch_agreement_resolves_and_attributes_both(self) -> None:
        """Unanimous branches resolve, and declared_by carries every declaring
        node so the rejection can cite the declarations it enforces."""
        from elspeth.core.dag.guarantees import resolve_guaranteed_field_type

        graph = self._two_branch_coalesce("id: int", "id: int")
        resolved = resolve_guaranteed_field_type(graph, "coal", "id")
        assert resolved is not None
        assert resolved.field_type == "int"
        assert resolved.declared_by == frozenset({"src_a", "src_b"})

    def test_nullability_disagreement_still_resolves_the_base_type(self) -> None:
        """'id: int' vs 'id: int?' agree on the BASE type, which is all the
        walk carries: the two declared-arm materializations disagree about
        nullability (the plugin factory ignores ``nullable``, the coalesce
        factory folds it into ``| None``), so consulting it would make the
        walk stricter than one declared control or the other and break the
        declare-more monotonicity invariant (panel Blocker 2). None deaths
        stay with the per-row preflight."""
        from elspeth.core.dag.guarantees import resolve_guaranteed_field_type

        graph = self._two_branch_coalesce("id: int", "id: int?")
        resolved = resolve_guaranteed_field_type(graph, "coal", "id")
        assert resolved is not None
        assert resolved.field_type == "int"

    def test_partial_abstention_collapses_the_vote(self) -> None:
        """One branch resolves, its sibling cannot (an observed source
        declares nothing): the value may come from the silent branch, so the
        resolving branch must not carry the vote alone."""
        from elspeth.core.dag.graph import ExecutionGraph
        from elspeth.core.dag.guarantees import resolve_guaranteed_field_type

        graph = ExecutionGraph()
        graph.add_node("src_a", node_type=NodeType.SOURCE, plugin_name="csv", output_schema_config=self._config(["id: int"]))
        graph.add_node(
            "src_b",
            node_type=NodeType.SOURCE,
            plugin_name="json",
            output_schema_config=SchemaConfig.from_dict({"mode": "observed"}),
        )
        graph.add_node("coal", node_type=NodeType.COALESCE, plugin_name="coalesce")
        graph.add_edge("src_a", "coal", label="a")
        graph.add_edge("src_b", "coal", label="b")
        assert resolve_guaranteed_field_type(graph, "coal", "id") is None

    def test_declaring_but_empty_pass_through_abstains(self) -> None:
        """A pass-through transform whose output config declares an EMPTY
        fields tuple has declared nothing — same abstention as observed mode.
        Pinned because ``blob_csv_expand`` with ``columns: null`` and
        ``include_row_index: false`` builds exactly this shape while merging
        data-derived CSV headers onto the row (panel sweep): ``fields is
        None`` alone would recurse through it to a stale ancestor type."""
        from elspeth.contracts.schema import SchemaConfig
        from elspeth.core.dag.graph import ExecutionGraph
        from elspeth.core.dag.guarantees import resolve_guaranteed_field_type

        graph = ExecutionGraph()
        graph.add_node("src", node_type=NodeType.SOURCE, plugin_name="csv", output_schema_config=self._config(["id: int"]))
        graph.add_node(
            "expander",
            node_type=NodeType.TRANSFORM,
            plugin_name="blob_csv_expand",
            output_schema_config=SchemaConfig(mode="flexible", fields=(), guaranteed_fields=None),
            passes_through_input=True,
        )
        graph.add_edge("src", "expander", label="in")
        assert resolve_guaranteed_field_type(graph, "expander", "id") is None

    def test_queue_and_row_union_fan_in_resolve_on_agreement(self) -> None:
        """The QUEUE and ROW_UNION recursion arms resolve when every arm
        agrees — pinned per kind because the fixture family above never
        exercises either barrier."""
        from elspeth.core.dag.graph import ExecutionGraph
        from elspeth.core.dag.guarantees import resolve_guaranteed_field_type

        for barrier_type, barrier_plugin in ((NodeType.QUEUE, "queue"), (NodeType.ROW_UNION, "row_union")):
            graph = ExecutionGraph()
            graph.add_node("src_a", node_type=NodeType.SOURCE, plugin_name="csv", output_schema_config=self._config(["id: int"]))
            graph.add_node("src_b", node_type=NodeType.SOURCE, plugin_name="csv", output_schema_config=self._config(["id: int"]))
            graph.add_node("barrier", node_type=barrier_type, plugin_name=barrier_plugin)
            graph.add_edge("src_a", "barrier", label="a")
            graph.add_edge("src_b", "barrier", label="b")
            resolved = resolve_guaranteed_field_type(graph, "barrier", "id")
            assert resolved is not None, barrier_plugin
            assert resolved.field_type == "int", barrier_plugin

    def test_any_declaration_abstains(self) -> None:
        """'id: any' states no type — the walk abstains at the declaration."""
        from elspeth.core.dag.guarantees import resolve_guaranteed_field_type

        graph = self._two_branch_coalesce("id: any", "id: any")
        assert resolve_guaranteed_field_type(graph, "coal", "id") is None

    def test_divert_in_edge_abstains_even_when_move_arms_agree(self) -> None:
        """A DIVERT in-edge carries error envelopes, not declared rows: the
        stream has no provable field types, so the walk abstains outright —
        the row_union guarantee-walk posture, defensive on the public
        add_edge surface (builder error routing is terminal)."""
        from elspeth.core.dag.graph import ExecutionGraph
        from elspeth.core.dag.guarantees import resolve_guaranteed_field_type

        graph = ExecutionGraph()
        graph.add_node("src_a", node_type=NodeType.SOURCE, plugin_name="csv", output_schema_config=self._config(["id: int"]))
        graph.add_node("err", node_type=NodeType.TRANSFORM, plugin_name="truncate", output_schema_config=self._config(["id: int"]))
        graph.add_node("ru", node_type=NodeType.ROW_UNION, plugin_name="row_union")
        graph.add_edge("src_a", "ru", label="a")
        graph.add_edge("err", "ru", label="divert", mode=RoutingMode.DIVERT)
        assert resolve_guaranteed_field_type(graph, "ru", "id") is None


_INVISIBLE_RETYPE_PIPELINE = """sources:
  primary:
    plugin: csv
    on_success: raw
    options:
      path: examples/fork_coalesce/input.csv
      schema:
        mode: fixed
        fields:
        - 'id: int'
        - 'description: str'
      on_validation_failure: discard
transforms:
- name: coerce_id
  plugin: type_coerce
  input: raw
  on_success: coerced
  on_error: discard
  options:
    conversions:
      - field: id
        to: str
    schema:
{coerce_schema}
- name: passthru
  plugin: truncate
  input: coerced
  on_success: output
  on_error: discard
  options:
    fields:
      description: 20
    suffix: "..."
    schema:
      mode: flexible
      fields:
      - 'description: str'
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: out.jsonl
      format: jsonl
      schema:
        mode: fixed
        fields:
        - 'id: {sink_id_type}'
        - 'description: str'
"""

_COERCE_OBSERVED = "      mode: observed"
_COERCE_FLEX_WITHOUT_TARGET = """      mode: flexible
      fields:
      - 'description: str'"""


def _build_invisible_retype_graph(*, coerce_schema: str, sink_id_type: str) -> ExecutionGraph:
    """source(id: int) -> type_coerce(id -> str) -> under-declared truncate -> locked sink."""
    from elspeth.cli_helpers import instantiate_plugins_from_config
    from elspeth.core.config import load_settings_from_yaml_string

    settings = load_settings_from_yaml_string(_INVISIBLE_RETYPE_PIPELINE.format(coerce_schema=coerce_schema, sink_id_type=sink_id_type))
    plugins = instantiate_plugins_from_config(settings)
    return ExecutionGraph.from_plugin_instances(
        sources=plugins.sources,
        source_settings_map=plugins.source_settings_map,
        transforms=plugins.transforms,
        sinks=plugins.sinks,
        aggregations=plugins.aggregations,
        gates=settings.gates,
        coalesce_settings=settings.coalesce,
    )


class TestInvisibleRetypeThroughPassThrough:
    """A pass-through that rewrites a field's type must not leave the walk
    trusting a stale ancestor declaration (elspeth-85e8afa2f5 panel review).

    type_coerce converts id int->str mid-chain while the final producer
    under-declares id back into the guarantee channel. Two closure halves,
    each pinned in both directions: with declared fields, type_coerce now
    DECLARES its conversion targets (nearest declaration correct); in
    observed mode the walk ABSTAINS at an undeclared pass-through rather
    than resolving through it (silence is not type-preservation).
    """

    def test_observed_coercer_with_matching_sink_builds(self) -> None:
        """The false-reject regression pin: rows genuinely carry str after the
        coercion, so the sink demanding str must build even though the only
        ancestor DECLARATION says int. The walk abstains at the observed
        type_coerce instead of resolving the stale source type."""
        _build_invisible_retype_graph(coerce_schema=_COERCE_OBSERVED, sink_id_type="str")

    def test_observed_coercer_with_conflicting_sink_keeps_per_row_posture(self) -> None:
        """Rows carry str and the sink demands int — doomed at runtime, but an
        observed coercer states nothing the build can prove either way, so the
        historical per-row preflight posture stands (green build). Pinned so a
        later 'improvement' that resolves through observed pass-throughs
        cannot silently reintroduce the stale-type claim."""
        _build_invisible_retype_graph(coerce_schema=_COERCE_OBSERVED, sink_id_type="int")

    def test_declaring_coercer_with_matching_sink_builds(self) -> None:
        """With declared fields, the appended conversion-target declaration
        (id: str) is the nearest declaration and matches the sink."""
        _build_invisible_retype_graph(coerce_schema=_COERCE_FLEX_WITHOUT_TARGET, sink_id_type="str")

    def test_declaring_coercer_with_conflicting_sink_is_rejected(self) -> None:
        """The improvement direction: the same doomed pipeline the observed
        arm must tolerate is CAUGHT when the coercer declares fields, because
        the appended target declaration is graph-visible."""
        from elspeth.core.dag.models import EdgeContractError

        with pytest.raises(EdgeContractError) as exc_info:
            _build_invisible_retype_graph(coerce_schema=_COERCE_FLEX_WITHOUT_TARGET, sink_id_type="int")

        result = exc_info.value.compatibility_result
        assert result is not None
        assert result.type_mismatches == (("id", "int", "str"),)
        assert exc_info.value.to_node_id is not None and exc_info.value.to_node_id.startswith("sink_output")
        assert "declared by: transform_coerce_id" in str(exc_info.value)

    def test_observed_direct_producer_keeps_the_bypass_posture(self) -> None:
        """An observed node DIRECTLY feeding the consumer stays on the bypass
        path even when a farther ancestor typed the field against the
        consumer. Two independent guards deliver this verdict — the pass
        skips observed producers, and the walk abstains at undeclared
        pass-throughs — so this build-level pin guards the OUTCOME, the
        posture the dynamic/observed paths have always shipped (per-row
        preflight, not build rejection)."""
        from elspeth.cli_helpers import instantiate_plugins_from_config
        from elspeth.core.config import load_settings_from_yaml_string

        settings = load_settings_from_yaml_string(
            """sources:
  primary:
    plugin: csv
    on_success: raw
    options:
      path: examples/fork_coalesce/input.csv
      schema:
        mode: fixed
        fields:
        - 'id: int'
        - 'description: str'
      on_validation_failure: discard
transforms:
- name: opaque
  plugin: truncate
  input: raw
  on_success: output
  on_error: discard
  options:
    fields:
      description: 20
    suffix: "..."
    schema:
      mode: observed
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: out.jsonl
      format: jsonl
      schema:
        mode: fixed
        fields:
        - 'id: str'
        - 'description: str'
"""
        )
        plugins = instantiate_plugins_from_config(settings)
        ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=settings.gates,
            coalesce_settings=settings.coalesce,
        )


# A locked sink that ALSO requires a field nothing upstream produces.
_SINK_REQUIRES_UNPRODUCED = """        fields:
        - 'description: str'
        - 'mandatory_flag: str'
"""

# The elspeth-3283f2eaec split: optional in the pydantic model (str?), required
# at option level (required_fields). check_compatibility's missing arm reads
# only model-required fields, so the requirement is invisible to it and
# validate_sink_required_fields owns the verdict.
_SINK_REQUIRES_UNPRODUCED_OPTION_LEVEL = """        fields:
        - 'description: str'
        - 'mandatory_flag: str?'
        required_fields:
        - mandatory_flag
"""


class TestDualViolationSinkDiagnosisPrecedence:
    """One edge violating BOTH rules must lead with the sink-required diagnosis.

    A coalesce that guarantees phantom extras against a locked sink can, on the
    same edge, fail to guarantee a field the sink requires. Reporting extras
    alone proposes "insert a field_mapper with select_only: true" — a repair
    that leaves the sink still requiring a field nothing provides, a FALSE
    repair signal to an LLM authoring loop (elspeth-6465c8bba7). The two paths
    resolve it differently and each needs its own pin:

    * BYPASS path (observed/dynamic producer): the helper fires from inside the
      per-edge loop, BEFORE validate_sink_required_fields can run, so it
      ACCUMULATES — it consults the sink rule's single owner
      (_sink_required_missing_fields) and assembles one verdict carrying both
      halves, sink half first.
    * TYPED path: the helper fires from validate_typed_producer_guaranteed_extras,
      deliberately ordered LAST, so the sink sweep raises first and ordering
      alone settles precedence.

    Until these tests, the accumulate arm had zero coverage: no test reached a
    sink whose declared_required_fields exceeded its upstream guarantees.
    """

    def test_bypass_path_reports_both_violations_in_one_verdict(self) -> None:
        """The accumulate arm: sink half leads, extras half follows, one raise.

        The eligibility precondition is asserted on a green sibling (flexible
        sink, so the locked-extras guard declines): observed branches leave the
        coalesce's effective producer schema DYNAMIC (None), which is what
        routes the failing sibling through validate_single_edge's first bypass
        arm rather than check_compatibility, and the guarantee channel really
        carries the five source fields. Without that proof this test could
        silently start exercising the typed path instead (the decoration trap).
        """
        from elspeth.core.dag.models import EdgeContractError

        green = _build_coalesce_graph(branch_schema=_BRANCH_OBSERVED, sink_mode="flexible")
        coalesce_id = next(n.node_id for n in green.get_nodes() if n.node_type.value == "coalesce")
        effective = schema_validation.get_effective_producer_schema(green, coalesce_id, _cache={})
        assert effective is None
        assert schema_validation.get_effective_guaranteed_fields(green, coalesce_id) == frozenset(
            {"id", "product", "price", "category", "description"}
        )

        with pytest.raises(EdgeContractError) as exc_info:
            _build_coalesce_graph(branch_schema=_BRANCH_OBSERVED, sink_fields=_SINK_REQUIRES_UNPRODUCED)

        message = str(exc_info.value)
        sink_half = message.find("requires fields ['mandatory_flag']")
        extras_half = message.find("Extra fields rejected by consumer input contract")
        assert sink_half != -1 and extras_half != -1
        assert sink_half < extras_half, "the actionable sink diagnosis must lead"
        assert "BOTH must be repaired" in message

        result = exc_info.value.compatibility_result
        assert result is not None
        assert result.missing_fields == ("mandatory_flag",)
        assert result.extra_fields == ("category", "id", "price", "product")

    def test_bypass_path_extras_only_carries_no_sink_verdict(self) -> None:
        """The accumulate arm must not invent a sink violation that is not there.

        Same bypass topology, but the sink's only required field (description —
        compact 'name: type' declarations are required by default) IS
        guaranteed, so _sink_required_missing_fields is empty and the verdict
        must be the plain extras report with nothing accumulated.
        """
        from elspeth.core.dag.models import EdgeContractError

        with pytest.raises(EdgeContractError) as exc_info:
            _build_coalesce_graph(branch_schema=_BRANCH_OBSERVED)

        message = str(exc_info.value)
        assert "BOTH must be repaired" not in message
        assert "does not guarantee them" not in message

        result = exc_info.value.compatibility_result
        assert result is not None
        assert result.missing_fields == ()
        assert result.extra_fields == ("category", "id", "price", "product")

    def test_locked_sink_without_required_fields_reports_only_extras(self) -> None:
        """An empty locked sink contract has no missing-field verdict to merge."""
        from elspeth.core.dag.models import EdgeContractError

        with pytest.raises(EdgeContractError) as exc_info:
            _build_coalesce_graph(
                branch_schema=_BRANCH_OBSERVED,
                sink_fields="""        fields:
        - 'description: str?'""",
            )

        result = exc_info.value.compatibility_result
        assert result is not None
        assert result.missing_fields == ()
        assert result.extra_fields == ("category", "id", "price", "product")

    def test_typed_path_sink_sweep_outranks_the_final_pass_extras(self) -> None:
        """Ordering discipline on the typed path: the sink sweep raises first.

        Under-declared FLEXIBLE branches give the coalesce a typed merged
        schema, so this edge is validate_typed_producer_guaranteed_extras
        territory — and that pass runs after validate_sink_required_fields
        precisely so a graph tripping both keeps the pre-existing sink error.
        The requirement is option-level (model-optional), so
        check_compatibility's missing arm cannot pre-empt the sweep either.

        The exception TYPE is the eligibility discriminator: had this shape
        taken the bypass path, the helper would raise EdgeContractError with
        the combined message; had check_compatibility owned it, the message
        would be its 'Missing fields' form. A plain GraphValidationError with
        the sink wording proves the sweep fired.
        """
        from elspeth.core.dag.models import EdgeContractError, GraphValidationError

        with pytest.raises(GraphValidationError) as exc_info:
            _build_coalesce_graph(sink_fields=_SINK_REQUIRES_UNPRODUCED_OPTION_LEVEL)

        assert not isinstance(exc_info.value, EdgeContractError)
        message = str(exc_info.value)
        assert "requires fields ['mandatory_flag']" in message
        assert "does not guarantee them" in message
        assert "Extra fields rejected" not in message

    def test_typed_path_model_required_is_owned_by_the_missing_arm(self) -> None:
        """A model-required unproduced field is check_compatibility's verdict.

        The same shape with mandatory_flag required IN THE MODEL is a true
        reject owned by the missing arm — the field is absent from the
        guarantee channel too, so when that arm becomes guarantee-aware
        (elspeth-7d68b04878) this must KEEP failing: a guarantee-aware arm
        may only forgive fields the graph proves present, and nothing proves
        mandatory_flag. Pinned now so that fix cannot over-widen unnoticed.

        The eligibility precondition rides a green sibling (the flipped-xfail
        shape, which builds precisely because forgiveness IS live there): if a
        future fixture edit ever made mandatory_flag guaranteed, this asserts
        the drift loudly instead of leaving only a less-diagnostic regex miss.
        """
        from elspeth.core.dag.models import EdgeContractError

        green = _build_coalesce_graph(sink_fields=_SINK_ADMITS_ALL)
        coalesce_id = next(n.node_id for n in green.get_nodes() if n.node_type.value == "coalesce")
        assert "mandatory_flag" not in schema_validation.get_effective_guaranteed_fields(green, coalesce_id)

        with pytest.raises(EdgeContractError, match="Missing fields: mandatory_flag"):
            _build_coalesce_graph(sink_fields=_SINK_REQUIRES_UNPRODUCED)


_LINEAR_PASS_THROUGH_PIPELINE = """sources:
  primary:
    plugin: csv
    on_success: raw
    options:
      path: examples/fork_coalesce/input.csv
      schema:
        mode: fixed
        fields:
        - 'id: int'
        - 'product: str'
        - 'description: str'
      on_validation_failure: discard

transforms:
- name: shorten
  plugin: truncate
  input: raw
  on_success: {shorten_out}
  on_error: discard
  options:
    fields:
      description: 20
    suffix: "..."
    schema:
      mode: flexible
      fields:
      - 'description: str'
{extra_transforms}
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: out.jsonl
      format: jsonl
      schema:
        mode: {sink_mode}
{sink_fields}"""

# The linear source declares three fields, so "admits all" is a 3-field lock here.
_SINK_ADMITS_ALL_LINEAR = """        fields:
        - 'id: int'
        - 'product: str'
        - 'description: str'
"""

# A locked TRANSFORM consumer whose own typed model REQUIRES every field the
# pass-through forwards — the non-sink calling context of the missing arm.
_LOCKED_TRANSFORM_REQUIRES_ALL_LINEAR = """- name: lock_it
  plugin: truncate
  input: mid
  on_success: output
  on_error: discard
  options:
    fields:
      description: 10
    suffix: "..."
    schema:
      mode: fixed
      fields:
      - 'id: int'
      - 'product: str'
      - 'description: str'
"""


def _build_linear_pass_through_graph(
    *,
    sink_mode: str = "fixed",
    sink_fields: str = _SINK_ADMITS_DESCRIPTION,
    shorten_out: str = "output",
    extra_transforms: str = "",
) -> ExecutionGraph:
    """Build source -> under-declaring pass-through transform -> sink. No coalesce."""
    from elspeth.cli_helpers import instantiate_plugins_from_config
    from elspeth.core.config import load_settings_from_yaml_string

    settings = load_settings_from_yaml_string(
        _LINEAR_PASS_THROUGH_PIPELINE.format(
            sink_mode=sink_mode,
            sink_fields=sink_fields,
            shorten_out=shorten_out,
            extra_transforms=extra_transforms,
        )
    )
    plugins = instantiate_plugins_from_config(settings)
    return ExecutionGraph.from_plugin_instances(
        sources=plugins.sources,
        source_settings_map=plugins.source_settings_map,
        transforms=plugins.transforms,
        sinks=plugins.sinks,
        aggregations=plugins.aggregations,
    )


class TestTypedPassThroughGuaranteedExtras:
    """The defect is NOT specific to union coalesce — pin its real scope.

    elspeth-1451ff385f was reported, diagnosed and fixed as a coalesce bug,
    because that is where the builder decouples the two channels
    STRUCTURALLY (typed fields from each branch's construction-time schema,
    guarantees from a separate graph walk). But the failing ingredient is
    only "a producer whose guarantees exceed its own declared fields, feeding
    a locked consumer", and any `passes_through_input=True` transform that
    declares a NARROWER schema than it forwards has exactly that shape with
    no coalesce anywhere.

    Verified by A/B against this same pipeline: with
    ``validate_typed_producer_guaranteed_extras`` disabled the build is
    GREEN, so this was a live silent defect on linear pipelines too and is
    not merely a coalesce symptom reached by another route.
    """

    def test_under_declaring_pass_through_against_locked_sink_is_rejected(self) -> None:
        """No fork, no coalesce — same phantom guarantees, same certain row death."""
        from elspeth.core.dag.models import EdgeContractError

        with pytest.raises(EdgeContractError) as exc_info:
            _build_linear_pass_through_graph()

        result = exc_info.value.compatibility_result
        assert result is not None
        assert result.extra_fields == ("id", "product")

    def test_under_declaring_pass_through_into_admitting_sink_still_builds(self) -> None:
        """False-reject guard: the same producer is fine when nothing is locked."""
        _build_linear_pass_through_graph(sink_mode="observed", sink_fields="")

    def test_requiring_sink_covered_by_guarantees_builds(self) -> None:
        """The missing-arm forgiveness on the LINEAR shape (elspeth-7d68b04878).

        This class's parity claim, pinned for the MISSING direction too: the
        sink requires id/product/description, the pass-through declares only
        description, and the guarantee walk proves the rest arrive. Resolution
        here runs through an ordinary transform's flexible schema, not
        build_coalesce_schema's union merge — so a regression confined to
        either resolution path cannot hide behind the other one's green
        (the coalesce twin is TestUnionCoalesceGuaranteedExtras.
        test_sink_admitting_every_guaranteed_field_builds).
        """
        _build_linear_pass_through_graph(sink_fields=_SINK_ADMITS_ALL_LINEAR)

    def test_requiring_locked_transform_consumer_is_forgiven_by_guarantees(self) -> None:
        """The forgiveness must thread to non-SINK consumers too.

        A sink is exactly the consumer Phase 1 exempts from its own
        missing-fields check (elspeth-3283f2eaec), so every sink-shaped pin in
        this arc exercises check_compatibility as the ONLY missing gate. A
        TRANSFORM consumer arrives with Phase 1 already run — but Phase 1
        reads option-level requirement declarations, while this consumer's
        requirement lives in its typed model alone, so Phase 2's
        producer_guaranteed threading is still what admits the build. Without
        this test the non-sink calling context had zero coverage.
        """
        _build_linear_pass_through_graph(
            shorten_out="mid",
            extra_transforms=_LOCKED_TRANSFORM_REQUIRES_ALL_LINEAR,
            sink_fields=_SINK_ADMITS_ALL_LINEAR,
        )


class TestReductiveProducerExtrasFirewall:
    """A producer that forbids extras cannot emit what it merely guarantees.

    The guaranteed-extras check rests on "a guarantee means every row WILL
    carry the field", which holds only for a guarantee about OUTPUT. A
    REDUCTIVE producer's guarantee channel can describe fields it CONSUMES —
    the batch_stats hazard: consuming `value` while emitting count/sum. Note
    the fixture is defence in depth for the decoupled-channel POPULATION, not
    a shape today's batch_stats itself produces: the real plugin (like every
    in-tree reductive plugin) publishes a `mode: observed` output config with
    guarantees recomputed to its emitted set, and observed producers bypass
    check_compatibility entirely. Direct-constructed graphs — the 45
    direct-SchemaConfig sites — can put a consumed-field guarantee on a FIXED
    output, and that is the shape pinned here: rows are exactly the declared
    fields, so the guaranteed-but-undeclared name provably never reaches the
    consumer and cannot kill a row there.

    That extras firewall is the discriminator between this shape and the one
    the check exists for: a union coalesce's merged schema is `mode:
    flexible`, as is an under-declaring pass-through transform's, and those
    really do forward fields they guarantee but never typed. Mirrors composer
    `_producer_emit_profile`'s `extras_firewall`.

    Regression guard: without it the check falsely rejected
    tests/unit/core/test_dag.py::test_validate_aggregation_dual_schema, a
    correct pipeline whose sink admits exactly what the aggregation emits.
    """

    @staticmethod
    def _reductive_graph(*, sink_fields: list[str] | None = None) -> ExecutionGraph:
        from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config

        consumed = SchemaConfig.from_dict({"mode": "fixed", "fields": ["value: float"]})
        emitted = SchemaConfig.from_dict({"mode": "fixed", "fields": ["count: int", "sum: float"]})
        sink = SchemaConfig.from_dict({"mode": "fixed", "fields": sink_fields or ["count: int", "sum: float"]})
        consumed_model = create_schema_from_config(consumed, "Consumed", allow_coercion=False)
        emitted_model = create_schema_from_config(emitted, "Emitted", allow_coercion=False)
        sink_model = create_schema_from_config(sink, "SinkInput", allow_coercion=False)

        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv", output_schema=consumed_model)
        graph.add_node(
            "agg",
            node_type=NodeType.AGGREGATION,
            plugin_name="batch_stats",
            input_schema=consumed_model,
            output_schema=emitted_model,
            # The guarantee channel describes what it CONSUMES, not emits.
            config={"schema": {"mode": "fixed", "fields": ["value: float"]}},
        )
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv", input_schema=sink_model)
        graph.add_edge("source", "agg", label="continue")
        graph.add_edge("agg", "sink", label="continue")
        return graph

    def test_reductive_producer_guarantee_does_not_reject_a_locked_consumer(self) -> None:
        """The sink admits exactly what the aggregation emits — this must build."""
        graph = self._reductive_graph()

        # The precondition that makes this a real guard, not a vacuous pass:
        # the guarantee channel names a field the emitted model does not.
        guaranteed = schema_validation.get_effective_guaranteed_fields(graph, "agg")
        assert "value" in guaranteed
        assert "value" not in graph.get_node_info("agg").output_schema.model_fields

        graph.validate_edge_compatibility()

    def test_reductive_producer_guarantee_does_not_satisfy_a_requiring_consumer(self) -> None:
        """The firewall's missing-arm direction (elspeth-7d68b04878).

        Same producer, but the sink now REQUIRES the guaranteed-but-never-
        emitted field. The guarantee-aware missing arm may forgive a required
        field only when the producer ADMITS undeclared fields; this producer
        is mode: fixed, so `value` provably never arrives and the missing
        verdict must stand. The sink also declares count/sum, so the verdict
        rides on the missing arm alone — the extras arm has nothing to reject
        and cannot mask an over-forgiving missing arm.
        """
        from elspeth.core.dag.models import EdgeContractError

        graph = self._reductive_graph(sink_fields=["count: int", "sum: float", "value: float"])

        with pytest.raises(EdgeContractError, match="Missing fields: value"):
            graph.validate_edge_compatibility()
