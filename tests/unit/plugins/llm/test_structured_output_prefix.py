"""Explicit structured publication names preserve ordered review and legacy defaults."""

from __future__ import annotations

import json
from types import MappingProxyType

import pytest

from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract
from elspeth.contracts.chat_parts import wire_messages
from elspeth.contracts.token_usage import TokenUsage
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.transforms.llm.provider import FinishReason, LLMQueryResult
from elspeth.plugins.transforms.llm.multi_query import QueryDefinition, QuerySpec
from elspeth.plugins.transforms.llm.transform import LLMTransform
from tests.unit.plugins.llm.test_transform import _make_config, _make_ctx, _make_row, _make_transform_with_mock_provider, _run_post_emission_check


def _response(data: object) -> LLMQueryResult:
    return LLMQueryResult(content=json.dumps(data), usage=TokenUsage.known(10, 5), model="approved-model", finish_reason=FinishReason.STOP)


def _ordered_options(*, prefix: str | None = ""):
    field = "draft_answer" if prefix is None else (f"{prefix}_answer" if prefix else "answer")
    return _make_config(
        query_input_mode="ordered",
        pool_size=1,
        schema={"mode": "flexible", "fields": ["text: str"]},
        queries=[
            {
                "name": "draft",
                "structured_output_prefix": prefix,
                "input_fields": {"text": "text"},
                "template": "Draft {{ row.text }}",
                "response_format": "structured",
                "output_fields": [{"suffix": "answer", "type": "string", "nullable": True}],
            },
            {
                "name": "review",
                "input_fields": {"candidate": field, "text": "text"},
                "template": "Review {{ row.candidate }} against {{ row.text }}",
                "response_format": "structured",
                "output_fields": [{"suffix": "admit", "type": "boolean"}],
            },
        ],
        result_checks=[{"name": "review_admitted", "expression": "row['review_admit']"}],
    )


@pytest.mark.parametrize("prefix", [None, "", "published"], ids=["legacy-none", "unprefixed", "custom-prefix"])
@pytest.mark.parametrize("admitted", [False, True], ids=["rejected", "admitted"])
def test_actual_writer_output_is_bound_to_a_separate_reviewer_before_release(prefix: str | None, admitted: bool) -> None:
    transform, provider = _make_transform_with_mock_provider(_ordered_options(prefix=prefix))
    provider.execute_query.side_effect = [_response({"answer": "actual generated answer"}), _response({"admit": admitted})]
    row = _make_row()
    result = transform._process_row(row, _make_ctx())
    assert provider.execute_query.call_count == 2
    assert provider.execute_query.call_args_list[1].args[0][-1].content == "Review actual generated answer against hello"
    for call, expected_keys in zip(provider.execute_query.call_args_list, ({"answer"}, {"admit"}), strict=True):
        properties = call.kwargs["response_format"]["json_schema"]["schema"]["properties"]
        assert set(properties) == expected_keys  # Publication names never rename response JSON keys.
    assert row.to_dict() == {"text": "hello"}
    if admitted:
        field = "draft_answer" if prefix is None else (f"{prefix}_answer" if prefix else "answer")
        assert result.status == "success" and result.row is not None
        assert result.row[field] == "actual generated answer"
        assert result.row["draft_llm_response"] == json.dumps({"answer": "actual generated answer"})
        assert result.row["draft_llm_response_model"] == "approved-model"
        assert result.row["review_admit"] is True
        assert result.row.contract.get_field(field).nullable is True
        assert result.row.contract.get_field(field).required is True
        _run_post_emission_check(transform, result.row)
        transform.output_schema.model_validate(result.row.to_dict(), strict=True)
    else:
        assert result.status == "error" and result.row is None and result.retryable is False
        assert result.reason == {"reason": "validation_failed", "operation": "review_admitted", "error_type": "result_check_false"}


@pytest.mark.parametrize("prefix", ["", "custom"])
def test_nullable_flat_output_is_preserved_for_later_explicit_binding(prefix: str) -> None:
    transform, provider = _make_transform_with_mock_provider(_ordered_options(prefix=prefix))
    provider.execute_query.side_effect = [_response({"answer": None}), _response({"admit": True})]
    result = transform._process_row(_make_row(), _make_ctx())
    field = "answer" if prefix == "" else "custom_answer"
    assert result.status == "success" and result.row[field] is None
    assert provider.execute_query.call_args_list[1].args[0][-1].content == "Review None against hello"


@pytest.mark.parametrize("prefix", ["bad stem", "1bad", "a-b", "x" * 65, "é", True, 1])
def test_invalid_prefix_fails_before_provider_startup(prefix: object) -> None:
    options = _ordered_options()
    options["queries"][0]["structured_output_prefix"] = prefix
    with pytest.raises(PluginConfigError):
        LLMTransform(options)


@pytest.mark.parametrize("collision", ["answer", "second_llm_response", "second_llm_response_usage", "second_llm_response_model"])
def test_any_explicit_override_refuses_all_structured_and_operational_final_collisions(collision: str) -> None:
    queries = {
        "first": {"input_fields": {"text": "text"}, "structured_output_prefix": "", "output_fields": [{"suffix": collision, "type": "string"}]},
        "second": {"input_fields": {"text": "text"}, "structured_output_prefix": "", "output_fields": [{"suffix": "answer", "type": "string"}]},
    }
    with pytest.raises(PluginConfigError, match="collision"):
        LLMTransform(_make_config(queries=queries))


