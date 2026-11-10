"""The gate-preview mismatch diagnostic must be truthful per ExpressionEvaluationKind."""

from typing import Any, get_args

import pytest

from elspeth.core.expression_parser import ExpressionEvaluationKind
from elspeth.web.composer.tools.generation import _gate_evaluation_failure_text

_ALL_KINDS = get_args(ExpressionEvaluationKind)
_NON_TYPING_KINDS = tuple(kind for kind in _ALL_KINDS if kind != "incompatible_types")


def _text(kind: ExpressionEvaluationKind, field: str | None = "price") -> tuple[str, str]:
    return _gate_evaluation_failure_text(kind, node_id="g", condition="row['price'] >= 100", field=field)


def test_kind_literal_is_the_expected_closed_set() -> None:
    # Positive control for the exhaustiveness tests below: a Literal that
    # silently became empty would make every parametrized test vacuous.
    assert len(_ALL_KINDS) == 8
    assert "missing_key" in _ALL_KINDS and "unexpected_error" in _ALL_KINDS


@pytest.mark.parametrize("kind", _ALL_KINDS)
def test_every_kind_has_distinct_value_free_text(kind: ExpressionEvaluationKind) -> None:
    message, repair = _text(kind)
    assert message.strip() and repair.strip()
    assert "g" in message and "row['price'] >= 100" in message
    assert "withheld" in message  # the raw evaluator text stays withheld


def test_texts_are_distinct_per_kind() -> None:
    texts = {_text(kind) for kind in _ALL_KINDS}
    assert len(texts) == len(_ALL_KINDS)


def test_incompatible_types_keeps_numeric_typing_repair() -> None:
    _, repair = _text("incompatible_types")
    assert "explicit numeric type" in repair
    assert "price: int" in repair
    _, repair_no_field = _text("incompatible_types", field=None)
    assert "<field>: int" in repair_no_field


@pytest.mark.parametrize("kind", _NON_TYPING_KINDS)
def test_other_kinds_do_not_prescribe_numeric_typing(kind: ExpressionEvaluationKind) -> None:
    message, repair = _text(kind)
    text = f"{message} {repair}".lower()
    assert "numeric" not in text
    assert ": int" not in text
    assert "schema.fields" not in text


def test_missing_key_points_at_field_names_and_inspect_source() -> None:
    _, repair = _text("missing_key")
    assert "inspect_source" in repair
    assert "field name" in repair.lower()


def test_division_by_zero_points_at_the_divisor() -> None:
    _, repair = _text("division_by_zero")
    assert "divisor" in repair.lower()


def test_unexpected_error_does_not_claim_a_typing_mismatch() -> None:
    message, _ = _text("unexpected_error")
    assert "not a field-typing" in message.lower()


def test_unknown_kind_is_refused_not_defaulted() -> None:
    unknown: Any = "not_a_kind"
    with pytest.raises(AssertionError):
        _gate_evaluation_failure_text(unknown, node_id="g", condition="c", field=None)
