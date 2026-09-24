"""Computed values and forwarded metadata obey ValueTransform declarations."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from elspeth.cli_helpers import instantiate_plugins_from_config
from elspeth.config_loading import load_settings_from_yaml_string
from elspeth.contracts.errors import SchemaConfigModeViolation
from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract
from elspeth.core.dag.graph import ExecutionGraph
from elspeth.core.dag.models import EdgeContractError
from elspeth.engine.executors.schema_config_mode import verify_schema_config_mode
from elspeth.plugins.sources.csv_source import CSVSource
from elspeth.plugins.transforms.value_transform import ValueTransform
from tests.fixtures.factories import make_context


def _csv_row(tmp_path: Path, *, declared: bool = False, numeric: bool = False) -> PipelineRow:
    path = tmp_path / "input.csv"
    path.write_text("Identifier,X\na,2\n")
    source = CSVSource(
        {
            "path": str(path),
            "field_mapping": {"identifier": "id"},
            "schema": {"mode": "flexible", "fields": ["id: str", "x: int" if numeric else "x: str"]} if declared else {"mode": "observed"},
            "on_validation_failure": "discard",
        }
    )
    (row,) = list(source.load(make_context()))
    return PipelineRow(row.row, row.contract)


def _verify(transform: ValueTransform, row: PipelineRow) -> None:
    assert transform._output_schema_config is not None
    verify_schema_config_mode(
        output_schema_config=transform._output_schema_config,
        emitted_rows=[row],
        plugin_name=transform.name,
        node_id="calculate",
        run_id="run",
        row_id="row",
        token_id="token",
    )
    assert row.contract.validate(row.to_dict()) == []


@pytest.mark.parametrize("mode", ["flexible", "fixed"])
@pytest.mark.parametrize("declared_source", [False, True])
def test_csv_forwarded_fields_reconcile_declarations_without_losing_aliases(tmp_path, mode, declared_source):
    row = _csv_row(tmp_path, declared=declared_source)
    transform = ValueTransform(
        {
            "schema": {
                "mode": mode,
                "fields": ["id: str", "x: str", {"name": "label", "type": "str", "required": False, "nullable": False}],
            },
            "operations": [{"target": "label", "expression": "row['Identifier'] + row['X']"}],
        }
    )
    transform.input_schema.model_validate(row.to_dict(), strict=True)
    result = transform.process(row, make_context())
    assert result.row is not None
    _verify(transform, result.row)
    assert result.row["label"] == "a2"
    for normalized, original in [("id", "Identifier"), ("x", "X")]:
        field = result.row.contract.get_field(normalized)
        assert field.required is True
        assert field.nullable is False
        assert field.original_name == original
        assert result.row[original] == row[original]


@pytest.mark.parametrize("expression, expected, expected_type", [("row['X'] > 0", True, bool), ("None", None, type(None))])
def test_computed_target_declares_presence_and_keeps_actual_runtime_type(tmp_path, expression, expected, expected_type):
    row = _csv_row(tmp_path, declared=True, numeric=True)
    transform = ValueTransform(
        {
            "schema": {"mode": "flexible", "fields": ["id: str", "x: int"]},
            "operations": [{"target": "x", "expression": expression}],
        }
    )
    transform.input_schema.model_validate(row.to_dict(), strict=True)
    result = transform.process(row, make_context())
    assert result.row is not None
    _verify(transform, result.row)
    assert result.row["x"] is expected
    assert result.row["X"] is expected
    field = result.row.contract.get_field("x")
    assert field.python_type is expected_type
    assert field.original_name == "X"
    assert field.required is True
    assert field.nullable is (expected is None)
    output = transform._output_schema_config
    assert output is not None and output.fields is not None
    declared = next(field for field in output.fields if field.name == "x")
    assert declared.field_type == "any"
    assert declared.required is True
    assert declared.nullable is True
    assert "x" in output.get_effective_guaranteed_fields()
    assert transform.input_schema.model_fields["x"].annotation is int


@pytest.mark.parametrize("invalid", [{"x": "2"}, {"id": None, "x": "2"}, {"id": 3, "x": "2"}])
def test_declared_forwarded_fields_reject_missing_null_and_wrong_type(invalid):
    transform = ValueTransform(
        {
            "schema": {"mode": "flexible", "fields": ["id: str", "x: str"]},
            "operations": [{"target": "label", "expression": "row['X']"}],
        }
    )
    with pytest.raises(ValidationError):
        transform.input_schema.model_validate(invalid, strict=True)


def test_nullable_forwarded_declaration_survives_nonnull_observation(tmp_path):
    row = _csv_row(tmp_path)
    transform = ValueTransform(
        {
            "schema": {
                "mode": "flexible",
                "fields": [{"name": "id", "type": "str", "required": True, "nullable": True}, "x: str"],
            },
            "operations": [{"target": "label", "expression": "row['X']"}],
        }
    )
    transform.input_schema.model_validate(row.to_dict(), strict=True)
    result = transform.process(row, make_context())
    assert result.row is not None
    _verify(transform, result.row)
    assert result.row.contract.get_field("id").nullable is True
    with pytest.raises(SchemaConfigModeViolation):
        _verify(transform, row)


@pytest.mark.parametrize("value", [2, 2.0, "2"])
def test_forwarded_numeric_type_remains_truthful_after_float_input_validation(value):
    row = PipelineRow(
        {"x": value},
        SchemaContract(
            mode="OBSERVED",
            fields=(FieldContract("x", "x", type(value), required=False, source="inferred", nullable=False),),
            locked=True,
        ),
    )
    transform = ValueTransform(
        {
            "schema": {"mode": "flexible", "fields": ["x: float"]},
            "operations": [{"target": "label", "expression": "1"}],
        }
    )
    if isinstance(value, str):
        with pytest.raises(ValidationError):
            transform.input_schema.model_validate(row.to_dict(), strict=True)
        return

    validated = transform.input_schema.model_validate(row.to_dict(), strict=True)
    assert type(validated.model_dump()["x"]) is float
    result = transform.process(row, make_context())
    assert result.row is not None
    assert type(result.row["x"]) is type(value)
    assert result.row.contract.get_field("x").python_type is type(value)
    assert result.row.contract.validate(result.row.to_dict()) == []
    if isinstance(value, int):
        # Pydantic admits int as float, but the executor uses the original row.
        # Keep the existing declaration mismatch visible without falsifying it.
        with pytest.raises(SchemaConfigModeViolation, match="field metadata mismatches for \\['x'\\]"):
            _verify(transform, result.row)
    else:
        _verify(transform, result.row)


def _graph(tmp_path: Path, *, normalize: bool) -> ExecutionGraph:
    transforms = [
        {
            "name": "calculate",
            "plugin": "value_transform",
            "input": "raw",
            "on_success": "calculated" if normalize else "output",
            "on_error": "discard",
            "options": {
                "schema": {"mode": "flexible", "fields": ["id: str", "x: int"]},
                "operations": [{"target": "x", "expression": "row['x'] > 0"}],
            },
        }
    ]
    if normalize:
        transforms.append(
            {
                "name": "normalize",
                "plugin": "type_coerce",
                "input": "calculated",
                "on_success": "output",
                "on_error": "discard",
                "options": {
                    "schema": {"mode": "flexible", "fields": ["id: str", "x: any"]},
                    "conversions": [{"field": "x", "to": "bool"}],
                },
            }
        )
    settings = load_settings_from_yaml_string(
        json.dumps(
            {
                "sources": {
                    "source": {
                        "plugin": "csv",
                        "on_success": "raw",
                        "options": {
                            "path": str(tmp_path / "input.csv"),
                            "schema": {"mode": "fixed", "fields": ["id: str", "x: int"]},
                            "on_validation_failure": "discard",
                        },
                    }
                },
                "transforms": transforms,
                "sinks": {
                    "output": {
                        "plugin": "json",
                        "on_write_failure": "discard",
                        "options": {
                            "path": str(tmp_path / "output.jsonl"),
                            "format": "jsonl",
                            "schema": {"mode": "fixed", "fields": ["id: str", "x: bool" if normalize else "x: int"]},
                        },
                    }
                },
            }
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


def test_computed_type_cannot_reuse_stale_input_type_as_output_proof(tmp_path):
    with pytest.raises(EdgeContractError) as raised:
        _graph(tmp_path, normalize=False)
    assert raised.value.compatibility_result.type_mismatches == (("x", "int", "typing.Any | None"),)


def test_explicit_type_coerce_establishes_computed_output_type(tmp_path):
    _graph(tmp_path, normalize=True)
