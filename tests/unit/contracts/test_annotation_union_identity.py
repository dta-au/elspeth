"""Typing union recognition must use identity, never a foreign metaclass equality."""

from __future__ import annotations

from operator import getitem
from types import GenericAlias
from typing import Annotated, Union

from pydantic import BaseModel

from elspeth.contracts.emitted_option import EmittedToOutput, _annotation_emitted_values
from elspeth.web.catalog.knob_schema import _dual_form_nested_model, _unwrap_optional
from elspeth.web.config import _coerce_env_scalar


class _EmittedUnionProbe(BaseModel):
    token: Annotated[str, EmittedToOutput("probe token is copied to output")]


def _non_union_alias(*arguments: object) -> tuple[GenericAlias, list[object]]:
    comparisons: list[object] = []

    class EqualToAnyUnion(type):
        def __eq__(cls, other: object) -> bool:
            comparisons.append(other)
            return True

        __hash__ = type.__hash__

    class OrdinaryGenericOrigin(metaclass=EqualToAnyUnion):
        pass

    alias = GenericAlias(OrdinaryGenericOrigin, arguments)
    return alias, comparisons


def test_optional_unwrap_preserves_real_unions_and_refuses_equal_non_union_origin() -> None:
    assert _unwrap_optional(str | None) == (str, True)
    assert _unwrap_optional(getitem(Union, (str, type(None)))) == (str, True)
    alias, comparisons = _non_union_alias(str, type(None))
    inner, nullable = _unwrap_optional(alias)
    assert inner is alias
    assert nullable is False
    assert comparisons == []


def test_dual_form_model_requires_real_union_origin() -> None:
    assert _dual_form_nested_model(list[_EmittedUnionProbe] | dict[str, _EmittedUnionProbe]) == (_EmittedUnionProbe, False)
    assert _dual_form_nested_model(getitem(Union, (list[_EmittedUnionProbe], dict[str, _EmittedUnionProbe], type(None)))) == (
        _EmittedUnionProbe,
        True,
    )
    alias, comparisons = _non_union_alias(list[_EmittedUnionProbe], dict[str, _EmittedUnionProbe])
    assert _dual_form_nested_model(alias) is None
    assert comparisons == []


def test_environment_numeric_coercion_does_not_infer_types_from_equal_non_union_origin() -> None:
    assert _coerce_env_scalar("17", int | None) == 17
    assert _coerce_env_scalar("17", getitem(Union, (int, type(None)))) == 17
    assert _coerce_env_scalar("17", bool | None) == "17"
    alias, comparisons = _non_union_alias(int, type(None))
    assert _coerce_env_scalar("17", alias) == "17"
    assert comparisons == []


def test_emitted_option_walker_does_not_descend_equal_non_union_origin() -> None:
    value = {"token": "literal"}
    expected = [("root.token", "literal", "probe token is copied to output")]
    assert list(_annotation_emitted_values(_EmittedUnionProbe | None, value, "root", for_env_expansion=False)) == expected
    assert (
        list(_annotation_emitted_values(getitem(Union, (_EmittedUnionProbe, type(None))), value, "root", for_env_expansion=False))
        == expected
    )
    alias, comparisons = _non_union_alias(_EmittedUnionProbe, type(None))
    assert list(_annotation_emitted_values(alias, value, "root", for_env_expansion=False)) == []
    assert comparisons == []
