"""HTTP providers keep endpoint routing separate from monetary audit identity."""

import json
from contextlib import nullcontext
from unittest.mock import patch

import httpx
import litellm
import pytest
import respx

from elspeth.contracts import CallType
from elspeth.contracts.chat_parts import ChatMessage
from elspeth.plugins.infrastructure.clients.llm import LLMClientError
from elspeth.plugins.transforms.llm.provider import LLMAuditParent
from elspeth.plugins.transforms.llm.providers.gateway import GatewayLLMProvider
from elspeth.plugins.transforms.llm.providers.openrouter import OpenRouterLLMProvider
from tests.unit.plugins.llm.test_provider_gateway import _MEMBER_TOKEN, _WORK_ITEM, FakeAuditRecorder


@pytest.mark.parametrize("provider_name", ["gateway", "openrouter"])
@pytest.mark.parametrize("known_price", [True, False])
@pytest.mark.parametrize("outcome", ["success", "refused", "malformed"])
@respx.mock
def test_http_provider_prices_independently_without_changing_wire_model(provider_name: str, known_price: bool, outcome: str) -> None:
    recorder = FakeAuditRecorder()
    endpoint = "https://provider.example.com/v1"
    pricing_model = "azure/gpt-4o" if known_price else "elspeth-unknown-pricing-identity"
    provider = (
        GatewayLLMProvider(
            endpoint=endpoint,
            api_key="test-key",
            contract_major=1,
            recorder=recorder,
            run_id="run-1",
            telemetry_emit=lambda event: None,
            pricing_model=pricing_model,
        )
        if provider_name == "gateway"
        else OpenRouterLLMProvider(
            base_url=endpoint,
            api_key="test-key",
            recorder=recorder,
            run_id="run-1",
            telemetry_emit=lambda event: None,
            pricing_model=pricing_model,
        )
    )
    route = respx.post(f"{endpoint}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            headers={"X-ELSPETH-LLM-Gateway-Contract": "1"},
            json={
                "model": "returned-concrete-model",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "answer" if outcome == "success" else None},
                        "finish_reason": "content_filter" if outcome == "refused" else "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15} if outcome != "malformed" else None,
            },
        )
    )
    with (
        patch.object(litellm, "cost_per_token", return_value=(0.01, 0.02)) as calculator,
        pytest.raises(LLMClientError) if outcome != "success" else nullcontext(),
    ):
        result = provider.execute_query(
            [ChatMessage(role="user", content="hello")],
            model="operator-datazone-alias",
            temperature=None,
            max_tokens=20,
            audit_parent=LLMAuditParent.for_row(member_token=_MEMBER_TOKEN, work_item=_WORK_ITEM, state_id="state-1", token_id="tok-1"),
        )
    request = json.loads(route.calls.last.request.content)
    assert request["model"] == "operator-datazone-alias"
    assert "pricing_model" not in request
    if outcome == "success":
        assert result.model == "returned-concrete-model"
    calls = [call for call in recorder.calls if call["call_type"] == CallType.LLM]
    assert len(calls) == 1
    payload = calls[0]["response_data" if outcome == "success" else "error"].to_dict()
    assert payload["pricing_model"] == pricing_model
    if outcome == "success":
        assert payload["model"] == "returned-concrete-model"
    if known_price and outcome != "malformed":
        assert calculator.call_args.kwargs["model"] == pricing_model
        assert payload["provider_cost"] == 0.03
        assert payload["provider_cost_source"] == "litellm.cost_per_token"
    else:
        calculator.assert_not_called()
        assert payload["provider_cost"] is None
        assert payload["provider_cost_source"] == "not_available"
