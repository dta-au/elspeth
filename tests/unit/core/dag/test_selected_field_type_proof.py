"""Selection carries same-name type evidence without inventing projection lineage."""

import pytest

from elspeth.contracts.enums import NodeType, RoutingMode
from elspeth.contracts.schema import FieldDefinition, SchemaConfig
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.guarantees import resolve_guaranteed_field_type
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.transforms.field_mapper import FieldMapper
from elspeth.testing import make_field
from tests.fixtures.factories import make_context


def _graph(mapper: FieldMapper, *, divert: bool = False) -> ExecutionGraph:
    graph = ExecutionGraph()
    graph.add_node(
        "source",
        node_type=NodeType.SOURCE,
        plugin_name="csv",
        output_schema_config=SchemaConfig(mode="flexible", fields=(FieldDefinition(name="id", field_type="str"),)),
    )
    graph.add_node(
        "mapper",
        node_type=NodeType.TRANSFORM,
        plugin_name=mapper.name,
        output_schema_config=mapper._output_schema_config,
        declared_input_fields=mapper.declared_input_fields,
        declared_output_fields=mapper.declared_output_fields,
        preserves_input_values=mapper.preserves_input_values,
        forwards_input_fields=mapper.forwards_input_fields,
        removed_input_fields=mapper.removed_input_fields,
    )
    graph.add_edge("source", "mapper", label="continue", mode=RoutingMode.DIVERT if divert else RoutingMode.MOVE)
    return graph


@pytest.mark.parametrize("source", ["id", "other", "record.id", "ID"])
def test_only_canonical_identity_selection_reuses_ancestor_type(source: str) -> None:
    mapper = FieldMapper({"schema": {"mode": "observed"}, "mapping": {source: "id"}, "select_only": True})
    resolution = resolve_guaranteed_field_type(_graph(mapper), "mapper", "id")
    if source == "id":
        assert resolution is not None
        assert resolution.field_type == "str"
        assert resolution.declared_by == frozenset({"source"})
    else:
        # An upstream id:str says nothing about a different field projected
        # onto id, including a nested value or an original-header alias.
        assert resolution is None


@pytest.mark.parametrize("source", ["record.id", "ID"])
def test_unresolved_identity_spelling_does_not_invent_input_lineage(source: str) -> None:
    mapper = FieldMapper({"schema": {"mode": "observed"}, "mapping": {source: source}, "select_only": True})
    graph = _graph(mapper)
    graph.add_node(
        "declares_spelling",
        node_type=NodeType.SOURCE,
        plugin_name="json",
        output_schema_config=SchemaConfig(mode="flexible", fields=(FieldDefinition(name=source, field_type="str"),)),
    )
    graph._graph.remove_edge("source", "mapper")
    graph.add_edge("declares_spelling", "mapper", label="continue")
    assert resolve_guaranteed_field_type(graph, "mapper", source) is None


def test_dotted_required_input_cannot_claim_flat_projection_lineage() -> None:
    with pytest.raises(PluginConfigError, match="identifier"):
        FieldMapper(
            {
                "schema": {"mode": "observed"},
                "mapping": {"record.id": "record.id"},
                "select_only": True,
                "required_input_fields": ["record.id"],
            }
        )


def test_rename_overwrites_selected_target_without_preserving_its_old_type() -> None:
    mapper = FieldMapper({"schema": {"mode": "observed"}, "mapping": {"other": "id"}, "select_only": True})
    row = PipelineRow(
        {"id": "old", "other": 42},
        SchemaContract(mode="OBSERVED", fields=(make_field("id", str), make_field("other", int)), locked=True),
    )
    result = mapper.process(row, make_context())
    assert result.row is not None
    assert result.row.to_dict() == {"id": 42}
    assert resolve_guaranteed_field_type(_graph(mapper), "mapper", "id") is None


@pytest.mark.parametrize("obstacle", ["no_input", "no_output", "rewrites_values", "created_output"])
def test_partial_selection_facts_do_not_prove_surviving_field_types(obstacle: str) -> None:
    graph = ExecutionGraph()
    graph.add_node(
        "source",
        node_type=NodeType.SOURCE,
        plugin_name="csv",
        output_schema_config=SchemaConfig(mode="flexible", fields=(FieldDefinition(name="id", field_type="str"),)),
    )
    graph.add_node(
        "selection",
        node_type=NodeType.TRANSFORM,
        plugin_name="selection",
        output_schema_config=SchemaConfig(mode="observed", guaranteed_fields=() if obstacle == "no_output" else ("id",)),
        declared_input_fields=frozenset() if obstacle == "no_input" else frozenset({"id"}),
        declared_output_fields=frozenset({"id"}) if obstacle == "created_output" else frozenset(),
        preserves_input_values=obstacle != "rewrites_values",
    )
    graph.add_edge("source", "selection", label="continue")
    assert resolve_guaranteed_field_type(graph, "selection", "id") is None


@pytest.mark.parametrize("obstacle", ["divert", "explicit_any", "unknown", "conflicting"])
def test_selection_retains_existing_type_evidence_boundaries(obstacle: str) -> None:
    schema = {"mode": "flexible", "fields": ["id: any"]} if obstacle == "explicit_any" else {"mode": "observed"}
    mapper = FieldMapper({"schema": schema, "mapping": {"id": "id"}, "select_only": True})
    graph = _graph(mapper, divert=obstacle == "divert")
    if obstacle in {"unknown", "conflicting"}:
        graph.add_node(
            "other",
            node_type=NodeType.SOURCE,
            plugin_name="json",
            output_schema_config=(
                SchemaConfig(mode="observed", guaranteed_fields=("id",))
                if obstacle == "unknown"
                else SchemaConfig(mode="flexible", fields=(FieldDefinition(name="id", field_type="int"),))
            ),
        )
        graph.add_edge("other", "mapper", label="continue")
    assert resolve_guaranteed_field_type(graph, "mapper", "id") is None
