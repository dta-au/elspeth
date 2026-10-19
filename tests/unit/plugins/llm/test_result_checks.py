"""Actual generated values must pass opt-in checks before downstream release."""

from __future__ import annotations

import json

import pytest

from elspeth.contracts.call_data import LLMCallRequest
from elspeth.contracts.token_usage import TokenUsage
from elspeth.plugins.infrastructure.batching.ports import CollectorOutputPort
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.transforms.llm.provider import FinishReason, LLMQueryResult
from elspeth.plugins.transforms.llm.transform import LLMTransform
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_context
from tests.fixtures.llm_sampling import _Harness
from tests.fixtures.mock_audit import mock_audit_authority
from tests.unit.plugins.llm.test_azure import make_token
from tests.unit.plugins.llm.test_transform import (
    _make_config,
    _make_ctx,
    _make_row,
    _make_transform_with_mock_provider,
    _run_post_emission_check,
)


@pytest.mark.parametrize("provider_name", ["azure", "openrouter", "bedrock", "gateway"])
@pytest.mark.parametrize("multi_query", [False, True], ids=["single", "multi"])
@pytest.mark.parametrize("accepted", [False, True], ids=["rejected", "accepted"])
def test_check_reads_actual_extracted_result_before_success(provider_name: str, multi_query: bool, accepted: bool) -> None:
    field = "quality_answer" if multi_query else "answer"
    config = _make_config(
        provider=provider_name,
        result_checks=[{"name": "answer_allowed", "expression": f"row['{field}'] == 'accepted'"}],
    )
    if provider_name == "gateway":
        config.update(model="approved-model", endpoint="https://gateway.example/v1", api_key="test-key")
    descriptor = {"suffix": "answer", "type": "string"}
    if multi_query:
        config["queries"] = {"quality": {"input_fields": {"text": "text"}, "output_fields": [descriptor]}}
    else:
        config["output_fields"] = [descriptor]
    transform, provider = _make_transform_with_mock_provider(config)
    raw = json.dumps({"answer": "accepted" if accepted else "row-private-rejected-value"})
    provider.execute_query.return_value = LLMQueryResult(
        content=raw, usage=TokenUsage.known(10, 5), model="approved-model", finish_reason=FinishReason.STOP
    )

    result = transform._process_row(_make_row(), _make_ctx())

    provider.execute_query.assert_called_once()
    if accepted:
        assert result.status == "success" and result.row is not None
        assert result.row[field] == "accepted"
        assert result.row["quality_llm_response" if multi_query else "llm_response"] == raw
        _run_post_emission_check(transform, result.row)
    else:
        assert result.status == "error" and result.row is None and result.rows is None
        assert result.retryable is False
        assert result.reason == {
            "reason": "validation_failed",
            "operation": "answer_allowed",
            "error_type": "result_check_false",
        }
        assert "row-private" not in str(result.reason)


@pytest.mark.parametrize(
    ("expression", "error_type"),
    [
        ("row['missing'] == 1", "result_check_evaluation_error"),
        ("len(row['answer']) > 0", "result_check_evaluation_error"),
        ("row['answer']", "result_check_not_boolean"),
        ("1", "result_check_not_boolean"),
        ("'true'", "result_check_not_boolean"),
    ],
)
def test_failed_evaluation_or_nonboolean_cannot_admit_a_result(expression: str, error_type: str) -> None:
    transform, provider = _make_transform_with_mock_provider(
        _make_config(
            result_checks=[{"name": "must_pass", "expression": expression}], output_fields=[{"suffix": "answer", "type": "integer"}]
        )
    )
    provider.execute_query.return_value = LLMQueryResult(
        content='{"answer":1}', usage=TokenUsage.known(1, 1), model="approved-model", finish_reason=FinishReason.STOP
    )
    result = transform._process_row(_make_row(), _make_ctx())
    assert result.status == "error" and result.retryable is False
    assert result.reason == {"reason": "validation_failed", "operation": "must_pass", "error_type": error_type}


def test_extraction_failure_is_not_replaced_by_result_check_failure() -> None:
    transform, provider = _make_transform_with_mock_provider(
        _make_config(result_checks=[{"name": "must_pass", "expression": "False"}], output_fields=[{"suffix": "answer", "type": "integer"}])
    )
    provider.execute_query.return_value = LLMQueryResult(
        content="{}", usage=TokenUsage.known(1, 1), model="approved-model", finish_reason=FinishReason.STOP
    )
    result = transform._process_row(_make_row(), _make_ctx())
    assert result.status == "error" and result.reason == {"reason": "missing_output_field", "field": "answer"}


