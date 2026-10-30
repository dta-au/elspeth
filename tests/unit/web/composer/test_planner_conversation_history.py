"""Planner clarification retains the conversation without minting user intent."""

from __future__ import annotations

from elspeth.web.composer.request_history import (
    authoritative_conversation_context as _freeform_planner_conversation_context,
)


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
