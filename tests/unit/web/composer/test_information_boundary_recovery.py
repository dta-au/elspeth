"""Actual planner delivery controls for emergency information-boundary repair."""

from collections import deque

import pytest

from elspeth.web.composer.anti_anchor import build_drift_hint
from elspeth.web.composer.protocol import COMPOSER_HISTORY_USER_AUTHORED_KEY
from elspeth.web.composer.request_history import (
    authoritative_conversation_context as _freeform_planner_conversation_context,
)


@pytest.mark.parametrize("message", ["Build the requested pipeline.", "Build a pipeline that reads CSV and writes JSON."])
def test_every_authenticated_amendment_reaches_planner(message: str) -> None:
    messages = [
        {"role": "user", "content": f"mandatory amendment {index}", COMPOSER_HISTORY_USER_AUTHORED_KEY: True} for index in range(12)
    ]
    context = _freeform_planner_conversation_context(message, messages)
    assert context is not None
    assert [request.content for request in context.prior_user_requests] == [row["content"] for row in messages]
    assert context.additional_prior_user_requests_omitted == 0


def test_changed_arguments_hint_does_not_invent_measured_nonprogress() -> None:
    hint = build_drift_hint(deque([("set_pipeline", "a"), ("set_pipeline", "b"), ("set_pipeline", "c")], maxlen=5))
    assert "different arguments" in hint
    assert "do not repeat" in hint.lower()
    assert "not compared" in hint
    assert "drift without convergence" not in hint
    assert "choose a new repair strategy" not in hint
