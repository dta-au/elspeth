"""The compose loop fits every provider request to the operator's context budget.

Each test drives the real compose loop against a recording ``litellm.acompletion``
fake, so what is asserted is the request that would have been sent, and the
call record that describes it.
"""

from __future__ import annotations

import json
import time
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from elspeth.contracts.composer_llm_audit import ComposerContextBudgetRecord, ComposerLLMCall, ComposerLLMCallStatus
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.context_budget import CONTEXT_BUDGET_TOOL_RESULT_STUB
from elspeth.web.composer.context_budget_apply import (
    CONTEXT_BUDGET_OMISSION_ALLOWANCE_BYTES,
    message_bytes,
    request_wire_bytes,
    stubbed_tool_message,
    tool_result_segments,
)
from elspeth.web.composer.context_budget_history import ContextBudgetHistory
from elspeth.web.composer.llm_response_parsing import build_llm_call_record
from elspeth.web.composer.prompts import STATE_CONTEXT_PREFIX
from elspeth.web.composer.protocol import (
    COMPOSER_HISTORY_USER_AUTHORED_KEY,
    COMPOSER_HISTORY_USER_MESSAGE_ID_KEY,
    ComposerConvergenceError,
    ComposerHistoryMessage,
)
from elspeth.web.composer.provider_gateway import composer_loop_tool_definitions
from elspeth.web.composer.recovery_controls import recovery_tool_definitions
from elspeth.web.composer.service import ComposerServiceImpl, _reply_stage_instruction
from elspeth.web.composer.state import CompositionState, PipelineMetadata, ValidationSummary
from elspeth.web.composer.tools import ToolResult
from elspeth.web.composer.turn_effects import TurnEvidence
from tests.unit.web.composer._helpers import _clean_advisor_checkpoint, _composer_service_with_session
from tests.unit.web.composer.test_compose_loop_llm_audit import (
    _empty_state,
    _make_llm_response,
    _make_settings,
    _mock_catalog,
    _passing_preflight,
)


@pytest.fixture(autouse=True)
def _advisor_end_gate_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """The END advisor gate is not under test; it returns CLEAN without a provider call."""
    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint, raising=True)


