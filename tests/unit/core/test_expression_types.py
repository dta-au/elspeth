"""A derived declaration must admit every successfully evaluated value."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from elspeth.contracts.schema import FieldDefinition
from elspeth.core.expression_parser import ExpressionParser
from elspeth.core.expression_types import declared_result, expression_kinds, input_kinds


@pytest.mark.parametrize(
    ("expression", "row", "types", "expected"),
    [
        ("row['a'] + row['b']", {"a": 2, "b": 3}, {"a": "int", "b": "int"}, ("int", False)),
        ("row['a'] / row['b']", {"a": 2, "b": 3}, {"a": "int", "b": "int"}, ("float", False)),
        ("row['a'] > row['b']", {"a": 2, "b": 3}, {"a": "int", "b": "int"}, ("bool", False)),
        ("not row['a']", {"a": True}, {"a": "bool"}, ("bool", False)),
        ("row['a'] and row['b']", {"a": 2, "b": 3}, {"a": "int", "b": "int"}, ("int", False)),
        ("row['a'] if row['b'] else None", {"a": 2, "b": True}, {"a": "int", "b": "bool"}, ("int", True)),
        ("row.get('a')", {"a": 2}, {"a": "int"}, ("int", True)),
        ("abs(row['a'])", {"a": -2}, {"a": "int"}, ("int", False)),
        ("lower(row['a'])", {"a": "ABC"}, {"a": "str"}, ("str", False)),
        ("len(row['a'])", {"a": "ABC"}, {"a": "str"}, ("int", False)),
        ("row['a']['x']", {"a": {"x": 2}}, {"a": "any"}, ("any", True)),
        ("row['a'] * 2", {"a": 2.0}, {"a": "float"}, ("float", False)),
        ("row['a'] + row['b']", {"a": "x", "b": "y"}, {"a": "str", "b": "str"}, ("str", False)),
        ("row['a'] * 2", {"a": "x"}, {"a": "str"}, ("str", False)),
    ],
)
def test_derived_result_admits_real_evaluation(expression, row, types, expected):
    parser = ExpressionParser(expression)
    fields = {name: FieldDefinition(name=name, field_type=field_type) for name, field_type in types.items()}
    declared = declared_result(expression_kinds(parser, lambda name: input_kinds(fields.get(name))))
    assert declared == expected
    value = parser.evaluate(row)
    if declared[0] != "any":
        if value is None:
            assert declared[1]
        else:
            assert type(value).__name__ == declared[0] or (declared[0] == "float" and type(value) is int)


@settings(max_examples=75)
@given(a=st.integers(min_value=-1000, max_value=1000), b=st.integers(min_value=1, max_value=1000))
def test_numeric_expressions_never_claim_an_unsound_result(a: int, b: int) -> None:
    fields = {name: FieldDefinition(name=name, field_type="int") for name in ("a", "b")}
    for expression in ("row['a'] + row['b']", "row['a'] / row['b']", "row['a'] > row['b']", "row['a'] and row['b']"):
        parser = ExpressionParser(expression)
        field_type, nullable = declared_result(expression_kinds(parser, lambda name: input_kinds(fields.get(name))))
        value = parser.evaluate({"a": a, "b": b})
        assert value is not None or nullable
        if field_type != "any":
            assert type(value).__name__ == field_type or (field_type == "float" and type(value) is int)