@pytest.mark.parametrize(
    "checks",
    [
        [],
        ({"name": "valid", "expression": "True"},),
        [{"name": 1, "expression": "True"}],
        [{"name": "valid", "expression": True}],
        [{"name": "", "expression": "True"}],
        [{"name": "bad name", "expression": "True"}],
        [{"name": "valid", "expression": " "}],
        [{"name": "valid", "expression": "row.__class__"}],
        [{"name": "valid", "expression": "__import__('os')"}],
        [{"name": "valid", "expression": "True", "unknown": True}],
        [{"name": "duplicate", "expression": "True"}, {"name": "duplicate", "expression": "False"}],
    ],
)
def test_invalid_checks_are_refused_at_construction(checks: object) -> None:
    with pytest.raises(PluginConfigError):
        LLMTransform(_make_config(result_checks=checks))


def test_omitted_checks_keep_native_request_and_output() -> None:
    row, ctx = _make_row(), _make_ctx()
    old, old_provider = _make_transform_with_mock_provider(_make_config())
    checked, checked_provider = _make_transform_with_mock_provider(
        _make_config(result_checks=[{"name": "response_nonempty", "expression": "len(row['llm_response']) > 0"}])
    )
    response = LLMQueryResult(
        content="Generated answer", usage=TokenUsage.known(1, 1), model="approved-model", finish_reason=FinishReason.STOP
    )
    old_provider.execute_query.return_value = response
    checked_provider.execute_query.return_value = response
    old_result, checked_result = old._process_row(row, ctx), checked._process_row(row, ctx)
    assert old_result.status == checked_result.status == "success"
    assert old_result.row is not None and checked_result.row is not None
    assert old_result.row.to_dict() == checked_result.row.to_dict()
    assert old_result.row.contract == checked_result.row.contract
    assert old_result.success_reason == checked_result.success_reason
    assert old_provider.execute_query.call_args == checked_provider.execute_query.call_args
    assert "result_checks" not in old.config


def test_runtime_check_snapshot_cannot_be_removed_by_mutating_config_list() -> None:
    transform, provider = _make_transform_with_mock_provider(_make_config(result_checks=[{"name": "deny", "expression": "False"}]))
    transform._config.result_checks.clear()
    provider.execute_query.return_value = LLMQueryResult(
        content="Generated answer", usage=TokenUsage.known(1, 1), model="approved-model", finish_reason=FinishReason.STOP
    )
    result = transform._process_row(_make_row(), _make_ctx())
    assert result.status == "error" and result.reason["operation"] == "deny"


@pytest.mark.parametrize("supplied", [None, 3])
def test_nullable_generated_field_is_a_postcondition_not_an_upstream_requirement(supplied: int | None) -> None:
    transform, provider = _make_transform_with_mock_provider(
        _make_config(
            output_fields=[{"suffix": "answer", "type": "integer", "nullable": True}],
            result_checks=[{"name": "nullable_answer", "expression": "row['answer'] is None or row['answer'] > 0"}],
        )
    )
    provider.execute_query.return_value = LLMQueryResult(
        content=json.dumps({"answer": supplied}), usage=TokenUsage.known(1, 1), model="approved-model", finish_reason=FinishReason.STOP
    )
    result = transform._process_row(_make_row(), _make_ctx())
    assert result.status == "success" and result.row["answer"] == supplied
    assert transform.declared_input_fields == frozenset({"text"})


@pytest.mark.parametrize("expression", ["row['check_only'] == 'private'", "row[row['selected']] == 'private'"])
def test_check_only_input_is_not_added_to_provider_template_visibility(expression: str) -> None:
    transform, provider = _make_transform_with_mock_provider(_make_config(result_checks=[{"name": "local_only", "expression": expression}]))
    provider.execute_query.return_value = LLMQueryResult(
        content="Generated answer", usage=TokenUsage.known(1, 1), model="approved-model", finish_reason=FinishReason.STOP
    )
    result = transform._process_row(_make_row({"text": "public", "check_only": "private", "selected": "check_only"}), _make_ctx())
    assert result.status == "success"
    messages = provider.execute_query.call_args.args[0]
    assert "private" not in str(messages) and "check_only" not in str(messages)


def test_missing_computed_private_key_fails_without_echoing_the_key() -> None:
    transform, provider = _make_transform_with_mock_provider(
        _make_config(result_checks=[{"name": "local_only", "expression": "row[row['selected']] == 'accepted'"}])
    )
    provider.execute_query.return_value = LLMQueryResult(
        content="Generated answer", usage=TokenUsage.known(1, 1), model="approved-model", finish_reason=FinishReason.STOP
    )
    result = transform._process_row(_make_row({"text": "public", "selected": "private-missing-key"}), _make_ctx())
    assert result.status == "error"
    assert result.reason == {"reason": "validation_failed", "operation": "local_only", "error_type": "result_check_evaluation_error"}
    assert "private-missing-key" not in str(result.reason)


