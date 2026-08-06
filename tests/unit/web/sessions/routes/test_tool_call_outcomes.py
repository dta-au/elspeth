"""Per-call outcome projection for the conversation channel (elspeth-f5e6723133).

The chat transcript labels every proposal-less tool call "Looked up", but in
auto_commit mode mutations execute without proposal rows, so applied changes
render as reads. The ground truth already exists in the Tier-1 tool rows:

* primary writer (``persist_compose_turn``): ``tool_calls`` is NULL and
  ``composition_state_id`` is set exactly when THAT call durably created a
  composition-state version;
* fallback writer (``_persist_tool_invocations``): the row carries one
  ``_kind="audit"`` envelope with per-call ``version_before`` /
  ``version_after`` / ``status``, while ``composition_state_id`` is the
  turn-shared post-compose id.

``_tool_call_outcomes_by_call_id`` projects those rows into a closed outcome
vocabulary keyed by tool_call_id so the GET messages route can stamp
server-authenticated outcomes onto the assistant rows' tool_calls envelopes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from elspeth.web.sessions.protocol import ChatMessageRecord
from elspeth.web.sessions.routes._helpers import (
    _tool_call_outcomes_by_call_id,
)

_NOW = datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC)
_SESSION = uuid4()


def _tool_row(
    *,
    tool_call_id: str,
    content: str = '{"success": true}',
    tool_calls: list[dict[str, object]] | None = None,
    composition_state_id=None,
):
    return ChatMessageRecord(
        id=uuid4(),
        session_id=_SESSION,
        role="tool",
        content=content,
        created_at=_NOW,
        writer_principal="compose_loop",
        tool_calls=tool_calls,
        composition_state_id=composition_state_id,
        tool_call_id=tool_call_id,
        parent_assistant_id=uuid4(),
    )


def _assistant_row(tool_call_ids: list[str]) -> ChatMessageRecord:
    return ChatMessageRecord(
        id=uuid4(),
        session_id=_SESSION,
        role="assistant",
        content="",
        created_at=_NOW,
        writer_principal="compose_loop",
        tool_calls=[
            {"id": call_id, "type": "function", "function": {"name": "set_pipeline", "arguments": "{}"}} for call_id in tool_call_ids
        ],
    )


class TestPrimaryWriterRows:
    """Rows from persist_compose_turn: tool_calls NULL, per-call state id."""

    def test_state_creating_call_is_applied_with_version(self) -> None:
        state_id = uuid4()
        outcomes = _tool_call_outcomes_by_call_id(
            [_tool_row(tool_call_id="call-1", composition_state_id=state_id)],
            state_versions_by_id={str(state_id): 3},
        )
        assert outcomes["call-1"].outcome == "applied"
        assert outcomes["call-1"].applied_state_version == 3

    def test_applied_without_version_map_entry_still_applied(self) -> None:
        outcomes = _tool_call_outcomes_by_call_id(
            [_tool_row(tool_call_id="call-1", composition_state_id=uuid4())],
            state_versions_by_id={},
        )
        assert outcomes["call-1"].outcome == "applied"
        assert outcomes["call-1"].applied_state_version is None

    def test_successful_lookup_is_completed(self) -> None:
        outcomes = _tool_call_outcomes_by_call_id(
            [_tool_row(tool_call_id="call-1", content=json.dumps({"success": True, "data": {}}))],
            state_versions_by_id={},
        )
        assert outcomes["call-1"].outcome == "completed"
        assert outcomes["call-1"].applied_state_version is None

    def test_semantic_rejection_is_rejected(self) -> None:
        # ComposerToolStatus.SUCCESS with ToolResult.success=False — the
        # validation-refused mutation the LLM retries. Not applied, not a
        # crash: its own honest label.
        outcomes = _tool_call_outcomes_by_call_id(
            [_tool_row(tool_call_id="call-1", content=json.dumps({"success": False, "validation": {}}))],
            state_versions_by_id={},
        )
        assert outcomes["call-1"].outcome == "rejected"

    def test_failure_projection_is_failed(self) -> None:
        content = json.dumps({"_redaction_status": "plugin_crash", "error_class": "ValueError"})
        outcomes = _tool_call_outcomes_by_call_id(
            [_tool_row(tool_call_id="call-1", content=content)],
            state_versions_by_id={},
        )
        assert outcomes["call-1"].outcome == "failed"

    def test_cancelled_projection_is_cancelled(self) -> None:
        content = json.dumps({"_redaction_status": "cancelled", "error_class": "CancelledError", "error_message": "cancelled"})
        outcomes = _tool_call_outcomes_by_call_id(
            [_tool_row(tool_call_id="call-1", content=content)],
            state_versions_by_id={},
        )
        assert outcomes["call-1"].outcome == "cancelled"


class TestFallbackWriterRows:
    """Rows from _persist_tool_invocations: audit envelope carries the delta."""

    def _envelope(self, **overrides: object) -> dict[str, object]:
        envelope: dict[str, object] = {
            "tool_call_id": "call-1",
            "tool_name": "set_pipeline",
            "status": "success",
            "version_before": 1,
            "version_after": 1,
            "error_class": None,
            "result_canonical": json.dumps({"success": True}),
        }
        envelope.update(overrides)
        return envelope

    def test_version_advance_is_applied_with_envelope_version(self) -> None:
        row = _tool_row(
            tool_call_id="call-1",
            tool_calls=[self._envelope(version_after=2)],
            # Fallback writer stamps the SHARED post-compose id on every row —
            # the envelope delta, not the state id, is the per-call truth.
            composition_state_id=uuid4(),
        )
        outcomes = _tool_call_outcomes_by_call_id([row], state_versions_by_id={})
        assert outcomes["call-1"].outcome == "applied"
        assert outcomes["call-1"].applied_state_version == 2

    def test_shared_state_id_without_version_advance_is_not_applied(self) -> None:
        row = _tool_row(
            tool_call_id="call-1",
            content=json.dumps({"success": True}),
            tool_calls=[self._envelope()],
            composition_state_id=uuid4(),
        )
        outcomes = _tool_call_outcomes_by_call_id([row], state_versions_by_id={})
        assert outcomes["call-1"].outcome == "completed"

    def test_crash_status_is_failed(self) -> None:
        row = _tool_row(
            tool_call_id="call-1",
            content=json.dumps({"_redaction_status": "plugin_crash", "error_class": "RuntimeError"}),
            tool_calls=[self._envelope(status="plugin_crash", version_after=None, error_class="RuntimeError")],
        )
        outcomes = _tool_call_outcomes_by_call_id([row], state_versions_by_id={})
        assert outcomes["call-1"].outcome == "failed"

    def test_cancelled_status_is_cancelled(self) -> None:
        row = _tool_row(
            tool_call_id="call-1",
            content=json.dumps({"_redaction_status": "cancelled", "error_class": "CancelledError"}),
            tool_calls=[self._envelope(status="cancelled", version_after=None, error_class="CancelledError")],
        )
        outcomes = _tool_call_outcomes_by_call_id([row], state_versions_by_id={})
        assert outcomes["call-1"].outcome == "cancelled"

    def test_rejected_mutation_via_content_success_false(self) -> None:
        row = _tool_row(
            tool_call_id="call-1",
            content=json.dumps({"success": False}),
            tool_calls=[self._envelope(result_canonical=json.dumps({"success": False}))],
        )
        outcomes = _tool_call_outcomes_by_call_id([row], state_versions_by_id={})
        assert outcomes["call-1"].outcome == "rejected"


class TestNonToolRows:
    def test_assistant_and_user_rows_are_ignored(self) -> None:
        user_row = ChatMessageRecord(
            id=uuid4(),
            session_id=_SESSION,
            role="user",
            content="hello",
            created_at=_NOW,
            writer_principal="route_user_message",
        )
        outcomes = _tool_call_outcomes_by_call_id(
            [user_row, _assistant_row(["call-1"])],
            state_versions_by_id={},
        )
        assert outcomes == {}

    def test_unparseable_content_defaults_to_completed(self) -> None:
        outcomes = _tool_call_outcomes_by_call_id(
            [_tool_row(tool_call_id="call-1", content="not json")],
            state_versions_by_id={},
        )
        assert outcomes["call-1"].outcome == "completed"
