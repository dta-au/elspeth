"""What the planner does when its budget is spent and a senior model is, or is not, there to take over.

The planner has one provider call per step of its work: every repair after a
rejected candidate is another call, and when a budget is spent the puzzle goes
once to the senior ("escape hatch") model if one is configured. These pin the
combinations the per-budget tests leave open:

* each repair is exactly one provider call, and no candidate is accepted
  without one;
* an exhaustion that engages the senior model costs exactly one more call,
  to that model;
* when the senior model's single turn fails, the exhaustion that engaged it
  is what the caller sees, and no further call is made.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.composer_planner_audit import ComposerPlannerAttemptLedTo, ComposerPlannerAttemptPhase
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.pipeline_planner import PipelinePlannerError
from elspeth.web.composer.tools._common import ToolContext
from tests.unit.web.composer.test_pipeline_planner import (
    _budget,
    _invalid_pipeline,
    _pipeline,
    _plan,
    _response,
    _response_with_call_id,
    _ScriptedCompletion,
    _truncated_response,
)

_SENIOR_MODEL = "openrouter/advisor-under-test"
_SENIOR: dict[str, object] = {"escape_hatch_model": _SENIOR_MODEL, "escape_hatch_provider": "openrouter"}


def _rejected(tmp_path: Path, call_id: str) -> Any:
    return _response_with_call_id(call_id, "emit_pipeline_proposal", {"pipeline": _invalid_pipeline(tmp_path)})


def _accepted(tmp_path: Path) -> Any:
    return _response(("emit_pipeline_proposal", {"pipeline": _pipeline(tmp_path)}))


def _tool_results(request: Mapping[str, Any]) -> list[str]:
    return [message["tool_call_id"] for message in request["messages"] if message["role"] == "tool"]


@pytest.mark.asyncio
@pytest.mark.parametrize("repairs", [0, 1, 3])
async def test_each_repair_is_one_provider_call_carrying_every_earlier_rejection(
    repairs: int, tmp_path: Path, tool_context: ToolContext
) -> None:
    rejected_ids = [f"rejected-{index}" for index in range(repairs)]
    completion = _ScriptedCompletion(*[_rejected(tmp_path, call_id) for call_id in rejected_ids], _accepted(tmp_path))

    proposal = await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        repair_budget=repairs,
        budget=_budget(max_total_provider_calls=repairs + 1),
    )

    assert deep_thaw(proposal.proposal.pipeline) == _pipeline(tmp_path)
    assert len(completion.requests) == repairs + 1
    for index, request in enumerate(completion.requests):
        # The model sees the result of every candidate it has had rejected so far, and nothing else.
        assert _tool_results(request) == rejected_ids[:index]
    assert _SENIOR_MODEL not in {request["model"] for request in completion.requests}


@pytest.mark.asyncio
async def test_senior_model_reply_that_is_cut_off_leaves_the_original_exhaustion_standing(
    tmp_path: Path, tool_context: ToolContext
) -> None:
    completion = _ScriptedCompletion(_rejected(tmp_path, "rejected-primary"), _truncated_response(completion_tokens=800))
    recorder = BufferingRecorder()

    with pytest.raises(PipelinePlannerError) as raised:
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            recorder=recorder,
            repair_budget=0,
            model_overrides=_SENIOR,
        )

    # The caller is told what exhausted the primary, not how the senior turn failed.
    assert raised.value.code == "REPAIR_EXHAUSTED"
    assert raised.value.detail_codes == ("source_on_success_dangling",)
    # One call to each model; the senior model gets one turn and no repair.
    assert [request["model"] == _SENIOR_MODEL for request in completion.requests] == [False, True]
    # The trail keeps the senior turn's own outcome.
    senior_attempt = recorder.planner_attempts[-1]
    assert senior_attempt.phase is ComposerPlannerAttemptPhase.HATCH
    assert senior_attempt.led_to is ComposerPlannerAttemptLedTo.TERMINAL


@pytest.mark.asyncio
async def test_unchanged_repeat_rejection_goes_to_the_senior_model_once(tmp_path: Path, tool_context: ToolContext) -> None:
    """A candidate re-sent unchanged after a rejection whose facts were withheld stops the primary; the senior model gets one turn."""

    def finalize(candidate: Mapping[str, Any]) -> Mapping[str, Any]:
        assert type(candidate) is dict
        candidate["source"]["options"]["private_flag"] = True
        return candidate

    candidate = _pipeline(tmp_path)
    completion = _ScriptedCompletion(
        _response_with_call_id("blind-first", "emit_pipeline_proposal", {"pipeline": deepcopy(candidate)}),
        _response_with_call_id("blind-repeat", "emit_pipeline_proposal", {"pipeline": deepcopy(candidate)}),
        _response_with_call_id("senior-repeat", "emit_pipeline_proposal", {"pipeline": deepcopy(candidate)}),
    )

    with pytest.raises(PipelinePlannerError) as raised:
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            repair_budget=5,
            candidate_finalizer=finalize,
            model_overrides=_SENIOR,
        )

    assert raised.value.code == "REPAIR_EXHAUSTED"
    assert "short-circuited" in str(raised.value)
    # Two primary calls (the repair budget of 5 is not spent), then exactly one senior call.
    assert [request["model"] == _SENIOR_MODEL for request in completion.requests] == [False, False, True]


@pytest.mark.asyncio
async def test_repeated_identical_rejected_arguments_go_to_the_senior_model(tmp_path: Path, tool_context: ToolContext) -> None:
    completion = _ScriptedCompletion(*[_response(("list_sources", {})) for _ in range(6)], _accepted(tmp_path))

    proposal = await _plan(
        tmp_path=tmp_path,
        tool_context=tool_context,
        completion=completion,
        budget=_budget(max_total_provider_calls=12),
        model_overrides={**_SENIOR, "tool_contract_dialect": ToolContractDialect.OPENAI_STRICT, "max_discovery_turns": 10},
    )

    assert deep_thaw(proposal.proposal.pipeline) == _pipeline(tmp_path)
    assert [request["model"] == _SENIOR_MODEL for request in completion.requests] == [False] * 6 + [True]
