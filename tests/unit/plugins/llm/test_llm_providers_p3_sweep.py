"""Regression tests for the 20261111-plugins-review P3 LLM-provider sweep findings."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from elspeth.plugins.infrastructure.clients.llm import LLMClientError
from elspeth.plugins.transforms.llm.providers.gateway import _validate_gateway_success_response
from tests.unit.plugins.llm.test_provider_gateway import _completion_body, _gateway_response


def _raised_by(call: Callable[[], object]) -> BaseException | None:
    """Return what ``call`` raised (or None) so every outcome lands on an assertion."""
    try:
        call()
    except Exception as exc:
        return exc
    return None


# ---------------------------------------------------------------------------
# Finding #62: a required 'usage' capability must fail closed on malformed usage
# ---------------------------------------------------------------------------


def _completion_with_usage(usage: object) -> dict[str, Any]:
    body = _completion_body()
    body["usage"] = usage
    return body


class TestGatewayRequiredUsageFailsClosed:
    @pytest.mark.parametrize(
        "usage",
        [
            pytest.param({}, id="empty-object"),
            pytest.param("junk", id="string"),
            pytest.param({"prompt_tokens": "10", "completion_tokens": "5"}, id="string-counts"),
            pytest.param(5, id="int"),
            pytest.param({"prompt_tokens": 10}, id="completion-missing"),
            pytest.param({"completion_tokens": 5}, id="prompt-missing"),
            pytest.param({"prompt_tokens": True, "completion_tokens": 5}, id="bool-count"),
        ],
    )
    def test_malformed_usage_is_rejected_when_required(self, usage: object) -> None:
        response = _gateway_response(_completion_with_usage(usage))
        raised = _raised_by(lambda: _validate_gateway_success_response(response, usage_required=True))
        assert isinstance(raised, LLMClientError), f"malformed usage {usage!r} was accepted: {raised!r}"
        assert raised.retryable is False
        assert "usage" in str(raised)

    def test_well_formed_usage_is_accepted_when_required(self) -> None:
        response = _gateway_response(_completion_with_usage({"prompt_tokens": 10, "completion_tokens": 5}))
        _, _, usage, _, _ = _validate_gateway_success_response(response, usage_required=True)
        assert usage.prompt_tokens == 10
        assert usage.completion_tokens == 5

    def test_malformed_usage_stays_unknown_when_not_required(self) -> None:
        response = _gateway_response(_completion_with_usage("junk"))
        _, _, usage, _, _ = _validate_gateway_success_response(response, usage_required=False)
        assert usage.prompt_tokens is None
        assert usage.completion_tokens is None
