"""Priceable requested models retain accounting across Azure deployment aliases."""

import math
import time
from datetime import UTC, datetime
from typing import Any

import litellm
import pytest
from litellm import ModelResponse, Usage

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.web.composer.llm_response_parsing import build_llm_call_record


def _response() -> ModelResponse:
    response = ModelResponse(
        model="gpt-5.6-terra-2026-07-09",
        usage=Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
    )
    response._hidden_params = {}
    return response


def _record(response: Any, *, model_requested: str = "openai/gpt-4o-2024-08-06"):
    return build_llm_call_record(
        model_requested=model_requested,
        messages=[{"role": "user", "content": "hello"}],
        tools=None,
        status=ComposerLLMCallStatus.SUCCESS,
        started_at=datetime.now(UTC),
        started_ns=time.monotonic_ns(),
        temperature=None,
        seed=None,
        response=response,
    )


def test_requested_model_prices_real_sdk_response_with_unpriceable_returned_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _response()

    def forbidden(**kwargs: Any) -> float:
        pytest.fail("completion_cost can replace requested identity with the dated returned model")

    monkeypatch.setattr(litellm, "completion_cost", forbidden)

    record = _record(response)

    assert record.provider_cost == pytest.approx(0.00045)
    assert record.provider_cost_source == "litellm.cost_per_token"
    assert record.model_returned == "gpt-5.6-terra-2026-07-09"


def test_calculator_receives_requested_model_and_validated_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def calculate(**kwargs: Any) -> tuple[float, float]:
        calls.append(kwargs)
        return 0.00025, 0.0002

    monkeypatch.setattr(litellm, "cost_per_token", calculate)
    record = _record(_response())
    assert len(calls) == 1
    assert calls[0]["model"] == "openai/gpt-4o-2024-08-06"
    assert calls[0]["prompt_tokens"] == 100
    assert calls[0]["completion_tokens"] == 20
    assert record.provider_cost == pytest.approx(0.00045)


def test_requested_model_pricing_preserves_cached_prompt_discount() -> None:
    response = _response()
    response.usage = Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120, prompt_tokens_details={"cached_tokens": 50})

    record = _record(response)

    assert record.provider_cost == pytest.approx(0.0003875)
    assert record.cached_prompt_tokens == 50
    assert record.provider_cost_source == "litellm.cost_per_token"


@pytest.mark.parametrize(
    "details", [{"prompt_tokens_details": {"cached_tokens": 101}}, {"completion_tokens_details": {"reasoning_tokens": 21}}]
)
def test_impossible_token_subtotals_never_reach_calculator(monkeypatch: pytest.MonkeyPatch, details: dict[str, Any]) -> None:
    def forbidden(**kwargs: Any) -> tuple[float, float]:
        pytest.fail("token subtotals must not exceed their parent count")

    monkeypatch.setattr(litellm, "cost_per_token", forbidden)
    response = _response()
    response.usage = Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120, **details)
    record = _record(response)
    assert record.provider_cost is None
    assert record.provider_cost_source == "not_available"


@pytest.mark.parametrize("location", ["usage", "hidden"])
@pytest.mark.parametrize("cost", [None, True, "0.1", -0.1, math.nan, math.inf])
def test_present_invalid_cost_never_calls_calculator(monkeypatch: pytest.MonkeyPatch, location: str, cost: object) -> None:
    def forbidden(**kwargs: Any) -> float:
        pytest.fail("present malformed cost must not be replaced by calculated cost")

    monkeypatch.setattr(litellm, "cost_per_token", forbidden)
    response = _response()
    if location == "usage":
        response.usage.cost = cost
    else:
        response._hidden_params["response_cost"] = cost

    record = _record(response)

    assert record.provider_cost is None
    assert record.provider_cost_source == "not_available"


def test_missing_usage_never_fabricates_calculated_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(**kwargs: Any) -> float:
        pytest.fail("absent usage must not be replaced by SDK token defaults")

    monkeypatch.setattr(litellm, "cost_per_token", forbidden)
    response = _response()
    response.usage = None

    assert _record(response).provider_cost is None


def test_calculator_failure_keeps_cost_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def unavailable(**kwargs: Any) -> float:
        calls.append(kwargs["model"])
        raise ValueError("pricing lookup unavailable")

    monkeypatch.setattr(litellm, "cost_per_token", unavailable)
    record = _record(_response())
    assert calls == ["openai/gpt-4o-2024-08-06"]
    assert record.provider_cost is None
    assert record.provider_cost_source == "not_available"


def test_unpriceable_requested_model_stays_unavailable() -> None:
    record = _record(_response(), model_requested="openai/unpriceable-cost-regression-model")
    assert record.provider_cost is None
    assert record.provider_cost_source == "not_available"


@pytest.mark.parametrize("model", ["openai/gpt-5.6-sol-datazone", "azure/gpt-5.6-unpriceable-deployment-pricing-regression"])
def test_unknown_deployment_alias_never_becomes_free(model: str) -> None:
    assert litellm.get_model_info(model=model)["key"] not in litellm.model_cost
    record = _record(_response(), model_requested=model)
    assert record.provider_cost is None
    assert record.provider_cost_source == "not_available"


def test_explicit_zero_catalog_prices_remain_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    model = "openai/explicit-free-pricing-control"
    monkeypatch.setitem(
        litellm.model_cost, model, {**litellm.model_cost["gpt-4o-2024-08-06"], "input_cost_per_token": 0.0, "output_cost_per_token": 0.0}
    )
    record = _record(_response(), model_requested=model)
    assert record.provider_cost == 0.0
    assert record.provider_cost_source == "litellm.cost_per_token"


@pytest.mark.parametrize("missing_rate", ["input_cost_per_token", "output_cost_per_token"])
def test_missing_catalog_rate_remains_unavailable(monkeypatch: pytest.MonkeyPatch, missing_rate: str) -> None:
    model = f"openai/missing-{missing_rate}-pricing-control"
    incomplete = dict(litellm.model_cost["gpt-4o-2024-08-06"])
    del incomplete[missing_rate]
    monkeypatch.setitem(litellm.model_cost, model, incomplete)
    record = _record(_response(), model_requested=model)
    assert record.provider_cost is None
    assert record.provider_cost_source == "not_available"


@pytest.mark.parametrize("costs", [None, [], [0.1, 0.2], (), (0.1,), (0.1, 0.2, 0.3), (1e308, 1e308)])
def test_malformed_calculator_result_stays_unavailable(monkeypatch: pytest.MonkeyPatch, costs: object) -> None:
    def malformed(**kwargs: Any) -> object:
        return costs

    monkeypatch.setattr(litellm, "cost_per_token", malformed)
    record = _record(_response())
    assert record.provider_cost is None
    assert record.provider_cost_source == "not_available"


@pytest.mark.parametrize("cost", [None, True, "0.1", -0.1, math.nan, math.inf])
@pytest.mark.parametrize("component", ["prompt", "completion"])
def test_invalid_calculated_cost_stays_unavailable(monkeypatch: pytest.MonkeyPatch, cost: object, component: str) -> None:
    def malformed(**kwargs: Any) -> object:
        return (cost, 0.0) if component == "prompt" else (0.0, cost)

    monkeypatch.setattr(litellm, "cost_per_token", malformed)
    record = _record(_response())
    assert record.provider_cost is None
    assert record.provider_cost_source == "not_available"
