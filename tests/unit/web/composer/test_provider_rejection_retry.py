"""A compose request the provider rejects as too large is retried once with every earlier turn left out.

The rule: if it fits it goes, even with everything else dropped. A request is
sent over its estimate and the provider judges its own limit, so a
context-length rejection is the real limit signal. While any earlier turn is
still in the request, the valid pathway is to leave out all of it and try
once more. If the retry is rejected too, or no earlier turn was left to
leave out, the turn alone does not fit the model and the existing size
refusal stands. Nothing of the turn in progress is ever left out.

The compose loop is driven against a ``litellm.acompletion`` fake that raises
the SDK's own exceptions. Every provider call made is on the recorder the
route persists (``llm_calls`` on the result or on the refusal).
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.contracts.composer_llm_audit import ComposerContextBudgetRecord, ComposerLLMCallStatus
from elspeth.web.composer.protocol import ComposerConvergenceError, ComposerHistoryMessage
from elspeth.web.composer.provider_gateway import _BadRequestLLMError
from tests.unit.web.composer.test_compose_loop_context_budget import _GENEROUS_TOKENS, _assistant, _tool_turn, _user
from tests.unit.web.composer.test_compose_loop_context_budget import _compose as _loop_compose
from tests.unit.web.composer.test_compose_loop_context_budget import _service as _loop_service
from tests.unit.web.composer.test_compose_loop_llm_audit import _make_llm_response
from tests.unit.web.composer.test_gateway_capacity_rejection import (
    _GENEROUS_SETTING,
    _compose,
    _other_rejection,
    _ScriptedProvider,
    _service,
    _too_long,
)
from tests.unit.web.composer.test_gateway_capacity_rejection import (
    _advisor_end_gate_clean as _advisor_end_gate_clean_fixture,
)

_advisor_end_gate_clean = _advisor_end_gate_clean_fixture

_CURRENT_MESSAGE = "Build a CSV pipeline"
_HISTORY_TEXTS = ("Please read the CSV file.", "I have set up the CSV source.", "Now add a filter.", "The filter is in place.")


def _history() -> list[ComposerHistoryMessage]:
    return [_user(0, _HISTORY_TEXTS[0]), _assistant(_HISTORY_TEXTS[1]), _user(2, _HISTORY_TEXTS[2]), _assistant(_HISTORY_TEXTS[3])]


def _history_in(request: dict[str, Any]) -> list[str]:
    return [message["content"] for message in request["messages"] if message["content"] in _HISTORY_TEXTS]


def _turn_in_progress(request: dict[str, Any]) -> list[dict[str, Any]]:
    """The current user message and everything after it, as sent."""
    messages = request["messages"]
    (start,) = [index for index, message in enumerate(messages) if message["role"] == "user" and message["content"] == _CURRENT_MESSAGE]
    return messages[start:]


def _budget(call: Any) -> ComposerContextBudgetRecord:
    assert call.context_budget is not None
    return call.context_budget


@pytest.mark.asyncio
async def test_a_rejection_with_history_present_is_retried_once_with_every_earlier_turn_left_out() -> None:
    service, session_id = _service(_GENEROUS_SETTING)
    provider = _ScriptedProvider(_too_long(), _make_llm_response(content="Pipeline built."))

    result = await _compose(service, session_id, provider, _history())

    assert len(provider.requests) == 2
    # The first request carried every earlier turn (the estimate left nothing out); the retry carries none.
    assert _history_in(provider.requests[0]) == list(_HISTORY_TEXTS)
    assert _history_in(provider.requests[1]) == []
    # The turn in progress is sent byte for byte the same in both.
    assert _turn_in_progress(provider.requests[1]) == _turn_in_progress(provider.requests[0])
    # Both provider calls are on the record the route persists.
    rejected, retried = result.llm_calls
    assert rejected.status is ComposerLLMCallStatus.BAD_REQUEST_ERROR
    assert rejected.error_message == "context_length_exceeded"
    assert retried.status is ComposerLLMCallStatus.SUCCESS
    assert _budget(rejected).shed_history_indices == ()
    assert _budget(rejected).retry_after_provider_rejection is None
    retry_budget = _budget(retried)
    assert retry_budget.retry_after_provider_rejection is True
    assert retry_budget.shed_history_indices == (0, 1, 2, 3)
    assert retry_budget.stubbed_tool_call_ids == ()
    assert retry_budget.to_dict()["retry_after_provider_rejection"] is True
    assert "retry_after_provider_rejection" not in _budget(rejected).to_dict()


@pytest.mark.asyncio
async def test_a_retry_that_is_rejected_too_is_the_size_refusal_after_exactly_two_calls() -> None:
    service, session_id = _service(_GENEROUS_SETTING)
    provider = _ScriptedProvider(_too_long(), _too_long(), _make_llm_response(content="never sent"))

    with pytest.raises(ComposerConvergenceError) as exc_info:
        await _compose(service, session_id, provider, _history())

    assert len(provider.requests) == 2  # never a third
    error = exc_info.value
    assert error.reason == "context_budget_exceeded"
    assert error.evidence["ceiling"] == "provider"
    first, second = error.llm_calls
    assert first.status is second.status is ComposerLLMCallStatus.BAD_REQUEST_ERROR
    assert _budget(second).retry_after_provider_rejection is True
    assert _budget(second).shed_history_indices == (0, 1, 2, 3)
    # The refusal is sized on the request that was sent last: the retry's.
    assert error.evidence["request_bytes"] == _budget(second).request_wire_bytes


@pytest.mark.asyncio
async def test_the_retry_is_made_once_even_if_the_rebuilt_request_still_carried_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bound is the retry itself, not only what it left out.

    The rebuild is made to ignore the instruction to leave everything out, so
    the retried request still carries every earlier turn. It is still the only
    retry: a second rejection is the refusal, with no third call.
    """
    from elspeth.web.composer.service import ComposerServiceImpl

    original = ComposerServiceImpl._shed_to_context_budget

    def ignoring_the_retry(self: ComposerServiceImpl, **kwargs: Any) -> Any:
        return original(self, **{**kwargs, "shed_all_history": False})

    monkeypatch.setattr(ComposerServiceImpl, "_shed_to_context_budget", ignoring_the_retry)
    service, session_id = _service(_GENEROUS_SETTING)
    provider = _ScriptedProvider(_too_long(), _too_long(), _make_llm_response(content="never sent"))

    with pytest.raises(ComposerConvergenceError) as exc_info:
        await _compose(service, session_id, provider, _history())

    assert len(provider.requests) == 2
    assert [_history_in(request) for request in provider.requests] == [list(_HISTORY_TEXTS), list(_HISTORY_TEXTS)]
    assert exc_info.value.evidence["ceiling"] == "provider"


