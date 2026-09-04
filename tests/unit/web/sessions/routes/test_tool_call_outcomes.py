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

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.canonical import canonical_json
from elspeth.web.composer.audit_storage import redacted_tool_invocation_content_and_envelope
from elspeth.web.sessions.protocol import ChatMessageRecord
from elspeth.web.sessions.routes._helpers import (
    _tool_call_outcomes_by_call_id,
    _ToolCallOutcomeKind,
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
        assert outcomes["call-1"].outcome is _ToolCallOutcomeKind.APPLIED
        assert outcomes["call-1"].applied_state_version == 3

    def test_applied_without_version_map_entry_still_applied(self) -> None:
        outcomes = _tool_call_outcomes_by_call_id(
            [_tool_row(tool_call_id="call-1", composition_state_id=uuid4())],
            state_versions_by_id={},
        )
        assert outcomes["call-1"].outcome is _ToolCallOutcomeKind.APPLIED
        assert outcomes["call-1"].applied_state_version is None

    def test_successful_lookup_is_completed(self) -> None:
        outcomes = _tool_call_outcomes_by_call_id(
            [_tool_row(tool_call_id="call-1", content=json.dumps({"success": True, "data": {}}))],
            state_versions_by_id={},
        )
        assert outcomes["call-1"].outcome is _ToolCallOutcomeKind.COMPLETED
        assert outcomes["call-1"].applied_state_version is None

    def test_semantic_rejection_is_rejected(self) -> None:
        # ComposerToolStatus.SUCCESS with ToolResult.success=False — the
        # validation-refused mutation the LLM retries. Not applied, not a
        # crash: its own honest label.
        outcomes = _tool_call_outcomes_by_call_id(
            [_tool_row(tool_call_id="call-1", content=json.dumps({"success": False, "validation": {}}))],
            state_versions_by_id={},
        )
        assert outcomes["call-1"].outcome is _ToolCallOutcomeKind.REJECTED

    def test_failure_projection_is_failed(self) -> None:
        content = json.dumps({"_redaction_status": "plugin_crash", "error_class": "ValueError"})
        outcomes = _tool_call_outcomes_by_call_id(
            [_tool_row(tool_call_id="call-1", content=content)],
            state_versions_by_id={},
        )
        assert outcomes["call-1"].outcome is _ToolCallOutcomeKind.FAILED

    def test_cancelled_projection_is_cancelled(self) -> None:
        content = json.dumps({"_redaction_status": "cancelled", "error_class": "CancelledError", "error_message": "cancelled"})
        outcomes = _tool_call_outcomes_by_call_id(
            [_tool_row(tool_call_id="call-1", content=content)],
            state_versions_by_id={},
        )
        assert outcomes["call-1"].outcome is _ToolCallOutcomeKind.CANCELLED


def _produced_audit_envelope(*, version_before: int, version_after: int | None, status: ComposerToolStatus) -> dict[str, object]:
    """Build a fallback-drain envelope through the REAL producer.

    ``_persist_tool_invocations`` writes exactly
    ``redacted_tool_invocation_content_and_envelope(invocation)[1]`` into the
    row's ``tool_calls``. Hand-building the envelope in a fixture is what let
    the shape drift go unnoticed, so this helper mints one the way production
    does and never restates its layout.
    """
    arguments_canonical = canonical_json({})
    result_canonical = canonical_json({"success": status is ComposerToolStatus.SUCCESS})
    invocation = ComposerToolInvocation(
        tool_call_id="call-1",
        tool_name="upsert_node",
        arguments_canonical=arguments_canonical,
        arguments_hash=hashlib.sha256(arguments_canonical.encode()).hexdigest(),
        result_canonical=result_canonical,
        result_hash=hashlib.sha256(result_canonical.encode()).hexdigest(),
        status=status,
        error_class=None,
        error_message=None,
        version_before=version_before,
        version_after=version_after,
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=1,
        actor="compose_loop",
    )
    return redacted_tool_invocation_content_and_envelope(invocation)[1]


class TestProducerBuiltFallbackEnvelope:
    """The projection must read the envelope the fallback writer ACTUALLY emits.

    ``redacted_tool_invocation_content_and_envelope`` returns
    ``{"_kind": "audit", "invocation": {...}}`` — the per-call delta lives
    one level DOWN, under ``invocation``. Reading ``version_after`` off the
    top level finds nothing on every real row, so an applied mutation
    silently fell through to COMPLETED and rendered in the transcript as a
    lookup: precisely the dishonesty this projection exists to remove.

    ``TestFallbackWriterRows`` below pins the same behaviours against a
    hand-built flat envelope, which is why the drift survived — these tests
    construct through the producer instead.
    """

    def test_producer_envelope_version_advance_is_applied(self) -> None:
        row = _tool_row(
            tool_call_id="call-1",
            tool_calls=[_produced_audit_envelope(version_before=1, version_after=2, status=ComposerToolStatus.SUCCESS)],
            # The fallback writer stamps the SHARED post-compose id on every
            # row, so the state id cannot be the per-call truth here.
            composition_state_id=uuid4(),
        )
        outcomes = _tool_call_outcomes_by_call_id([row], state_versions_by_id={})
        assert outcomes["call-1"].outcome is _ToolCallOutcomeKind.APPLIED
        assert outcomes["call-1"].applied_state_version == 2

    def test_producer_envelope_without_version_advance_is_not_applied(self) -> None:
        row = _tool_row(
            tool_call_id="call-1",
            tool_calls=[_produced_audit_envelope(version_before=1, version_after=1, status=ComposerToolStatus.SUCCESS)],
            composition_state_id=uuid4(),
        )
        outcomes = _tool_call_outcomes_by_call_id([row], state_versions_by_id={})
        assert outcomes["call-1"].outcome is _ToolCallOutcomeKind.COMPLETED

    def test_producer_envelope_cancelled_status_is_cancelled(self) -> None:
        row = _tool_row(
            tool_call_id="call-1",
            tool_calls=[_produced_audit_envelope(version_before=1, version_after=None, status=ComposerToolStatus.CANCELLED)],
        )
        outcomes = _tool_call_outcomes_by_call_id([row], state_versions_by_id={})
        assert outcomes["call-1"].outcome is _ToolCallOutcomeKind.CANCELLED

    def test_producer_envelope_crash_status_is_failed(self) -> None:
        row = _tool_row(
            tool_call_id="call-1",
            tool_calls=[_produced_audit_envelope(version_before=1, version_after=None, status=ComposerToolStatus.PLUGIN_CRASH)],
        )
        outcomes = _tool_call_outcomes_by_call_id([row], state_versions_by_id={})
        assert outcomes["call-1"].outcome is _ToolCallOutcomeKind.FAILED


class TestFallbackWriterRows:
    """Rows from _persist_tool_invocations: audit envelope carries the delta.

    The per-call fields sit under ``invocation``, matching the
    ``{"_kind": "audit", "invocation": {...}}`` shape
    ``redacted_tool_invocation_content_and_envelope`` emits. These cases
    cover status/content combinations the producer helper cannot mint
    directly; ``TestProducerBuiltFallbackEnvelope`` above is the pin that
    the layout itself is right.
    """

    def _envelope(self, **overrides: object) -> dict[str, object]:
        invocation: dict[str, object] = {
            "tool_call_id": "call-1",
            "tool_name": "set_pipeline",
            "status": "success",
            "version_before": 1,
            "version_after": 1,
            "error_class": None,
            "result_canonical": json.dumps({"success": True}),
        }
        invocation.update(overrides)
        return {"_kind": "audit", "invocation": invocation}

    def test_version_advance_is_applied_with_envelope_version(self) -> None:
        row = _tool_row(
            tool_call_id="call-1",
            tool_calls=[self._envelope(version_after=2)],
            # Fallback writer stamps the SHARED post-compose id on every row —
            # the envelope delta, not the state id, is the per-call truth.
            composition_state_id=uuid4(),
        )
        outcomes = _tool_call_outcomes_by_call_id([row], state_versions_by_id={})
        assert outcomes["call-1"].outcome is _ToolCallOutcomeKind.APPLIED
        assert outcomes["call-1"].applied_state_version == 2

    def test_shared_state_id_without_version_advance_is_not_applied(self) -> None:
        row = _tool_row(
            tool_call_id="call-1",
            content=json.dumps({"success": True}),
            tool_calls=[self._envelope()],
            composition_state_id=uuid4(),
        )
        outcomes = _tool_call_outcomes_by_call_id([row], state_versions_by_id={})
        assert outcomes["call-1"].outcome is _ToolCallOutcomeKind.COMPLETED

    def test_crash_status_is_failed(self) -> None:
        row = _tool_row(
            tool_call_id="call-1",
            content=json.dumps({"_redaction_status": "plugin_crash", "error_class": "RuntimeError"}),
            tool_calls=[self._envelope(status="plugin_crash", version_after=None, error_class="RuntimeError")],
        )
        outcomes = _tool_call_outcomes_by_call_id([row], state_versions_by_id={})
        assert outcomes["call-1"].outcome is _ToolCallOutcomeKind.FAILED

    def test_cancelled_status_is_cancelled(self) -> None:
        row = _tool_row(
            tool_call_id="call-1",
            content=json.dumps({"_redaction_status": "cancelled", "error_class": "CancelledError"}),
            tool_calls=[self._envelope(status="cancelled", version_after=None, error_class="CancelledError")],
        )
        outcomes = _tool_call_outcomes_by_call_id([row], state_versions_by_id={})
        assert outcomes["call-1"].outcome is _ToolCallOutcomeKind.CANCELLED

    def test_rejected_mutation_via_content_success_false(self) -> None:
        row = _tool_row(
            tool_call_id="call-1",
            content=json.dumps({"success": False}),
            tool_calls=[self._envelope(result_canonical=json.dumps({"success": False}))],
        )
        outcomes = _tool_call_outcomes_by_call_id([row], state_versions_by_id={})
        assert outcomes["call-1"].outcome is _ToolCallOutcomeKind.REJECTED


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

    def test_undecodable_content_is_tier1_corruption(self) -> None:
        # Both tool-row writers persist JSON content, so an undecodable row
        # is corrupted Tier-1 data: it crashes instead of silently
        # classifying the call COMPLETED (the old defensive default).
        with pytest.raises(AuditIntegrityError):
            _tool_call_outcomes_by_call_id(
                [_tool_row(tool_call_id="call-1", content="not json")],
                state_versions_by_id={},
            )
