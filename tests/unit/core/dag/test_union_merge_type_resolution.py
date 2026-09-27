"""The union-merge type walk and the certain-conflict predicate (ADR-050 D8, lane 5887 G2).

``resolve_guaranteed_field_type(mode="union_merge")`` answers the contract type
a field WILL carry at runtime, for the union-coalesce refusal; ``edge`` mode
(the edge type check) is unchanged. ``certain_union_type_conflict`` is the ONE
predicate the build and the composer both call.
"""

from __future__ import annotations

import pytest

from elspeth.contracts.enums import NodeType, RoutingMode
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.schema_contract import FieldContract, OutputFieldDeclaration
from elspeth.contracts.union_merge import (
    KnownBranchFieldType,
    UnionFieldTypeConflict,
    certain_union_type_conflict,
    union_type_conflict_message,
)
from elspeth.core.dag import ExecutionGraph, GraphValidationError
from elspeth.core.dag.guarantees import resolve_guaranteed_field_type


def _declaration(name: str, python_type: type) -> OutputFieldDeclaration:
    contract = FieldContract(
        normalized_name=name,
        original_name=name,
        python_type=python_type,
        required=True,
        source="declared",
        nullable=python_type is object,
    )
    return OutputFieldDeclaration(contract, "plugin")


def _source(graph: ExecutionGraph, fields: list[str]) -> None:
    graph.add_node(
        "source",
        node_type=NodeType.SOURCE,
        plugin_name="csv",
        output_schema_config=SchemaConfig.from_dict({"mode": "fixed", "fields": fields}),
    )


def _transform(
    graph: ExecutionGraph,
    node_id: str,
    upstream: str,
    *,
    schema: dict[str, object] | None = None,
    declarations: dict[str, OutputFieldDeclaration] | None = None,
    carried: dict[str, str] | None = None,
    passes_through: bool = False,
) -> None:
    graph.add_node(
        node_id,
        node_type=NodeType.TRANSFORM,
        plugin_name="value_transform",
        output_schema_config=SchemaConfig.from_dict(schema or {"mode": "observed"}),
        output_field_declarations=declarations,
        carried_output_sources=carried,
        passes_through_input=passes_through,
        preserves_input_values=passes_through,
    )
    graph.add_edge(upstream, node_id, label="continue", mode=RoutingMode.MOVE)


class TestUnionMergeWalk:
    def test_a_declared_any_source_field_is_the_known_type_any_only_in_union_mode(self) -> None:
        graph = ExecutionGraph()
        _source(graph, ["price: any"])
        assert resolve_guaranteed_field_type(graph, "source", "price") is None
        resolved = resolve_guaranteed_field_type(graph, "source", "price", mode="union_merge")
        assert resolved is not None
        assert (resolved.field_type, resolved.declared_by) == ("any", frozenset({"source"}))

    def test_stamp_arm_answers_the_published_table_including_any(self) -> None:
        graph = ExecutionGraph()
        _source(graph, ["price: int"])
        _transform(graph, "rewrite", "source", declarations={"price": _declaration("price", object)}, passes_through=True)

        resolved = resolve_guaranteed_field_type(graph, "rewrite", "price", mode="union_merge")
        assert resolved is not None
        assert (resolved.field_type, resolved.declared_by) == ("any", frozenset({"rewrite"}))

    def test_the_table_overrides_a_flexible_placeholder_any(self) -> None:
        """A flexible config's name-only placeholder is ``any``; the plugin's table types it: the table is the runtime fact."""
        graph = ExecutionGraph()
        _source(graph, ["id: int"])
        _transform(
            graph,
            "typed",
            "source",
            schema={"mode": "flexible", "fields": ["score: any"]},
            declarations={"score": _declaration("score", int)},
        )
        resolved = resolve_guaranteed_field_type(graph, "typed", "score", mode="union_merge")
        assert resolved is not None
        assert resolved.field_type == "int"

    def test_a_config_declaration_the_table_does_not_stamp_abstains(self) -> None:
        """No stamp at runtime means the config's type is no runtime fact (a test double without an output config)."""
        graph = ExecutionGraph()
        _source(graph, ["price: int"])
        _transform(graph, "unstamped", "source", schema={"mode": "flexible", "fields": ["price: str"]}, passes_through=True)
        assert resolve_guaranteed_field_type(graph, "unstamped", "price", mode="union_merge") is None
        # Edge mode still reads the config declaration, as before.
        edge = resolve_guaranteed_field_type(graph, "unstamped", "price")
        assert edge is not None
        assert edge.field_type == "str"

    def test_carried_rename_follows_the_source_name_upstream(self) -> None:
        graph = ExecutionGraph()
        _source(graph, ["r: int", "q: str"])
        _transform(graph, "rename", "source", carried={"q": "r"})

        resolved = resolve_guaranteed_field_type(graph, "rename", "q", mode="union_merge")
        assert resolved is not None
        # The renamed value is r's (int), never the unrelated upstream q (str).
        assert (resolved.field_type, resolved.declared_by) == ("int", frozenset({"source"}))
        assert resolve_guaranteed_field_type(graph, "rename", "q") is None

    def test_a_gate_is_recursed_through(self) -> None:
        graph = ExecutionGraph()
        _source(graph, ["price: int"])
        graph.add_node(
            "gate", node_type=NodeType.GATE, plugin_name="config_gate", output_schema_config=SchemaConfig.from_dict({"mode": "observed"})
        )
        graph.add_edge("source", "gate", label="continue", mode=RoutingMode.MOVE)
        _transform(graph, "pass", "gate", passes_through=True)

        resolved = resolve_guaranteed_field_type(graph, "pass", "price", mode="union_merge")
        assert resolved is not None
        assert resolved.field_type == "int"

    def test_a_fan_in_node_declaring_the_field_abstains(self) -> None:
        """A coalesce's config is a build-time MERGE of branch configs, not a runtime fact."""
        graph = ExecutionGraph()
        _source(graph, ["price: int"])
        graph.add_node(
            "merge",
            node_type=NodeType.COALESCE,
            plugin_name="coalesce",
            output_schema_config=SchemaConfig.from_dict({"mode": "flexible", "fields": ["price: any"]}),
        )
        graph.add_edge("source", "merge", label="continue", mode=RoutingMode.MOVE)
        assert resolve_guaranteed_field_type(graph, "merge", "price", mode="union_merge") is None

    def test_the_stamp_table_is_only_meaningful_on_plugin_bearing_nodes(self) -> None:
        graph = ExecutionGraph()
        with pytest.raises(GraphValidationError, match="only meaningful for TRANSFORM, AGGREGATION, or COLLECTOR"):
            graph.add_node(
                "source",
                node_type=NodeType.SOURCE,
                plugin_name="csv",
                output_schema_config=SchemaConfig.from_dict({"mode": "observed"}),
                output_field_declarations={"price": _declaration("price", int)},
            )


