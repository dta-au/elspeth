"""Reject invalid entry rows before making a writer request."""

from __future__ import annotations

import pytest

from elspeth.plugins.infrastructure.config_base import PluginConfigError
from tests.unit.plugins.llm.test_structured_output_prefix import _ordered_options, _response
from tests.unit.plugins.llm.test_transform import _make_config, _make_ctx, _make_row, _make_transform_with_mock_provider


@pytest.mark.parametrize("provider_name", ["azure", "openrouter", "bedrock", "gateway"])
@pytest.mark.parametrize("mode", ["single", "sequential", "pooled", "ordered"])
@pytest.mark.parametrize("admitted", [False, True])
def test_input_admission_runs_before_every_strategy_without_widening_prompt_visibility(
    provider_name: str, mode: str, admitted: bool
) -> None:
    options = _ordered_options() if mode == "ordered" else _make_config()
    options.update(provider=provider_name, input_checks=[{"name": "source_admitted", "expression": "row['admitted']"}])
    if provider_name != "azure":
        options.pop("deployment_name")
        options.pop("endpoint")
        options["model"] = "bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0" if provider_name == "bedrock" else "approved-model"
    if provider_name == "gateway":
        options["endpoint"] = "https://gateway.example/v1"
        if mode == "ordered":
            options["required_capabilities"] = ["json_schema"]
    if mode in {"sequential", "pooled"}:
        options.update(queries={"draft": {"input_fields": {"text": "text"}}}, pool_size=2 if mode == "pooled" else 1)
    transform, provider = _make_transform_with_mock_provider(options)
    provider.execute_query.side_effect = [_response({"answer": "generated"}), _response({"admit": True})]
    row = _make_row({"text": "hello", "admitted": admitted, "private_local": "must-not-reach-model"})
    result = transform._process_row(row, _make_ctx())
    if admitted:
        assert result.status == "success"
        assert provider.execute_query.call_count == (2 if mode == "ordered" else 1)
        for call in provider.execute_query.call_args_list:
            assert "must-not-reach-model" not in str(call.args[0])
    else:
        assert result.status == "error" and result.row is None and result.retryable is False
        assert result.reason == {"reason": "validation_failed", "operation": "source_admitted", "error_type": "input_check_false"}
        provider.execute_query.assert_not_called()
    assert row.to_dict() == {"text": "hello", "admitted": admitted, "private_local": "must-not-reach-model"}


@pytest.mark.parametrize(
    "expression,error_type",
    [("row['missing']", "input_check_evaluation_error"), ("1", "input_check_not_boolean"), ("'true'", "input_check_not_boolean")],
)
def test_missing_or_truthy_input_cannot_start_provider_work(expression: str, error_type: str) -> None:
    transform, provider = _make_transform_with_mock_provider(_make_config(input_checks=[{"name": "must_pass", "expression": expression}]))
    result = transform._process_row(_make_row(), _make_ctx())
    assert result.status == "error" and result.reason["error_type"] == error_type
    provider.execute_query.assert_not_called()


@pytest.mark.parametrize(
    "checks",
    [
        [],
        ({"name": "okay", "expression": "True"},),
        [{"name": "unsafe", "expression": "row.__class__"}],
        [{"name": "same", "expression": "True"}, {"name": "same", "expression": "False"}],
    ],
)
def test_input_checks_reject_unsafe_or_ambiguous_configuration(checks: object) -> None:
    with pytest.raises(PluginConfigError):
        _make_transform_with_mock_provider(_make_config(input_checks=checks))


def test_omitted_null_and_passing_checks_preserve_native_request_and_output() -> None:
    row, ctx = _make_row(), _make_ctx()
    traces = []
    for option in ({}, {"input_checks": None}, {"input_checks": [{"name": "allowed", "expression": "True"}]}):
        options = _make_config(**option)
        transform, provider = _make_transform_with_mock_provider(options)
        assert transform.config == options
        provider.execute_query.return_value = _response({"answer": "generated"})
        result = transform._process_row(row, ctx)
        assert result.status == "success"
        traces.append((provider.execute_query.call_args, result.row.to_dict(), result.row.contract, result.success_reason))
    assert traces[0] == traces[1] == traces[2]


def test_input_check_configuration_is_snapshotted_before_processing() -> None:
    transform, provider = _make_transform_with_mock_provider(_make_config(input_checks=[{"name": "deny", "expression": "False"}]))
    transform._config.input_checks.clear()
    result = transform._process_row(_make_row(), _make_ctx())
    assert result.status == "error" and result.reason["operation"] == "deny"
    provider.execute_query.assert_not_called()
