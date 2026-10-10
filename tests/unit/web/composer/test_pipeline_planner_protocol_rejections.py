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
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus, ToolContractDialect
from elspeth.contracts.composer_planner_audit import (
    ComposerPlannerAttemptLedTo,
    ComposerPlannerAttemptOutcome,
    ComposerPlannerAttemptPhase,
    ComposerPlannerCode,
)
from elspeth.web.composer import pipeline_planner
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.pipeline_planner import PipelinePlannerError, _parse_response_tool_calls
from elspeth.web.composer.tools._common import ToolContext
from elspeth.web.sessions.routes._helpers import freeform_planner_progress_reason
from tests.unit.web.composer.test_pipeline_planner import (
    _Function,
    _lifecycle,
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
        _parse_response_tool_calls(response, max_tool_calls=3, dialect=ToolContractDialect.NONE, sent_tool_names=frozenset())

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


def _length_stopped_tool_response(tmp_path: Path, *, discovery: bool = False) -> _Response:
    response = _response(("list_sources", {}) if discovery else ("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)}))
    choice = _ChoiceWithFinishReason(message=response.choices[0].message, finish_reason="length")
    response.choices[0] = cast(Any, choice)
    return response


@pytest.mark.asyncio
@pytest.mark.parametrize("discovery", [False, True], ids=["parseable-proposal", "parseable-discovery"])
async def test_length_stopped_parseable_calls_are_discarded_before_dispatch(
    tmp_path: Path,
    tool_context: ToolContext,
    discovery: bool,
) -> None:
    completion = _ScriptedCompletion(
        _length_stopped_tool_response(tmp_path, discovery=discovery),
        _response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)})),
    )
    recorder = BufferingRecorder()

    await _plan(tmp_path=tmp_path, tool_context=tool_context, completion=completion, recorder=recorder)

    assert len(completion.requests) == 2
    assert [call.planner_call_ordinal for call in recorder.llm_calls] == [1, 2]
    assert recorder.llm_calls[0].finish_reason == "length"
    assert recorder.llm_calls[0].status is ComposerLLMCallStatus.MALFORMED_RESPONSE
    assert recorder.llm_calls[0].error_message == "RESPONSE_TRUNCATED"
    assert recorder.planner_attempts[0].outcome is ComposerPlannerAttemptOutcome.TRUNCATED
    assert recorder.planner_attempts[0].led_to is ComposerPlannerAttemptLedTo.REPAIR
    assert recorder.planner_attempts[-1].outcome is ComposerPlannerAttemptOutcome.ACCEPTED
    assert all(invocation.tool_name != "list_sources" for invocation in recorder.invocations)
    assert all(message["role"] != "assistant" for message in completion.requests[1]["messages"])


@pytest.mark.asyncio
async def test_length_stopped_parseable_proposal_never_reaches_the_finalizer_without_repair_budget(
    tmp_path: Path,
    tool_context: ToolContext,
) -> None:
    completion = _ScriptedCompletion(_length_stopped_tool_response(tmp_path))
    recorder = BufferingRecorder()
    finalized: list[object] = []

    def finalize(candidate: object) -> object:
        finalized.append(candidate)
        return candidate

    with pytest.raises(PipelinePlannerError) as caught:
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            recorder=recorder,
            repair_budget=0,
            candidate_finalizer=finalize,
        )

    assert caught.value.code == "REPAIR_EXHAUSTED"
    assert finalized == []
    assert len(recorder.llm_calls) == 1
    assert recorder.invocations == ()
    assert all(attempt.outcome is not ComposerPlannerAttemptOutcome.ACCEPTED for attempt in recorder.planner_attempts)


@pytest.mark.asyncio
@pytest.mark.parametrize("expired_on_reply", [False, True], ids=["repair-with-fourteen-seconds", "expired-before-repair"])
async def test_length_stop_repair_keeps_the_original_deadline_and_audits_every_attempt(
    tmp_path: Path,
    tool_context: ToolContext,
    expired_on_reply: bool,
) -> None:
    elapsed = 0.0
    calls = 0

    class DeadlineCompletion(_ScriptedCompletion):
        async def __call__(self, **kwargs: Any) -> _Response:
            nonlocal elapsed, calls
            calls += 1
            if calls == 1:
                elapsed = 300.0 if expired_on_reply else 286.0
                return await super().__call__(**kwargs)
            self.requests.append(deepcopy(kwargs))
            elapsed = 300.0
            raise TimeoutError("controlled remaining provider budget expired")

    completion = DeadlineCompletion(_length_stopped_tool_response(tmp_path))
    recorder = BufferingRecorder()
    events: list[str] = []
    with (
        patch.object(pipeline_planner, "_planner_deadline_time", side_effect=lambda: elapsed),
        pytest.raises(PipelinePlannerError) as caught,
    ):
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            recorder=recorder,
            model_overrides={"timeout_seconds": 300.0},
            repair_budget=2,
            lifecycle=_lifecycle(events),
        )

    assert caught.value.code == "TIMEOUT"
    assert events[-1] == "settled:failed"
    assert calls == (1 if expired_on_reply else 2)
    assert len(recorder.llm_calls) == calls
    assert [call.planner_call_ordinal for call in recorder.llm_calls] == list(range(1, calls + 1))
    assert recorder.llm_calls[0].error_message == "RESPONSE_TRUNCATED"
    if not expired_on_reply:
        assert recorder.llm_calls[1].status is ComposerLLMCallStatus.TIMEOUT
    assert recorder.invocations == ()
    assert all(attempt.outcome is not ComposerPlannerAttemptOutcome.ACCEPTED for attempt in recorder.planner_attempts)


@pytest.mark.asyncio
async def test_reasoning_only_length_stop_spends_the_shared_repair_budget(
    tmp_path: Path,
    tool_context: ToolContext,
) -> None:
    response = _cut_off_response(completion_tokens=800, finish_reason="length")
    response.choices[0].message.tool_calls = None
    response.usage = {**response.usage, "completion_tokens_details": {"reasoning_tokens": 800}}
    completion = _ScriptedCompletion(response, response)
    recorder = BufferingRecorder()

    with pytest.raises(PipelinePlannerError) as caught:
        await _plan(tmp_path=tmp_path, tool_context=tool_context, completion=completion, recorder=recorder, repair_budget=1)

    assert caught.value.code == "REPAIR_EXHAUSTED"
    assert len(completion.requests) == 2
    assert [call.planner_call_ordinal for call in recorder.llm_calls] == [1, 2]
    assert all(call.completion_tokens == 800 for call in recorder.llm_calls)
    assert all(call.reasoning_tokens == 800 for call in recorder.llm_calls)
    assert all(call.error_message == "RESPONSE_TRUNCATED" for call in recorder.llm_calls)
    assert recorder.invocations == ()
