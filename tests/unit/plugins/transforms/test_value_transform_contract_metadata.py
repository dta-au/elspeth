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


def _verify_declaration_only(transform: ValueTransform, row: PipelineRow) -> None:
    """The ADR-014 output-declaration check alone."""
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


def _verify(transform: ValueTransform, row: PipelineRow) -> None:
    _verify_declaration_only(transform, row)
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


@pytest.mark.parametrize("expression, expected", [("row['X'] > 0", True), ("None", None)])
def test_untyped_computed_target_declares_presence_as_any_before_row_one(tmp_path, expression, expected):
    """The arriving int is not an output proof: an untyped target is declared ``any``, never typed by a row.

    ADR-050 (reconciled with 63a2e1825): the declaration is fixed before the
    first row, so the recorded contract is ``object``/``declared``/nullable on
    every row whatever the expression computes; the value is written as
    computed.
    """
    row = _csv_row(tmp_path, declared=True, numeric=True)
    transform = ValueTransform(
        {
            "schema": {"mode": "flexible", "fields": ["id: str"]},
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
    assert field.python_type is object
    assert field.source == "declared"
    assert field.original_name == "X"
    assert field.required is True
    assert field.nullable is True
    output = transform._output_schema_config
    assert output is not None and output.fields is not None
    declared = next(field for field in output.fields if field.name == "x")
    assert declared.field_type == "any"
    assert declared.required is True
    assert declared.nullable is True
    assert "x" in output.get_effective_guaranteed_fields()


def test_an_operator_authored_any_target_is_projected_as_a_guaranteed_nullable_any():
    """A target the operator declared ``any`` (or ``any?``) is written on every row and may compute None.

    The projection therefore records it required AND nullable whatever
    presence the author wrote, and the node's output schema (the producer
    side of every build-time edge) carries ``Any | None``, required. A target
    absent from ``schema.fields`` never reaches this rewrite —
    ``declare_missing_guaranteed_fields`` already yields ``any``, required,
    nullable — so only an authored ``any`` observes it (review-REBASE2 L1).
    """
    transform = ValueTransform(
        {
            "schema": {"mode": "flexible", "fields": ["id: str", "x: any", "y: any?"]},
            "operations": [{"target": "x", "expression": "1"}, {"target": "y", "expression": "None"}],
        }
    )
    output = transform._output_schema_config
    assert output is not None and output.fields is not None
    projected = {field.name: (field.field_type, field.required, field.nullable) for field in output.fields}
    assert projected == {"id": ("str", True, False), "x": ("any", True, True), "y": ("any", True, True)}
    produced = {name: (str(info.annotation), info.is_required()) for name, info in transform.output_schema.model_fields.items()}
    assert produced["x"] == produced["y"] == ("typing.Any | None", True)


@pytest.mark.parametrize("expression, actual", [("row['X'] > 0", "bool"), ("None", "NoneType")])
def test_typed_computed_target_is_pinned_to_the_operators_declaration(tmp_path, expression, actual):
    """A target the node's schema TYPES is the operator's output declaration (ADR-050: operator > plugin > any).

    The projection keeps ``x: int``, and a computed value of another type is
    that row's routed ``type_mismatch`` error — value-free — never a record
    that advertises ``int`` over a ``bool``.
    """
    row = _csv_row(tmp_path, declared=True, numeric=True)
    transform = ValueTransform(
        {
            "schema": {"mode": "flexible", "fields": ["id: str", "x: int"]},
            "operations": [{"target": "x", "expression": expression}],
        }
    )
    transform.input_schema.model_validate(row.to_dict(), strict=True)
    result = transform.process(row, make_context())
    assert result.status == "error"
    assert result.row is None
    assert result.reason is not None
    assert result.reason["reason"] == "type_mismatch"
    assert result.reason["field"] == "x"
    assert result.reason["expected"] == "int"
    assert result.reason["actual"] == actual
    output = transform._output_schema_config
    assert output is not None and output.fields is not None
    declared = next(field for field in output.fields if field.name == "x")
    assert declared.field_type == "int"
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
def test_forwarded_float_declaration_is_stamped_and_never_aborts(value):
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
    # The value is never coerced: the executor forwards the original payload.
    assert type(result.row["x"]) is type(value)
    # ADR-050 D7: the operator's declaration of a forwarded field is stamped on
    # the emitted contract, so the ADR-014 check passes and the run does not
    # abort on a row the strict input check admitted (63a2e1825 left this int
    # case raising SchemaConfigModeViolation, a Tier-1 abort).
    field = result.row.contract.get_field("x")
    assert field.python_type is float
    assert field.source == "declared"
    _verify_declaration_only(transform, result.row)
    # The admission split 63a2e1825 recorded as open is resolved (ruling
    # 2026-09-25, C3): an int value SATISFIES a float declaration under the
    # one rule SchemaContract.validate shares with pydantic strict, so the
    # recorded ``float, declared`` is true of the int row as well, with no
    # value conversion.
    assert result.row.contract.validate(result.row.to_dict()) == []


_TYPED_TARGET_FIELDS = ("id: str", "x: int")
_UNTYPED_TARGET_FIELDS = ("id: str",)


def _calculate_options(fields: tuple[str, ...]) -> dict[str, object]:
    return {
        "schema": {"mode": "flexible", "fields": list(fields)},
        "operations": [{"target": "x", "expression": "row['x'] > 0"}],
    }


def _graph(tmp_path: Path, *, normalize: bool, calculate_fields: tuple[str, ...] = _TYPED_TARGET_FIELDS) -> ExecutionGraph:
    transforms = [
        {
            "name": "calculate",
            "plugin": "value_transform",
            "input": "raw",
            "on_success": "calculated" if normalize else "output",
            "on_error": "discard",
            "options": _calculate_options(calculate_fields),
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


def test_untyped_computed_target_cannot_reuse_the_arriving_type_as_output_proof(tmp_path):
    """The arriving ``x: int`` (source-declared) proves nothing about the computed ``x``: the edge refuses at build."""
    with pytest.raises(EdgeContractError) as raised:
        _graph(tmp_path, normalize=False, calculate_fields=_UNTYPED_TARGET_FIELDS)
    assert raised.value.compatibility_result.type_mismatches == (("x", "int", "typing.Any | None"),)


def test_typed_computed_target_is_the_output_proof_the_pin_enforces(tmp_path):
    """A target the node's schema types is the output declaration (ADR-050), so the typed edge builds.

    The proof is honest because it is enforced: the same node returns a row
    whose computed value breaks the declaration as a routed ``type_mismatch``
    error instead of emitting it under ``int``.
    """
    _graph(tmp_path, normalize=False)

    transform = ValueTransform(_calculate_options(_TYPED_TARGET_FIELDS))
    row = PipelineRow(
        {"id": "a", "x": 2},
        SchemaContract(
            mode="FIXED",
            fields=(
                FieldContract("id", "id", str, required=True, source="declared", nullable=False),
                FieldContract("x", "x", int, required=True, source="declared", nullable=False),
            ),
            locked=True,
        ),
    )
    result = transform.process(row, make_context())
    assert result.status == "error"
    assert result.reason is not None
    assert (result.reason["reason"], result.reason["field"], result.reason["expected"], result.reason["actual"]) == (
        "type_mismatch",
        "x",
        "int",
        "bool",
    )


def test_explicit_type_coerce_establishes_computed_output_type(tmp_path):
    _graph(tmp_path, normalize=True)
    _graph(tmp_path, normalize=True, calculate_fields=_UNTYPED_TARGET_FIELDS)
