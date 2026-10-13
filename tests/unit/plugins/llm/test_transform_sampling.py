"""Actual row execution preserves the operator sampling and audit contract.

HTTP providers reach fake HTTP transport; Bedrock reaches the LiteLLM entry
point. These controls do not assert upstream model acceptance or AWS wire.
"""

from __future__ import annotations

import pytest

from elspeth.contracts.call_data import LLMCallRequest
from elspeth.core.config import _lower_llm_profile_node_options
from elspeth.core.llm_profiles import LLM_PROFILE_PRIVATE_FIELDS, LLMProfileSettings
from elspeth.plugins.infrastructure.batching.ports import CollectorOutputPort
from elspeth.plugins.transforms.llm.transform import LLMTransform
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_context
from tests.fixtures.mock_audit import mock_audit_authority
from tests.unit.plugins.llm.test_azure import make_token
from tests.unit.plugins.llm.test_preflight_sampling import _Harness

pytest_plugins = ["tests.unit.plugins.llm.test_preflight_sampling"]


@pytest.mark.parametrize("query_mode", ["single", "sequential", "pooled"])
@pytest.mark.parametrize("temperature", ["omitted", None, 0.0, 0.7])
@pytest.mark.parametrize("profiled", [False, True])
@pytest.mark.parametrize("reject_explicit", [False, True])
def test_transform_row_sampling_matches_transport_and_audit(
    harness: _Harness,
    query_mode: str,
    temperature: str | float | None,
    profiled: bool,
    reject_explicit: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness.reject_sampling[0] = reject_explicit
    config = dict(harness.config)
    config["required_input_fields"] = ["text"]
    config["prompt_template"] = "{{ row.text }}"
    if temperature != "omitted":
        config["temperature"] = temperature
    if query_mode != "single":
        config["queries"] = {
            name: {"input_fields": {"text": "text"}, "output_fields": [{"suffix": "score", "type": "integer"}]}
            for name in ("first", "second")
        }
    if query_mode == "pooled":
        config["pool_size"] = 2
    if profiled:
        values = {key: value for key, value in config.items() if key in LLMProfileSettings.model_fields}
        values["model"] = config.get("model", config.get("deployment_name"))
        if config["provider"] == "gateway":
            values["contract_major"] = 1
            values["required_capabilities"] = ()
        if "api_key" in config:
            values["credential_ref"] = "SAMPLING_TEST_KEY"
            values["credential_scope"] = "server"
        options = {key: value for key, value in config.items() if key not in LLM_PROFILE_PRIVATE_FIELDS}
        options["profile"] = "operator"
        config, _ = _lower_llm_profile_node_options("operator", LLMProfileSettings.model_validate(values), options)
        if "api_key" in harness.config:
            config["api_key"] = harness.config["api_key"]
    transform = LLMTransform(config)
    monkeypatch.setattr(transform, "_create_provider", lambda **kwargs: harness.provider)
    collector = CollectorOutputPort()
    try:
        transform.on_start(make_context(**mock_audit_authority("test"), run_id="test", landscape=harness.recorder))
        transform.connect_output(collector, max_pending=10)
        ctx = make_context(
            **mock_audit_authority("test"), run_id="test", state_id="sampling-state", token=make_token(), landscape=harness.recorder
        )
        transform.accept(make_pipeline_row({"text": "synthetic input"}), ctx)
        transform.flush_batch_processing(timeout=10)
        assert len(collector.results) == 1
        rejected = reject_explicit and temperature not in (None, "omitted")
        assert collector.results[0][1].status == ("error" if rejected else "success")
        expected_calls = 1 if query_mode == "single" else 2
        if rejected:
            # Sequential strategy stops at the first failed query; pooled
            # siblings may already have dispatched. Neither retries a changed request.
            assert 1 <= len(harness.requests) <= expected_calls
        else:
            assert len(harness.requests) == expected_calls
        audited_requests = [call["request_data"] for call in harness.recorder.calls if isinstance(call["request_data"], LLMCallRequest)]
        assert len(audited_requests) == len(harness.requests)
        expected = None if temperature == "omitted" else temperature
        for payload in harness.requests:
            assert payload.get("temperature") == expected
            assert ("temperature" in payload) is (expected is not None)
        for request in audited_requests:
            assert request.temperature == expected
            assert ("temperature" in request.to_dict()) is (expected is not None)
        assert harness.recorder.operation_calls == []
    finally:
        transform.close()


@pytest.mark.parametrize("model", ["anthropic.claude-sonnet-4-5-20250929-v1:0", "anthropic.claude-opus-4-7-v1"])
@pytest.mark.parametrize("temperature", [None, 0.0, 1.0])
def test_locked_bedrock_translation_sampling_is_model_specific(model: str, temperature: float | None) -> None:
    """Exercise installed AWS translator below the mocked completion boundary.

    This measures the locked adapter, not live AWS acceptance. No global
    drop_params override or model-name dispatch is added to ELSPETH.
    """
    import litellm
    from litellm.llms.bedrock.chat.converse_transformation import AmazonConverseConfig

    assert litellm.drop_params is False
    params = {} if temperature is None else {"temperature": temperature}
    translate = AmazonConverseConfig().map_openai_params
    if model == "anthropic.claude-opus-4-7-v1" and temperature == 0.0:
        with pytest.raises(litellm.UnsupportedParamsError):
            translate(params, {}, model, False)
    else:
        mapped = translate(params, {}, model, False)
        assert mapped.get("temperature") == temperature
        assert ("temperature" in mapped) is (temperature is not None)
