"""GET /guided must not carry chat-message write machinery (elspeth-23fc70ce9c).

``get_guided`` documents a no-write-on-GET custody contract: a fresh
session's initial turn is built in memory, a persisted occurrence is
replayed verbatim, and only a fenced mutation may materialize state or
audit evidence. The endpoint once constructed a ``ComposerCallRecorder``
that nothing fed and drained it through context-free ``add_message``
call sites on every exit path — dead machinery that contradicted the
endpoint's own contract. These tests pin the contract structurally so
the drain cannot quietly return.

``tests/unit/web/sessions/test_routes.py::
test_get_guided_does_not_persist_empty_initial_state`` pins the
behavioural half (no composition-state allocation on first GET).
"""

from __future__ import annotations

import inspect

from elspeth.web.sessions.routes import _helpers
from elspeth.web.sessions.routes.composer.guided import get_guided


def test_get_guided_has_no_recorder_or_audit_drain() -> None:
    """The GET endpoint neither creates a recorder nor persists audit rows."""
    source = inspect.getsource(get_guided)
    assert "BufferingRecorder" not in source
    assert "_persist_tool_invocations" not in source
    assert "_persist_llm_calls" not in source
    assert "add_message" not in source


def test_persist_chat_turns_helper_is_deleted() -> None:
    """``_persist_chat_turns`` had zero production callers; it must stay gone
    rather than invite a fourth context-free ``add_message`` drain."""
    source = inspect.getsource(_helpers)
    assert "def _persist_chat_turns" not in source
    assert "_persist_chat_turns" not in _helpers.__all__
