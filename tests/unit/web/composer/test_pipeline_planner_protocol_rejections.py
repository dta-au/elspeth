"""Planner response-protocol rejections that must stay classified and repairable.

Covers three parser/loop classification seams in ``pipeline_planner``:

* over-cap tool batches report ``TOOL_CALLS_EXHAUSTED`` (not a provider fault);
* a terminal proposal batched with discovery calls is a repairable protocol
  rejection that closes the tool protocol and charges the repair budget;
* a tool call cut off by the provider's own output limit below the configured
  completion cap (``finish_reason='length'``) is repaired as a truncation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.composer_planner_audit import (
    ComposerPlannerAttemptLedTo,
    ComposerPlannerAttemptOutcome,
    ComposerPlannerAttemptPhase,
    ComposerPlannerCode,
)
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.pipeline_planner import PipelinePlannerError, _parse_response_tool_calls
from elspeth.web.composer.tools._common import ToolContext
from elspeth.web.sessions.routes._helpers import freeform_planner_progress_reason
from tests.unit.web.composer.test_pipeline_planner import (
    _Function,
    _Message,
    _pipeline,
    _plan,
    _planner_usage,
    _Response,
    _response,
    _ScriptedCompletion,
    _ToolCall,
)

_HATCH_OVERRIDES = {
    "escape_hatch_model": "openrouter/advisor-under-test",
    "escape_hatch_provider": "openrouter",
}


# --------------------------------------------------------------------------- #
# Over-cap tool batches                                                        #
# --------------------------------------------------------------------------- #


def _four_discovery_calls() -> _Response:
    return _response(("list_sources", {}), ("list_sinks", {}), ("list_models", {}), ("list_transforms", {}))


def test_parser_classifies_over_cap_batch_as_tool_calls_exhausted() -> None:
    response = _four_discovery_calls()

    with pytest.raises(PipelinePlannerError, match="per-turn tool call limit") as caught:
        _parse_response_tool_calls(response, max_tool_calls=3)

    assert caught.value.code == "TOOL_CALLS_EXHAUSTED"


@pytest.mark.asyncio
@pytest.mark.parametrize("hatch", [False, True], ids=["no-hatch", "hatch-configured"])
async def test_over_cap_batch_reaches_the_tool_call_cap_disposition(
    tmp_path: Path,
    tool_context: ToolContext,
    hatch: bool,
) -> None:
    completion = _ScriptedCompletion(_four_discovery_calls())
    recorder = BufferingRecorder()
    overrides: dict[str, object] = {"max_tool_calls_per_turn": 3}
    if hatch:
        overrides.update(_HATCH_OVERRIDES)

    with pytest.raises(PipelinePlannerError) as caught:
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            recorder=recorder,
            model_overrides=overrides,
        )

    assert caught.value.code == "TOOL_CALLS_EXHAUSTED"
    assert freeform_planner_progress_reason(caught.value.code) == "tool_call_cap_exceeded"
    assert len(completion.requests) == 1
    # The provider call is still audited even though the loop never runs.
    (llm_call,) = recorder.llm_calls
    assert llm_call.planner_call_ordinal == 1
    (attempt,) = recorder.planner_attempts
    assert attempt.outcome is ComposerPlannerAttemptOutcome.BUDGET_EXHAUSTED
    assert attempt.planner_code is ComposerPlannerCode.TOOL_CALLS_EXHAUSTED
    assert attempt.led_to is ComposerPlannerAttemptLedTo.TERMINAL
    assert recorder.invocations == ()


# --------------------------------------------------------------------------- #
# Terminal proposal batched with discovery                                     #
# --------------------------------------------------------------------------- #


def _terminal_with_sibling(tmp_path: Path) -> _Response:
    return _response(("list_sources", {}), ("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)}))


@pytest.mark.asyncio
async def test_terminal_batched_with_discovery_is_repaired_without_dispatch(
    tmp_path: Path,
    tool_context: ToolContext,
) -> None:
    completion = _ScriptedCompletion(
        _terminal_with_sibling(tmp_path),
        _response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})),
    )
    recorder = BufferingRecorder()

    await _plan(tmp_path=tmp_path, tool_context=tool_context, completion=completion, recorder=recorder)

    assert len(completion.requests) == 2
    assert recorder.llm_calls[0].status is ComposerLLMCallStatus.SUCCESS
    # Neither the discovery sibling nor the batched proposal was executed.
    assert [invocation.tool_name for invocation in recorder.invocations if invocation.tool_name == "list_sources"] == []
    repair_messages = completion.requests[1]["messages"]
    assistant_index = max(
        index
        for index, message in enumerate(repair_messages)
        if message["role"] == "assistant" and len(message.get("tool_calls") or ()) == 2
    )
    replies = repair_messages[assistant_index + 1 : assistant_index + 3]
    assert [message["role"] for message in replies] == ["tool", "tool"]
    assert [message["tool_call_id"] for message in replies] == ["call-1", "call-2"]
    for reply in replies:
        content = json.loads(reply["content"])
        assert content["success"] is False
        assert content["error_code"] == "TERMINAL_CALL_NOT_ALONE"
        assert "emit_pipeline_proposal must be the only tool call" in content["message"]
    first_attempt = recorder.planner_attempts[0]
    assert first_attempt.outcome is ComposerPlannerAttemptOutcome.GUARD_FIRED
    assert first_attempt.led_to is ComposerPlannerAttemptLedTo.REPAIR
    assert recorder.planner_attempts[-1].outcome is ComposerPlannerAttemptOutcome.ACCEPTED


@pytest.mark.asyncio
async def test_terminal_batched_with_discovery_spends_repair_budget_then_terminates(
    tmp_path: Path,
    tool_context: ToolContext,
) -> None:
    completion = _ScriptedCompletion(_terminal_with_sibling(tmp_path))
    recorder = BufferingRecorder()

    with pytest.raises(PipelinePlannerError) as caught:
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            recorder=recorder,
            repair_budget=0,
        )

    assert caught.value.code == "REPAIR_EXHAUSTED"
    assert len(completion.requests) == 1
    (attempt,) = recorder.planner_attempts
    assert attempt.outcome is ComposerPlannerAttemptOutcome.GUARD_FIRED
    assert attempt.planner_code is ComposerPlannerCode.REPAIR_EXHAUSTED
    assert attempt.led_to is ComposerPlannerAttemptLedTo.TERMINAL
    assert recorder.invocations == ()


@pytest.mark.asyncio
async def test_terminal_batched_with_discovery_goes_to_hatch_when_budget_spent(
    tmp_path: Path,
    tool_context: ToolContext,
) -> None:
    completion = _ScriptedCompletion(
        _terminal_with_sibling(tmp_path),
        _response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})),
    )
    recorder = BufferingRecorder()

    await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        recorder=recorder,
        repair_budget=0,
        model_overrides=_HATCH_OVERRIDES,
    )

    assert len(completion.requests) == 2
    assert completion.requests[1]["model"] == "openrouter/advisor-under-test"
    first_attempt = recorder.planner_attempts[0]
    assert first_attempt.planner_code is ComposerPlannerCode.REPAIR_EXHAUSTED
    assert first_attempt.led_to is ComposerPlannerAttemptLedTo.HATCH
    # The rejected turn is protocol-complete in the hatch transcript.
    hatch_messages = completion.requests[1]["messages"]
    tool_replies = [message for message in hatch_messages if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in tool_replies] == ["call-1", "call-2"]


@pytest.mark.asyncio
async def test_hatch_turn_batching_terminal_with_discovery_reraises_the_original_exhaustion(
    tmp_path: Path,
    tool_context: ToolContext,
) -> None:
    """The advisor gets one shot: a batched terminal call on the hatch turn is final, not a repair.

    The raised code alone cannot tell the arms apart — an ordinary-turn
    fall-through would also end REPAIR_EXHAUSTED once the hatch is spent — so
    the hatch attempt's own classification and the absence of any rejection
    replies to the advisor's calls carry the assertion.
    """
    completion = _ScriptedCompletion(_terminal_with_sibling(tmp_path), _terminal_with_sibling(tmp_path))
    recorder = BufferingRecorder()

    with pytest.raises(PipelinePlannerError) as caught:
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            recorder=recorder,
            repair_budget=0,
            model_overrides=_HATCH_OVERRIDES,
        )

    assert caught.value.code == "REPAIR_EXHAUSTED"
    assert len(completion.requests) == 2
    assert completion.requests[1]["model"] == "openrouter/advisor-under-test"
    assert recorder.invocations == ()
    assert [attempt.led_to for attempt in recorder.planner_attempts] == [
        ComposerPlannerAttemptLedTo.HATCH,
        ComposerPlannerAttemptLedTo.TERMINAL,
    ]
    hatch_attempt = recorder.planner_attempts[-1]
    assert hatch_attempt.phase is ComposerPlannerAttemptPhase.HATCH
    assert hatch_attempt.outcome is ComposerPlannerAttemptOutcome.MALFORMED_RESPONSE
    assert hatch_attempt.planner_code is ComposerPlannerCode.MALFORMED_RESPONSE


@pytest.mark.asyncio
async def test_two_terminal_calls_in_one_turn_stay_malformed(
    tmp_path: Path,
    tool_context: ToolContext,
) -> None:
    completion = _ScriptedCompletion(
        _response(("emit_pipeline_proposal", {"pipeline": {}}), ("emit_pipeline_proposal", {"pipeline": {}})),
    )
    recorder = BufferingRecorder()

    with pytest.raises(PipelinePlannerError) as caught:
        await _plan(tmp_path=tmp_path, tool_context=tool_context, completion=completion, recorder=recorder)

    assert caught.value.code == "MALFORMED_RESPONSE"
    assert len(completion.requests) == 1


# --------------------------------------------------------------------------- #
# Truncation signalled by finish_reason below the completion cap              #
# --------------------------------------------------------------------------- #


@dataclass
class _ChoiceWithFinishReason:
    message: _Message
    finish_reason: str | None = None


_PARTIAL_PROPOSAL_ARGUMENTS = '{"pipeline": {"source": {"plugin": "csv", "opti'


def _cut_off_response(*, completion_tokens: int, finish_reason: str | None) -> _Response:
    call = _ToolCall(id="call-1", function=_Function(name="emit_pipeline_proposal", arguments=_PARTIAL_PROPOSAL_ARGUMENTS))
    usage = dict(_planner_usage())
    usage["completion_tokens"] = completion_tokens
    usage["total_tokens"] = 10 + completion_tokens
    choice = cast(Any, _ChoiceWithFinishReason(message=_Message(content=None, tool_calls=[call]), finish_reason=finish_reason))
    return _Response(choices=[choice], usage=usage)


@pytest.mark.asyncio
async def test_length_finish_below_cap_with_unparseable_arguments_is_repaired_as_truncation(
    tmp_path: Path,
    tool_context: ToolContext,
) -> None:
    completion = _ScriptedCompletion(
        _cut_off_response(completion_tokens=640, finish_reason="length"),
        _response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})),
    )
    recorder = BufferingRecorder()

    await _plan(tmp_path=tmp_path, tool_context=tool_context, completion=completion, recorder=recorder)

    assert len(completion.requests) == 2
    first_call = recorder.llm_calls[0]
    assert first_call.max_completion_tokens_requested == 800
    assert first_call.completion_tokens == 640
    assert first_call.finish_reason == "length"
    assert first_call.error_message == "RESPONSE_TRUNCATED"
    notices = [message["content"] for message in completion.requests[1]["messages"] if message["role"] == "user"]
    assert any("cut off at the output token limit" in notice for notice in notices)
    assert recorder.planner_attempts[0].outcome is ComposerPlannerAttemptOutcome.TRUNCATED


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", [None, "tool_calls", "stop"])
async def test_unparseable_arguments_below_cap_without_length_finish_stay_fatal(
    tmp_path: Path,
    tool_context: ToolContext,
    finish_reason: str | None,
) -> None:
    completion = _ScriptedCompletion(_cut_off_response(completion_tokens=640, finish_reason=finish_reason))
    recorder = BufferingRecorder()

    with pytest.raises(PipelinePlannerError) as caught:
        await _plan(tmp_path=tmp_path, tool_context=tool_context, completion=completion, recorder=recorder)

    assert caught.value.code == "MALFORMED_RESPONSE"
    assert len(completion.requests) == 1
    assert recorder.llm_calls[0].error_message == "MALFORMED_RESPONSE"
