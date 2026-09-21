"""Calculator diagnostics preserve unavailable cost without exporting SDK prose."""

import litellm
import pytest
from structlog.testing import capture_logs

from elspeth.web.composer.llm_response_parsing import _calculate_missing_provider_cost


def test_calculator_exception_emits_only_safe_structured_debug_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(**kwargs: object) -> tuple[float, float]:
        raise RuntimeError("secret response content and credentials must never be logged")

    monkeypatch.setattr(litellm, "cost_per_token", unavailable)
    with capture_logs() as logs:
        result = _calculate_missing_provider_cost({"prompt_tokens": 11, "completion_tokens": 3}, model_requested="azure/gpt-4.1")
    assert result == (None, "not_available")
    assert logs == [
        {
            "event": "planner_cost_recovery",
            "log_level": "debug",
            "reason": "calculator_exception",
            "error_class": "RuntimeError",
            "model_requested": "azure/gpt-4.1",
            "prompt_tokens": 11,
            "completion_tokens": 3,
            "cached_prompt_tokens": None,
            "reasoning_tokens": None,
            "cache_creation_input_tokens": None,
            "cache_read_input_tokens": None,
        }
    ]


def test_successful_cost_recovery_does_not_create_parallel_call_result_log(monkeypatch: pytest.MonkeyPatch) -> None:
    def priced(**kwargs: object) -> tuple[float, float]:
        return 0.01, 0.02

    monkeypatch.setattr(litellm, "cost_per_token", priced)
    with capture_logs() as logs:
        result = _calculate_missing_provider_cost({"prompt_tokens": 11, "completion_tokens": 3}, model_requested="azure/gpt-4.1")
    assert result == (0.03, "litellm.cost_per_token")
    assert logs == []


def test_diagnostic_model_identity_is_redacted_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(**kwargs: object) -> tuple[float, float]:
        raise ValueError("private exception prose")

    monkeypatch.setattr(litellm, "cost_per_token", unavailable)
    with capture_logs() as logs:
        result = _calculate_missing_provider_cost(
            {"prompt_tokens": 11, "completion_tokens": 3},
            model_requested="https://private-provider.invalid/secret-deployment/" + "x" * 1000,
        )
    assert result == (None, "not_available")
    assert len(logs) == 1
    identity = logs[0]["model_requested"]
    assert len(identity) <= 512
    assert "private-provider" not in identity
    assert "secret-deployment" not in identity
    assert "raw_error_hash=" in identity
