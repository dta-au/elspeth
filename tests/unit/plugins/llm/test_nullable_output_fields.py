"""Required nullable structured fields retain one parser/schema/row contract."""

from __future__ import annotations

import json

import pytest

from elspeth.contracts.schema import FieldDefinition, SchemaConfig
from elspeth.contracts.token_usage import TokenUsage
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config
from elspeth.plugins.transforms.llm import build_llm_source_output_schema_config
from elspeth.plugins.transforms.llm.multi_query import OutputFieldConfig, OutputFieldType, ResponseFormat
from elspeth.plugins.transforms.llm.provider import FinishReason, LLMQueryResult
from elspeth.plugins.transforms.llm.validation import build_structured_response_directive, extract_structured_fields, parse_field_value
from tests.unit.plugins.llm.test_transform import (
    _make_config,
    _make_ctx,
    _make_row,
    _make_transform_with_mock_provider,
    _run_post_emission_check,
)


def _field(kind: OutputFieldType, *, nullable: bool | None = True) -> OutputFieldConfig:
    raw = {"suffix": "answer", "type": kind.value}
    if kind is OutputFieldType.ENUM:
        raw["values"] = ["green", "blue"]
    if nullable is not None:
        raw["nullable"] = nullable
    return OutputFieldConfig.from_dict(raw)


@pytest.mark.parametrize("kind", list(OutputFieldType))
def test_nullable_expands_only_the_value_domain_and_keeps_the_key_required(kind: OutputFieldType) -> None:
    field = _field(kind)
    expected_type = "string" if kind is OutputFieldType.ENUM else kind.value
    schema = field.to_json_schema()
    assert schema["type"] == [expected_type, "null"]
    if kind is OutputFieldType.ENUM:
        assert schema["enum"] == ["green", "blue", None]
        assert field.values == ["green", "blue"]
    assert parse_field_value(None, field) == (None, None)
    assert extract_structured_fields('{"answer":null}', (field,)) == ({"answer": None}, None)
    extracted, error = extract_structured_fields("{}", (field,))
    assert extracted == {} and error == {"reason": "missing_output_field", "field": "answer"}


@pytest.mark.parametrize("kind", list(OutputFieldType))
@pytest.mark.parametrize("nullable", [None, False], ids=["absent", "false"])
def test_old_fields_preserve_the_nonnullable_wire_and_null_refusal(kind: OutputFieldType, nullable: bool | None) -> None:
    field = _field(kind, nullable=nullable)
    expected_type = "string" if kind is OutputFieldType.ENUM else kind.value
    expected = {"type": expected_type}
    if kind is OutputFieldType.ENUM:
        expected["enum"] = ["green", "blue"]
    assert field.nullable is False
    assert field.to_json_schema() == expected
    value, error = parse_field_value(None, field)
    assert value is None and error is not None


@pytest.mark.parametrize("flag", [None, "true", "false", 0, 1])
def test_nullable_requires_an_explicit_boolean(flag: object) -> None:
    with pytest.raises(PluginConfigError):
        OutputFieldConfig.from_dict({"suffix": "answer", "type": "number", "nullable": flag})


@pytest.mark.parametrize("kind", [OutputFieldType.INTEGER, OutputFieldType.NUMBER])
@pytest.mark.parametrize("value", [True, False, float("nan"), float("inf"), float("-inf")])
def test_nullable_numeric_keeps_boolean_and_nonfinite_refusal(kind: OutputFieldType, value: object) -> None:
    parsed, error = parse_field_value(value, _field(kind))
    assert parsed is None and error is not None


def test_nullable_enum_keeps_closed_string_membership_and_config_requirements() -> None:
    assert parse_field_value("green", _field(OutputFieldType.ENUM)) == ("green", None)
    assert parse_field_value("other", _field(OutputFieldType.ENUM))[1] is not None
    assert parse_field_value(1, _field(OutputFieldType.ENUM))[1] is not None
    for values in (None, []):
        with pytest.raises(PluginConfigError):
            OutputFieldConfig.from_dict({"suffix": "answer", "type": "enum", "values": values, "nullable": True})


@pytest.mark.parametrize("mode", list(ResponseFormat))
def test_provider_contract_advertises_nullable_type_in_both_response_modes(mode: ResponseFormat) -> None:
    field = _field(OutputFieldType.NUMBER)
    response, prompt = build_structured_response_directive(
        schema_name="nullable_result", output_fields=(field,), response_format=mode, prompt="Inspect the record."
    )
    expected = {"type": "object", "properties": {"answer": {"type": ["number", "null"]}}, "required": ["answer"]}
    if mode is ResponseFormat.STRUCTURED:
        assert response == {"type": "json_schema", "json_schema": {"name": "nullable_result", "schema": expected}}
        assert prompt == "Inspect the record."
    else:
        assert response == {"type": "json_object"}
        assert json.loads(prompt.split("(JSON Schema):\n", 1)[1]) == expected


