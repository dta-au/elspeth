"""Only earlier turns are left out to fit the context budget; the turn in progress is always sent whole.

The rule: nothing belonging to the turn in progress (its user message, its
assistant tool-call messages, its tool results, the escape hatch's record of
this turn) is ever shed, stubbed or truncated. Earlier turns are left out in
the existing order. When everything from earlier turns is gone and the request
is still over the token setting it is sent and recorded as over the estimate;
only a turn whose own content is over the byte maximum is refused.

The compose-loop tests drive the real loop against a recording
``litellm.acompletion`` fake; the planner tests drive the real planner against
its scripted completion. Thresholds are measured from a run under generous
ceilings, so no test pins the size of the skill, catalog or tools.
"""

from __future__ import annotations

import json
import math
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from elspeth.web.composer import pipeline_planner
from elspeth.web.composer import service as composer_service
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.context_budget import CONTEXT_BUDGET_TOOL_RESULT_STUB
from elspeth.web.composer.context_budget_apply import message_wire_split, request_wire_bytes
from elspeth.web.composer.context_budget_history import ContextBudgetHistory
from elspeth.web.composer.pipeline_planner import (
    _HATCH_TRANSCRIPT_HEADER,
    PipelinePlannerError,
    _fit_planner_context_budget,
    _planner_request_message,
)
from elspeth.web.composer.protocol import ComposerConvergenceError
from elspeth.web.composer.provider_gateway import composer_loop_tool_definitions
from elspeth.web.composer.recovery_controls import recovery_tool_definitions
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.tools import ToolContext
from tests.unit.web.composer.test_compose_loop_context_budget import (
    _GENEROUS_TOKENS,
    _assert_record_matches_request,
    _base_request_bytes,
    _cap_for,
    _compose,
    _long_history,
    _make_llm_response,
    _RecordingProvider,
    _service,
    _tool_turn,
    _two_round_turn,
    _wire_bytes,
)
from tests.unit.web.composer.test_compose_loop_context_budget import (
    _advisor_end_gate_clean as _advisor_end_gate_clean_fixture,
)
from tests.unit.web.composer.test_compose_loop_llm_audit import _empty_state
from tests.unit.web.composer.test_context_budget_history import _audit_row, _call
from tests.unit.web.composer.test_pipeline_planner import _context_policy, _plan
from tests.unit.web.composer.test_planner_context_budget import (
    _AWKWARD_EVENTS,
    _FALLBACK_RATIO,
    _TRANSCRIPT_ALLOWANCE,
    _context,
    _discovery_run,
    _discovery_then_emit,
    _earlier_call,
    _fit_hatch_request,
    _hatch_message,
    _request_size_of,
    _sent_bytes,
    _sent_matches_record,
)

_advisor_end_gate_clean = _advisor_end_gate_clean_fixture

_CURRENT_MESSAGE = "Build a CSV pipeline"
_CALL_IDS = ("call-1", "call-2", "call-3")


def _three_rounds() -> tuple[Any, ...]:
    return (*(_tool_turn(call_id) for call_id in _CALL_IDS), _make_llm_response(content="Pipeline built."))


def _results(size: int) -> dict[str, Any]:
    return {call_id: {"rows": "r" * size + call_id} for call_id in _CALL_IDS}


