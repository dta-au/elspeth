"""Output declarations belong to TypeCoerce, while observed aliases survive."""

from dataclasses import replace

import pytest

from elspeth.contracts.errors import SchemaConfigModeViolation
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.engine.executors.schema_config_mode import verify_schema_config_mode
from elspeth.plugins.transforms.type_coerce import TypeCoerce
from elspeth.testing import make_field
from tests.fixtures.factories import make_context


def _verify(transform: TypeCoerce, row: PipelineRow) -> None:
    assert transform._output_schema_config is not None
    verify_schema_config_mode(
        output_schema_config=transform._output_schema_config,
        emitted_rows=[row],
        plugin_name=transform.name,
        node_id="coerce",
        run_id="run",
        row_id="row",
        token_id="token",
    )


@pytest.mark.parametrize("input_value", ["42", 42])
def test_declared_conversion_and_passthrough_fields_keep_aliases_and_honest_metadata(input_value):
    transform = TypeCoerce(
        {
            "schema": {
                "mode": "flexible",
                "fields": ["id: str", "quantity: any", {"name": "note", "type": "str", "required": False, "nullable": True}],
            },
            "conversions": [{"field": "quantity", "to": "int"}],
        }
    )
    # Downstream row lookup must retain the original CSV header after this
    # transform strengthens its output declaration metadata.
    contract = SchemaContract(
        mode="OBSERVED",
        fields=(
            make_field("id", str, original_name="ID", required=False, source="inferred"),
            make_field("quantity", type(input_value), original_name="Quantity", required=False, source="inferred", nullable=True),
            make_field("note", str, required=True, source="inferred", nullable=False),
            make_field("extra", str, original_name="Extra", required=False, source="inferred"),
        ),
        locked=True,
    )
    row = PipelineRow({"id": "a", "quantity": input_value, "note": "kept", "extra": "untouched"}, contract)
    result = transform.process(row, make_context())
    assert result.row is not None
    assert result.row["Quantity"] == 42
    assert result.row["ID"] == "a"
    assert result.row.contract.get_field("quantity").python_type is int
    assert result.row.contract.get_field("id").required is True
    assert result.row.contract.get_field("note").required is False
    assert result.row.contract.get_field("note").nullable is True
    assert result.row.contract.get_field("extra") == contract.get_field("extra")
    assert row.contract == contract
    assert not result.row.contract.validate(result.row.to_dict())
    _verify(transform, result.row)
    assert result.row.contract.get_field("quantity").nullable is False
    assert result.row.contract.get_field("quantity").required is True


def test_emitter_fix_does_not_weaken_the_executor_metadata_guard():
    transform = TypeCoerce({"schema": {"mode": "flexible", "fields": ["x: str"]}, "conversions": [{"field": "x", "to": "int"}]})
    row = PipelineRow({"x": "4"}, SchemaContract(mode="OBSERVED", fields=(make_field("x", str, required=False),), locked=True))
    result = transform.process(row, make_context())
    assert result.row is not None
    _verify(transform, result.row)
    corrupt = replace(result.row.contract, fields=(replace(result.row.contract.get_field("x"), required=False),))
    with pytest.raises(SchemaConfigModeViolation, match="metadata mismatches"):
        _verify(transform, PipelineRow(result.row.to_dict(), corrupt))
