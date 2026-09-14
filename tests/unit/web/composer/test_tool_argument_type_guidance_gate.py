"""The JSON-type repair guidance names a type fault only when there is one.

``ToolArgumentError`` appends "Match the tool's declared JSON types ... not
strings containing JSON" to schema-model failures. That sentence is the
planner's only prose remedy, so it must not be attached to a failure whose
cause is a reserved identifier, a length cap, or a missing field.
"""

from __future__ import annotations

from typing import Any

import pytest
from annotated_types import MaxLen
from jsonschema import Draft202012Validator
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.redaction import SpliceTransformArgumentsModel, _InlineBlobModel, _NodeTriggerModel
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools import get_tool_definitions
from elspeth.web.composer.tools._common import _validate_mutation_arguments
from elspeth.web.composer.tools._dispatch import _closed_root_schema, _schema_tool_argument_error, _validate_tool_arguments

_GUIDANCE = (
    ". Match the tool's declared JSON types. Supply object and array fields as actual JSON objects and arrays, not strings containing JSON."
)


def _splice_arguments(*, node_id: str = "classify", options: object = None) -> dict[str, Any]:
    return {
        "predecessor_id": "source",
        "successor_id": "sink",
        "node": {"id": node_id, "plugin": "passthrough", "options": {} if options is None else options},
    }


def test_splice_arguments_fixture_is_valid() -> None:
    SpliceTransformArgumentsModel.model_validate(_splice_arguments())


def _raise_from_cause(cause: PydanticValidationError, argument: str, model_name: str) -> ToolArgumentError:
    try:
        raise ToolArgumentError(
            argument=argument, expected=f"object conforming to {model_name}", actual_type=type(cause).__name__
        ) from cause
    except ToolArgumentError as exc:
        return exc


@pytest.mark.parametrize("wrong_value", ["many", "3", True], ids=["text", "numeric-text", "boolean"])
def test_json_integer_field_given_non_integer_keeps_json_type_guidance(wrong_value: object) -> None:
    # _JsonInteger fields report a wrong JSON type as value_error, not int_type.
    with pytest.raises(PydanticValidationError) as caught:
        _NodeTriggerModel.model_validate({"count": wrong_value})

    exc = _raise_from_cause(caught.value, "set_pipeline arguments", "SetPipelineArgumentsModel")

    assert exc.expected == "object conforming to SetPipelineArgumentsModel" + _GUIDANCE


class _LaxScalarsModel(BaseModel):
    number: int
    flag: bool


def test_lax_scalar_parsing_failure_keeps_json_type_guidance() -> None:
    with pytest.raises(PydanticValidationError) as caught:
        _LaxScalarsModel.model_validate({"number": "abc", "flag": "maybe"})
    assert {error["type"] for error in caught.value.errors(include_input=False, include_context=False, include_url=False)} == {
        "int_parsing",
        "bool_parsing",
    }

    exc = _raise_from_cause(caught.value, "set_pipeline arguments", "SetPipelineArgumentsModel")

    assert exc.expected == "object conforming to SetPipelineArgumentsModel" + _GUIDANCE


def test_type_shape_pydantic_failure_keeps_json_type_guidance() -> None:
    with pytest.raises(ToolArgumentError) as caught:
        _validate_mutation_arguments(SpliceTransformArgumentsModel, _splice_arguments(options="{}"), "splice_transform arguments")

    assert isinstance(caught.value.__cause__, PydanticValidationError)
    assert caught.value.expected == "object conforming to SpliceTransformArgumentsModel" + _GUIDANCE
    assert caught.value.safe_message.endswith("not strings containing JSON., got ValidationError")


@pytest.mark.parametrize("reserved", ["fork", "continue", "on_success"])
def test_reserved_node_id_failure_drops_json_type_guidance(reserved: str) -> None:
    with pytest.raises(ToolArgumentError) as caught:
        _validate_mutation_arguments(SpliceTransformArgumentsModel, _splice_arguments(node_id=reserved), "splice_transform arguments")

    assert isinstance(caught.value.__cause__, PydanticValidationError)
    assert caught.value.expected == "object conforming to SpliceTransformArgumentsModel"
    assert caught.value.safe_message == (
        "'splice_transform arguments' must be object conforming to SpliceTransformArgumentsModel, got ValidationError"
    )
    assert "strings containing JSON" not in str(caught.value)
    assert caught.value.args == (caught.value.safe_message,)


def test_length_cap_failure_drops_json_type_guidance() -> None:
    try:
        _InlineBlobModel.model_validate({"filename": "rows.csv", "mime_type": "text/csv", "content": "x" * 262_145})
    except PydanticValidationError as cause:
        [error] = cause.errors(include_input=False, include_context=False, include_url=False)
        assert error["type"] == "string_too_long"
        with pytest.raises(ToolArgumentError) as caught:
            raise ToolArgumentError(
                argument="set_pipeline arguments",
                expected="object conforming to SetPipelineArgumentsModel",
                actual_type=type(cause).__name__,
            ) from cause
    else:  # pragma: no cover - fixture integrity
        raise AssertionError("inline blob cap did not reject oversize content")

    assert caught.value.expected == "object conforming to SetPipelineArgumentsModel"


def test_missing_field_failure_drops_json_type_guidance() -> None:
    arguments = _splice_arguments()
    del arguments["predecessor_id"]
    with pytest.raises(ToolArgumentError) as caught:
        _validate_mutation_arguments(SpliceTransformArgumentsModel, arguments, "splice_transform arguments")

    assert caught.value.expected == "object conforming to SpliceTransformArgumentsModel"


