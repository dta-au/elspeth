"""Build-time half of the field-name spelling rule (operator ruling 2026-09-25, elspeth-5887fb7928).

A DECLARATION — a schema field, a required or column-naming option, a created
target — names a field as rows carry it; a header spelling of a field the
upstream carries is refused. ``validate_declared_field_spellings`` refuses it
where a participating upstream PROVES it and abstains elsewhere, leaving the
row to the runtime residual. These tests pin both the refusals and every
soundness leg that keeps it from rejecting a runnable pipeline.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.contracts.enums import NodeType, RoutingMode
from elspeth.core.dag import schema_validation
from elspeth.core.dag.graph import ExecutionGraph
from elspeth.core.dag.models import GraphValidationError, NodeInfo

_FIXED_ID_NAME: dict[str, object] = {"mode": "fixed", "fields": ["id: str", "name: str"]}
_FLEXIBLE_ID_NAME: dict[str, object] = {"mode": "flexible", "fields": ["id: str", "name: str"]}


def _graph(
    *,
    source_schema: dict[str, object],
    consumer_type: NodeType = NodeType.TRANSFORM,
    reads: frozenset[str] = frozenset(),
    creates: frozenset[str] = frozenset(),
    passes_through_input: bool = False,
    forwards_input_fields: bool = False,
    removed_input_fields: frozenset[str] = frozenset(),
    edge_mode: RoutingMode = RoutingMode.MOVE,
) -> ExecutionGraph:
    """source -> consumer (-> sink), the consumer carrying the declaration under test."""
    graph = ExecutionGraph()
    graph.add_node("src", node_type=NodeType.SOURCE, plugin_name="csv", config={"schema": source_schema})
    consumer_kwargs: dict[str, Any] = {
        "declared_read_fields": reads,
        "passes_through_input": passes_through_input,
        "forwards_input_fields": forwards_input_fields,
        "removed_input_fields": removed_input_fields,
    }
    if consumer_type == NodeType.TRANSFORM:
        consumer_kwargs["declared_created_fields"] = creates
    if consumer_type == NodeType.SINK:
        consumer_kwargs = {"declared_read_fields": reads}
    graph.add_node(
        "consumer",
        node_type=consumer_type,
        plugin_name="value_transform" if consumer_type == NodeType.TRANSFORM else "json",
        config={"schema": {"mode": "observed"}},
        **consumer_kwargs,
    )
    graph.add_edge("src", "consumer", label="continue", mode=edge_mode)
    if consumer_type != NodeType.SINK:
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="json", config={"schema": {"mode": "observed"}})
        graph.add_edge("consumer", "sink", label="out", mode=RoutingMode.MOVE)
    return graph


class TestReadDeclarations:
    """A declared READ is refused only against a participating AND closed upstream."""

    def test_a_header_spelled_read_is_refused_against_a_closed_upstream(self) -> None:
        graph = _graph(source_schema=_FIXED_ID_NAME, reads=frozenset({"Name"}))

        with pytest.raises(GraphValidationError, match="Field name header spelling") as exc_info:
            schema_validation.validate_declared_field_spellings(graph)

        message = str(exc_info.value)
        assert "'Name' is a header spelling of 'name'" in message
        assert "Declare 'name'" in message
        assert "src" in message

    def test_the_canonical_read_builds(self) -> None:
        schema_validation.validate_declared_field_spellings(_graph(source_schema=_FIXED_ID_NAME, reads=frozenset({"name"})))

    def test_an_open_upstream_abstains(self) -> None:
        """Absence of 'Name' needs an upper bound: a flexible upstream may carry it as a field of its own."""
        schema_validation.validate_declared_field_spellings(_graph(source_schema=_FLEXIBLE_ID_NAME, reads=frozenset({"Name"})))

    def test_an_abstaining_upstream_abstains(self) -> None:
        schema_validation.validate_declared_field_spellings(_graph(source_schema={"mode": "observed"}, reads=frozenset({"Name"})))

    def test_a_divert_predecessor_is_not_checked(self) -> None:
        graph = _graph(source_schema=_FIXED_ID_NAME, reads=frozenset({"Name"}), edge_mode=RoutingMode.DIVERT)

        schema_validation.validate_declared_field_spellings(graph)

    def test_a_field_the_upstream_carries_as_written_is_not_a_header_spelling(self) -> None:
        """A source ``field_mapping`` value keeps 'Name' as the row key: declaring 'Name' is correct."""
        graph = _graph(source_schema={"mode": "fixed", "fields": ["id: str", "Name: str"]}, reads=frozenset({"Name"}))

        schema_validation.validate_declared_field_spellings(graph)

    @pytest.mark.parametrize("consumer_type", [NodeType.SINK, NodeType.AGGREGATION], ids=["sink", "aggregation"])
    def test_sinks_and_aggregations_declare_reads_too(self, consumer_type: NodeType) -> None:
        graph = _graph(source_schema=_FIXED_ID_NAME, consumer_type=consumer_type, reads=frozenset({"Name"}))

        with pytest.raises(GraphValidationError, match="'Name' is a header spelling of 'name'"):
            schema_validation.validate_declared_field_spellings(graph)


class TestCreatedNames:
    """A created name is refused against a participating upstream, where the write path keeps the row."""

    def test_a_header_spelled_target_is_refused_against_an_open_participating_upstream(self) -> None:
        """Probe A/B of the Q4 amendment: flexible source guaranteeing 'name', target 'Name'."""
        graph = _graph(source_schema=_FLEXIBLE_ID_NAME, creates=frozenset({"Name"}), passes_through_input=True)

        with pytest.raises(GraphValidationError, match="'Name' is a header spelling of the arriving field 'name'"):
            schema_validation.validate_declared_field_spellings(graph)

    @pytest.mark.parametrize("target", ["Total", "name"], ids=["unrelated-created-name", "canonical-overwrite"])
    def test_other_targets_build(self, target: str) -> None:
        """Probes C and E: a new capitalised name and a canonical overwrite are not header spellings."""
        graph = _graph(source_schema=_FLEXIBLE_ID_NAME, creates=frozenset({target}), passes_through_input=True)

        schema_validation.validate_declared_field_spellings(graph)

    def test_a_rename_that_removes_the_field_it_respells_shadows_nothing(self) -> None:
        """``{name: Name}`` removes 'name' and writes 'Name': the header is restored, nothing is shadowed."""
        graph = _graph(
            source_schema=_FLEXIBLE_ID_NAME,
            creates=frozenset({"Name"}),
            forwards_input_fields=True,
            removed_input_fields=frozenset({"name"}),
        )

        schema_validation.validate_declared_field_spellings(graph)

    def test_a_fresh_row_writer_is_not_checked(self) -> None:
        """A transform that keeps no input row (select_only field_mapper) cannot shadow an arriving field."""
        graph = _graph(source_schema=_FLEXIBLE_ID_NAME, creates=frozenset({"Name"}))

        schema_validation.validate_declared_field_spellings(graph)

    def test_an_abstaining_upstream_abstains(self) -> None:
        """Probe D: behind an observed source the build cannot see 'name'; the executor routes the row."""
        graph = _graph(source_schema={"mode": "observed"}, creates=frozenset({"Name"}), passes_through_input=True)

        schema_validation.validate_declared_field_spellings(graph)


def test_it_speaks_before_the_missing_field_checks() -> None:
    """Run FIRST: a header-spelled required read would otherwise be reported only as 'missing'."""
    graph = _graph(source_schema=_FIXED_ID_NAME, reads=frozenset({"Name"}))

    with pytest.raises(GraphValidationError, match="Field name header spelling"):
        graph.validate_edge_compatibility()


class TestNodeInfoGuards:
    def test_created_names_are_transform_only(self) -> None:
        with pytest.raises(GraphValidationError, match="declared_created_fields is only meaningful for TRANSFORM"):
            NodeInfo(node_id="n1", node_type=NodeType.SINK, plugin_name="json", declared_created_fields=frozenset({"Name"}))

    def test_reads_never_sit_on_a_source(self) -> None:
        with pytest.raises(GraphValidationError, match="declared_read_fields is only meaningful for"):
            NodeInfo(node_id="n1", node_type=NodeType.SOURCE, plugin_name="csv", declared_read_fields=frozenset({"Name"}))

    @pytest.mark.parametrize("node_type", [NodeType.TRANSFORM, NodeType.AGGREGATION, NodeType.COLLECTOR, NodeType.SINK])
    def test_reads_sit_on_every_plugin_consumer(self, node_type: NodeType) -> None:
        info = NodeInfo(node_id="n1", node_type=node_type, plugin_name="p", declared_read_fields=frozenset({"name"}))

        assert info.declared_read_fields == frozenset({"name"})


class TestPhaseOneHint:
    """``required_input_fields`` fails closed at Phase 1; the verdict now names the spelling."""

    def test_a_header_spelled_required_field_names_its_canonical_form(self) -> None:
        graph = ExecutionGraph()
        graph.add_node("src", node_type=NodeType.SOURCE, plugin_name="csv", config={"schema": _FLEXIBLE_ID_NAME})
        graph.add_node(
            "t1",
            node_type=NodeType.TRANSFORM,
            plugin_name="value_transform",
            config={"schema": {"mode": "observed"}, "required_input_fields": ["Name"]},
        )
        graph.add_edge("src", "t1", label="continue", mode=RoutingMode.MOVE)

        with pytest.raises(GraphValidationError, match="Header spellings: 'Name' is a header spelling of 'name'"):
            schema_validation.validate_single_edge(graph, "src", "t1")

    def test_a_plainly_missing_field_carries_no_hint(self) -> None:
        graph = ExecutionGraph()
        graph.add_node("src", node_type=NodeType.SOURCE, plugin_name="csv", config={"schema": _FLEXIBLE_ID_NAME})
        graph.add_node(
            "t1",
            node_type=NodeType.TRANSFORM,
            plugin_name="value_transform",
            config={"schema": {"mode": "observed"}, "required_input_fields": ["total"]},
        )
        graph.add_edge("src", "t1", label="continue", mode=RoutingMode.MOVE)

        with pytest.raises(GraphValidationError) as exc_info:
            schema_validation.validate_single_edge(graph, "src", "t1")
        assert "Header spellings" not in str(exc_info.value)