@pytest.fixture
def delivery_confirmations(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Each ``TurnEvidence.confirm_delivery`` result, in call order.

    ``confirm_delivery`` answers ``False`` (silently) when the hash
    ``prepare_delivery`` took differs from the recorded ``messages_hash``,
    so a ``True`` per provider call proves one request was hashed, recorded
    and sent.
    """
    confirmations: list[bool] = []
    original = TurnEvidence.confirm_delivery

    def _spy(self: TurnEvidence, attempt: Any, retained_calls: Any) -> bool:
        confirmed = original(self, attempt, retained_calls)
        confirmations.append(confirmed)
        return confirmed

    monkeypatch.setattr(TurnEvidence, "confirm_delivery", _spy, raising=True)
    return confirmations


_GENEROUS_TOKENS = 10_000_000


class _RecordingProvider:
    """A ``litellm.acompletion`` fake that records each request and replays responses."""

    def __init__(self, *responses: Any) -> None:
        self._responses = iter(responses)
        self.requests: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> Any:
        self.requests.append(deepcopy(kwargs))
        return next(self._responses)


_ANTHROPIC_MODEL = "anthropic/claude-3-5-sonnet-20241022"


def _service(max_prompt_tokens: int, **overrides: Any) -> tuple[ComposerServiceImpl, str]:
    return _composer_service_with_session(
        catalog=_mock_catalog(), settings=_make_settings(composer_context_max_prompt_tokens=max_prompt_tokens, **overrides)
    )


async def _compose(
    service: ComposerServiceImpl,
    session_id: str,
    history: list[ComposerHistoryMessage],
    provider: _RecordingProvider,
    *,
    tool_data: dict[str, Any] | None = None,
    budget_history: ContextBudgetHistory | None = None,
    progress: Any = None,
    message: str = "Build a CSV pipeline",
) -> Any:
    state = _empty_state()
    results = iter(range(2, 100))

    def _execute(tool_name: str, arguments: dict[str, Any], current: CompositionState, *_args: Any, **_kwargs: Any) -> ToolResult:
        version = next(results)
        updated = replace(current, metadata=PipelineMetadata(name=f"v{version}"), version=version)
        return ToolResult(
            success=True,
            updated_state=updated,
            validation=ValidationSummary(is_valid=True, errors=(), warnings=(), suggestions=(), semantic_contracts=()),
            affected_nodes=(),
            data=None if tool_data is None else tool_data[arguments["patch"]["name"]],
        )

    with (
        patch("litellm.acompletion", new_callable=AsyncMock, side_effect=provider.complete),
        patch("elspeth.web.composer.tool_batch.execute_tool", side_effect=_execute),
        patch.object(service._preflight, "cached_runtime_preflight", new_callable=AsyncMock, return_value=_passing_preflight()),
    ):
        return await service.compose(
            message, history, state, session_id=session_id, context_budget_history=budget_history, progress=progress
        )


def _user(index: int, content: str) -> ComposerHistoryMessage:
    return {
        "role": "user",
        "content": content,
        COMPOSER_HISTORY_USER_AUTHORED_KEY: True,
        COMPOSER_HISTORY_USER_MESSAGE_ID_KEY: f"history-user-{index}",
    }


def _assistant(content: str) -> ComposerHistoryMessage:
    return {"role": "assistant", "content": content}


def _state_context(messages: list[dict[str, Any]]) -> dict[str, Any]:
    (state_message,) = [m for m in messages if m["role"] == "user" and m["content"].startswith(STATE_CONTEXT_PREFIX)]
    payload: dict[str, Any] = json.loads(state_message["content"][len(STATE_CONTEXT_PREFIX) + 1 :])
    return payload


def _tool_turn(*call_ids: str) -> Any:
    return _make_llm_response(
        tool_calls=[{"id": call_id, "name": "set_metadata", "arguments": {"patch": {"name": call_id}}} for call_id in call_ids],
        request_id=f"chatcmpl-{'-'.join(call_ids)}",
    )


async def _base_request_bytes() -> int:
    """Bytes of the protected request (system, catalog, tools, state, current message), measured live."""
    service, session_id = _service(_GENEROUS_TOKENS)
    provider = _RecordingProvider(_make_llm_response(content="Done."))
    result = await _compose(service, session_id, [], provider)
    split = result.llm_calls[0].request_byte_split
    assert split is not None
    return int(split.total_bytes)


def _wire_bytes(request: dict[str, Any]) -> int:
    """Canonical JSON bytes of the ``{messages, tools}`` a provider request carried: what the byte ceiling bounds."""
    return len(canonical_json({"messages": request["messages"], "tools": request["tools"]}).encode("utf-8"))


def _cap_for(base_bytes: int) -> int:
    """A token cap at which the protected request alone sits at half the cap (fallback 3.0 bytes/token)."""
    return (2 * base_bytes) // 3


def _assert_record_matches_request(call: ComposerLLMCall, request: dict[str, Any]) -> None:
    assert call.messages_hash == stable_hash(request["messages"])
    assert call.tools_spec_hash == stable_hash(request["tools"])


def _long_history(base_bytes: int) -> list[ComposerHistoryMessage]:
    # Three assistant replies of a quarter of the protected request each push
    # the request over the high mark; shedding to the low mark needs all three
    # gone, and no user request.
    prose = "p" * (base_bytes // 4)
    return [
        _user(0, "Please read the CSV file."),
        _assistant(prose + "0"),
        _user(2, "Now add a filter."),
        _assistant(prose + "1"),
        _user(4, "And write the output."),
        _assistant(prose + "2"),
    ]


@pytest.mark.asyncio
async def test_long_history_sheds_assistant_prose_and_keeps_user_requests(delivery_confirmations: list[bool]) -> None:
    base = await _base_request_bytes()
    delivery_confirmations.clear()
    history = _long_history(base)
    service, session_id = _service(_cap_for(base))
    provider = _RecordingProvider(_make_llm_response(content="Done."))

    result = await _compose(service, session_id, history, provider)

    (request,) = provider.requests
    sent = request["messages"]
    assert [m for m in sent if m["role"] == "assistant"] == []
    sent_contents = [m["content"] for m in sent]
    for row in history:
        if row["role"] == "user":
            assert row["content"] in sent_contents
    assert _state_context(sent)["conversation_omissions"] == {
        "prior_user_requests_omitted": 0,
        "prior_assistant_messages_omitted": 3,
    }
    (call,) = result.llm_calls
    assert call.context_budget is not None
    assert call.context_budget.shed_history_indices == (1, 3, 5)
    assert call.context_budget.stubbed_tool_call_ids == ()
    assert call.context_budget.estimated_tokens_after < call.context_budget.estimated_tokens_before
    _assert_record_matches_request(call, request)
    assert delivery_confirmations == [True]
    # The caller's history is never edited to shed.
    assert history == _long_history(base)


@pytest.mark.asyncio
async def test_long_history_negative_control_with_a_high_cap_sends_every_message() -> None:
    base = await _base_request_bytes()
    history = _long_history(base)
    service, session_id = _service(_GENEROUS_TOKENS)
    provider = _RecordingProvider(_make_llm_response(content="Done."))

    result = await _compose(service, session_id, history, provider)

    (request,) = provider.requests
    sent_contents = [m["content"] for m in request["messages"]]
    for row in history:
        assert row["content"] in sent_contents
    assert "conversation_omissions" not in _state_context(request["messages"])
    (call,) = result.llm_calls
    assert call.context_budget is not None
    assert call.context_budget.shed_history_indices == ()


@pytest.mark.asyncio
async def test_under_the_high_mark_the_request_is_unchanged_and_the_record_is_empty() -> None:
    base = await _base_request_bytes()
    history = [_user(0, "Please read the CSV file."), _assistant("I read it.")]
    unbudgeted_service, unbudgeted_session = _service(_GENEROUS_TOKENS)
    unbudgeted = _RecordingProvider(_make_llm_response(content="Done."))
    await _compose(unbudgeted_service, unbudgeted_session, history, unbudgeted)
    service, session_id = _service(_cap_for(base))
    provider = _RecordingProvider(_make_llm_response(content="Done."))

    result = await _compose(service, session_id, history, provider, budget_history=ContextBudgetHistory.empty())

    assert provider.requests[0]["messages"] == unbudgeted.requests[0]["messages"]
    assert stable_hash(provider.requests[0]["messages"]) == stable_hash(unbudgeted.requests[0]["messages"])
    (call,) = result.llm_calls
    assert call.context_budget is not None
    assert call.context_budget.shed_history_indices == ()
    assert call.context_budget.stubbed_tool_call_ids == ()
    assert call.context_budget.shed_hatch_event_indices == ()
    assert call.context_budget.estimated_tokens_after == call.context_budget.estimated_tokens_before
    _assert_record_matches_request(call, provider.requests[0])


@pytest.mark.asyncio
@pytest.mark.parametrize(("previously_shed", "expected_shed"), [(frozenset({"history:1"}), (1,)), (frozenset(), ())])
async def test_history_shed_on_the_previous_turn_stays_shed_between_the_marks(
    previously_shed: frozenset[str], expected_shed: tuple[int, ...]
) -> None:
    base = await _base_request_bytes()
    # Between the low and the high mark: nothing new is shed, but what the
    # previous call shed stays shed. The empty set is the control.
    history = [_user(0, "Please read the CSV file."), _assistant("p" * (base * 3 // 10))]
    service, session_id = _service(_cap_for(base))
    provider = _RecordingProvider(_make_llm_response(content="Done."))

    result = await _compose(
        service,
        session_id,
        history,
        provider,
        budget_history=ContextBudgetHistory(bytes_per_token=None, previously_shed_history=previously_shed),
    )

    (call,) = result.llm_calls
    assert call.context_budget is not None
    assert call.context_budget.shed_history_indices == expected_shed
    sent_assistant = [m for m in provider.requests[0]["messages"] if m["role"] == "assistant"]
    assert len(sent_assistant) == (0 if expected_shed else 1)


@pytest.mark.asyncio
async def test_three_round_tool_turn_stubs_earlier_results_and_keeps_the_latest_round(delivery_confirmations: list[bool]) -> None:
    base = await _base_request_bytes()
    delivery_confirmations.clear()
    big = "r" * (base * 3 // 10)
    tool_data = {"call-1": {"rows": big + "1"}, "call-2": {"rows": big + "2"}, "call-3": {"rows": big + "3"}}
    service, session_id = _service(_cap_for(base))
    provider = _RecordingProvider(
        _tool_turn("call-1"), _tool_turn("call-2"), _tool_turn("call-3"), _make_llm_response(content="Pipeline built.")
    )

    result = await _compose(service, session_id, [], provider, tool_data=tool_data)

    assert len(provider.requests) == len(result.llm_calls) == 4
    for call, request in zip(result.llm_calls, provider.requests, strict=True):
        assert call.context_budget is not None
        _assert_record_matches_request(call, request)
    assert [call.context_budget.stubbed_tool_call_ids for call in result.llm_calls if call.context_budget is not None] == [
        (),
        (),
        (),
        ("call-1", "call-2"),
    ]
    tool_messages = {m["tool_call_id"]: m for m in provider.requests[3]["messages"] if m["role"] == "tool"}
    assert set(tool_messages) == {"call-1", "call-2", "call-3"}
    assert tool_messages["call-1"]["content"] == CONTEXT_BUDGET_TOOL_RESULT_STUB
    assert tool_messages["call-2"]["content"] == CONTEXT_BUDGET_TOOL_RESULT_STUB
    assert big + "3" in tool_messages["call-3"]["content"]
    # Every tool call still has its result, in the order the provider requires.
    assistant_ids = [call["id"] for m in provider.requests[3]["messages"] if m["role"] == "assistant" for call in m["tool_calls"]]
    assert assistant_ids == ["call-1", "call-2", "call-3"]
    assert delivery_confirmations == [True, True, True, True]


@pytest.mark.asyncio
async def test_latest_round_with_parallel_tool_calls_keeps_both_results() -> None:
    base = await _base_request_bytes()
    tool_data = {
        "call-1": {"rows": "a" * (base * 4 // 10)},
        "call-2a": {"rows": "b" * (base * 2 // 10)},
        "call-2b": {"rows": "c" * (base * 2 // 10)},
    }
    service, session_id = _service(_cap_for(base))
    provider = _RecordingProvider(_tool_turn("call-1"), _tool_turn("call-2a", "call-2b"), _make_llm_response(content="Pipeline built."))

    result = await _compose(service, session_id, [], provider, tool_data=tool_data)

    last_call = result.llm_calls[2]
    assert last_call.context_budget is not None
    assert last_call.context_budget.stubbed_tool_call_ids == ("call-1",)
    tool_messages = {m["tool_call_id"]: m["content"] for m in provider.requests[2]["messages"] if m["role"] == "tool"}
    assert tool_messages["call-1"] == CONTEXT_BUDGET_TOOL_RESULT_STUB
    assert "b" * (base * 2 // 10) in tool_messages["call-2a"]
    assert "c" * (base * 2 // 10) in tool_messages["call-2b"]
    _assert_record_matches_request(last_call, provider.requests[2])


@pytest.mark.asyncio
async def test_tool_result_shorter_than_the_stub_is_never_stubbed() -> None:
    base = await _base_request_bytes()
    service, _session_id = _service(_cap_for(base))
    plugin_snapshot, policy_catalog = service._policy_context.build("test-user")
    history = _long_history(base)
    state = _empty_state()
    llm_messages = service._build_messages(
        history, state, "Build a CSV pipeline", plugin_snapshot=plugin_snapshot, policy_catalog=policy_catalog
    )
    frame = service._context_budget_frame(
        chat_history=history,
        llm_messages=llm_messages,
        state=state,
        session_id=None,
        plugin_snapshot=plugin_snapshot,
        policy_catalog=policy_catalog,
        history=ContextBudgetHistory.empty(),
    )
    short = "ok"
    assert len(short) < len(CONTEXT_BUDGET_TOOL_RESULT_STUB)
    for call_id in ("call-1", "call-2"):
        llm_messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": call_id, "type": "function", "function": {"name": "get_pipeline_state", "arguments": "{}"}}],
            }
        )
        llm_messages.append({"role": "tool", "tool_call_id": call_id, "content": short})
    unshed = deepcopy(llm_messages)

    selected, _tools, _delivery, record = await service._prepare_primary_delivery(
        messages=llm_messages,
        tools=composer_loop_tool_definitions(service._planner_dialect),
        recorder=BufferingRecorder(),
        turn_evidence=None,
        budget_frame=frame,
        state=state,
        initial_version=state.version,
        composition_turns_used=0,
        discovery_turns_used=0,
        failed_turn=None,
        progress=None,
    )

    # The history pushed the request over the high mark, so shedding ran ...
    assert record.shed_history_indices == (1, 3, 5)
    # ... yet the short earlier result was not stubbed: stubbing it saves nothing.
    assert record.stubbed_tool_call_ids == ()
    assert [m["content"] for m in selected if m["role"] == "tool"] == [short, short]
    # The caller's list is untouched.
    assert llm_messages == unshed


@pytest.mark.asyncio
async def test_current_message_larger_than_the_cap_is_refused_before_any_provider_call() -> None:
    base = await _base_request_bytes()
    # The message is larger than any protected component (the system prompt
    # included) yet below the credential guard's per-string bound, so only
    # the context budget can stop it before dispatch.
    current_message = "Please build the pipeline. " * (base * 85 // 100 // 27)
    assert len(current_message.encode()) < 262_144
    cap = base // 2
    service, session_id = _service(cap)
    provider = _RecordingProvider(_make_llm_response(content="Done."))

    with (
        patch("litellm.acompletion", new_callable=AsyncMock, side_effect=provider.complete),
        pytest.raises(ComposerConvergenceError) as exc_info,
    ):
        await service.compose(current_message, [], _empty_state(), session_id=session_id)

    assert provider.requests == []
    error = exc_info.value
    assert error.reason == "context_budget_exceeded"
    # The refusal is not a non-convergence; its message is the user-facing refusal.
    assert str(error) == "This conversation is too large for the composer to continue. Start a new session, or shorten your request."
    assert error.llm_calls == ()
    assert error.evidence["largest_component"] == "current_message"
    assert error.evidence["ceiling"] == "tokens"
    assert error.evidence["max_prompt_tokens"] == cap
    assert error.evidence["max_request_bytes"] == 960 * 1024
    assert error.evidence["estimated_tokens"] > cap
    # The protected request plus the oversized message, nothing shed.
    assert error.evidence["request_bytes"] > len(current_message) + base // 2


@pytest.mark.asyncio
async def test_anthropic_shed_request_is_marked_hashed_recorded_and_sent_as_one(delivery_confirmations: list[bool]) -> None:
    base = await _base_request_bytes()
    history = _long_history(base)
    service, session_id = _service(_cap_for(base), composer_model=_ANTHROPIC_MODEL)
    provider = _RecordingProvider(_make_llm_response(content="Done."))
    delivery_confirmations.clear()

    result = await _compose(service, session_id, history, provider)

    (request,) = provider.requests
    sent = request["messages"]
    # Cache markers were applied to the request that was sent ...
    assert "cache_control" in sent[0]
    assert "cache_control" in sent[-1]
    assert "cache_control" in request["tools"][-1]
    # ... and it is the shed request: no assistant history, omissions counted.
    assert [m for m in sent if m["role"] == "assistant"] == []
    assert _state_context(sent)["conversation_omissions"] == {
        "prior_user_requests_omitted": 0,
        "prior_assistant_messages_omitted": 3,
    }
    (call,) = result.llm_calls
    assert call.context_budget is not None
    assert call.context_budget.shed_history_indices == (1, 3, 5)
    _assert_record_matches_request(call, request)
    assert delivery_confirmations == [True]


_MODELS = pytest.mark.parametrize("model", ["openrouter/openai/gpt-5.5", _ANTHROPIC_MODEL], ids=["unmarked", "anthropic-marked"])


async def _measure_sizes(model: str, history: list[ComposerHistoryMessage], message: str = "Build a CSV pipeline") -> tuple[int, int]:
    """Under a generous cap: the recorded content split (the token basis) and the wire bytes sent (the byte-ceiling basis)."""
    service, session_id = _service(_GENEROUS_TOKENS, composer_model=model)
    provider = _RecordingProvider(_make_llm_response(content="Done."))
    result = await _compose(service, session_id, history, provider, message=message)
    split = result.llm_calls[0].request_byte_split
    assert split is not None
    assert "conversation_omissions" not in _state_context(provider.requests[0]["messages"])
    return int(split.total_bytes), _wire_bytes(provider.requests[0])


async def _compose_or_refusal(
    model: str,
    history: list[ComposerHistoryMessage],
    *,
    max_prompt_tokens: int,
    max_request_bytes: int,
    message: str = "Build a CSV pipeline",
) -> tuple[_RecordingProvider, Any]:
    """Compose under the given ceilings; return the provider and either the result or the refusal."""
    service, session_id = _service(max_prompt_tokens, composer_model=model, composer_planner_max_request_bytes=max_request_bytes)
    provider = _RecordingProvider(_make_llm_response(content="Done."))
    try:
        result = await _compose(service, session_id, history, provider, message=message)
    except ComposerConvergenceError as refusal:
        return provider, refusal
    return provider, result


# The first reply is shed first (oldest assistant prose); the second is the "one more" segment.
_FIRST_REPLY = _assistant("p" * 5_000)
_SECOND_REPLY = _assistant("q" * 2_000)


@pytest.mark.asyncio
@_MODELS
async def test_a_request_within_the_re_render_margin_sheds_one_more_segment_and_is_sent(model: str) -> None:
    """Shedding the first reply lands ten bytes under the byte ceiling before the state message is re-rendered.

    The re-rendered ``conversation_omissions`` (and, for Anthropic, the cache
    markers the decision measured) would push it over, so the decision sheds
    the second reply too and the request is sent at or under the ceiling.
    """
    user_request = _user(0, "Please read the CSV file.")
    _content, wire = await _measure_sizes(model, [user_request, _SECOND_REPLY])
    max_request_bytes = wire + 10

    provider, result = await _compose_or_refusal(
        model, [user_request, _FIRST_REPLY, _SECOND_REPLY], max_prompt_tokens=_GENEROUS_TOKENS, max_request_bytes=max_request_bytes
    )

    assert not isinstance(result, ComposerConvergenceError), result
    (request,) = provider.requests
    (call,) = result.llm_calls
    _assert_record_matches_request(call, request)
    assert _wire_bytes(request) <= max_request_bytes
    assert [m for m in request["messages"] if m["role"] == "assistant"] == []
    assert user_request["content"] in [m["content"] for m in request["messages"]]
    assert _state_context(request["messages"])["conversation_omissions"] == {
        "prior_user_requests_omitted": 0,
        "prior_assistant_messages_omitted": 2,
    }
    assert call.context_budget is not None
    assert call.context_budget.shed_history_indices == (1, 2)


@pytest.mark.asyncio
@_MODELS
async def test_a_dispatched_request_never_exceeds_either_ceiling(model: str) -> None:
    """Over ceilings straddling the size after one shed, every request is sent within both ceilings or refused unsent."""
    user_request = _user(0, "Please read the CSV file.")
    # Both ceilings straddle the wire size after one shed.
    _one_shed_content, one_shed_wire = await _measure_sizes(model, [user_request, _SECOND_REPLY])
    history = [user_request, _FIRST_REPLY, _SECOND_REPLY]
    deltas = (-3_000, -400, -10, 0, 10, 100, 400)
    ceilings = [(_GENEROUS_TOKENS, one_shed_wire + delta) for delta in deltas]
    ceilings += [(-(-(one_shed_wire + delta) // 3), 960 * 1024) for delta in deltas]
    sent = 0
    for max_prompt_tokens, max_request_bytes in ceilings:
        provider, outcome = await _compose_or_refusal(
            model, history, max_prompt_tokens=max_prompt_tokens, max_request_bytes=max_request_bytes
        )
        if isinstance(outcome, ComposerConvergenceError):
            assert outcome.reason == "context_budget_exceeded", outcome.reason
            assert provider.requests == [] and outcome.llm_calls == ()
            continue
        sent += 1
        (call,) = outcome.llm_calls
        (request,) = provider.requests
        # The byte ceiling holds on the bytes of the request actually sent.
        assert _wire_bytes(request) <= max_request_bytes, (max_prompt_tokens, max_request_bytes)
        assert call.context_budget is not None
        assert call.context_budget.estimated_tokens_after <= max_prompt_tokens, (max_prompt_tokens, max_request_bytes)
    assert sent >= len(ceilings) - 2  # the two tightest ceilings may refuse; the rest are sent


@pytest.mark.asyncio
async def test_with_nothing_left_to_shed_a_request_over_the_byte_ceiling_is_refused() -> None:
    """Protected content alone over the byte ceiling is refused by the decision, naming the ceiling and the component."""
    current_message = "Please build the pipeline. " * 7_000
    assert len(current_message.encode()) < 262_144
    max_request_bytes = len(current_message.encode()) + 1_000
    service, session_id = _service(_GENEROUS_TOKENS, composer_planner_max_request_bytes=max_request_bytes)
    provider = _RecordingProvider(_make_llm_response(content="Done."))

    with (
        patch("litellm.acompletion", new_callable=AsyncMock, side_effect=provider.complete),
        pytest.raises(ComposerConvergenceError) as exc_info,
    ):
        await service.compose(current_message, [_user(0, "Please read the CSV file.")], _empty_state(), session_id=session_id)

    assert provider.requests == []
    error = exc_info.value
    assert error.reason == "context_budget_exceeded"
    assert error.llm_calls == ()
    assert error.evidence["ceiling"] == "bytes"
    assert error.evidence["max_request_bytes"] == max_request_bytes
    assert error.evidence["request_bytes"] > max_request_bytes
    assert error.evidence["largest_component"] == "current_message"


@pytest.mark.asyncio
async def test_the_final_byte_guard_refuses_a_request_the_decision_did_not_fit() -> None:
    """The post-application guard, reached directly.

    The decision measures the marked request plus the omission allowance, so
    no constructible input reaches this guard while anything is sheddable. A
    wrapper grows the current message after the decision to prove the guard
    still refuses rather than sends.
    """
    base_content, base_wire = await _measure_sizes("openrouter/openai/gpt-5.5", [])
    # The growth is all quotes: 300 000 content bytes, 600 000 on the wire. The
    # grown request's content is under the ceiling and its wire size is over,
    # so only a guard that compares wire bytes refuses it.
    growth = '"' * 300_000
    max_request_bytes = base_wire + 450_000
    assert base_content + len(growth) < max_request_bytes < base_wire + 2 * len(growth)
    service, session_id = _service(_GENEROUS_TOKENS, composer_planner_max_request_bytes=max_request_bytes)
    provider = _RecordingProvider(_make_llm_response(content="Done."))
    decide = service._shed_to_context_budget

    def _grown_after_decision(**kwargs: Any) -> Any:
        messages, record, bytes_per_token = decide(**kwargs)
        grown = list(messages)
        current = kwargs["frame"].state_index + 1
        assert grown[current]["content"] == "Build a CSV pipeline"
        grown[current] = {**grown[current], "content": grown[current]["content"] + growth}
        return grown, record, bytes_per_token

    with (
        patch.object(service, "_shed_to_context_budget", side_effect=_grown_after_decision),
        patch("litellm.acompletion", new_callable=AsyncMock, side_effect=provider.complete),
        pytest.raises(ComposerConvergenceError) as exc_info,
    ):
        await service.compose("Build a CSV pipeline", [], _empty_state(), session_id=session_id)

    assert provider.requests == []
    error = exc_info.value
    assert error.reason == "context_budget_exceeded"
    assert error.llm_calls == ()
    assert error.evidence["ceiling"] == "bytes"
    assert error.evidence["request_bytes"] == base_wire + 2 * len(growth)
    assert error.evidence["largest_component"] == "current_message"


# --- Wire bytes against the byte ceiling; content bytes for the token estimate -----------------

# Content that grows when carried as a JSON string: quotes, backslashes, a
# JSON document nested as a string twice, and control characters.
_ESCAPING_UNIT = '"q" \\ ' + json.dumps(json.dumps({"k": 'v"\\'})) + " \n\t\u0001\u001f "
_ESCAPING_PROSE = _ESCAPING_UNIT * 600


@pytest.mark.asyncio
@_MODELS
async def test_a_request_under_the_byte_ceiling_by_content_but_over_it_on_the_wire_is_shed(model: str) -> None:
    user_request = _user(0, "Please read the CSV file.")
    history = [user_request, _assistant(_ESCAPING_PROSE)]
    full_content, full_wire = await _measure_sizes(model, history)
    _kept_content, kept_wire = await _measure_sizes(model, [user_request])
    max_request_bytes = full_wire - 5_000
    # Unshed, the content measure is under the ceiling and the wire size is over it;
    # without the reply the wire size fits with room for the re-rendered state message.
    assert full_content < max_request_bytes < full_wire
    assert kept_wire + 1_000 < max_request_bytes

    provider, result = await _compose_or_refusal(model, history, max_prompt_tokens=_GENEROUS_TOKENS, max_request_bytes=max_request_bytes)

    assert not isinstance(result, ComposerConvergenceError), result
    (request,) = provider.requests
    (call,) = result.llm_calls
    assert _wire_bytes(request) <= max_request_bytes
    assert call.context_budget is not None
    assert call.context_budget.shed_history_indices == (1,)
    assert [m for m in request["messages"] if m["role"] == "assistant"] == []
    _assert_record_matches_request(call, request)


@pytest.mark.asyncio
@_MODELS
async def test_protected_content_under_the_byte_ceiling_by_content_but_over_it_on_the_wire_is_refused_unsent(model: str) -> None:
    current_message = "Build a CSV pipeline. " + _ESCAPING_PROSE
    full_content, full_wire = await _measure_sizes(model, [], message=current_message)
    max_request_bytes = full_wire - 5_000
    assert full_content < max_request_bytes < full_wire

    provider, refusal = await _compose_or_refusal(
        model, [], max_prompt_tokens=_GENEROUS_TOKENS, max_request_bytes=max_request_bytes, message=current_message
    )

    assert isinstance(refusal, ComposerConvergenceError), refusal
    assert provider.requests == []
    assert refusal.reason == "context_budget_exceeded"
    assert refusal.llm_calls == ()
    assert refusal.evidence["ceiling"] == "bytes"
    # The refusal reports the wire size, the measure the ceiling bounds.
    assert refusal.evidence["request_bytes"] == full_wire
    assert refusal.evidence["max_request_bytes"] == max_request_bytes


@pytest.mark.asyncio
@_MODELS
async def test_the_token_estimate_and_the_recorded_size_are_the_wire_bytes_of_the_request_sent(model: str) -> None:
    history = [_user(0, "Please read the CSV file."), _assistant(_ESCAPING_PROSE)]
    content, wire = await _measure_sizes(model, history)
    assert wire > content + len(_ESCAPING_PROSE) // 2
    service, session_id = _service(_GENEROUS_TOKENS, composer_model=model)
    provider = _RecordingProvider(_make_llm_response(content="Done."))

    result = await _compose(service, session_id, history, provider)

    (call,) = result.llm_calls
    (request,) = provider.requests
    assert call.context_budget is not None
    # Fallback ratio 3.0 over the wire size plus the omission allowance the decision carries.
    assert call.context_budget.estimated_tokens_before == -(-(wire + CONTEXT_BUDGET_OMISSION_ALLOWANCE_BYTES) // 3)
    # The record carries the exact wire size sent; the content split beside it is unchanged in meaning.
    assert call.context_budget.request_wire_bytes == _wire_bytes(request) == wire
    assert call.request_byte_split is not None and call.request_byte_split.total_bytes == content


def _argument_heavy_turn(call_id: str, padding: str) -> Any:
    """A tool-call turn with a few bytes of prose and large tool-call arguments."""
    return _make_llm_response(
        content="ok",
        tool_calls=[{"id": call_id, "name": "set_metadata", "arguments": {"patch": {"name": call_id, "description": padding}}}],
        request_id=f"chatcmpl-{call_id}",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("ceiling", ["tokens", "bytes"])
async def test_tool_call_arguments_count_toward_both_ceilings(ceiling: str) -> None:
    """An assistant message with tiny content and large tool-call arguments is counted, not measured as its content.

    Arguments are protected, so the second request of the turn, which carries
    them, cannot be fitted: it is refused unsent rather than dispatched over
    the ceiling.
    """
    model = "openrouter/openai/gpt-5.5"
    base_content, base_wire = await _measure_sizes(model, [])
    padding = "a" * 200_000
    # The first request fits with room to spare; the arguments alone carry the second past the ceiling.
    limit_bytes = base_wire + 100_000
    max_prompt_tokens, max_request_bytes = (limit_bytes // 3, 960 * 1024) if ceiling == "tokens" else (_GENEROUS_TOKENS, limit_bytes)
    service, session_id = _service(max_prompt_tokens, composer_model=model, composer_planner_max_request_bytes=max_request_bytes)
    provider = _RecordingProvider(_argument_heavy_turn("call-1", padding), _make_llm_response(content="Done."))

    with pytest.raises(ComposerConvergenceError) as exc_info:
        await _compose(service, session_id, [], provider)

    assert len(provider.requests) == 1
    refusal = exc_info.value
    assert refusal.reason == "context_budget_exceeded"
    assert refusal.evidence["ceiling"] == ceiling
    assert refusal.evidence["request_bytes"] > base_wire + len(padding)
    assert len(refusal.llm_calls) == 1

    # Control: the same turn under generous ceilings sends the second request, whose
    # content split still counts that assistant message as its two bytes of prose.
    generous, generous_session = _service(_GENEROUS_TOKENS, composer_model=model)
    control = _RecordingProvider(_argument_heavy_turn("call-1", padding), _make_llm_response(content="Done."))
    result = await _compose(generous, generous_session, [], control)
    second = result.llm_calls[1]
    assert second.request_byte_split is not None and second.context_budget is not None
    assert second.request_byte_split.total_bytes < base_content + 20_000 < limit_bytes
    assert second.context_budget.request_wire_bytes == _wire_bytes(control.requests[1]) > base_wire + len(padding)


def test_the_wire_measure_counts_tool_call_arguments_and_thinking_blocks_that_the_content_split_leaves_out() -> None:
    plain: list[dict[str, Any]] = [{"role": "system", "content": "s"}, {"role": "assistant", "content": "ok"}]
    with_arguments = [
        plain[0],
        {
            **plain[1],
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "t", "arguments": json.dumps({"pad": "a" * 5_000})}}],
        },
    ]
    with_thinking = [plain[0], {**plain[1], "thinking_blocks": [{"type": "thinking", "thinking": "t" * 5_000, "signature": "sig"}]}]
    for grown in (with_arguments, with_thinking):
        assert message_bytes(grown, None).total_bytes == message_bytes(plain, None).total_bytes == 3
        assert request_wire_bytes(grown, None) > request_wire_bytes(plain, None) + 5_000


_ESCAPING_CONTENTS = [
    '"' * 500,
    "\\" * 500,
    json.dumps(json.dumps({"rows": ['a"b', "c\\d"] * 40})),
    "\u0000\u0001\u001f\n\r\t" * 100,
    _ESCAPING_PROSE,
    "plain ascii with nothing to escape " * 20,
    "non-ASCII \u00e9\u4e2d\U0001f600 \u2028\u2029 " * 40,
]


@pytest.mark.parametrize("content", _ESCAPING_CONTENTS)
def test_a_segments_wire_saving_is_never_less_than_its_content_saving(content: str) -> None:
    """The byte-ceiling arithmetic subtracts content savings from wire bytes; that is safe only while this holds."""
    prefix: list[dict[str, Any]] = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    tool_round: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": content},
    ]
    messages = [*prefix, *tool_round]
    segments, positions = tool_result_segments(messages, message_bytes(messages, None), start=2, stop=len(messages))
    (segment,) = segments
    stubbed = [stubbed_tool_message(m) if i in positions[segment.key] else m for i, m in enumerate(messages)]
    assert request_wire_bytes(messages, None) - request_wire_bytes(stubbed, None) >= segment.saving_bytes

    # Removing a history message: the content saving is the message's content bytes.
    history = [*prefix, {"role": "assistant", "content": content}, {"role": "user", "content": "now"}]
    content_saving = message_bytes(history, None).message_bytes[2]
    assert content_saving == len(content.encode("utf-8"))
    assert request_wire_bytes(history, None) - request_wire_bytes([*history[:2], history[3]], None) >= content_saving


# --- The shed-history notice ----------------------------------------------------------------


_CONTEXT_REDUCED_HEADLINE = "Earlier conversation is no longer visible to the composer."


class _NoticeProbe(_RecordingProvider):
    """A provider and progress sink that record how many shed-history notices preceded each provider call."""

    def __init__(self, *responses: Any) -> None:
        super().__init__(*responses)
        self.events: list[Any] = []
        self.notices_at_call: list[int] = []

    async def sink(self, event: Any) -> None:
        self.events.append(event)

    def notices(self) -> list[Any]:
        return [event for event in self.events if event.reason == "context_reduced"]

    async def complete(self, **kwargs: Any) -> Any:
        self.notices_at_call.append(len(self.notices()))
        return await super().complete(**kwargs)


@pytest.mark.asyncio
async def test_a_turn_that_sheds_history_announces_it_once_before_the_first_shedding_call() -> None:
    base = await _base_request_bytes()
    service, session_id = _service(_cap_for(base))
    probe = _NoticeProbe(_tool_turn("call-1"), _make_llm_response(content="Done."))

    result = await _compose(service, session_id, _long_history(base), probe, progress=probe.sink)

    # Both calls of the turn shed history; the notice is sent once, before the first.
    assert len(result.llm_calls) == 2
    for call in result.llm_calls:
        assert call.context_budget is not None and call.context_budget.shed_history_indices == (1, 3, 5)
    assert probe.notices_at_call == [1, 1]
    (notice,) = probe.notices()
    assert notice.phase == "calling_model"
    assert notice.headline == _CONTEXT_REDUCED_HEADLINE


@pytest.mark.asyncio
async def test_a_turn_that_sheds_nothing_sends_no_notice() -> None:
    base = await _base_request_bytes()
    service, session_id = _service(_GENEROUS_TOKENS)
    probe = _NoticeProbe(_tool_turn("call-1"), _make_llm_response(content="Done."))

    result = await _compose(service, session_id, _long_history(base), probe, progress=probe.sink)

    for call in result.llm_calls:
        assert call.context_budget is not None and call.context_budget.shed_history_indices == ()
    assert probe.events, "the turn emitted its ordinary progress events"
    assert probe.notices() == []


@pytest.mark.asyncio
async def test_stubbing_tool_results_alone_sends_no_notice() -> None:
    base = await _base_request_bytes()
    big = "r" * (base * 3 // 10)
    tool_data = {"call-1": {"rows": big + "1"}, "call-2": {"rows": big + "2"}, "call-3": {"rows": big + "3"}}
    service, session_id = _service(_cap_for(base))
    probe = _NoticeProbe(_tool_turn("call-1"), _tool_turn("call-2"), _tool_turn("call-3"), _make_llm_response(content="Pipeline built."))

    result = await _compose(service, session_id, [], probe, tool_data=tool_data, progress=probe.sink)

    last = result.llm_calls[-1].context_budget
    assert last is not None and last.stubbed_tool_call_ids == ("call-1", "call-2")
    assert probe.notices() == []


# --- The reply stage: its request is derived from the budgeted loop messages ---------------------


def _tool_round(call_id: str, content: str) -> list[dict[str, Any]]:
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": call_id, "type": "function", "function": {"name": "get_pipeline_state", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": call_id, "content": content},
    ]


async def _prepare_reply(
    service: ComposerServiceImpl, llm_messages: list[dict[str, Any]], recorder: Any = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Any]:
    """Prepare the reply-stage request for a turn whose loop messages are ``llm_messages``."""
    plugin_snapshot, policy_catalog = service._policy_context.build("test-user")
    state = _empty_state()
    frame = service._context_budget_frame(
        chat_history=[],
        llm_messages=llm_messages[:4],
        state=state,
        session_id=None,
        plugin_snapshot=plugin_snapshot,
        policy_catalog=policy_catalog,
        history=ContextBudgetHistory.empty(),
    )
    selected, tools, _delivery, record = await service._prepare_primary_delivery(
        messages=llm_messages,
        tools=recovery_tool_definitions(service._planner_dialect),
        recorder=recorder if recorder is not None else BufferingRecorder(),
        turn_evidence=None,
        budget_frame=frame,
        state=state,
        initial_version=state.version,
        composition_turns_used=0,
        discovery_turns_used=0,
        failed_turn=None,
        progress=None,
        reply_instruction=_reply_stage_instruction(),
    )
    return selected, tools, record


def _two_round_turn(service: ComposerServiceImpl, first: str, second: str) -> list[dict[str, Any]]:
    plugin_snapshot, policy_catalog = service._policy_context.build("test-user")
    llm_messages = service._build_messages(
        [], _empty_state(), "Build a CSV pipeline", plugin_snapshot=plugin_snapshot, policy_catalog=policy_catalog
    )
    assert len(llm_messages) == 4
    return [*llm_messages, *_tool_round("call-1", first), *_tool_round("call-2", second)]


@pytest.mark.asyncio
async def test_the_reply_stage_request_stubs_earlier_tool_results_and_keeps_the_latest_round() -> None:
    base = await _base_request_bytes()
    first, second = "f" * (base * 6 // 10), "s" * (base * 6 // 10)
    generous, _ = _service(_GENEROUS_TOKENS)
    unshed, unshed_tools, unshed_record = await _prepare_reply(generous, _two_round_turn(generous, first, second))
    full_content = message_bytes(unshed, unshed_tools).total_bytes
    assert unshed_record.stubbed_tool_call_ids == ()
    # The reply stage sends no tool-role message: each tool record is quoted as text.
    assert [m["role"] for m in unshed[4:]] == ["assistant", "user", "assistant", "user", "system"]
    assert first in unshed[5]["content"] and second in unshed[7]["content"]
    # The unshed reply request is over the high mark and under the cap.
    cap = -(-full_content // 3) * 10 // 9
    service, _ = _service(cap)
    llm_messages = _two_round_turn(service, first, second)
    before = deepcopy(llm_messages)

    selected, tools, record = await _prepare_reply(service, llm_messages)

    assert record.stubbed_tool_call_ids == ("call-1",)
    assert record.estimated_tokens_after < record.estimated_tokens_before <= cap
    assert [m["role"] for m in selected[4:]] == ["assistant", "user", "assistant", "user", "system"]
    # The earlier round's result is quoted as the stub; the latest round is quoted in full.
    assert first not in selected[5]["content"] and CONTEXT_BUDGET_TOOL_RESULT_STUB in selected[5]["content"]
    assert second in selected[7]["content"]
    assert selected[8] == _reply_stage_instruction()
    assert message_bytes(selected, tools).total_bytes < full_content - len(first) + 1_000
    assert request_wire_bytes(selected, tools) <= service._context_budget_policy.max_request_bytes
    # The loop's own messages keep their tool roles and full results.
    assert llm_messages == before


@pytest.mark.asyncio
async def test_a_result_stubbed_on_an_earlier_call_stays_stubbed_in_the_reply_stage_request() -> None:
    base = await _base_request_bytes()
    first, second = "f" * (base * 6 // 10), "s" * (base * 6 // 10)
    generous, _ = _service(_GENEROUS_TOKENS)
    unshed, unshed_tools, _ = await _prepare_reply(generous, _two_round_turn(generous, first, second))
    full_content = message_bytes(unshed, unshed_tools).total_bytes
    # The unshed reply request sits at 70% of the cap: between the marks, so nothing new is shed.
    service, _ = _service(-(-full_content // 3) * 10 // 7)
    earlier = build_llm_call_record(
        model_requested="openrouter/openai/gpt-5.5",
        messages=[{"role": "system", "content": "skill"}],
        tools=None,
        status=ComposerLLMCallStatus.SUCCESS,
        started_at=datetime.now(UTC),
        started_ns=time.monotonic_ns(),
        temperature=None,
        seed=None,
        response=None,
        context_budget=ComposerContextBudgetRecord.from_shed_keys(
            policy_hash="a" * 64,
            bytes_per_token=3.0,
            estimated_tokens_before=2,
            estimated_tokens_after=1,
            request_wire_bytes=1200,
            shed_keys=("tool:call-1",),
        ),
    )

    for earlier_calls, expected in (((earlier,), ("call-1",)), ((), ())):
        recorder = SimpleNamespace(llm_calls=earlier_calls, invocations=())
        selected, _tools, record = await _prepare_reply(service, _two_round_turn(service, first, second), recorder)
        assert record.stubbed_tool_call_ids == expected
        assert (first in selected[5]["content"]) == (not expected)
        assert second in selected[7]["content"]


# --- The bytes-per-token ratio is the budgeted model's own ---------------------------------------


@pytest.mark.asyncio
async def test_the_compose_loop_uses_the_ratio_measured_on_its_own_model() -> None:
    model = "openrouter/openai/gpt-5.5"
    service, session_id = _service(_GENEROUS_TOKENS, composer_model=model)
    provider = _RecordingProvider(_make_llm_response(content="Done."))
    # The newest budgeted call of any model was the escape hatch's; the compose loop's own model measured 4.25.
    budget_history = ContextBudgetHistory(
        bytes_per_token=2.5,
        previously_shed_history=frozenset(),
        bytes_per_token_by_model=(("test/escape-hatch", 2.5), (model, 4.25)),
    )

    result = await _compose(service, session_id, [], provider, budget_history=budget_history)

    (call,) = result.llm_calls
    assert call.model_requested == model
    assert call.context_budget is not None
    assert call.context_budget.bytes_per_token == repr(4.25)
    (request,) = provider.requests
    assert call.context_budget.request_wire_bytes == _wire_bytes(request)
    assert call.context_budget.estimated_tokens_before == -(-_wire_bytes(request) * 100 // 425)
