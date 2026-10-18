"""Required gateway usage admission and its audited completion boundary."""

from typing import Any

import httpx
import pytest
import respx

from elspeth.contracts import CallStatus, CallType
from elspeth.contracts.call_governance import LLMCallGovernance
from elspeth.contracts.chat_parts import ChatMessage
from elspeth.contracts.token_usage import TokenUsage
from elspeth.plugins.infrastructure.clients.llm import LLMClientError, public_llm_error_category
from elspeth.plugins.transforms.llm.provider import LLMAuditParent
from elspeth.plugins.transforms.llm.providers.gateway import GatewayLLMProvider, _validate_gateway_success_response
from tests.unit.plugins.llm.test_provider_gateway import (
    _ENDPOINT,
    _LEADER_TOKEN,
    _READYZ_ROOT,
    FakeAuditRecorder,
    FakeTelemetryEmit,
    _completion_body,
    _gateway_response,
    _readyz_body,
)

_SENTINEL = "untrusted-usage-evidence"
_UNUSABLE = [
    None,
    {},
    {_SENTINEL: 100},
    _SENTINEL,
    [],
    {"cost": 1},
    {"prompt_tokens": True, "completion_tokens": -1, "total_tokens": "10"},
    {"prompt_tokens_details": {"cached_tokens": False}, "completion_tokens_details": {"reasoning_tokens": 1.5}},
]


def _response(usage: Any) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "standard",
            "choices": [{"message": {"content": "accepted text"}, "finish_reason": "stop"}],
            "usage": usage,
        },
    )


@pytest.mark.parametrize("raw_usage", _UNUSABLE)
def test_required_usage_refuses_no_usable_token_evidence(raw_usage: Any) -> None:
    with pytest.raises(LLMClientError, match="required") as error:
        _validate_gateway_success_response(_response(raw_usage), usage_required=True)
    assert error.value.retryable is False
    assert public_llm_error_category(error.value) == "client"
    assert _SENTINEL not in str(error.value)


def test_omitted_required_usage_retains_its_existing_message() -> None:
    body = _response(None).json()
    del body["usage"]
    with pytest.raises(LLMClientError) as error:
        _validate_gateway_success_response(httpx.Response(200, json=body), usage_required=True)
    assert str(error.value) == "Gateway response omitted usage data required by this profile's required_capabilities"
    assert error.value.retryable is False


@pytest.mark.parametrize("raw_usage", _UNUSABLE)
def test_optional_usage_preserves_unknown_without_fabricating_zero(raw_usage: Any) -> None:
    _, content, usage, _, _ = _validate_gateway_success_response(_response(raw_usage), usage_required=False)
    assert content == "accepted text"
    assert usage == TokenUsage.unknown()
    assert usage.to_dict() == {}


@pytest.mark.parametrize(
    ("raw_usage", "expected"),
    [
        ({"prompt_tokens": 0, "completion_tokens": 0}, TokenUsage.known(0, 0)),
        ({"prompt_tokens": 3}, TokenUsage(prompt_tokens=3)),
        ({"completion_tokens": 0}, TokenUsage(completion_tokens=0)),
        ({"total_tokens": 0}, TokenUsage(reported_total=0)),
        ({"cached_prompt_tokens": 0}, TokenUsage(cached_prompt_tokens=0)),
        ({"prompt_tokens_details": {"cached_tokens": 2}}, TokenUsage(cached_prompt_tokens=2)),
        ({"cache_creation_input_tokens": 1}, TokenUsage(cache_creation_input_tokens=1)),
        ({"cache_read_input_tokens": 0}, TokenUsage(cache_read_input_tokens=0)),
        ({"reasoning_tokens": 0}, TokenUsage(reasoning_tokens=0)),
        ({"completion_tokens_details": {"reasoning_tokens": 4}}, TokenUsage(reasoning_tokens=4)),
        ({"output_tokens_details": {"reasoning_tokens": 1}}, TokenUsage(reasoning_tokens=1)),
        ({"cached_prompt_tokens": -1, "prompt_tokens_details": {"cached_tokens": 0}}, TokenUsage(cached_prompt_tokens=0)),
        ({"reasoning_tokens": False, "completion_tokens_details": {"reasoning_tokens": 0}}, TokenUsage(reasoning_tokens=0)),
        ({"prompt_tokens": -1, "completion_tokens": 5}, TokenUsage(completion_tokens=5)),
    ],
)
def test_required_usage_keeps_supported_partial_aggregate_and_zero_forms(raw_usage: Any, expected: TokenUsage) -> None:
    _, content, usage, _, _ = _validate_gateway_success_response(_response(raw_usage), usage_required=True)
    assert content == "accepted text"
    assert usage == expected