def test_scalar_type_failure_keeps_json_type_guidance() -> None:
    arguments = _splice_arguments()
    arguments["predecessor_id"] = 42
    with pytest.raises(ToolArgumentError) as caught:
        _validate_mutation_arguments(SpliceTransformArgumentsModel, arguments, "splice_transform arguments")

    assert caught.value.expected == "object conforming to SpliceTransformArgumentsModel" + _GUIDANCE


def test_mixed_type_and_value_failures_keep_json_type_guidance() -> None:
    with pytest.raises(ToolArgumentError) as caught:
        _validate_mutation_arguments(
            SpliceTransformArgumentsModel,
            _splice_arguments(node_id="fork", options="{}"),
            "splice_transform arguments",
        )

    assert caught.value.expected == "object conforming to SpliceTransformArgumentsModel" + _GUIDANCE


def test_error_without_a_classifiable_cause_keeps_json_type_guidance() -> None:
    exc = ToolArgumentError(
        argument="splice_transform arguments",
        expected="object conforming to SpliceTransformArgumentsModel",
        actual_type="ValidationError",
    )

    assert exc.expected == "object conforming to SpliceTransformArgumentsModel" + _GUIDANCE


def _first_schema_error(schema: dict[str, Any], instance: object) -> Any:
    errors = list(Draft202012Validator(schema).iter_errors(instance))
    assert errors
    return errors[0]


def test_schema_type_violation_keeps_json_type_guidance() -> None:
    error = _first_schema_error(
        {"type": "object", "properties": {"options": {"type": "object"}}},
        {"options": "{}"},
    )

    exc = _schema_tool_argument_error("splice_transform", error)

    assert exc.expected == "object conforming to SpliceTransformArgumentsModel" + _GUIDANCE
    assert exc.actual_type == "invalid_schema"


@pytest.mark.parametrize(
    ("schema", "instance"),
    [
        ({"type": "object", "required": ["predecessor_id"]}, {}),
        ({"type": "object", "properties": {}, "additionalProperties": False}, {"extra": 1}),
        ({"type": "object", "properties": {"mode": {"enum": ["a", "b"]}}}, {"mode": "c"}),
        ({"type": "object", "properties": {"id": {"type": "string", "maxLength": 3}}}, {"id": "toolong"}),
    ],
    ids=["required", "additionalProperties", "enum", "maxLength"],
)
def test_schema_non_type_violation_drops_json_type_guidance(schema: dict[str, Any], instance: object) -> None:
    exc = _schema_tool_argument_error("splice_transform", _first_schema_error(schema, instance))

    assert exc.expected == "object conforming to SpliceTransformArgumentsModel"


# The dispatch path (``_validate_tool_arguments``) sorts every jsonschema error
# by path. The JSON-type guidance must be decided over all of them, not only
# over the one that happens to sort first.


def _empty_state() -> CompositionState:
    return CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)


def _dispatch_schema_error(arguments: dict[str, Any]) -> ToolArgumentError:
    with pytest.raises(ToolArgumentError) as caught:
        _validate_tool_arguments("splice_transform", arguments, _empty_state(), raise_on_error=True)
    return caught.value


def test_dispatch_schema_fixture_is_valid() -> None:
    assert _validate_tool_arguments("splice_transform", _splice_arguments(), _empty_state(), raise_on_error=True) is None


def test_dispatch_schema_only_type_fault_keeps_json_type_guidance() -> None:
    exc = _dispatch_schema_error(_splice_arguments(options="{}"))

    assert exc.expected == "object conforming to SpliceTransformArgumentsModel" + _GUIDANCE


def test_dispatch_schema_only_required_fault_drops_json_type_guidance() -> None:
    arguments = _splice_arguments()
    del arguments["predecessor_id"]

    exc = _dispatch_schema_error(arguments)

    assert exc.expected == "object conforming to SpliceTransformArgumentsModel"


def test_dispatch_schema_type_fault_sorted_after_a_non_type_fault_keeps_json_type_guidance() -> None:
    # The required failure sits at path () and sorts before the type failure at
    # ('node', 'options'); only a decision over all errors sees the type fault.
    arguments = _splice_arguments(options="{}")
    del arguments["predecessor_id"]

    exc = _dispatch_schema_error(arguments)

    assert exc.expected == "object conforming to SpliceTransformArgumentsModel" + _GUIDANCE


def test_dispatch_schema_type_fault_sorted_before_a_non_type_fault_keeps_json_type_guidance() -> None:
    # Mirror order: the type failure at ('node', 'description') sorts before the
    # reserved-id failure at ('node', 'id'), so a selection that takes the last
    # error loses the guidance just as one taking the first did above.
    arguments = _splice_arguments(node_id="fork")
    arguments["node"]["description"] = 5
    errors = sorted(
        Draft202012Validator(_closed_root_schema("splice_transform")).iter_errors(arguments),
        key=lambda error: tuple(error.absolute_path),
    )
    assert [(tuple(error.absolute_path), error.validator) for error in errors] == [
        (("node", "description"), "type"),
        (("node", "id"), "not"),
    ]

    exc = _dispatch_schema_error(arguments)

    assert exc.expected == "object conforming to SpliceTransformArgumentsModel" + _GUIDANCE


def test_set_pipeline_declares_the_inline_blob_content_cap_the_model_enforces() -> None:
    [definition] = [definition for definition in get_tool_definitions() if definition["name"] == "set_pipeline"]
    content_schema = definition["parameters"]["properties"]["source"]["properties"]["inline_blob"]["properties"]["content"]
    enforced = [constraint.max_length for constraint in _InlineBlobModel.model_fields["content"].metadata if isinstance(constraint, MaxLen)]

    assert enforced == [262_144]
    assert content_schema["maxLength"] == enforced[0]
