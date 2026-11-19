"""A request sent over the estimate and then rejected by the gateway."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from litellm.exceptions import BadRequestError, ContextWindowExceededError

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.protocol import ComposerConvergenceError, ComposerHistoryMessage
from elspeth.web.composer.provider_gateway import _BadRequestLLMError
from elspeth.web.composer.service import ComposerServiceImpl
from tests.unit.web.composer._helpers import _clean_advisor_checkpoint, _composer_service_with_session
from tests.unit.web.composer.test_compose_loop_context_budget import _assistant, _user
from tests.unit.web.composer.test_compose_loop_llm_audit import (
    _empty_state,
    _make_settings,
    _mock_catalog,
    _passing_preflight,
)

_GENEROUS_SETTING = 10_000_000
_SMALL_SETTING = 20_000


@pytest.fixture(autouse=True)
def _advisor_end_gate_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """The END advisor gate is not under test; it returns CLEAN without a provider call."""
    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint, raising=True)


class _ScriptedProvider:
    """A ``litellm.acompletion`` fake: records each request, then returns or raises the next scripted item."""

    def __init__(self, *items: Any) -> None:
        self._items = iter(items)
        self.requests: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> Any:
        self.requests.append(deepcopy(kwargs))
        item = next(self._items)
        if isinstance(item, BaseException):
            raise item
        return item


def _too_long() -> ContextWindowExceededError:
    return ContextWindowExceededError(
        message="AnthropicError - prompt is too long: 251000 tokens > 200000 maximum", model="test-model", llm_provider="anthropic"
    )


def _other_rejection(message: str = "GatewayError - unsupported value for field 'temperature'") -> BadRequestError:
    return BadRequestError(message=message, model="test-model", llm_provider="openai")


def _service(max_prompt_tokens: int) -> tuple[ComposerServiceImpl, str]:
    return _composer_service_with_session(
        catalog=_mock_catalog(), settings=_make_settings(composer_context_max_prompt_tokens=max_prompt_tokens)
    )


async def _compose(
    service: ComposerServiceImpl, session_id: str, provider: _ScriptedProvider, history: list[ComposerHistoryMessage] | None = None
) -> Any:
    with (
        patch("litellm.acompletion", new_callable=AsyncMock, side_effect=provider.complete),
        patch.object(service._preflight, "cached_runtime_preflight", new_callable=AsyncMock, return_value=_passing_preflight()),
    ):
        return await service.compose("Build a CSV pipeline", history or [], _empty_state(), session_id=session_id)


def _history() -> list[ComposerHistoryMessage]:
    return [_user(0, "Please read the CSV file."), _assistant("I have set up the CSV source.")]


def _history_texts(request: dict[str, Any]) -> list[str]:
    wanted = {row["content"] for row in _history()}
    return [message["content"] for message in request["messages"] if message["content"] in wanted]


@pytest.mark.asyncio
async def test_a_capacity_rejection_reaches_the_user_as_one() -> None:
    """Nothing is left to leave out: the gateway's rejection is the size refusal, naming the gateway as the ceiling."""
    service, session_id = _service(_SMALL_SETTING)
    provider = _ScriptedProvider(_too_long())

    with pytest.raises(ComposerConvergenceError) as exc_info:
        await _compose(service, session_id, provider)

    assert len(provider.requests) == 1
    error = exc_info.value
    assert error.reason == "context_budget_exceeded"
    assert error.evidence["ceiling"] == "provider"
    # The rejected call is on the audit trail with a closed reason, not provider text.
    (call,) = error.llm_calls
    assert call.status is ComposerLLMCallStatus.BAD_REQUEST_ERROR
    assert call.error_class == "ContextWindowExceededError"
    assert call.error_message == "context_length_exceeded"
    assert call.context_budget is not None


@pytest.mark.asyncio
async def test_an_unrecognised_rejection_of_a_request_sent_over_the_estimate_carries_that_fact() -> None:
    service, session_id = _service(_SMALL_SETTING)
    provider = _ScriptedProvider(_other_rejection())

    with pytest.raises(_BadRequestLLMError) as exc_info:
        await _compose(service, session_id, provider)

    assert len(provider.requests) == 1
    assert exc_info.value.sent_over_estimate is True