@pytest.mark.parametrize(
    ("value", "kind", "expression", "accepted"),
    [
        ("Status: pass", "string", "starts_with(row['answer'], 'Status:')", True),
        ("Earlier Status: pass", "string", "starts_with(row['answer'], 'Status:')", False),
        ("one two three", "string", "3 <= word_count(row['answer']) <= 4", True),
        ("one two three four", "string", "3 <= word_count(row['answer']) <= 4", True),
        ("one two", "string", "3 <= word_count(row['answer']) <= 4", False),
        ("one two three four five", "string", "3 <= word_count(row['answer']) <= 4", False),
        (2.35, "number", "decimal_within(row['answer'], 2.30, 0.05)", True),
        (2.351, "number", "decimal_within(row['answer'], 2.30, 0.05)", False),
    ],
    ids=[
        "leading",
        "later-substring",
        "word-lower",
        "word-upper",
        "word-too-short",
        "word-too-long",
        "decimal-boundary",
        "decimal-outside",
    ],
)
def test_scalar_checks_admit_actual_generated_boundaries(value: object, kind: str, expression: str, accepted: bool) -> None:
    transform, provider = _make_transform_with_mock_provider(
        _make_config(
            output_fields=[{"suffix": "answer", "type": kind}], result_checks=[{"name": "actual_result", "expression": expression}]
        )
    )
    provider.execute_query.return_value = LLMQueryResult(
        content=json.dumps({"answer": value}), usage=TokenUsage.known(1, 1), model="approved-model", finish_reason=FinishReason.STOP
    )
    result = transform._process_row(_make_row(), _make_ctx())
    provider.execute_query.assert_called_once()
    assert result.status == ("success" if accepted else "error")
    if not accepted:
        assert result.row is None and result.retryable is False
        assert result.reason == {"reason": "validation_failed", "operation": "actual_result", "error_type": "result_check_false"}


@pytest.mark.parametrize("query_mode", ["single", "sequential", "pooled", "ordered"])
@pytest.mark.parametrize("accepted", [False, True], ids=["rejected", "accepted"])
def test_checks_preserve_real_provider_transport_audit_and_batch_delivery(
    harness: _Harness, query_mode: str, accepted: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OpenRouter/Gateway reach fake HTTP; Azure/Bedrock reach SDK boundaries."""
    config = dict(harness.config)
    field = "score" if query_mode == "single" else "second_score"
    config.update(
        required_input_fields=["text"],
        prompt_template="{{ row.text }}",
        result_checks=[{"name": "score_allowed", "expression": f"row['{field}'] == {1 if accepted else 2}"}],
    )
    descriptor = {"suffix": "score", "type": "integer"}
    if query_mode == "single":
        config["output_fields"] = [descriptor]
    else:
        config["queries"] = {name: {"input_fields": {"text": "text"}, "output_fields": [descriptor]} for name in ("first", "second")}
    if query_mode == "pooled":
        config["pool_size"] = 2
    if query_mode == "ordered":
        config["query_input_mode"] = "ordered"
        config["queries"] = [
            {"name": "first", "input_fields": {"text": "text"}, "output_fields": [descriptor]},
            {
                "name": "second",
                "input_fields": {"text": "text", "prior": "first_score"},
                "template": "Score {{ row.prior }} for {{ row.text }}",
                "output_fields": [descriptor],
            },
        ]
    transform = LLMTransform(config)
    monkeypatch.setattr(transform, "_create_provider", lambda **kwargs: harness.provider)
    collector = CollectorOutputPort()
    try:
        transform.on_start(make_context(**mock_audit_authority("test"), run_id="test", landscape=harness.recorder))
        transform.connect_output(collector, max_pending=10)
        ctx = make_context(
            **mock_audit_authority("test"), run_id="test", state_id="check-state", token=make_token(), landscape=harness.recorder
        )
        transform.accept(make_pipeline_row({"text": "generic input"}), ctx)
        transform.flush_batch_processing(timeout=10)
        assert len(collector.results) == 1
        result = collector.results[0][1]
        assert result.status == ("success" if accepted else "error")
        assert result.retryable is False
        expected_calls = 1 if query_mode == "single" else 2
        assert len(harness.requests) == expected_calls
        audited = [call for call in harness.recorder.calls if isinstance(call["request_data"], LLMCallRequest)]
        assert len(audited) == expected_calls
        assert all(call["response_data"] is not None for call in audited)
        assert all("result_checks" not in request for request in harness.requests)
        assert harness.recorder.operation_calls == []
    finally:
        transform.close()
