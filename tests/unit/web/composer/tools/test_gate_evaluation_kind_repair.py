"""The gate-preview mismatch diagnostic must be truthful per ExpressionEvaluationKind."""

from typing import Any, get_args

import pytest

from elspeth.core.expression_parser import ExpressionEvaluationKind
from elspeth.web.composer.tools.generation import _gate_evaluation_failure_text

_ALL_KINDS = get_args(ExpressionEvaluationKind)
_NON_TYPING_KINDS = tuple(kind for kind in _ALL_KINDS if kind != "incompatible_types")


def _text(kind: ExpressionEvaluationKind, field: str | None = "price", absent_fields: tuple[str, ...] = ()) -> tuple[str, str]:
    return _gate_evaluation_failure_text(kind, node_id="g", condition="row['price'] >= 100", field=field, absent_fields=absent_fields)


# The instruction that belongs to exactly one kind. Each phrase must appear in
# its own kind's text and in no other kind's, so swapping any two kinds' texts
# turns a test red (without pinning a whole paragraph byte for byte).
_DISTINGUISHING_PHRASE: dict[str, str] = {
    "incompatible_types": "explicit numeric type",
    "missing_key": "shorter than the header",
    "index_out_of_range": "length first",
    "division_by_zero": "divisor",
    "arithmetic_overflow": "too large to represent",
    "invalid_value": "decimal_within",
    "non_finite_result": "NaN or infinity",
    "unexpected_error": "memory exhaustion",
}


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


def test_distinguishing_phrase_table_covers_every_kind() -> None:
    assert set(_DISTINGUISHING_PHRASE) == set(_ALL_KINDS)


@pytest.mark.parametrize("kind", _ALL_KINDS)
def test_each_kind_carries_its_own_instruction(kind: ExpressionEvaluationKind) -> None:
    message, repair = _text(kind)
    assert _DISTINGUISHING_PHRASE[kind] in f"{message} {repair}"


@pytest.mark.parametrize("kind", _ALL_KINDS)
@pytest.mark.parametrize("other", _ALL_KINDS)
def test_no_kind_carries_another_kinds_instruction(kind: ExpressionEvaluationKind, other: ExpressionEvaluationKind) -> None:
    if kind == other:
        return
    message, repair = _text(kind)
    assert _DISTINGUISHING_PHRASE[other] not in f"{message} {repair}"


def test_missing_key_names_both_causes_and_inspect_source() -> None:
    message, repair = _text("missing_key")
    assert "inspect_source" in repair
    assert "field name" in repair.lower()
    assert "not a column of the source" in message
    assert "shorter than the header" in message
    assert "first rows of the file" in message  # the check is over a sample
    assert "row.get(" in repair  # an exit when the names are right but rows are short


def test_division_by_zero_points_at_the_divisor() -> None:
    _, repair = _text("division_by_zero")
    assert "divisor" in repair.lower()


@pytest.mark.parametrize("kind", ("arithmetic_overflow", "invalid_value", "non_finite_result"))
def test_repairs_advise_only_constructs_the_gate_grammar_allows(kind: ExpressionEvaluationKind) -> None:
    # int()/float() and ** are rejected by the gate expression validator, so a
    # repair must not point at them as the cause or as something to avoid.
    message, repair = _text(kind)
    text = f"{message} {repair}"
    for forbidden in ("int()", "float()", "power", "**"):
        assert forbidden not in text


def test_invalid_value_names_the_one_reachable_source() -> None:
    _, repair = _text("invalid_value")
    assert "decimal_within" in repair
    assert "non-negative tolerance" in repair


def test_incompatible_types_with_an_absent_field_checks_the_name_before_typing() -> None:
    message, repair = _text("incompatible_types", field="prcie", absent_fields=("prcie",))
    assert "'prcie'" in message and "yields None" in message
    assert "does not carry" in message
    assert "inspect_source" in repair
    # The name check comes first, and the typing repair is conditional, not certain.
    assert repair.index("inspect_source") < repair.index("explicit numeric type")
    assert "Only if the names are right" in repair


def test_incompatible_types_without_an_absent_field_is_the_unconditional_typing_repair() -> None:
    message, repair = _text("incompatible_types", absent_fields=())
    assert "yields None" not in message
    assert "inspect_source" not in repair
    assert "Only if" not in repair


def test_unexpected_error_does_not_claim_a_typing_mismatch() -> None:
    message, _ = _text("unexpected_error")
    assert "not a field-typing" in message.lower()


def test_unknown_kind_is_refused_not_defaulted() -> None:
    unknown: Any = "not_a_kind"
    with pytest.raises(AssertionError):
        _gate_evaluation_failure_text(unknown, node_id="g", condition="c", field=None, absent_fields=())