@pytest.mark.parametrize("multi_query", [False, True], ids=["single", "multi"])
@pytest.mark.parametrize("mode", ["observed", "flexible"])
@pytest.mark.parametrize(
    ("kind", "python_type", "value"),
    [
        (OutputFieldType.STRING, str, "green"),
        (OutputFieldType.INTEGER, int, 3.0),
        (OutputFieldType.NUMBER, float, 3),
        (OutputFieldType.BOOLEAN, bool, True),
        (OutputFieldType.ENUM, str, "green"),
    ],
)
def test_nullable_transform_preserves_required_type_through_actual_strategy_and_emission(
    multi_query: bool, mode: str, kind: OutputFieldType, python_type: type, value: object
) -> None:
    descriptor = _field(kind).model_dump(by_alias=True, exclude={"schema_config"})
    schema = {"mode": mode}
    if mode == "flexible":
        schema["fields"] = ["text: str"]
    config = _make_config(schema=schema)
    if multi_query:
        config["queries"] = {"quality": {"input_fields": {"text": "text"}, "output_fields": [descriptor], "response_format": "structured"}}
    else:
        config.update(output_fields=[descriptor], response_format="structured")
    transform, provider = _make_transform_with_mock_provider(config)
    field_name = "quality_answer" if multi_query else "answer"
    created = {field.name: field for field in transform.created_output_fields()}
    assert created[field_name].nullable is True and created[field_name].required is True
    for supplied in (None, value):
        raw = json.dumps({"answer": supplied}, allow_nan=False)
        provider.execute_query.return_value = LLMQueryResult(
            content=raw, usage=TokenUsage.known(10, 5), model="gpt-4o", finish_reason=FinishReason.STOP
        )
        result = transform._process_row(_make_row(), _make_ctx())
        assert result.status == "success" and result.row is not None
        _run_post_emission_check(transform, result.row)
        data = result.row.to_dict()
        if supplied is None:
            assert data[field_name] is None
        else:
            assert type(data[field_name]) is python_type
            assert data[field_name] == value
        emitted = result.row.contract.get_field(field_name)
        assert emitted.python_type is python_type and emitted.nullable and emitted.required
        transform.output_schema.model_validate(data, strict=True)
        if mode == "flexible":
            assert transform.output_schema.model_fields[field_name].is_required()
            assert transform._output_schema_config is not None
            declared = {field.name: field for field in transform._output_schema_config.fields}
            assert declared[field_name].nullable is True and declared[field_name].required is True
        sent = provider.execute_query.call_args.kwargs["response_format"]["json_schema"]["schema"]
        assert sent["required"] == ["answer"]
        assert sent["properties"]["answer"] == _field(kind).to_json_schema()


@pytest.mark.parametrize("multi_query", [False, True], ids=["single", "multi"])
@pytest.mark.parametrize(
    ("nullable", "content", "reason"),
    [
        (False, '{"answer":null}', "field_type_mismatch"),
        (True, "{}", "missing_output_field"),
        (True, '{"answer":true}', "field_type_mismatch"),
    ],
)
def test_nullable_transform_keeps_existing_row_failure_routing(multi_query: bool, nullable: bool, content: str, reason: str) -> None:
    descriptor = {"suffix": "answer", "type": "number", "nullable": nullable}
    config = _make_config()
    if multi_query:
        config["queries"] = {"quality": {"input_fields": {"text": "text"}, "output_fields": [descriptor]}}
    else:
        config["output_fields"] = [descriptor]
    transform, provider = _make_transform_with_mock_provider(config)
    provider.execute_query.return_value = LLMQueryResult(
        content=content, usage=TokenUsage.known(10, 5), model="gpt-4o", finish_reason=FinishReason.STOP
    )
    result = transform._process_row(_make_row(), _make_ctx())
    assert result.status == "error" and result.row is None and result.reason is not None
    assert result.reason["reason"] == reason


def test_source_schema_uses_the_same_required_nullable_field_contract() -> None:
    schema = build_llm_source_output_schema_config(
        SchemaConfig(mode="fixed", fields=(FieldDefinition("response", "str"),)),
        "response",
        (_field(OutputFieldType.NUMBER),),
    )
    fields = {field.name: field for field in schema.fields}
    assert fields["answer"] == FieldDefinition("answer", "float", required=True, nullable=True)
    row_schema = create_schema_from_config(schema, "NullableSourceOutput", allow_coercion=False)
    assert row_schema.model_fields["answer"].is_required()
    row_schema.model_validate({"response": '{"answer":null}', "response_usage": None, "response_model": "model", "answer": None})


@pytest.mark.parametrize(("output_nullable", "authored_nullable"), [(False, True), (True, False)])
def test_source_schema_refuses_a_mismatched_authored_nullability(output_nullable: bool, authored_nullable: bool) -> None:
    with pytest.raises(ValueError, match="nullable"):
        build_llm_source_output_schema_config(
            SchemaConfig(mode="fixed", fields=(FieldDefinition("answer", "float", nullable=authored_nullable),)),
            "response",
            (_field(OutputFieldType.NUMBER, nullable=output_nullable),),
        )
