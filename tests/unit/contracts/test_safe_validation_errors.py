"""Value-free rendering of Pydantic validation failures (elspeth-a300402c58, elspeth-5887fb7928).

``str(ValidationError)`` echoes the offending INPUT VALUE by default, and even
without the ``input`` echo, Pydantic's ``msg`` (custom validator text), the
``loc`` of a dict-typed field (the row's own dict KEY) and a
``PydanticCustomError`` type string can each carry row content. The renderer
emits only the top-level field, placeholders for deeper locations, and
pydantic-core's own type codes.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_core import PydanticCustomError

from elspeth.contracts.safe_validation_errors import safe_validation_error_text

SECRET = "SECRET-abc123"


class _Model(BaseModel):
    amount: int
    name: str


class _MapModel(BaseModel):
    scores: dict[str, int]


class _IntKeyMapModel(BaseModel):
    scores: dict[int, str]


class _ListModel(BaseModel):
    tags: list[int]


class _Nested(BaseModel):
    code: int


class _NestedModel(BaseModel):
    inner: _Nested


class _EchoingValidatorModel(BaseModel):
    customer: str

    @field_validator("customer")
    @classmethod
    def _reject(cls, value: str) -> str:
        raise ValueError(f"Invalid customer {value}")


class _CustomTypeModel(BaseModel):
    customer: str

    @field_validator("customer")
    @classmethod
    def _reject(cls, value: str) -> str:
        raise PydanticCustomError(f"customer_{value}", "customer {value} rejected", {"value": value})


class _ForbidModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: int


class _AliasModel(BaseModel):
    amount: int = Field(alias="Amount")


def _validation_error(model: type[BaseModel], payload: object) -> ValidationError:
    try:
        model.model_validate(payload)
    except ValidationError as exc:
        return exc
    raise AssertionError("model_validate unexpectedly succeeded")


class TestSafeValidationErrorText:
    def test_premise_str_echoes_input_value(self) -> None:
        """Guard the premise: str(ValidationError) DOES echo the input value.

        If Pydantic ever stops echoing input by default this boundary can be
        revisited — this pin makes that change visible.
        """
        assert SECRET in str(_validation_error(_Model, {"amount": SECRET, "name": "ok"}))

    def test_renders_the_declared_field_and_type_code_only(self) -> None:
        text = safe_validation_error_text(_validation_error(_Model, {"amount": SECRET, "name": "ok"}))
        assert text == "1 validation error: amount: [int_parsing]"

    def test_multiple_errors_all_rendered(self) -> None:
        text = safe_validation_error_text(_validation_error(_Model, {"amount": SECRET}))
        assert text == "2 validation errors: amount: [int_parsing]; name: [missing]"

    def test_root_level_error_renders_placeholder_loc(self) -> None:
        text = safe_validation_error_text(_validation_error(_Model, SECRET))
        assert text == "1 validation error: <root>: [model_type]"

    @pytest.mark.parametrize(
        ("model", "payload"),
        [
            pytest.param(_EchoingValidatorModel, {"customer": SECRET}, id="custom-validator-msg"),
            pytest.param(_MapModel, {"scores": {SECRET: "not-an-int"}}, id="dict-key-in-loc"),
            pytest.param(_CustomTypeModel, {"customer": SECRET}, id="custom-error-type-code"),
            pytest.param(_ForbidModel, {"amount": 1, SECRET: 1}, id="undeclared-key-in-loc"),
            pytest.param(_Model, {"amount": SECRET, "name": "ok"}, id="plain-input-echo"),
        ],
    )
    def test_no_row_content_reaches_the_text(self, model: type[BaseModel], payload: object) -> None:
        exc = _validation_error(model, payload)
        assert SECRET in str(exc), "positive control: pydantic's own rendering carries the sentinel"
        assert SECRET not in safe_validation_error_text(exc)

    def test_custom_validator_message_is_dropped_type_code_kept(self) -> None:
        text = safe_validation_error_text(_validation_error(_EchoingValidatorModel, {"customer": SECRET}))
        assert text == "1 validation error: customer: [value_error]"

    def test_dict_key_renders_as_a_placeholder(self) -> None:
        text = safe_validation_error_text(_validation_error(_MapModel, {"scores": {SECRET: "not-an-int"}}))
        assert text == "1 validation error: scores.[item]: [int_parsing]"

    def test_an_int_dict_key_is_not_rendered_either(self) -> None:
        """An int in ``loc`` can be a row's dict key, not only a list index."""
        exc = _validation_error(_IntKeyMapModel, {"scores": {582971: 5}})
        assert "582971" in str(exc), "positive control"
        assert safe_validation_error_text(exc) == "1 validation error: scores.[item]: [string_type]"

    def test_list_index_and_nested_field_render_as_placeholders(self) -> None:
        assert safe_validation_error_text(_validation_error(_ListModel, {"tags": [1, "x"]})) == (
            "1 validation error: tags.[item]: [int_parsing]"
        )
        assert safe_validation_error_text(_validation_error(_NestedModel, {"inner": {"code": "x"}})) == (
            "1 validation error: inner.[item]: [int_parsing]"
        )

    def test_custom_error_type_code_is_replaced(self) -> None:
        text = safe_validation_error_text(_validation_error(_CustomTypeModel, {"customer": SECRET}))
        assert text == "1 validation error: customer: [custom]"

    def test_undeclared_field_name_is_replaced(self) -> None:
        text = safe_validation_error_text(_validation_error(_ForbidModel, {"amount": 1, SECRET: 1}))
        assert text == "1 validation error: [undeclared]: [extra_forbidden]"

    def test_an_alias_is_a_declared_name(self) -> None:
        text = safe_validation_error_text(_validation_error(_AliasModel, {"Amount": "x"}))
        assert text == "1 validation error: Amount: [int_parsing]"