def _turn_in_progress(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The current user message and every round after it, as sent."""
    (start,) = [index for index, message in enumerate(messages) if message["role"] == "user" and message["content"] == _CURRENT_MESSAGE]
    return messages[start:]


def _assert_every_result_whole(request: dict[str, Any], tool_data: dict[str, Any], call_ids: tuple[str, ...]) -> None:
    results = {message["tool_call_id"]: message["content"] for message in request["messages"] if message["role"] == "tool"}
    assert set(results) == set(call_ids)
    for call_id in call_ids:
        assert tool_data[call_id]["rows"] in results[call_id], call_id
        assert results[call_id] != CONTEXT_BUDGET_TOOL_RESULT_STUB, call_id


# --- Compose loop -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_multi_round_turn_under_pressure_sheds_history_and_sends_every_result_of_the_turn_whole() -> None:
    base = await _base_request_bytes()
    tool_data = _results(base * 3 // 10)
    history = _long_history(base)
    generous_service, generous_session = _service(_GENEROUS_TOKENS)
    generous = _RecordingProvider(*_three_rounds())
    await _compose(generous_service, generous_session, history, generous, tool_data=tool_data)
    service, session_id = _service(_cap_for(base))
    provider = _RecordingProvider(*_three_rounds())

    result = await _compose(service, session_id, history, provider, tool_data=tool_data)

    assert len(provider.requests) == len(result.llm_calls) == 4
    for call, request in zip(result.llm_calls, provider.requests, strict=True):
        assert call.context_budget is not None
        assert call.context_budget.stubbed_tool_call_ids == ()
        _assert_record_matches_request(call, request)
    # The request was under pressure: earlier turns were left out to make room. The pressure
    # grows mid-turn as results arrive (the sequence where the old rule shed history AND
    # stubbed a result of the turn on the same call).
    first, last = result.llm_calls[0].context_budget, result.llm_calls[3].context_budget
    assert first is not None and last is not None and last.shed_history_indices
    assert len(first.shed_history_indices) < len(last.shed_history_indices)
    assert [m for m in provider.requests[3]["messages"] if m["role"] == "assistant" and not m.get("tool_calls")] == []
    # Everything of the turn in progress is sent exactly as it is sent without any pressure.
    for pressured, unpressured in zip(provider.requests, generous.requests, strict=True):
        assert _turn_in_progress(pressured["messages"]) == _turn_in_progress(unpressured["messages"])
    _assert_every_result_whole(provider.requests[3], tool_data, _CALL_IDS)


@pytest.mark.asyncio
async def test_a_multi_round_turn_over_the_token_setting_with_no_history_sheds_nothing_and_is_sent_over_the_estimate() -> None:
    base = await _base_request_bytes()
    tool_data = _results(base // 2)
    generous_service, generous_session = _service(_GENEROUS_TOKENS)
    generous = _RecordingProvider(*_three_rounds())
    await _compose(generous_service, generous_session, [], generous, tool_data=tool_data)
    cap = _cap_for(base)
    assert _wire_bytes(generous.requests[3]) > cap  # one byte per token: the last request is over the setting
    service, session_id = _service(cap)
    provider = _RecordingProvider(*_three_rounds())

    result = await _compose(service, session_id, [], provider, tool_data=tool_data)

    assert len(provider.requests) == len(result.llm_calls) == 4
    for call, request, unpressured in zip(result.llm_calls, provider.requests, generous.requests, strict=True):
        assert call.context_budget is not None
        assert call.context_budget.stubbed_tool_call_ids == () and call.context_budget.shed_history_indices == ()
        assert request["messages"] == unpressured["messages"]
        _assert_record_matches_request(call, request)
    last = result.llm_calls[3].context_budget
    assert last is not None
    assert last.over_estimate is True
    assert last.max_prompt_tokens == cap < last.estimated_tokens_after
    _assert_every_result_whole(provider.requests[3], tool_data, _CALL_IDS)


@pytest.mark.asyncio
async def test_a_multi_round_turn_whose_own_content_exceeds_the_byte_maximum_is_refused_unsent() -> None:
    base = await _base_request_bytes()
    tool_data = _results(base // 2)
    generous_service, generous_session = _service(_GENEROUS_TOKENS)
    generous = _RecordingProvider(*_three_rounds())
    await _compose(generous_service, generous_session, [], generous, tool_data=tool_data)
    wire = [_wire_bytes(request) for request in generous.requests]
    max_request_bytes = wire[3] - 1_000
    assert wire[2] < max_request_bytes
    service, session_id = _service(_GENEROUS_TOKENS, composer_planner_max_request_bytes=max_request_bytes)
    provider = _RecordingProvider(*_three_rounds())

    with pytest.raises(ComposerConvergenceError) as caught:
        await _compose(service, session_id, [], provider, tool_data=tool_data)

    # The three requests that fit were sent whole; the fourth was refused before dispatch.
    assert [request["messages"] for request in provider.requests] == [request["messages"] for request in generous.requests[:3]]
    refusal = caught.value
    assert refusal.reason == "context_budget_exceeded"
    assert refusal.evidence["ceiling"] == "bytes"
    assert refusal.evidence["request_bytes"] == wire[3]
    assert refusal.evidence["max_request_bytes"] == max_request_bytes
    assert len(refusal.llm_calls) == 3
    for call in refusal.llm_calls:
        assert call.context_budget is not None and call.context_budget.stubbed_tool_call_ids == ()


@pytest.mark.asyncio
async def test_a_multi_round_turn_just_under_the_byte_maximum_is_sent_whole() -> None:
    """Control for the refusal: with the maximum just above the turn's size the same turn is sent unchanged."""
    base = await _base_request_bytes()
    tool_data = _results(base // 2)
    generous_service, generous_session = _service(_GENEROUS_TOKENS)
    generous = _RecordingProvider(*_three_rounds())
    await _compose(generous_service, generous_session, [], generous, tool_data=tool_data)
    max_request_bytes = _wire_bytes(generous.requests[3]) + 1_000
    service, session_id = _service(_GENEROUS_TOKENS, composer_planner_max_request_bytes=max_request_bytes)
    provider = _RecordingProvider(*_three_rounds())

    result = await _compose(service, session_id, [], provider, tool_data=tool_data)

    assert [request["messages"] for request in provider.requests] == [request["messages"] for request in generous.requests]
    for call in result.llm_calls:
        assert call.context_budget is not None and call.context_budget.stubbed_tool_call_ids == ()


class _AnchoringProvider(_RecordingProvider):
    """A recording provider whose replies report a plausible prompt count, so later calls of the turn anchor on them."""

    async def complete(self, **kwargs: Any) -> Any:
        response = await super().complete(**kwargs)
        response.usage.prompt_tokens = request_wire_bytes(kwargs["messages"], kwargs["tools"]) // 4
        return response


def _assert_decision_inputs_hold(inputs: dict[str, Any]) -> None:
    """Each accounting precondition ``decide_context_budget`` checks, asserted on the inputs a caller fed it."""
    request_bytes = inputs["request_bytes"]
    segments = inputs["segments"]
    savings = sum(segment.saving_bytes for segment in segments)
    assert type(request_bytes) is int and request_bytes >= 0
    assert len({segment.key for segment in segments}) == len(segments)
    assert savings <= request_bytes
    assert 0 <= inputs["anchor_shared_bytes"] <= request_bytes - savings
    assert inputs["anchor_tokens"] is None or (type(inputs["anchor_tokens"]) is int and inputs["anchor_tokens"] > 0)
    assert inputs["anchor_tokens"] is not None or inputs["anchor_shared_bytes"] == 0
    assert inputs["anchor_authored_bytes"] is None or 0 <= inputs["anchor_authored_bytes"] <= inputs["anchor_wire_bytes"]
    assert 0 <= inputs["authored_bytes"] <= request_bytes - savings


@pytest.mark.asyncio
async def test_every_accounting_input_holds_on_a_multi_round_turn_and_the_turns_rounds_earn_anchor_credit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = await _base_request_bytes()
    tool_data = _results(base * 3 // 10)
    decisions: list[dict[str, Any]] = []
    decide = composer_service.decide_context_budget

    def _spy(**kwargs: Any) -> Any:
        decisions.append(kwargs)
        return decide(**kwargs)

    monkeypatch.setattr(composer_service, "decide_context_budget", _spy)
    service, session_id = _service(_cap_for(base))
    provider = _AnchoringProvider(*_three_rounds())

    result = await _compose(service, session_id, _long_history(base), provider, tool_data=tool_data)

    assert len(decisions) == len(provider.requests) == 4
    for inputs in decisions:
        _assert_decision_inputs_hold(inputs)
        # Only earlier turns are segments.
        assert inputs["segments"] and all(segment.key.startswith("history:") for segment in inputs["segments"])
    # The last call anchors on the one before it. The turn's earlier rounds are protected and
    # unchanged since that call, so their wire bytes are now part of the anchor's shared bytes.
    last = decisions[3]
    assert last["anchor_tokens"] is not None
    sent = provider.requests[3]["messages"]
    turn_start = sent.index(_turn_in_progress(sent)[0])
    earlier_rounds = message_wire_split(sent[turn_start + 1 : -2], None).total_bytes
    assert earlier_rounds > 2 * (base * 3 // 10)
    assert last["anchor_shared_bytes"] >= earlier_rounds
    budget = result.llm_calls[3].context_budget
    assert budget is not None and budget.anchor_shared_bytes == last["anchor_shared_bytes"]


async def _fit_two_round_turn(
    service: ComposerServiceImpl,
    llm_messages: list[dict[str, Any]],
    *,
    recorder: Any,
    history: ContextBudgetHistory,
    reply_stage: bool,
) -> tuple[list[dict[str, Any]], Any]:
    plugin_snapshot, policy_catalog = service._policy_context.build("test-user")
    state = _empty_state()
    frame = service._context_budget_frame(
        chat_history=[],
        llm_messages=llm_messages[:4],
        state=state,
        session_id=None,
        plugin_snapshot=plugin_snapshot,
        policy_catalog=policy_catalog,
        history=history,
    )
    tools = recovery_tool_definitions(service._planner_dialect) if reply_stage else composer_loop_tool_definitions(service._planner_dialect)
    selected, _tools, _delivery, record = await service._prepare_primary_delivery(
        messages=llm_messages,
        tools=tools,
        recorder=recorder,
        turn_evidence=None,
        budget_frame=frame,
        state=state,
        initial_version=state.version,
        composition_turns_used=0,
        discovery_turns_used=0,
        failed_turn=None,
        progress=None,
        reply_stage=reply_stage,
    )
    return selected, record


def _stored_history_that_stubbed(*tool_call_ids: str, shed: list[int]) -> ContextBudgetHistory:
    """The shed state a session loads from a stored call that recorded stubbed tool results."""
    stored = _call(shed=shed)
    stored["context_budget"]["stubbed_tool_call_ids"] = list(tool_call_ids)
    return ContextBudgetHistory.from_records([_audit_row(1, stored)])


def _sent_text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(str(message["content"]) for message in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_stage", [False, True], ids=["loop", "reply-stage"])
async def test_tool_keys_carried_from_an_earlier_call_never_stub_a_result_of_the_turn(reply_stage: bool) -> None:
    """A ``tool:`` key recorded by an earlier call (of this turn, or stored on the session) is ignored, without error."""
    base = await _base_request_bytes()
    first, second = "f" * (base * 6 // 10), "s" * (base * 6 // 10)
    # Under pressure: the request is far over the high mark, so a carried key would be applied if honoured.
    service, _ = _service(_cap_for(base) // 2)
    earlier = _earlier_call(("tool:call-1", "tool:call-2"))
    for recorder, history in (
        (SimpleNamespace(llm_calls=(earlier,), invocations=()), ContextBudgetHistory.empty()),
        (BufferingRecorder(), _stored_history_that_stubbed("call-1", "call-2", shed=[])),
    ):
        llm_messages = _two_round_turn(service, first, second)
        before = deepcopy(llm_messages)

        selected, record = await _fit_two_round_turn(service, llm_messages, recorder=recorder, history=history, reply_stage=reply_stage)

        assert record.stubbed_tool_call_ids == ()
        assert record.over_estimate is True
        sent = _sent_text(selected)
        assert first in sent and second in sent and CONTEXT_BUDGET_TOOL_RESULT_STUB not in sent
        assert llm_messages == before


def test_stored_records_that_stubbed_tool_results_load_as_history_state_only() -> None:
    """Records written when tool results of a turn were stubbed still load; their tool ids carry no shed state."""
    history = _stored_history_that_stubbed("call-1", "call-2", shed=[1])

    assert history.previously_shed_history == frozenset({"history:1"})


# --- Planner ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_multi_round_planner_turn_under_pressure_sheds_history_and_sends_every_result_whole(
    tmp_path: Path, tool_context: ToolContext
) -> None:
    context = _context(users={0: "Build CSV to JSON."}, assistants={1: "a" * 20_000})

    async def _run(policy: Any) -> tuple[Any, BufferingRecorder]:
        completion = _discovery_then_emit(tmp_path)
        recorder = BufferingRecorder()
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            recorder=recorder,
            conversation_context=context,
            context_policy=policy,
            model_overrides={"max_discovery_turns": 5},
        )
        assert len(completion.requests) == len(recorder.llm_calls) == 4
        return completion, recorder

    measured, _ = await _run(None)
    totals = [_sent_bytes(request) for request in measured.requests]
    # Every request is over the high mark, so every call must leave something out.
    policy = _context_policy(max_prompt_tokens=math.ceil(totals[0] / _FALLBACK_RATIO), max_request_bytes=1_000_000)
    assert totals[0] / _FALLBACK_RATIO > policy.shed_above * policy.max_prompt_tokens

    completion, recorder = await _run(policy)

    for request, call, unpressured in zip(completion.requests, recorder.llm_calls, measured.requests, strict=True):
        _sent_matches_record(request, call)
        assert call.context_budget is not None
        assert call.context_budget.stubbed_tool_call_ids == ()
        assert call.context_budget.shed_history_indices
        # Everything after the planner request is this turn's rounds, sent exactly as without pressure.
        assert request["messages"][3:] == unpressured["messages"][3:]
    tool_results = [message for message in completion.requests[3]["messages"] if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in tool_results] == ["schema-llm", "schema-scrape", "schema-csv"]
    assert all(message["content"] != CONTEXT_BUDGET_TOOL_RESULT_STUB for message in tool_results)


@pytest.mark.asyncio
async def test_a_multi_round_planner_turn_over_the_token_setting_sheds_nothing_and_is_sent_over_the_estimate(
    tmp_path: Path, tool_context: ToolContext
) -> None:
    measured, _ = await _discovery_run(tmp_path, tool_context, model="openai/gpt-planner", policy=None)
    totals = [_sent_bytes(request) for request in measured.requests]
    max_prompt_tokens = math.floor((totals[2] + totals[3]) / 2 / _FALLBACK_RATIO)
    policy = _context_policy(max_prompt_tokens=max_prompt_tokens, max_request_bytes=1_000_000)

    completion, recorder = await _discovery_run(tmp_path, tool_context, model="openai/gpt-planner", policy=policy)

    for request, call, unpressured in zip(completion.requests, recorder.llm_calls, measured.requests, strict=True):
        _sent_matches_record(request, call)
        assert call.context_budget is not None
        assert call.context_budget.stubbed_tool_call_ids == () and call.context_budget.shed_history_indices == ()
        assert request["messages"] == unpressured["messages"]
    last = recorder.llm_calls[3].context_budget
    assert last is not None
    assert last.over_estimate is True
    assert last.estimated_tokens_after > max_prompt_tokens


@pytest.mark.asyncio
async def test_a_multi_round_planner_turn_whose_own_content_exceeds_the_byte_maximum_is_refused(
    tmp_path: Path, tool_context: ToolContext
) -> None:
    measured, _ = await _discovery_run(tmp_path, tool_context, model="openai/gpt-planner", policy=None)
    totals = [_sent_bytes(request) for request in measured.requests]
    for max_request_bytes, refused in ((totals[3] - 100, True), (totals[3] + 100, False)):
        assert totals[2] < max_request_bytes
        policy = _context_policy(max_request_bytes=max_request_bytes)
        completion = _discovery_then_emit(tmp_path)
        recorder = BufferingRecorder()
        try:
            await _plan(
                tmp_path=tmp_path,
                tool_context=tool_context,
                completion=completion,
                recorder=recorder,
                context_policy=policy,
                model_overrides={"model_identifier": "openai/gpt-planner", "max_discovery_turns": 5},
            )
        except PipelinePlannerError as refusal:
            assert refused, max_request_bytes
            assert refusal.code == "CONTEXT_BUDGET_EXHAUSTED"
            assert refusal.detail_codes[0] == "context_ceiling_bytes"
            assert len(completion.requests) == len(recorder.llm_calls) == 3
        else:
            assert not refused, max_request_bytes
            assert len(completion.requests) == 4
        for request, unpressured in zip(completion.requests, measured.requests, strict=False):
            assert request["messages"] == unpressured["messages"]
        for call in recorder.llm_calls:
            assert call.context_budget is not None and call.context_budget.stubbed_tool_call_ids == ()


def _planner_two_round_messages(first: str, second: str) -> list[dict[str, Any]]:
    def _round(call_id: str, content: str) -> list[dict[str, Any]]:
        return [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": call_id, "type": "function", "function": {"name": "get_pipeline_state", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": call_id, "content": content},
        ]

    return [
        {"role": "system", "content": "skill"},
        {"role": "user", "content": "reference"},
        _planner_request_message({"intent": "Convert the data."}),
        *_round("c1", first),
        *_round("c2", second),
    ]


def test_tool_keys_carried_from_an_earlier_planner_call_never_stub_a_result_of_the_turn() -> None:
    first, second = "f" * 20_000, "s" * 20_000
    messages = _planner_two_round_messages(first, second)
    size = _request_size_of(messages)
    policy = _context_policy(max_prompt_tokens=math.ceil(size / _FALLBACK_RATIO / 2), max_request_bytes=1_000_000)
    for earlier_calls, history in (
        ((_earlier_call(("tool:c1", "tool:c2")),), ContextBudgetHistory.empty()),
        ((), _stored_history_that_stubbed("c1", "c2", shed=[])),
    ):
        before = deepcopy(messages)

        applied, record = _fit_planner_context_budget(
            messages=messages,
            tools=[],
            request_conversation_context=None,
            hatch_transcript_index=None,
            hatch_transcript=None,
            surface="hatch",
            policy=policy,
            history=history,
            model="test/advisor",
            earlier_calls=earlier_calls,
            mark_for_cache=False,
        )

        assert applied == messages == before
        assert record.stubbed_tool_call_ids == ()
        assert record.over_estimate is True


@pytest.mark.asyncio
async def test_every_accounting_input_holds_on_a_multi_round_planner_turn(
    tmp_path: Path, tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    decisions: list[dict[str, Any]] = []
    decide = pipeline_planner.decide_context_budget

    def _spy(**kwargs: Any) -> Any:
        decisions.append(kwargs)
        return decide(**kwargs)

    monkeypatch.setattr(pipeline_planner, "decide_context_budget", _spy)
    context = _context(users={0: "Build CSV to JSON."}, assistants={1: "a" * 20_000})
    completion = _discovery_then_emit(tmp_path)
    recorder = BufferingRecorder()

    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        recorder=recorder,
        conversation_context=context,
        context_policy=_context_policy(max_prompt_tokens=20_000, max_request_bytes=1_000_000),
        model_overrides={"max_discovery_turns": 5},
    )

    assert len(decisions) == len(completion.requests) == 4
    for inputs in decisions:
        _assert_decision_inputs_hold(inputs)
        assert {segment.key for segment in inputs["segments"]} == {"history:0", "history:1"}


# --- The escape hatch's transcript --------------------------------------------------------------

# ``_AWKWARD_EVENTS`` holds three events of earlier turns, the current request's reference (index
# 3), and two events of the turn in progress (indices 4 and 5).
_EARLIER_EVENTS = (0, 1, 2)
_THIS_TURN_EVENTS = (4, 5)


def _hatch_events_sent(applied: list[dict[str, Any]]) -> list[Any]:
    content = applied[3]["content"]
    assert content.startswith(_HATCH_TRANSCRIPT_HEADER)
    events = json.loads(content.removeprefix(_HATCH_TRANSCRIPT_HEADER))["events"]
    assert type(events) is list
    return events


def test_the_hatch_sheds_only_events_of_earlier_turns_oldest_first_and_refuses_when_the_turn_alone_is_over() -> None:
    transcript, _ = _hatch_message(_AWKWARD_EVENTS, [])
    assert transcript.sheddable_event_indices() == _EARLIER_EVENTS
    full, _, _ = _fit_hatch_request(transcript, earlier_calls=(), policy=_context_policy(max_request_bytes=1_000_000))
    full_bytes = _request_size_of(full) + _TRANSCRIPT_ALLOWANCE
    sizes = transcript.event_bytes()
    refused = 0
    for count in range(len(_EARLIER_EVENTS) + len(_THIS_TURN_EVENTS) + 1):
        # Past the earlier events, the ceiling would need an event of this turn to go.
        ceiling = full_bytes - sum(sizes[index] for index in (*_EARLIER_EVENTS, *_THIS_TURN_EVENTS)[:count])
        try:
            applied, record, _ = _fit_hatch_request(transcript, earlier_calls=(), policy=_context_policy(max_request_bytes=ceiling))
        except PipelinePlannerError as refusal:
            assert count > len(_EARLIER_EVENTS), count
            assert refusal.code == "CONTEXT_BUDGET_EXHAUSTED" and refusal.detail_codes[0] == "context_ceiling_bytes"
            refused += 1
            continue
        assert set(record.shed_hatch_event_indices) <= set(_EARLIER_EVENTS)
        assert record.shed_hatch_event_indices == _EARLIER_EVENTS[: len(record.shed_hatch_event_indices)]
        sent = _hatch_events_sent(applied)
        assert sent[-3:] == [_AWKWARD_EVENTS[3], *(_AWKWARD_EVENTS[index] for index in _THIS_TURN_EVENTS)]
    assert refused >= 1


def test_over_the_token_setting_the_hatch_sends_this_turns_events_whole_over_the_estimate() -> None:
    transcript, _ = _hatch_message(_AWKWARD_EVENTS, [])
    full, _, _ = _fit_hatch_request(transcript, earlier_calls=(), policy=_context_policy(max_request_bytes=1_000_000))
    policy = _context_policy(max_prompt_tokens=math.ceil(_request_size_of(full) / _FALLBACK_RATIO / 4), max_request_bytes=1_000_000)

    applied, record, _ = _fit_hatch_request(transcript, earlier_calls=(), policy=policy)

    assert record.over_estimate is True
    assert record.shed_hatch_event_indices == _EARLIER_EVENTS
    assert _hatch_events_sent(applied) == [_AWKWARD_EVENTS[index] for index in (3, *_THIS_TURN_EVENTS)]


def test_hatch_keys_of_this_turn_carried_from_an_earlier_call_are_ignored() -> None:
    transcript, _ = _hatch_message(_AWKWARD_EVENTS, [])

    applied, record, _ = _fit_hatch_request(transcript, earlier_calls=(_earlier_call(("hatch:4", "hatch:5", "hatch:1")),))

    # Between the marks: the earlier-turn key stays shed, the keys of this turn's events do nothing.
    assert record.shed_hatch_event_indices == (1,)
    assert _hatch_events_sent(applied) == [_AWKWARD_EVENTS[index] for index in (0, 2, 3, *_THIS_TURN_EVENTS)]


def test_a_hatch_transcript_without_a_current_request_reference_has_nothing_to_shed() -> None:
    transcript, _ = _hatch_message([event for event in _AWKWARD_EVENTS if "content_ref" not in event or event is _AWKWARD_EVENTS[5]], [])

    assert transcript.sheddable_event_indices() == ()
    with pytest.raises(ValueError, match="sheddable"):
        transcript.without_events(frozenset({0}))


@pytest.mark.asyncio
async def test_a_turns_first_call_is_priced_at_the_sessions_stored_density() -> None:
    # The first call of a turn has no live anchor. The session's stored call (4.4 bytes per
    # provider token, same model and dialect) prices it, less the 10% margin, so it is judged
    # as the turn's later calls are and what is shed does not flip at the turn boundary.
    service, _ = _service(_GENEROUS_TOKENS)
    dialect = service._planner_dialect
    stored = _call(total_bytes=44_000, prompt_tokens=10_000, model=service._model)
    stored["tool_contract_dialect"] = None if dialect is None else dialect.value
    llm_messages = _two_round_turn(service, "f" * 2_000, "s" * 2_000)

    _selected, record = await _fit_two_round_turn(
        service,
        llm_messages,
        recorder=BufferingRecorder(),
        history=ContextBudgetHistory.from_records([_audit_row(1, stored)]),
        reply_stage=False,
    )

    assert record.anchor_status == "no_previous_call"
    assert record.bytes_per_token == repr(4.4 * 0.9)
    # The record says the density came from the session's stored call, as measured.
    assert (record.surface, record.density_source, record.measured_bytes_per_token) == ("compose_loop", "stored_call", repr(4.4))
    # Control: with no stored call the same request is priced at the policy's fallback rate.
    _selected, fallback = await _fit_two_round_turn(
        service, llm_messages, recorder=BufferingRecorder(), history=ContextBudgetHistory.empty(), reply_stage=False
    )
    assert fallback.bytes_per_token == repr(service._context_budget_policy.fallback_bytes_per_token)
    assert (fallback.density_source, fallback.measured_bytes_per_token) == ("fallback", None)
