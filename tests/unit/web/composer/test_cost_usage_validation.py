"""Malformed optional usage cannot silently become absent during pricing."""

from typing import Any

import litellm
import pytest

from tests.unit.web.composer.test_llm_response_parsing_azure_cost import _record, _response


@pytest.mark.parametrize(
    "counter", ["cache_read_input_tokens", "cache_creation_input_tokens", "reasoning_tokens", "cached", "reasoning", "output_reasoning"]
)
@pytest.mark.parametrize("bad", [True, -1, "50", 1.5, float("nan"), float("inf")])
def test_malformed_optional_usage_blocks_calculation(monkeypatch: pytest.MonkeyPatch, counter: str, bad: object) -> None:
    def forbidden(**kwargs: Any) -> tuple[float, float]:
        pytest.fail("malformed optional usage must not reach pricing")

    monkeypatch.setattr(litellm, "cost_per_token", forbidden)
    usage: dict[str, Any] = {"prompt_tokens": 100, "completion_tokens": 20}
    if counter == "cached":
        usage["prompt_tokens_details"] = {"cached_tokens": bad}
    elif counter == "reasoning":
        usage["completion_tokens_details"] = {"reasoning_tokens": bad}
    elif counter == "output_reasoning":
        usage["output_tokens_details"] = {"reasoning_tokens": bad}
    else:
        usage[counter] = bad

    record = _record({"usage": usage})
    assert record.provider_cost is None


@pytest.mark.parametrize("details", ["prompt_tokens_details", "completion_tokens_details", "output_tokens_details"])
@pytest.mark.parametrize("bad", ["broken", [], 3, False])
def test_malformed_detail_container_blocks_calculation(monkeypatch: pytest.MonkeyPatch, details: str, bad: object) -> None:
    def forbidden(**kwargs: Any) -> tuple[float, float]:
        pytest.fail("malformed detail container must not reach pricing")

    monkeypatch.setattr(litellm, "cost_per_token", forbidden)
    assert _record({"usage": {"prompt_tokens": 100, "completion_tokens": 20, details: bad}}).provider_cost is None


@pytest.mark.parametrize("value", [None, 0])
def test_none_and_zero_optional_counters_remain_supported(value: int | None) -> None:
    record = _record({"usage": {"prompt_tokens": 100, "completion_tokens": 20, "cache_read_input_tokens": value}})
    assert record.provider_cost == pytest.approx(0.00045)


def test_explicit_provider_cost_does_not_require_optional_counter_repair() -> None:
    record = _record({"usage": {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.02, "cache_read_input_tokens": "bad"}})
    assert record.provider_cost == 0.02
    assert record.provider_cost_source == "response_usage.cost"


def test_private_provider_cost_preserved_with_malformed_optional_counter() -> None:
    response = _response()
    response.usage.cache_read_input_tokens = "bad"
    response._hidden_params["response_cost"] = 0.02
    record = _record(response)
    assert record.provider_cost == 0.02
    assert record.provider_cost_source == "_hidden_params.response_cost"
