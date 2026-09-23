"""Value-free rendering of Pydantic validation failures (elspeth-a300402c58, elspeth-5887fb7928).

``str(ValidationError)`` echoes the offending INPUT VALUE by default, and even
without the ``input`` echo, Pydantic's ``msg`` (custom validator text), the
``loc`` of a dict-typed field (the row's own dict KEY), a ``loc[0]`` that a
model validator or typed extras fill with a row key, and a
``PydanticCustomError`` type string can each carry row content. The renderer
emits only a top-level name the validated schema declares, placeholders for
everything else, and pydantic-core's own type codes.
"""

from __future__ import annotations

from typing import Any, Self

import pytest
from pydantic import (
    AliasChoices,
    AliasPath,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from elspeth.contracts.data import PluginSchema
from elspeth.contracts.safe_validation_errors import safe_validation_error_text

SECRET = "SECRET-abc123"


class _Model(PluginSchema):
    amount: int
    name: str


class _MapModel(PluginSchema):
    scores: dict[str, int]


class _IntKeyMapModel(PluginSchema):
    scores: dict[int, str]


class _ListModel(PluginSchema):
    tags: list[int]


class _Nested(PluginSchema):
    code: int


class _NestedModel(PluginSchema):
    inner: _Nested


class _EchoingValidatorModel(PluginSchema):
    customer: str

    @field_validator("customer")
    @classmethod
    def _reject(cls, value: str) -> str:
        raise ValueError(f"Invalid customer {value}")


class _CustomTypeModel(PluginSchema):
    customer: str

    @field_validator("customer")
    @classmethod
    def _reject(cls, value: str) -> str:
        raise PydanticCustomError(f"customer_{value}", "customer {value} rejected", {"value": value})


class _ForbidModel(PluginSchema):
    model_config = ConfigDict(extra="forbid")
    amount: int


class _AliasModel(PluginSchema):
    amount: int = Field(alias="Amount")


class _PopulateByNameAliasModel(PluginSchema):
    model_config = ConfigDict(populate_by_name=True)
    amount: int = Field(alias="Amount")


class _ValidationAliasModel(PluginSchema):
    amount: int = Field(validation_alias="AmountIn")


class _AliasChoicesModel(PluginSchema):
    amount: int = Field(validation_alias=AliasChoices("amount_a", AliasPath("amounts", 0)))


class _AliasPathModel(PluginSchema):
    amount: int = Field(validation_alias=AliasPath("totals", 0, "amount"))


class _AfterReRaisingModel(PluginSchema):
    """An after-validator re-raises an inner ValidationError: its dict KEY lands at ``loc[0]``."""

    scores: dict[str, Any]

    @model_validator(mode="after")
    def _scores_are_ints(self) -> Self:
        TypeAdapter(dict[str, int]).validate_python(self.scores)
        return self


class _BeforeReRaisingModel(PluginSchema):
    """A before-validator re-raises an inner ValidationError: its dict KEY lands at ``loc[0]``."""

    scores: dict[str, Any]

    @model_validator(mode="before")
    @classmethod
    def _scores_are_ints(cls, data: Any) -> Any:
        TypeAdapter(dict[str, int]).validate_python(data["scores"])
        return data


class _ListReRaisingModel(PluginSchema):
    """A re-raised inner list validation puts an int index at ``loc[0]``."""

    tags: list[Any]

    @model_validator(mode="after")
    def _tags_are_ints(self) -> Self:
        TypeAdapter(list[int]).validate_python(self.tags)
        return self


class _CollidingReRaisingModel(PluginSchema):
    """A re-raised row key that happens to equal a declared name renders as that declared name."""

    amount: int
    scores: dict[str, Any]

    @model_validator(mode="after")
    def _scores_are_ints(self) -> Self:
        TypeAdapter(dict[str, int]).validate_python(self.scores)
        return self


class _TypedExtrasModel(PluginSchema):
    """Typed extras report an undeclared row key at ``loc[0]`` under an ordinary type code."""

    model_config = ConfigDict(extra="allow")
    __pydantic_extra__: dict[str, int] = Field(init=False)
    amount: int


def _validation_error(model: type[PluginSchema], payload: object) -> ValidationError:
    try:
        model.model_validate(payload)
    except ValidationError as exc:
        return exc
    raise AssertionError("model_validate unexpectedly succeeded")


def _render(model: type[PluginSchema], payload: object) -> str:
    return safe_validation_error_text(_validation_error(model, payload), model)


class TestSafeValidationErrorText:
    def test_premise_str_echoes_input_value(self) -> None:
        """Guard the premise: str(ValidationError) DOES echo the input value.

        If Pydantic ever stops echoing input by default this boundary can be
        revisited — this pin makes that change visible.
        """
        assert SECRET in str(_validation_error(_Model, {"amount": SECRET, "name": "ok"}))

    def test_renders_the_declared_field_and_type_code_only(self) -> None:
        assert _render(_Model, {"amount": SECRET, "name": "ok"}) == "1 validation error: amount: [int_parsing]"

    def test_multiple_errors_all_rendered(self) -> None:
        assert _render(_Model, {"amount": SECRET}) == "2 validation errors: amount: [int_parsing]; name: [missing]"

    def test_root_level_error_renders_placeholder_loc(self) -> None:
        assert _render(_Model, SECRET) == "1 validation error: <root>: [model_type]"

    @pytest.mark.parametrize(
        ("model", "payload"),
        [
            pytest.param(_EchoingValidatorModel, {"customer": SECRET}, id="custom-validator-msg"),
            pytest.param(_MapModel, {"scores": {SECRET: "not-an-int"}}, id="dict-key-in-loc"),
            pytest.param(_CustomTypeModel, {"customer": SECRET}, id="custom-error-type-code"),
            pytest.param(_ForbidModel, {"amount": 1, SECRET: 1}, id="undeclared-key-in-loc"),
            pytest.param(_Model, {"amount": SECRET, "name": "ok"}, id="plain-input-echo"),
            pytest.param(_AfterReRaisingModel, {"scores": {SECRET: "not-an-int"}}, id="after-validator-reraise"),
            pytest.param(_BeforeReRaisingModel, {"scores": {SECRET: "not-an-int"}}, id="before-validator-reraise"),
            pytest.param(_TypedExtrasModel, {"amount": 1, SECRET: "not-an-int"}, id="typed-extras-key"),
        ],
    )
    def test_no_row_content_reaches_the_text(self, model: type[PluginSchema], payload: object) -> None:
        exc = _validation_error(model, payload)
        assert SECRET in str(exc), "positive control: pydantic's own rendering carries the sentinel"
        assert SECRET not in safe_validation_error_text(exc, model)

    def test_custom_validator_message_is_dropped_type_code_kept(self) -> None:
        assert _render(_EchoingValidatorModel, {"customer": SECRET}) == "1 validation error: customer: [value_error]"

    def test_dict_key_renders_as_a_placeholder(self) -> None:
        assert _render(_MapModel, {"scores": {SECRET: "not-an-int"}}) == "1 validation error: scores.[item]: [int_parsing]"

    def test_an_int_dict_key_is_not_rendered_either(self) -> None:
        """An int in ``loc`` can be a row's dict key, not only a list index."""
        exc = _validation_error(_IntKeyMapModel, {"scores": {582971: 5}})
        assert "582971" in str(exc), "positive control"
        assert safe_validation_error_text(exc, _IntKeyMapModel) == "1 validation error: scores.[item]: [string_type]"

    def test_list_index_and_nested_field_render_as_placeholders(self) -> None:
        assert _render(_ListModel, {"tags": [1, "x"]}) == "1 validation error: tags.[item]: [int_parsing]"
        assert _render(_NestedModel, {"inner": {"code": "x"}}) == "1 validation error: inner.[item]: [int_parsing]"

    def test_custom_error_type_code_is_replaced(self) -> None:
        assert _render(_CustomTypeModel, {"customer": SECRET}) == "1 validation error: customer: [custom]"

    def test_undeclared_field_name_is_replaced(self) -> None:
        assert _render(_ForbidModel, {"amount": 1, SECRET: 1}) == "1 validation error: [undeclared]: [extra_forbidden]"


class TestTopLevelNameIsRenderedOnlyWhenTheSchemaDeclaresIt:
    """``loc[0]`` is printed only when the validated schema declares it (field name or alias)."""

    @pytest.mark.parametrize(
        ("model", "payload"),
        [
            pytest.param(_AfterReRaisingModel, {"scores": {SECRET: "not-an-int"}}, id="after-validator"),
            pytest.param(_BeforeReRaisingModel, {"scores": {SECRET: "not-an-int"}}, id="before-validator"),
        ],
    )
    def test_a_model_validator_reraising_an_inner_error_renders_the_placeholder(self, model: type[PluginSchema], payload: object) -> None:
        exc = _validation_error(model, payload)
        assert [detail["loc"] for detail in exc.errors()] == [(SECRET,)], "premise: the row's dict key sits at loc[0]"
        assert safe_validation_error_text(exc, model) == "1 validation error: [undeclared]: [int_parsing]"

    def test_a_reraised_list_index_at_loc0_renders_the_placeholder(self) -> None:
        exc = _validation_error(_ListReRaisingModel, {"tags": [1, "x"]})
        assert [detail["loc"] for detail in exc.errors()] == [(1,)], "premise: an int sits at loc[0]"
        assert safe_validation_error_text(exc, _ListReRaisingModel) == "1 validation error: [undeclared]: [int_parsing]"

    def test_a_typed_extras_key_renders_the_placeholder(self) -> None:
        exc = _validation_error(_TypedExtrasModel, {"amount": 1, SECRET: "not-an-int"})
        assert [(detail["loc"], detail["type"]) for detail in exc.errors()] == [((SECRET,), "int_parsing")], (
            "premise: an ordinary type code, not extra_forbidden"
        )
        assert safe_validation_error_text(exc, _TypedExtrasModel) == "1 validation error: [undeclared]: [int_parsing]"

    def test_a_declared_field_keeps_its_name_beside_an_undeclared_one(self) -> None:
        text = _render(_TypedExtrasModel, {"amount": "x", SECRET: "not-an-int"})
        assert text == "2 validation errors: amount: [int_parsing]; [undeclared]: [int_parsing]"

    def test_a_reraised_key_equal_to_a_declared_name_renders_as_that_name(self) -> None:
        """Only names the schema itself publishes are printed, so a colliding key discloses nothing new."""
        assert _render(_CollidingReRaisingModel, {"amount": 1, "scores": {"amount": "x"}}) == "1 validation error: amount: [int_parsing]"

    @pytest.mark.parametrize(
        ("model", "payload", "expected"),
        [
            pytest.param(_AliasModel, {"Amount": "x"}, "1 validation error: Amount: [int_parsing]", id="alias"),
            pytest.param(_AliasModel, {}, "1 validation error: Amount: [missing]", id="alias-missing"),
            pytest.param(_PopulateByNameAliasModel, {"amount": "x"}, "1 validation error: amount: [int_parsing]", id="populate-by-name"),
            pytest.param(_ValidationAliasModel, {"AmountIn": "x"}, "1 validation error: AmountIn: [int_parsing]", id="validation-alias"),
            pytest.param(_AliasChoicesModel, {"amount_a": "x"}, "1 validation error: amount_a: [int_parsing]", id="alias-choices-str"),
            pytest.param(
                _AliasChoicesModel, {"amounts": ["x"]}, "1 validation error: amounts.[item]: [int_parsing]", id="alias-choices-path"
            ),
            pytest.param(
                _AliasPathModel, {"totals": [{"amount": "x"}]}, "1 validation error: totals.[item].[item]: [int_parsing]", id="alias-path"
            ),
        ],
    )
    def test_a_declared_alias_is_a_declared_name(self, model: type[PluginSchema], payload: object, expected: str) -> None:
        assert _render(model, payload) == expected

    def test_the_name_is_judged_against_the_schema_that_was_validated(self) -> None:
        """The same error rendered against a schema that does not declare the field hides its name."""
        exc = _validation_error(_Model, {"amount": "x", "name": "ok"})
        assert safe_validation_error_text(exc, _Model) == "1 validation error: amount: [int_parsing]"
        assert safe_validation_error_text(exc, _MapModel) == "1 validation error: [undeclared]: [int_parsing]"
