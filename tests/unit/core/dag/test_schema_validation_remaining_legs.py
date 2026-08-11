"""Focused coverage for schema-validation policy and defensive graph legs."""

from __future__ import annotations

from typing import Literal

import pytest

from elspeth.contracts import PluginSchema
from elspeth.contracts.enums import NodeType
from elspeth.contracts.schema import FieldDefinition, SchemaConfig
from elspeth.core.dag import schema_validation
from elspeth.core.dag.graph import ExecutionGraph
from elspeth.core.dag.models import GraphValidationError


class WideSchema(PluginSchema):
    id: int
    name: str


class NarrowSchema(PluginSchema):
    id: int


class IncompatibleSchema(PluginSchema):
    label: str


def _config(mode: Literal["fixed", "flexible", "observed"], *fields: tuple[str, str]) -> SchemaConfig:
    return SchemaConfig(
        mode=mode,
        fields=tuple(FieldDefinition(name, field_type) for name, field_type in fields),
    )


def _gate_with_predecessor_configs(*configs: SchemaConfig | None) -> ExecutionGraph:
    graph = ExecutionGraph()
    graph.add_node("gate", node_type=NodeType.GATE, plugin_name="config_gate")
    for index, config in enumerate(configs):
        source_id = f"source_{index}"
        graph.add_node(
            source_id,
            node_type=NodeType.SOURCE,
            plugin_name="csv",
            output_schema_config=config,
        )
        graph.add_edge(source_id, "gate", label=f"input_{index}")
    return graph


def test_select_coalesce_without_selected_branch_is_rejected() -> None:
    graph = ExecutionGraph()
    graph.add_node(
        "coalesce",
        node_type=NodeType.COALESCE,
        plugin_name="coalesce",
        config={"merge": "select"},
    )

    with pytest.raises(GraphValidationError, match="no 'select_branch'") as exc_info:
        graph.get_effective_producer_schema("coalesce")

    assert exc_info.value.component_id == "coalesce"
    assert exc_info.value.component_type == "coalesce"


def test_gate_rejects_incompatible_explicit_predecessor_schemas() -> None:
    graph = ExecutionGraph()
    graph.add_node("wide", node_type=NodeType.SOURCE, plugin_name="csv", output_schema=WideSchema)
    graph.add_node(
        "incompatible",
        node_type=NodeType.SOURCE,
        plugin_name="csv",
        output_schema=IncompatibleSchema,
    )
    graph.add_node("gate", node_type=NodeType.GATE, plugin_name="config_gate")
    graph.add_edge("wide", "gate", label="wide")
    graph.add_edge("incompatible", "gate", label="incompatible")

    with pytest.raises(GraphValidationError, match="receives incompatible schemas") as exc_info:
        graph.get_effective_producer_schema("gate")

    assert exc_info.value.component_id == "gate"
    assert exc_info.value.component_type == "gate"
    assert "WideSchema" in str(exc_info.value)
    assert "IncompatibleSchema" in str(exc_info.value)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param(None, None, id="both-observed"),
        pytest.param(None, WideSchema, id="observed-first"),
        pytest.param(WideSchema, None, id="observed-second"),
    ],
)
def test_observed_schema_pairs_are_structurally_compatible(
    first: type[PluginSchema] | None,
    second: type[PluginSchema] | None,
) -> None:
    assert schema_validation.schemas_structurally_compatible(first, second) == (True, "")


@pytest.mark.parametrize(
    ("first", "second", "reported_direction", "absent_direction"),
    [
        pytest.param(WideSchema, NarrowSchema, "NarrowSchema -> WideSchema", "WideSchema -> NarrowSchema", id="reverse-fails"),
        pytest.param(NarrowSchema, WideSchema, "NarrowSchema -> WideSchema", "WideSchema -> NarrowSchema", id="forward-fails"),
    ],
)
def test_asymmetric_structural_incompatibility_reports_only_the_failing_direction(
    first: type[PluginSchema],
    second: type[PluginSchema],
    reported_direction: str,
    absent_direction: str,
) -> None:
    compatible, message = schema_validation.schemas_structurally_compatible(first, second)

    assert compatible is False
    assert reported_direction in message
    assert absent_direction not in message


