"""Schema diagnostics preserve declared types and reject invalid internal values."""

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from elspeth.web.composer.tools._dispatch import _json_type_label, _schema_error_summary
from elspeth.web.composer.tools.generation import _row_fields_referenced_by_condition


@pytest.mark.parametrize("invalid_value", [42, ["string", 42]])
def test_json_type_label_does_not_coerce_invalid_schema_values(invalid_value: Any) -> None:
    with pytest.raises(TypeError):
        _json_type_label(invalid_value)


def test_schema_error_formats_declared_type_union_without_instance_content() -> None:
    validator = Draft202012Validator({"type": ["string", "null"]})
    error = next(validator.iter_errors({"private": "not for diagnostics"}))
    assert _schema_error_summary(error) == "arguments must be of type string or null"


def test_schema_error_formats_nested_missing_field_without_array_index() -> None:
    validator = Draft202012Validator(
        {
            "type": "object",
            "properties": {
                "nodes": {
                    "type": "array",
                    "items": {"type": "object", "required": ["name"]},
                },
            },
        }
    )
    error = next(validator.iter_errors({"nodes": [{}, {}]}))
    assert _schema_error_summary(error) == "arguments.nodes[].name is missing required property"


def test_schema_error_redacts_nonidentifier_path_segment() -> None:
    validator = Draft202012Validator({"additionalProperties": {"type": "integer"}})
    error = next(validator.iter_errors({"private/path": "not for diagnostics"}))
    assert _schema_error_summary(error) == "arguments.<item> must be of type integer"


def test_required_validator_does_not_produce_missing_field_error_for_scalar() -> None:
    validator = Draft202012Validator({"type": "object", "required": ["name"]})
    errors = list(validator.iter_errors("not an object"))
    assert len(errors) == 1
    assert _schema_error_summary(errors[0]) == "arguments must be of type object"


def test_gate_field_discovery_distinguishes_literal_fields_from_other_ast_values() -> None:
    assert _row_fields_referenced_by_condition(
        "row['direct'] == row.get('optional') and row['direct'] != row[0] and row.get(True) is None"
    ) == ("direct", "optional")


def test_gate_field_discovery_ignores_non_row_receivers() -> None:
    assert _row_fields_referenced_by_condition("other.get('private') == row.get('field')") == ("field",)
