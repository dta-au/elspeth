"""Missing-cost recovery retains provider pricing dimensions."""

from typing import Any

import pytest
from litellm import ModelResponse, Usage

from elspeth.core.llm_pricing import provider_cost_from_response


def _mock_catalog(monkeypatch: pytest.MonkeyPatch, model: str, **rates: Any) -> None:
    import litellm

    info = {"key": model, "input_cost_per_token": 0.0, "output_cost_per_token": 0.0, **rates}
    monkeypatch.setitem(litellm.model_cost, model, info)
    monkeypatch.setattr(litellm, "get_model_info", lambda **kwargs: info)


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
    cost, source = provider_cost_from_response({"usage": _usage()}, pricing_model="bedrock/anthropic.claude-sonnet-4-5-20250929-v1:0")
    # 20 uncached input * $3/M + 80 one-hour writes * $6/M + 20 output * $15/M.
    assert cost == pytest.approx(0.00084)
    assert source == "litellm.cost_per_token"


@pytest.mark.parametrize("tier,expected", [("default", 0.000325), ("priority", 0.00065), ("flex", 0.0001625)])
def test_service_tier_preserves_price(tier: str, expected: float) -> None:
    cost, _ = provider_cost_from_response(
        {"usage": {"prompt_tokens": 100, "completion_tokens": 20}, "service_tier": tier}, pricing_model="openai/gpt-5"
    )
    assert cost == pytest.approx(expected)


@pytest.mark.parametrize("tier", ["unknown", True, 3, {}])
def test_invalid_tier_stays_unavailable(tier: object) -> None:
    response = {"usage": {"prompt_tokens": 100, "completion_tokens": 20}, "service_tier": tier}
    assert provider_cost_from_response(response, pricing_model="openai/gpt-5") == (None, "not_available")
    response["usage"]["cost"] = 0.04
    assert provider_cost_from_response(response, pricing_model="openai/gpt-5") == (0.04, "response_usage.cost")


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
    assert provider_cost_from_response({"usage": usage}, pricing_model="openai/gpt-5") == (None, "not_available")


def test_bedrock_standard_tier_and_private_cost_preserved() -> None:
    cost, _ = provider_cost_from_response(
        {"usage": _usage(), "service_tier": "standard"}, pricing_model="bedrock/anthropic.claude-sonnet-4-5-20250929-v1:0"
    )
    assert cost == pytest.approx(0.00084)
    response = ModelResponse(model="gpt-5", usage=Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120))
    response.service_tier = "malformed"
    response._hidden_params["response_cost"] = 0.07
    assert provider_cost_from_response(response, pricing_model="openai/gpt-5") == (0.07, "_hidden_params.response_cost")


def test_unpriced_one_hour_writes_stay_unavailable() -> None:
    # This model's bundled map has five-minute pricing but no one-hour rate.
    assert provider_cost_from_response({"usage": _usage()}, pricing_model="bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0") == (
        None,
        "not_available",
    )


@pytest.mark.parametrize("rate", [None, True, -1.0, "0.1", float("nan"), float("inf")])
@pytest.mark.parametrize("one_hour", [False, True])
def test_missing_or_invalid_required_cache_rate_prevents_calculator(monkeypatch: pytest.MonkeyPatch, rate: object, one_hour: bool) -> None:
    import litellm

    field = "cache_creation_input_token_cost_above_1hr" if one_hour else "cache_creation_input_token_cost"
    _mock_catalog(monkeypatch, "bedrock/model", **{field: rate})

    def forbidden(**kwargs: Any) -> tuple[float, float]:
        pytest.fail("required cache-write price is unavailable")

    monkeypatch.setattr(litellm, "cost_per_token", forbidden)
    usage = _usage()
    if not one_hour:
        usage.pop("prompt_tokens_details")
    assert provider_cost_from_response({"usage": usage}, pricing_model="bedrock/model") == (None, "not_available")


def test_supported_default_cache_write_rate_is_preserved() -> None:
    usage = _usage()
    usage.pop("prompt_tokens_details")
    cost, source = provider_cost_from_response({"usage": usage}, pricing_model="bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0")
    assert cost == pytest.approx(0.00066)
    assert source == "litellm.cost_per_token"


def test_explicit_zero_cache_write_rate_is_not_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import litellm

    _mock_catalog(monkeypatch, "bedrock/model", cache_creation_input_token_cost_above_1hr=0.0)
    monkeypatch.setattr(litellm, "cost_per_token", lambda **kwargs: (0.0, 0.0))
    assert provider_cost_from_response({"usage": _usage()}, pricing_model="bedrock/model") == (0.0, "litellm.cost_per_token")


def test_explicit_provider_cost_wins_over_unpriced_cache_duration() -> None:
    usage = _usage()
    usage["cost"] = 0.02
    assert provider_cost_from_response({"usage": usage}, pricing_model="bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0") == (
        0.02,
        "response_usage.cost",
    )


@pytest.mark.parametrize("model", ["vertex_ai/gemini-3.1-pro-preview", "openrouter/google/gemini-3.1-pro-preview"])
@pytest.mark.parametrize("prompt_tokens", [200000, 200001, 300000])
def test_threshold_only_cache_write_price(model: str, prompt_tokens: int) -> None:
    response = {"usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 20, "cache_creation_input_tokens": 80000}}
    cost, source = provider_cost_from_response(response, pricing_model=model)
    if prompt_tokens <= 200000:
        assert (cost, source) == (None, "not_available")
    else:
        # The bundled map supplies the write rate only above 200k tokens.
        assert cost == pytest.approx((prompt_tokens - 80000) * 4e-6 + 80000 * 2.5e-7 + 20 * 18e-6)
        assert source == "litellm.cost_per_token"


@pytest.mark.parametrize("rate", [True, -1.0, "0.1", float("nan"), float("inf")])
@pytest.mark.parametrize("threshold", [False, True])
def test_malformed_selected_cache_rate_rejects(monkeypatch: pytest.MonkeyPatch, rate: object, threshold: bool) -> None:
    import litellm

    key = "cache_creation_input_token_cost" + ("_above_200k_tokens" if threshold else "") + "_priority"
    info = {"cache_creation_input_token_cost": 0.001, "input_cost_per_token_above_200k_tokens": 0.002, key: rate}
    _mock_catalog(monkeypatch, "openai/model", **info)

    def forbidden(**kwargs: Any) -> tuple[float, float]:
        pytest.fail("selected cache rate is malformed")

    monkeypatch.setattr(litellm, "cost_per_token", forbidden)
    response = {
        "usage": {"prompt_tokens": 300000 if threshold else 100, "completion_tokens": 20, "cache_creation_input_tokens": 80},
        "service_tier": "priority",
    }
    assert provider_cost_from_response(response, pricing_model="openai/model") == (None, "not_available")
    response["usage"]["cost"] = 0.03
    assert provider_cost_from_response(response, pricing_model="openai/model") == (0.03, "response_usage.cost")


def test_zero_selected_threshold_rate_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    import litellm

    info = {"input_cost_per_token_above_200k_tokens": 0.002, "cache_creation_input_token_cost_above_200k_tokens_priority": 0.0}
    _mock_catalog(monkeypatch, "openai/model", **info)
    monkeypatch.setattr(litellm, "cost_per_token", lambda **kwargs: (0.0, 0.0))
    response = {"usage": {"prompt_tokens": 300000, "completion_tokens": 20, "cache_creation_input_tokens": 80}, "service_tier": "priority"}
    assert provider_cost_from_response(response, pricing_model="openai/model") == (0.0, "litellm.cost_per_token")