@pytest.mark.parametrize("explicit_none", [False, True], ids=["omitted", "explicit-none"])
def test_upstream_default_keeps_legacy_operational_precedence(explicit_none: bool) -> None:
    definition = {"input_fields": {"text": "text"}, "output_fields": [{"suffix": "llm_response", "type": "integer"}]}
    if explicit_none:
        definition["structured_output_prefix"] = None
    transform, provider = _make_transform_with_mock_provider(_make_config(queries={"quality": definition}))
    provider.execute_query.return_value = _response({"llm_response": 42})
    result = transform._process_row(_make_row(), _make_ctx())
    assert result.status == "success" and result.row["quality_llm_response"] == json.dumps({"llm_response": 42})
    _run_post_emission_check(transform, result.row)


@pytest.mark.parametrize("mode,pool_size", [("ordered", 1), ("upstream", 1), ("upstream", 2)])
def test_flat_generated_field_cannot_be_read_as_an_original_header_alias(mode: str, pool_size: int) -> None:
    options = _ordered_options()
    options.update(query_input_mode=mode, pool_size=pool_size)
    if mode == "upstream":
        options["queries"][1]["input_fields"] = {"candidate": "text", "text": "text"}
    transform, provider = _make_transform_with_mock_provider(options)
    provider.execute_query.side_effect = [_response({"answer": "generated"}), _response({"admit": True})]
    row = PipelineRow(
        {"text": "hello", "source_value": "must not be overwritten"},
        SchemaContract(
            mode="FLEXIBLE",
            fields=(FieldContract("text", "text", str, True, "declared"), FieldContract("source_value", "answer", str, True, "declared")),
            locked=True,
        ),
    )
    result = transform._process_row(row, _make_ctx())
    assert result.status == "error" and result.reason["reason"] == "field_collision"
    provider.execute_query.assert_not_called()


@pytest.mark.parametrize("prefix", ["", "published"])
@pytest.mark.parametrize("path", ["self", "forward", "required", "schema", "source_row", "alias"])
def test_renamed_fields_keep_original_and_generated_namespaces_separate(prefix: str, path: str) -> None:
    options = _ordered_options(prefix=prefix)
    field = f"{prefix}_answer" if prefix else "answer"
    if path == "self":
        options["queries"][0]["input_fields"] = {"text": field}
    elif path == "forward":
        options["queries"][0]["input_fields"] = {"text": "review_admit"}
    elif path == "required":
        options["required_input_fields"] = ["text", field]
    elif path == "schema":
        options["schema"] = {"mode": "observed", "required_fields": [field]}
    elif path == "source_row":
        options["queries"][1]["template"] = f"{{{{ row.source_row.{field} }}}}"
    else:
        options["queries"][1]["input_fields"]["candidate"] = "Published Answer" if prefix else "Answer"
    with pytest.raises(PluginConfigError, match=r"earlier query|generated|ambiguously"):
        LLMTransform(options)


def test_explicit_same_prefix_activates_collision_refusal_for_default_named_other_queries() -> None:
    options = _make_config(
        queries={
            "first": {
                "structured_output_prefix": "first",
                "input_fields": {"text": "text"},
                "output_fields": [{"suffix": "detail_llm_response", "type": "integer"}],
            },
            "first_detail": {"input_fields": {"text": "text"}},
        }
    )
    with pytest.raises(PluginConfigError, match="collision"):
        LLMTransform(options)


def test_omitted_and_explicit_null_preserve_request_output_and_schema_identity() -> None:
    traces = []
    ctx = _make_ctx()
    for explicit_null in (False, True):
        options = _ordered_options(prefix=None)
        if not explicit_null:
            del options["queries"][0]["structured_output_prefix"]
        transform, provider = _make_transform_with_mock_provider(options)
        assert transform.config == options
        provider.execute_query.side_effect = [_response({"answer": "actual answer"}), _response({"admit": True})]
        result = transform._process_row(_make_row(), ctx)
        assert result.status == "success"
        requests = [(wire_messages(call.args[0]), call.kwargs) for call in provider.execute_query.call_args_list]
        traces.append((requests, result.row.to_dict(), transform.created_output_fields(), transform.output_schema.model_json_schema()))
    assert traces[0] == traces[1]


def test_runtime_spec_keeps_existing_positional_allow_empty_inputs_and_validates_prefix() -> None:
    from elspeth.plugins.transforms.llm.multi_query import ResponseFormat

    spec = QuerySpec("constant", MappingProxyType({}), ResponseFormat.STANDARD, None, None, None, True, structured_output_prefix="")
    assert spec.structured_field_name("answer") == "answer"
    with pytest.raises(ValueError, match="structured_output_prefix"):
        QuerySpec("constant", MappingProxyType({}), allow_empty_inputs=True, structured_output_prefix="bad stem")


def test_public_query_schema_advertises_the_mapping_without_renaming_response_keys() -> None:
    schema = QueryDefinition.model_json_schema()
    prefix = schema["properties"]["structured_output_prefix"]
    assert prefix["default"] is None
    assert {option["type"] for option in prefix["anyOf"]} == {"string", "null"}
    assert prefix["anyOf"][0]["maxLength"] == 64
