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
        - 'id: int'
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
  policy: require_all
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
    extra_transforms: str = "",
    coalesce_out: str = "\n  on_success: output",
) -> ExecutionGraph:
    """Build fork -> two pass-through branches -> union coalesce -> locked sink."""
    from elspeth.cli_helpers import instantiate_plugins_from_config
    from elspeth.core.config import load_settings_from_yaml_string

    settings = load_settings_from_yaml_string(
        _COALESCE_PIPELINE.format(
            branch_schema=branch_schema,
            sink_mode=sink_mode,
            extra_transforms=extra_transforms,
            sink_fields=sink_fields,
            coalesce_out=coalesce_out,
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
        guarantees four fields it never declares and the new pass DOES
        evaluate this edge. A `mode: flexible` consumer is `extra='allow'`,
        so no row can die at its preflight and the check must decline. This
        is the shape that would break if the check ever stopped consulting
        the consumer's extras policy.
        """
        _build_coalesce_graph(sink_mode="flexible")

    @pytest.mark.xfail(
        reason=(
            "Inverse defect, deliberately out of scope for elspeth-1451ff385f: "
            "check_compatibility's MISSING-fields arm reads the declared channel only, "
            "the mirror of the extras-arm defect this class fixes. The coalesce declares "
            "['description'], so a sink admitting every GUARANTEED field is rejected with "
            "'Missing fields' even though the rows carry them and the pipeline runs. "
            "Pinned so the asymmetry is visible in the suite, not only in the ticket."
        ),
        strict=True,
    )
    def test_sink_admitting_every_guaranteed_field_is_wrongly_rejected(self) -> None:
        """The remedy the rejection message used to propose first — a false reject."""
        _build_coalesce_graph(sink_fields=_SINK_ADMITS_ALL)

    def test_select_only_field_mapper_clears_the_rejection(self) -> None:
        """The rejection message's remedy 3 must actually clear the rejection.

        A field_mapper with select_only drops the extras, so the walk carries
        only 'description' past it — an error whose own remedy did not clear it
        would mean the guarantee walk over-attributes through field-dropping
        nodes.
        """
        graph = _build_coalesce_graph(extra_transforms=_SELECT_ONLY_MAPPER, coalesce_out="")

        mapper = next(n for n in graph.get_nodes() if n.node_id.startswith("transform_drop_extras"))
        assert schema_validation.get_effective_guaranteed_fields(graph, mapper.node_id) == frozenset({"description"})


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
        mode: {sink_mode}
{sink_fields}"""


def _build_linear_pass_through_graph(*, sink_mode: str = "fixed", sink_fields: str = _SINK_ADMITS_DESCRIPTION) -> ExecutionGraph:
    """Build source -> under-declaring pass-through transform -> sink. No coalesce."""
    from elspeth.cli_helpers import instantiate_plugins_from_config
    from elspeth.core.config import load_settings_from_yaml_string

    settings = load_settings_from_yaml_string(_LINEAR_PASS_THROUGH_PIPELINE.format(sink_mode=sink_mode, sink_fields=sink_fields))
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


class TestReductiveProducerExtrasFirewall:
    """A producer that forbids extras cannot emit what it merely guarantees.

    The guaranteed-extras check rests on "a guarantee means every row WILL
    carry the field", which holds only for a guarantee about OUTPUT. A
    REDUCTIVE producer's guarantee channel can describe fields it CONSUMES:
    batch_stats declares `value` while emitting count/sum. Its output
    contract is `mode: fixed` (extra='forbid'), so its rows are exactly its
    declared fields — the guaranteed-but-undeclared name provably never
    reaches the consumer and cannot kill a row there.

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
    def _reductive_graph() -> ExecutionGraph:
        from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config

        consumed = SchemaConfig.from_dict({"mode": "fixed", "fields": ["value: float"]})
        emitted = SchemaConfig.from_dict({"mode": "fixed", "fields": ["count: int", "sum: float"]})
        consumed_model = create_schema_from_config(consumed, "Consumed", allow_coercion=False)
        emitted_model = create_schema_from_config(emitted, "Emitted", allow_coercion=False)

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
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="csv", input_schema=emitted_model)
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
