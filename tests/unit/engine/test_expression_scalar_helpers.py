"""Strict scalar helpers close mechanical checks without opening Python calls."""

from __future__ import annotations

from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from elspeth.core.config import GateSettings
from elspeth.core.expression_parser import ExpressionEvaluationError, ExpressionParser, ExpressionSecurityError
from elspeth.core.expression_types import declared_result, expression_kinds


@pytest.mark.parametrize(
    ("value", "prefix", "expected"),
    [
        ("Status: pass", "Status:", True),
        ("Earlier Status: pass", "Status:", False),
        (" Status: pass", "Status:", False),
        ("status: pass", "Status:", False),
        ("\u00e9clair", "\u00e9", True),
        ("", "", True),
        ("abc", "", True),
        ("", "a", False),
    ],
)
def test_starts_with_is_exact_leading_text(value: str, prefix: str, expected: bool) -> None:
    parser = ExpressionParser("starts_with(row['value'], row['prefix'])")
    assert parser.evaluate({"value": value, "prefix": prefix}) is expected
    assert parser.is_boolean_expression() and not parser.is_provably_non_routable()
    assert declared_result(expression_kinds(parser, lambda name: None)) == ("bool", False)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("", 0), (" \t\r\n", 0), ("one", 1), ("one two", 2), (" one\t two\nthree ", 3), ("one\u2003two\u00a0three", 3), ("one-two,three", 1)],
)
def test_word_count_uses_whitespace_tokens(text: str, expected: int) -> None:
    parser = ExpressionParser("word_count(row['text'])")
    assert parser.evaluate({"text": text}) == expected
    assert not parser.is_boolean_expression() and parser.is_provably_non_routable()
    assert declared_result(expression_kinds(parser, lambda name: None)) == ("int", False)


@pytest.mark.parametrize(
    ("left", "right", "tolerance", "expected"),
    [
        (2.35, 2.30, 0.05, True),
        (2.30, 2.35, 0.05, True),
        (2.349, 2.30, 0.05, True),
        (2.351, 2.30, 0.05, False),
        (-2.35, -2.30, 0.05, True),
        (2, 2.0, 0, True),
        (2.01, 2, 0, False),
        (1e-300, 2e-300, 1e-300, True),
        (10**1000, 10**1000 + 1, 1, True),
        (10**1000, 10**1000 + 2, 1, False),
    ],
    ids=["inclusive", "reverse", "inside", "outside", "negative", "zero", "nonzero", "scientific", "large-int-inside", "large-int-outside"],
)
def test_decimal_within_compares_numeric_decimal_spellings_inclusively(left, right, tolerance, expected: bool) -> None:
    parser = ExpressionParser("decimal_within(row['left'], row['right'], row['tolerance'])")
    with localcontext() as context:
        context.prec = 2  # Ambient Decimal arithmetic cannot change this helper.
        assert parser.evaluate({"left": left, "right": right, "tolerance": tolerance}) is expected
    assert parser.is_boolean_expression() and not parser.is_provably_non_routable()
    assert declared_result(expression_kinds(parser, lambda name: None)) == ("bool", False)


def test_decimal_boundary_against_independent_decimal_oracle() -> None:
    parser = ExpressionParser("decimal_within(row['left'], row['right'], 0.05)")
    with localcontext() as context:
        context.prec = 40
        for hundredths in range(-100, 101):
            left = hundredths / 100
            for delta in (-0.051, -0.05, -0.049, 0, 0.049, 0.05, 0.051):
                right = (hundredths + int(Decimal(str(delta)) * 1000) / 10) / 100
                expected = abs(Decimal(str(left)) - Decimal(str(right))) <= Decimal("0.05")
                assert parser.evaluate({"left": left, "right": right}) is expected, (left, right)


def test_integer_numerator_then_one_division_preserves_the_entire_bounded_hundredth_domain() -> None:
    """A generic arithmetic control, not execution of an authored pipeline."""
    parser = ExpressionParser("decimal_within(row['calculated'], row['recorded'], 0.05)")
    with localcontext() as context:
        context.prec = 40
        for numerator in range(100, 501):
            calculated = numerator / 100
            exact = Decimal(numerator) / 100
            assert Decimal(str(calculated)) == exact
            for thousandths_delta in range(-51, 52):
                recorded = (numerator * 10 + thousandths_delta) / 1000
                expected = abs(exact - Decimal(str(recorded))) <= Decimal("0.05")
                assert parser.evaluate({"calculated": calculated, "recorded": recorded}) is expected, (numerator, thousandths_delta)


@pytest.mark.parametrize(
    ("expression", "row"),
    [
        ("starts_with(row['v'], 'x')", {"v": 1}),
        ("starts_with('x', row['v'])", {"v": ("x", "y")}),
        ("starts_with('x', row['v'])", {"v": b"private-value"}),
        ("word_count(row['v'])", {"v": None}),
        ("word_count(row['v'])", {"v": ["private-value"]}),
        ("word_count(row['v'])", {"v": True}),
        ("decimal_within(row['v'], 2, 0.05)", {"v": "private-value"}),
        ("decimal_within(row['v'], 2, 0.05)", {"v": True}),
        ("decimal_within(row['v'], 2, 0.05)", {"v": float("nan")}),
        ("decimal_within(2, row['v'], 0.05)", {"v": float("inf")}),
        ("decimal_within(2, 2, row['v'])", {"v": -0.05}),
    ],
)
def test_helpers_refuse_wrong_data_without_echoing_values(expression: str, row) -> None:
    with pytest.raises(ExpressionEvaluationError) as failure:
        ExpressionParser(expression).evaluate(row)
    assert "private-value" not in str(failure.value)


@pytest.mark.parametrize(
    "expression",
    [
        "starts_with('a')",
        "starts_with('a', 'a', 0)",
        "starts_with('a', prefix='a')",
        "word_count('a', ' ')",
        "decimal_within(1, 1)",
        "row['x'].startswith('a')",
        "row['x'].split()",
        "row['x'][:2]",
        "starts_with",
        "str(row['x'])",
    ],
)
def test_helpers_preserve_closed_call_and_attribute_grammar(expression: str) -> None:
    with pytest.raises(ExpressionSecurityError):
        ExpressionParser(expression)


def test_gate_routing_and_allocation_classifications_keep_boolean_helpers_distinct() -> None:
    predicate = "starts_with(row['text'], 'A') and starts_with(row['other'], 'B')"
    parser = ExpressionParser(predicate)
    assert not parser.has_repeated_string_copying_calls()
    assert not ExpressionParser("[starts_with(row['text'], 'A')]").has_eager_container_string_allocation_risk()
    assert ExpressionParser("starts_with(lower(row['text']), lower(row['other']))").has_repeated_string_copying_calls()
    assert ExpressionParser("starts_with(row['text'] * row['n'], 'A')").has_string_amplification_risk()
    GateSettings(name="g", input="in", condition=predicate, routes={"true": "accepted", "false": "failed"})
    with pytest.raises(ValidationError):
        GateSettings(name="g", input="in", condition="word_count(row['text'])", routes={"true": "accepted", "false": "failed"})
