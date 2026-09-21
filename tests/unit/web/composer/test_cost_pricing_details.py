"""Missing-cost recovery retains provider pricing dimensions."""

from typing import Any

import pytest
from litellm import ModelResponse, Usage

from elspeth.web.composer.llm_response_parsing import _provider_cost_from_response


def _usage() -> dict[str, Any]:
    return {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "cache_creation_input_tokens": 80,
        "prompt_tokens_details": {
            "cache_creation_tokens": 80,
            "cache_creation_token_details": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 80},
        },
    }


def test_cache_write_ttl_preserves_price() -> None:
    cost, source = _provider_cost_from_response({"usage": _usage()}, model_requested="bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0")
    assert cost == pytest.approx(0.00096)
    assert source == "litellm.cost_per_token"


@pytest.mark.parametrize("tier,expected", [("default", 0.000325), ("priority", 0.00065), ("flex", 0.0001625)])
def test_service_tier_preserves_price(tier: str, expected: float) -> None:
    cost, _ = _provider_cost_from_response(
        {"usage": {"prompt_tokens": 100, "completion_tokens": 20}, "service_tier": tier}, model_requested="openai/gpt-5"
    )
    assert cost == pytest.approx(expected)


@pytest.mark.parametrize("tier", ["unknown", True, 3, {}])
def test_invalid_tier_stays_unavailable(tier: object) -> None:
    response = {"usage": {"prompt_tokens": 100, "completion_tokens": 20}, "service_tier": tier}
    assert _provider_cost_from_response(response, model_requested="openai/gpt-5") == (None, "not_available")
    response["usage"]["cost"] = 0.04
    assert _provider_cost_from_response(response, model_requested="openai/gpt-5") == (0.04, "response_usage.cost")


@pytest.mark.parametrize(
    "detail",
    [
        True,
        {},
        {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 81},
        {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": True},
    ],
)
def test_invalid_ttl_stays_unavailable(detail: object) -> None:
    usage = _usage()
    usage["prompt_tokens_details"]["cache_creation_token_details"] = detail
    assert _provider_cost_from_response({"usage": usage}, model_requested="openai/gpt-5") == (None, "not_available")


def test_bedrock_standard_tier_and_private_cost_preserved() -> None:
    cost, _ = _provider_cost_from_response(
        {"usage": _usage(), "service_tier": "standard"}, model_requested="bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0"
    )
    assert cost == pytest.approx(0.00096)
    response = ModelResponse(model="gpt-5", usage=Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120))
    response.service_tier = "malformed"
    response._hidden_params["response_cost"] = 0.07
    assert _provider_cost_from_response(response, model_requested="openai/gpt-5") == (0.07, "_hidden_params.response_cost")
