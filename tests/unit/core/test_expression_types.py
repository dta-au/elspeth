"""A derived declaration must admit every successfully evaluated value."""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from elspeth.contracts.schema import FieldDefinition
from elspeth.contracts.schema_contract import declared_type_admits
from elspeth.core.expression_parser import ExpressionEvaluationError, ExpressionParser, ExpressionSecurityError
from elspeth.core.expression_types import declared_result, expression_kinds, input_kinds

_SCALAR_TYPES = {"int": int, "float": float, "str": str, "bool": bool}


@st.composite
def _declared_rows(draw):
    fields = {}
    row = {}
    for name in ("a", "b", "c"):
        field_type = draw(st.sampled_from(tuple(_SCALAR_TYPES)))
        nullable = draw(st.booleans())
        if field_type == "int":
            values = st.integers(min_value=-5, max_value=5)
        elif field_type == "float":
            values = st.one_of(st.integers(min_value=-5, max_value=5), st.floats(min_value=-5, max_value=5, allow_nan=False))
        elif field_type == "str":
            values = st.text(alphabet="abc", max_size=4)
        else:
            values = st.booleans()
        fields[name] = FieldDefinition(name=name, field_type=field_type, nullable=nullable)
        row[name] = draw(st.one_of(st.none(), values) if nullable else values)
    return fields, row


_LEAVES = st.sampled_from(("row['a']", "row['b']", "row['c']", "row.get('a')", "None", "True", "False", "0", "1", "2.0", "'a'"))
_EXPRESSIONS = st.recursive(
    _LEAVES,
    lambda child: st.one_of(
        st.tuples(st.sampled_from(("+", "-", "*", "/", "//", "%", "and", "or", "==", "<")), child, child).map(
            lambda parts: f"({parts[1]} {parts[0]} {parts[2]})"
        ),
        st.tuples(st.sampled_from(("not", "+", "-")), child).map(lambda parts: f"({parts[0]} {parts[1]})"),
        st.tuples(child, child, child).map(lambda parts: f"({parts[0]} if {parts[1]} else {parts[2]})"),
        st.tuples(st.sampled_from(("len", "abs", "lower", "upper", "strip", "casefold")), child).map(
            lambda parts: f"{parts[0]}({parts[1]})"
        ),
        child.map(lambda item: f"[{item}]"),
        child.map(lambda item: f"({item},)"),
        child.map(lambda item: f"{{'item': {item}}}"),
        child.map(lambda item: f"{{{item}}}"),
        child.map(lambda item: f"({item})['item']"),
    ),
    max_leaves=16,
)


@settings(max_examples=350, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(declared_row=_declared_rows(), expression=_EXPRESSIONS)
def test_generated_grammar_result_is_sound_for_successful_evaluations(declared_row, expression):
    """Successful real evaluations must fit the derived type, including nullable inputs."""
    fields, row = declared_row
    try:
        parser = ExpressionParser(expression)
        value = parser.evaluate(row)
    except (ExpressionSecurityError, ExpressionEvaluationError, ValueError, TypeError, KeyError, IndexError, ArithmeticError):
        return
    derived_type, nullable = declared_result(expression_kinds(parser, lambda name: input_kinds(fields.get(name))))
    if value is None:
        assert nullable, expression
    elif derived_type != "any":
        assert declared_type_admits(_SCALAR_TYPES[derived_type], type(value)), (expression, row, derived_type, value)


@pytest.mark.parametrize(
    ("expression", "fields", "expected"),
    [
        ("row['a'] if row['b'] else row['c']", {"a": "int", "b": "bool", "c": "float"}, ("float", False)),
        ("row['a'] / row['b']", {"a": "int", "b": "int"}, ("float", False)),
        ("row['a'] or row['b']", {"a": "bool", "b": "int"}, ("any", True)),
        ("row['a']", {"a": "bool"}, ("bool", False)),
    ],
    ids=("int-float-widens", "division-is-float", "boolop-uses-all-operands", "bool-is-not-an-int-declaration"),
)
def test_typer_mutant_controls(expression, fields, expected):
    definitions = {name: FieldDefinition(name=name, field_type=field_type) for name, field_type in fields.items()}
    actual = declared_result(expression_kinds(ExpressionParser(expression), lambda name: input_kinds(definitions.get(name))))
    assert actual == expected


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