@pytest.mark.parametrize("preflight", [False, True])
@pytest.mark.parametrize("raw_usage,accepted", [({}, False), ({"completion_tokens": 0}, True)])
@respx.mock
def test_required_usage_keeps_audit_governance_and_operation_parent(raw_usage: dict[str, int], accepted: bool, preflight: bool) -> None:
    recorder = FakeAuditRecorder()
    events: list[str] = []

    def before() -> str:
        events.append("before")
        return "attempt-required-usage"

    def after(attempt_id: str, call_id: str) -> None:
        assert attempt_id == "attempt-required-usage"
        assert call_id == f"operation-call-{len(recorder.operation_calls)}"
        assert recorder.operation_calls[-1]["call_type"] is CallType.LLM
        events.append("after")

    provider = GatewayLLMProvider(
        endpoint=_ENDPOINT,
        api_key="test-key",
        contract_major=1,
        required_capabilities=("usage",),
        recorder=recorder,
        run_id="run-1",
        telemetry_emit=FakeTelemetryEmit(),
        approved_prompt_artifact_hash="c" * 64,
        llm_call_governance=LLMCallGovernance(before_call=before, after_call=after),
    )
    completion = respx.post(f"{_ENDPOINT}/chat/completions").mock(return_value=_gateway_response(_completion_body(usage=raw_usage)))
    if preflight:
        ready = respx.get(f"{_READYZ_ROOT}/readyz").mock(return_value=_gateway_response(_readyz_body()))

    def execute() -> None:
        if preflight:
            provider.runtime_preflight(operation_id="op-usage", model="standard", temperature=0.0, coordination_token=_LEADER_TOKEN)
        else:
            provider.execute_query(
                [ChatMessage(role="user", content="hello")],
                model="standard",
                temperature=0.0,
                max_tokens=32,
                audit_parent=LLMAuditParent.for_operation(operation_id="op-usage", coordination_token=_LEADER_TOKEN),
            )

    if accepted:
        execute()
    else:
        with pytest.raises(LLMClientError, match="required") as error:
            execute()
        assert error.value.retryable is False

    assert completion.call_count == 1
    if preflight:
        assert ready.call_count == 1
    assert events == ["before", "after"]
    assert recorder.calls == []
    assert recorder.allocated_state_ids == []
    assert recorder.allocated_operation_ids == ["op-usage"] * (3 if preflight else 2)
    calls = recorder.operation_calls
    assert [call["call_index"] for call in calls] == list(range(len(calls)))
    assert all(call["operation_id"] == "op-usage" for call in calls)
    http_calls = [call for call in calls if call["call_type"] is CallType.HTTP]
    assert len(http_calls) == (2 if preflight else 1)
    assert all(call["status"] is CallStatus.SUCCESS for call in http_calls)
    llm_calls = [call for call in calls if call["call_type"] is CallType.LLM]
    assert len(llm_calls) == 1
    call = llm_calls[0]
    assert call["status"] is (CallStatus.SUCCESS if accepted else CallStatus.ERROR)
    expected = TokenUsage(completion_tokens=0) if accepted else TokenUsage.unknown()
    assert call["token_usage"] == expected
    assert call["token_usage"].to_dict() == ({"completion_tokens": 0} if accepted else {})
    assert call["approved_prompt_artifact_hash"] == "c" * 64
    if not accepted:
        assert call["error"].category == "client"
        assert call["error"].retryable is False
    assert provider._http_clients == {}
    assert provider._http_client_refs == {}