def test_coalesce_without_predecessors_is_rejected() -> None:
    graph = ExecutionGraph()
    graph.add_node(
        "coalesce",
        node_type=NodeType.COALESCE,
        plugin_name="coalesce",
        config={"merge": "union"},
    )

    with pytest.raises(GraphValidationError, match="has no incoming edges") as exc_info:
        schema_validation.validate_coalesce_compatibility(graph, "coalesce")

    assert exc_info.value.component_id == "coalesce"
    assert exc_info.value.component_type == "coalesce"


def test_single_predecessor_coalesce_is_compatible() -> None:
    graph = ExecutionGraph()
    graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="csv", output_schema=WideSchema)
    graph.add_node(
        "coalesce",
        node_type=NodeType.COALESCE,
        plugin_name="coalesce",
        config={"merge": "union"},
    )
    graph.add_edge("source", "coalesce", label="only_branch")

    schema_validation.validate_coalesce_compatibility(graph, "coalesce")


def test_effective_schema_config_abstains_for_unknown_predecessor() -> None:
    graph = _gate_with_predecessor_configs(None)

    assert schema_validation.get_effective_producer_schema_config(graph, "gate") is None


def test_effective_schema_config_terminates_on_gate_cycle() -> None:
    graph = ExecutionGraph()
    graph.add_node("gate_a", node_type=NodeType.GATE, plugin_name="config_gate")
    graph.add_node("gate_b", node_type=NodeType.GATE, plugin_name="config_gate")
    graph.add_edge("gate_a", "gate_b", label="a_to_b")
    graph.add_edge("gate_b", "gate_a", label="b_to_a")

    assert schema_validation.get_effective_producer_schema_config(graph, "gate_a") is None


def test_effective_schema_config_returns_unanimous_known_config() -> None:
    config = _config("fixed", ("id", "int"))
    graph = _gate_with_predecessor_configs(config, config)

    assert schema_validation.get_effective_producer_schema_config(graph, "gate") is config


def test_effective_schema_config_abstains_when_known_configs_disagree() -> None:
    graph = _gate_with_predecessor_configs(
        _config("fixed", ("id", "int")),
        _config("fixed", ("id", "str")),
    )

    assert schema_validation.get_effective_producer_schema_config(graph, "gate") is None


def test_row_union_schema_config_with_unknown_fields_abstains() -> None:
    observed = SchemaConfig(mode="observed", fields=None)
    fixed = _config("fixed", ("id", "int"))

    assert schema_validation.row_union_schema_configs_compatible(observed, fixed) == (True, (), "")


def test_row_union_compatible_fixed_configs_are_accepted() -> None:
    first = _config("fixed", ("id", "int"), ("name", "str"))
    second = _config("fixed", ("id", "int"), ("name", "str"))

    assert schema_validation.row_union_schema_configs_compatible(first, second) == (True, (), "")


def test_row_union_incompatible_fixed_configs_report_conflicting_fields() -> None:
    first = _config("fixed", ("id", "int"), ("name", "str"))
    second = _config("fixed", ("id", "str"), ("name", "str"))

    compatible, conflicts, message = schema_validation.row_union_schema_configs_compatible(first, second)

    assert compatible is False
    assert conflicts == ("id",)
    assert "id" in message


def test_row_union_flexible_configs_allow_any_shared_field_type() -> None:
    wildcard = _config("flexible", ("id", "any"))
    concrete = _config("flexible", ("id", "int"))

    assert schema_validation.row_union_schema_configs_compatible(wildcard, concrete) == (True, (), "")


def test_row_union_flexible_configs_reject_conflicting_shared_field_types() -> None:
    first = _config("flexible", ("id", "int"))
    second = _config("flexible", ("id", "str"))

    compatible, conflicts, message = schema_validation.row_union_schema_configs_compatible(first, second)

    assert compatible is False
    assert conflicts == ("id",)
    assert message == "Conflicting shared field types: id (int vs str)"
