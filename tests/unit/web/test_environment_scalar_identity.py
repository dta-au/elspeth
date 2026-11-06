"""Environment type classification must not invoke a foreign metaclass hash/equality."""

from __future__ import annotations

from operator import getitem
from typing import Union

import pytest
from pydantic.fields import FieldInfo

from elspeth.web.config import WebSettings, _annotation_scalar_types, _coerce_env_scalar


@pytest.mark.parametrize(
    "name,value,expected",
    [
        ("composer_max_tool_calls_per_turn", "17", 17),
        ("composer_timeout_seconds", "17.5", 17.5),
        ("composer_temperature", "0.5", 0.5),
        ("quota_default_tokens_per_day", "17", 17),
        ("composer_boot_probe_enabled", "true", "true"),
    ],
)
def test_environment_coercion_uses_actual_current_field_annotations(name, value, expected):
    field = WebSettings.model_fields[name]
    assert type(field) is FieldInfo
    actual = _coerce_env_scalar(value, field.annotation)
    assert type(actual) is type(expected)
    assert actual == expected


@pytest.mark.parametrize("target", [int, float, bool])
def test_non_numeric_class_equal_to_a_numeric_type_keeps_the_original_value_without_comparison(target):
    assert _coerce_env_scalar("17", int | None) == 17
    assert _coerce_env_scalar("17", getitem(Union, (float, type(None)))) == 17.0
    effects: list[str] = []

    class NumericImpostorMeta(type):
        def __hash__(cls):
            effects.append("hash")
            return hash(target)

        def __eq__(cls, other):
            effects.append("equality")
            return cls is other or other is target

    class OrdinaryNonNumeric(metaclass=NumericImpostorMeta):
        pass

    assert OrdinaryNonNumeric is not target
    assert type(OrdinaryNonNumeric) is NumericImpostorMeta
    value = "17"
    assert _coerce_env_scalar(value, OrdinaryNonNumeric) is value
    assert effects == []


def test_environment_classification_does_not_hash_or_compare_an_annotation_class():
    assert _coerce_env_scalar("17.5", float) == 17.5

    class EffectRefusingMeta(type):
        def __hash__(cls):
            raise AssertionError("annotation metaclass hash executed")

        def __eq__(cls, other):
            raise AssertionError("annotation metaclass equality executed")

    class OrdinaryNonNumeric(metaclass=EffectRefusingMeta):
        pass

    assert type(OrdinaryNonNumeric) is EffectRefusingMeta
    value = "17.5"
    assert _coerce_env_scalar(value, OrdinaryNonNumeric) is value
    retained = _annotation_scalar_types(OrdinaryNonNumeric)
    assert len(retained) == 1
    assert retained[0] is OrdinaryNonNumeric


@pytest.mark.parametrize("annotation", [int | float | None, getitem(Union, (int, float, type(None)))])
def test_annotation_normalizer_keeps_actual_type_identities_and_coercion_precedence(annotation):
    retained = _annotation_scalar_types(annotation)
    expected = (int, float, type(None))
    assert len(retained) == len(expected)
    assert all(actual is wanted for actual, wanted in zip(retained, expected, strict=True))
    assert type(_coerce_env_scalar("17", annotation)) is float
    assert _coerce_env_scalar("17", bool | int | float) == "17"
    value = "not numeric"
    assert _coerce_env_scalar(value, annotation) is value
