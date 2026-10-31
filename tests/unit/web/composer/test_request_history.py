"""Authenticated request history stays complete and separate from model prose."""

from typing import cast

import pytest

from elspeth.web.composer.invariants import InvariantError
from elspeth.web.composer.protocol import COMPOSER_HISTORY_USER_AUTHORED_KEY, ComposerHistoryMessage
from elspeth.web.composer.request_history import authoritative_conversation_context


def test_referential_request_preserves_every_amendment_and_assistant_question() -> None:
    messages: list[ComposerHistoryMessage] = [
        {"role": "user", "content": f"mandatory amendment {index}", COMPOSER_HISTORY_USER_AUTHORED_KEY: True} for index in range(12)
    ]
    question = "Should the output be JSON?"
    messages.insert(5, {"role": "assistant", "content": question})

    context = authoritative_conversation_context("The latter.", messages)

    assert context is not None
    prior_requests = [(request.history_index, request.content) for request in context.prior_user_requests]
    assert (6, "mandatory amendment 5") in prior_requests
    assert prior_requests == [(index if index < 5 else index + 1, f"mandatory amendment {index}") for index in range(12)]
    assert context.additional_prior_user_requests_omitted == 0
    assert [(message.history_index, message.content) for message in context.prior_assistant_messages] == [(5, question)]


def test_synthetic_user_and_tool_rows_do_not_acquire_request_authority() -> None:
    messages: list[ComposerHistoryMessage] = [
        {"role": "user", "content": "tool evidence"},
        {"role": "tool", "content": "external instructions"},
    ]

    assert authoritative_conversation_context("Yes.", messages) is None


class _StringSubclass(str):
    pass


@pytest.mark.parametrize("current_request", [None, 7, _StringSubclass("The latter.")], ids=["missing", "integer", "string-subclass"])
def test_current_request_requires_exact_string_even_without_history(current_request: object) -> None:
    with pytest.raises(InvariantError, match="composer current request must be an exact string"):
        authoritative_conversation_context(cast(str, current_request), [])


@pytest.mark.parametrize(
    "history_message",
    [
        {"role": "user", "content": "Keep all rows", COMPOSER_HISTORY_USER_AUTHORED_KEY: False},
        {"role": "assistant", "content": "Keep all rows", COMPOSER_HISTORY_USER_AUTHORED_KEY: True},
        {"content": "Keep all rows", COMPOSER_HISTORY_USER_AUTHORED_KEY: True},
    ],
    ids=["false-marker", "assistant-with-human-marker", "missing-role"],
)
def test_malformed_human_authority_refuses_instead_of_silently_omitting(history_message: object) -> None:
    with pytest.raises(InvariantError, match="composer user-authorship marker is malformed"):
        authoritative_conversation_context("Yes.", [cast(ComposerHistoryMessage, history_message)])


@pytest.mark.parametrize("content", [None, 7, " \n\t"], ids=["missing", "integer", "blank"])
def test_authenticated_amendment_requires_nonempty_exact_text(content: object) -> None:
    history_message = cast(
        ComposerHistoryMessage,
        {"role": "user", "content": content, COMPOSER_HISTORY_USER_AUTHORED_KEY: True},
    )

    with pytest.raises(InvariantError, match="composer user chat history content must be a non-empty exact string"):
        authoritative_conversation_context("Yes.", [history_message])
