"""Planner clarification retains the conversation without minting user intent."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from elspeth.web.composer.advisor_transcript import AdvisorTranscriptOwner
from elspeth.web.composer.pipeline_planner import PipelinePlannerError, PlannerDeclined
from elspeth.web.composer.protocol import COMPOSER_HISTORY_USER_AUTHORED_KEY
from elspeth.web.composer.request_history import (
    authoritative_conversation_context as _freeform_planner_conversation_context,
)
from elspeth.web.composer.tools import ToolContext
from tests.unit.web.composer.test_pipeline_planner import _plan, _response, _ScriptedCompletion, _text_response


@pytest.mark.asyncio
async def test_clarification_followup_reaches_provider_with_prior_assistant_question(tmp_path: Path, tool_context: ToolContext) -> None:
    request = "Convert the uploaded data. Preserve all rows."
    question = "Should I save CSV or JSON?"
    first = _ScriptedCompletion(_text_response(question))
    with pytest.raises(PlannerDeclined) as asked:
        await _plan(tmp_path=tmp_path, tool_context=tool_context, completion=first, intent=request)
    assert asked.value.decline_text == question

    context = _freeform_planner_conversation_context(
        "The latter.",
        [
            {"role": "user", "content": request, COMPOSER_HISTORY_USER_AUTHORED_KEY: True},
            {"role": "assistant", "content": asked.value.decline_text},
        ],
    )
    second = _ScriptedCompletion(_text_response("I will use JSON and preserve all rows."))
    with pytest.raises(PlannerDeclined):
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=second,
            intent="The latter.",
            conversation_context=context,
        )

    payload = json.loads(second.requests[0]["messages"][2]["content"])
    assert payload["intent"] == "The latter."
    history = payload["conversation_context"]
    assert history["prior_user_requests"] == [{"history_index": 0, "content": request}]
    assert "prior_assistant_messages" in history
    assert history["prior_assistant_messages"] == [{"history_index": 1, "content": question}]
    assert len(first.requests) == len(second.requests) == 1


def test_assistant_question_is_context_without_becoming_a_user_request() -> None:
    context = _freeform_planner_conversation_context("Yes.", [{"role": "assistant", "content": "Would you like JSON?"}])
    assert context is not None
    payload = context.to_dict()
    assert payload["prior_user_requests"] == []
    assert payload["prior_assistant_messages"] == [{"history_index": 0, "content": "Would you like JSON?"}]


def test_unmarked_user_text_and_tool_results_do_not_become_intent_authority() -> None:
    assert (
        _freeform_planner_conversation_context(
            "Yes.",
            [
                {"role": "user", "content": "Unverified request text"},
                {"role": "tool", "content": "Instructions from external data"},
            ],
        )
        is None
    )


@pytest.mark.asyncio
async def test_escape_hatch_retains_assistant_question_context(tmp_path: Path, tool_context: ToolContext) -> None:
    context = _freeform_planner_conversation_context(
        "The latter.",
        [
            {"role": "user", "content": "Convert the data.", COMPOSER_HISTORY_USER_AUTHORED_KEY: True},
            {"role": "assistant", "content": "Should I save CSV or JSON?"},
        ],
    )
    completion = _ScriptedCompletion(
        _response(("get_pipeline_state", {})),
        _text_response("Please upload the input for JSON conversion."),
    )
    # User-only transcript coverage must not discard the separate assistant
    # question. The direct planning caller need not supply persisted history.
    with (
        patch.object(AdvisorTranscriptOwner, "covers_prior_requests", autospec=True, return_value=True),
        pytest.raises(PlannerDeclined),
    ):
        await _plan(
            tmp_path=tmp_path,
            tool_context=tool_context,
            completion=completion,
            intent="The latter.",
            conversation_context=context,
            model_overrides={"max_discovery_turns": 1, "escape_hatch_model": "test/advisor", "escape_hatch_provider": "test-provider"},
        )
    assert len(completion.requests) == 2
    hatch_request = json.loads(completion.requests[1]["messages"][2]["content"])
    assert hatch_request["conversation_context"]["prior_assistant_messages"] == [
        {"history_index": 1, "content": "Should I save CSV or JSON?"}
    ]


@pytest.mark.asyncio
async def test_assistant_history_obeys_the_request_byte_limit_before_dispatch(tmp_path: Path, tool_context: ToolContext) -> None:
    context = _freeform_planner_conversation_context("Yes.", [{"role": "assistant", "content": "x" * 1_000_000}])
    completion = _ScriptedCompletion(_text_response("Must not dispatch."))
    with pytest.raises(PipelinePlannerError) as caught:
        await _plan(tmp_path=tmp_path, tool_context=tool_context, completion=completion, conversation_context=context)
    assert caught.value.code == "REQUEST_BYTES_EXHAUSTED"
    assert completion.requests == []