def _known(field_type: str, declarer: str) -> KnownBranchFieldType:
    return KnownBranchFieldType(field_type=field_type, declared_by=(declarer,))


class TestCertainUnionTypeConflict:
    def test_two_known_present_types_that_differ_conflict(self) -> None:
        conflict = certain_union_type_conflict(
            {"a": {"price": _known("any", "transform 'vt'")}, "b": {"price": _known("int", "source 'src'")}},
            all_branches_merge=True,
            branch_order=("a", "b"),
        )
        assert conflict == UnionFieldTypeConflict(
            field="price",
            branch_a="a",
            type_a=_known("any", "transform 'vt'"),
            branch_b="b",
            type_b=_known("int", "source 'src'"),
        )

    def test_exact_inequality_like_the_runtime_merge_int_vs_float_conflicts(self) -> None:
        conflict = certain_union_type_conflict(
            {"a": {"x": _known("int", "a")}, "b": {"x": _known("float", "b")}},
            all_branches_merge=True,
            branch_order=("a", "b"),
        )
        assert conflict is not None

    @pytest.mark.parametrize(
        "branch_fields",
        [
            {"a": {"x": _known("any", "a")}, "b": {"x": _known("any", "b")}},
            {"a": {"x": _known("any", "a")}, "b": {}},
        ],
        ids=["agreeing-types", "one-branch-abstains"],
    )
    def test_agreement_or_abstention_is_not_a_conflict(self, branch_fields: dict[str, dict[str, KnownBranchFieldType]]) -> None:
        assert certain_union_type_conflict(branch_fields, all_branches_merge=True, branch_order=("a", "b")) is None

    def test_without_all_branch_semantics_nothing_is_certain(self) -> None:
        branch_fields = {"a": {"x": _known("any", "a")}, "b": {"x": _known("int", "b")}}
        assert certain_union_type_conflict(branch_fields, all_branches_merge=False, branch_order=("a", "b")) is None

    def test_deterministic_first_field_in_sorted_order_branches_in_declaration_order(self) -> None:
        branch_fields = {
            "b": {"z": _known("int", "b"), "y": _known("str", "b")},
            "a": {"z": _known("any", "a"), "y": _known("any", "a")},
        }
        conflict = certain_union_type_conflict(branch_fields, all_branches_merge=True, branch_order=("a", "b"))
        assert conflict is not None
        assert (conflict.field, conflict.branch_a, conflict.branch_b) == ("y", "a", "b")

    def test_message_names_both_declarers_and_the_remedy_type(self) -> None:
        conflict = UnionFieldTypeConflict(
            field="price",
            branch_a="a",
            type_a=_known("any", "transform 'vt_a' (value_transform)"),
            branch_b="b",
            type_b=_known("int", "source 'src' (csv)"),
        )
        message = union_type_conflict_message("'merge'", conflict)
        assert "receives incompatible types for field 'price' in union merge" in message
        assert "transform 'vt_a' (value_transform)" in message
        assert "source 'src' (csv)" in message
        assert "declare 'price: int' on the output schema of every branch's last node" in message