@pytest.mark.asyncio
async def test_a_rejection_with_no_history_is_the_existing_refusal_after_one_call() -> None:
    service, session_id = _service(_GENEROUS_SETTING)
    provider = _ScriptedProvider(_too_long(), _make_llm_response(content="never sent"))

    with pytest.raises(ComposerConvergenceError) as exc_info:
        await _compose(service, session_id, provider)

    assert len(provider.requests) == 1
    assert exc_info.value.evidence["ceiling"] == "provider"
    (call,) = exc_info.value.llm_calls
    assert _budget(call).retry_after_provider_rejection is None


@pytest.mark.asyncio
async def test_control_a_rejection_that_is_not_about_size_sheds_nothing_and_is_not_retried() -> None:
    service, session_id = _service(_GENEROUS_SETTING)
    provider = _ScriptedProvider(_other_rejection(), _make_llm_response(content="never sent"))

    with pytest.raises(_BadRequestLLMError):
        await _compose(service, session_id, provider, _history())

    assert len(provider.requests) == 1
    assert _history_in(provider.requests[0]) == list(_HISTORY_TEXTS)


@pytest.mark.asyncio
async def test_history_left_out_by_the_retry_stays_out_for_the_rest_of_the_turn() -> None:
    """The retry's shed is held for the turn: a later call, however small, never sends the history the provider refused."""
    service, session_id = _loop_service(_GENEROUS_TOKENS)
    provider = _ScriptedProvider(_too_long(), _tool_turn("call-1"), _make_llm_response(content="Pipeline built."))

    result = await _loop_compose(service, session_id, _history(), provider, tool_data={"call-1": {"rows": "r" * 2_000}})

    assert len(provider.requests) == 3
    assert [_history_in(request) for request in provider.requests] == [list(_HISTORY_TEXTS), [], []]
    later = _budget(result.llm_calls[2])
    assert later.shed_history_indices == (0, 1, 2, 3)
    # Only the retry itself carries the marker.
    assert later.retry_after_provider_rejection is None
    # The round made after the retry is in the last request whole.
    (tool_message,) = [message for message in _turn_in_progress(provider.requests[2]) if message["role"] == "tool"]
    assert "r" * 2_000 in tool_message["content"]


@pytest.mark.asyncio
async def test_a_rejection_mid_turn_is_retried_with_every_round_of_the_turn_whole() -> None:
    """Pressure arrives after a tool round: the retry leaves out only earlier turns, never the round already made."""
    rows = "r" * 20_000
    service, session_id = _loop_service(_GENEROUS_TOKENS)
    provider = _ScriptedProvider(_tool_turn("call-1"), _too_long(), _make_llm_response(content="Pipeline built."))

    result = await _loop_compose(service, session_id, _history(), provider, tool_data={"call-1": {"rows": rows}})

    assert len(provider.requests) == len(result.llm_calls) == 3
    assert [_history_in(request) for request in provider.requests] == [list(_HISTORY_TEXTS), list(_HISTORY_TEXTS), []]
    # The rejected request and its retry carry the same turn in progress, the tool round whole.
    assert _turn_in_progress(provider.requests[2]) == _turn_in_progress(provider.requests[1])
    (tool_message,) = [message for message in _turn_in_progress(provider.requests[2]) if message["role"] == "tool"]
    assert rows in tool_message["content"]
    assert [call.status for call in result.llm_calls] == [
        ComposerLLMCallStatus.SUCCESS,
        ComposerLLMCallStatus.BAD_REQUEST_ERROR,
        ComposerLLMCallStatus.SUCCESS,
    ]
    assert [_budget(call).retry_after_provider_rejection for call in result.llm_calls] == [None, None, True]
    assert all(_budget(call).stubbed_tool_call_ids == () for call in result.llm_calls)
