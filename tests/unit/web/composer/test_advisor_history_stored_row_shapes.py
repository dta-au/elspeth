"""Stored rows the composer's own writers produce must read back as advisor history.

``AdvisorTranscriptHistory.from_records`` parses the whole stored session
history on every composer turn and refuses a row it cannot account for. A row
shape that a writer legitimately produced and this reader refuses would stop
every later turn of that session, so each case here takes a row from the code
that writes it (the compose loop's own persistence, the chat-storage projection
of a tool invocation, a reply the backend changed) and
checks that it reads, and what it reads as.

Nothing here asserts what happens to a row no writer produced.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from elspeth.contracts.composer_audit import ComposerToolInvocation, ToolArgumentErrorCategory
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.advisor_transcript import AdvisorTranscriptHistory
from elspeth.web.composer.audit import (
    begin_dispatch,
    finish_arg_error,
    finish_cancelled,
    finish_plugin_crash,
    finish_success,
)
from elspeth.web.composer.audit_storage import redacted_tool_invocation_content_and_envelope
from elspeth.web.sessions.protocol import ChatMessageRecord
from tests.unit.web.composer import test_tool_batch_exit_contract as exit_contract
from tests.unit.web.composer import test_visible_tool_event_persistence as visible_persistence
from tests.unit.web.composer._helpers import (
    _clean_advisor_checkpoint,
    _composer_service_with_session,
    _make_settings,
    _mock_catalog,
)
from tests.unit.web.composer.test_tools import _stage1_valid_preview_state

visible_writer = visible_persistence.visible_writer


def _events(*, session_id: str, current: ChatMessageRecord, request: str, records: Any) -> list[dict[str, Any]]:
    history = AdvisorTranscriptHistory.from_records(
        session_id=session_id,
        actor_user_id="actor",
        current_request_id=str(current.id),
        request=request,
        records=records,
    )
    events = json.loads(history.events_json)
    assert isinstance(events, list)
    return events


def _stored_row(session_id: UUID, role: str, content: str, *, sequence_no: int, writer: str = "compose_loop", **fields: Any) -> Any:
    return ChatMessageRecord(
        id=uuid4(),
        session_id=session_id,
        role=role,
        content=content,
        created_at=datetime.now(UTC),
        writer_principal=writer,
        sequence_no=sequence_no,
        **fields,
    )


def _invocation(status: str) -> ComposerToolInvocation:
    """One tool invocation as the dispatch audit helpers build it for ``status``."""
    audit = begin_dispatch("call-1", "set_metadata", {"patch": {"name": "Renamed"}}, version_before=3, actor="composer")
    if status == "success":
        return finish_success(audit, result_payload={"success": True, "data": {"name": "Renamed"}}, version_after=4)
    if status == "arg_error":
        return finish_arg_error(
            audit,
            error_class="ToolArgumentError",
            error_category=ToolArgumentErrorCategory.MODEL_VALIDATION,
            error_message="patch must be an object",
        )
    if status == "plugin_crash":
        return finish_plugin_crash(audit, exc=RuntimeError("handler defect"))
    if status == "cancelled":
        return finish_cancelled(audit, exc=asyncio.CancelledError())
    raise AssertionError(f"unknown status {status}")


@pytest.mark.parametrize("stored_role", ["tool", "audit"])
@pytest.mark.parametrize("status", ["success", "arg_error", "plugin_crash", "cancelled"])
def test_tool_row_as_chat_storage_projects_it_reads_for_every_dispatch_status(status: str, stored_role: str) -> None:
    """Every status a dispatch can end with is stored by one projection; each of them must read.

    A crashed or cancelled dispatch has no result, so its stored invocation
    carries neither a result nor a result hash.
    """
    session_id = uuid4()
    invocation = _invocation(status)
    content, envelope = redacted_tool_invocation_content_and_envelope(invocation)
    tool_row = _stored_row(session_id, stored_role, content, sequence_no=1, tool_call_id="call-1", tool_calls=[envelope])
    current = _stored_row(session_id, "user", "Continue", sequence_no=2, writer="route_user_message")

    events = _events(session_id=str(session_id), current=current, request="Continue", records=(tool_row, current))

    tool_event = events[0]
    assert tool_event["role"] == "tool"
    assert tool_event["tool_call_id"] == "call-1"
    [stored] = tool_event["legacy_invocations"]
    assert stored["status"] == status
    assert stored["tool_name"] == "set_metadata"
    has_result = invocation.result_canonical is not None
    assert (stored["result_canonical"] is not None) is has_result
    assert (stored["result_hash"] is not None) is has_result
    # The reader shows what the row stored: the result itself, or the closed failure projection.
    shown = tool_event["content"] if "content" in tool_event else stored[tool_event["content_ref"].removeprefix("legacy_invocations[0].")]
    assert shown == content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "raw_content", "expected"),
    [
        ("The model's reply.\n\nBackend note appended.", "The model's reply.", {"provider_visible_prose_char_range": [0, 18]}),
        (
            "A backend-rewritten reply.",
            "The model's original reply.",
            {"provider_visible_prose_before_backend_augmentation": "The model's original reply."},
        ),
    ],
    ids=["backend-appended", "backend-rewrote"],
)
async def test_assistant_reply_the_backend_changed_reads_with_the_model_s_own_words_marked(
    visible_writer: Any, content: str, raw_content: str, expected: dict[str, Any]
) -> None:
    """A stored reply keeps the provider's prose beside what the backend published; the reader says which part is whose."""
    service, session, _authority, holder = visible_writer
    context = holder["context"]
    await service.add_message(
        session.id, "assistant", content, raw_content=raw_content, writer_principal="compose_loop", session_operation_context=context
    )
    current = await service.add_message(
        session.id, "user", "Continue", writer_principal="route_user_message", session_operation_context=context
    )
    records = await service.get_messages(session.id, limit=None, include_private_evidence=False)

    events = _events(session_id=str(session.id), current=current, request="Continue", records=records)

    assistant = events[0]
    assert assistant["role"] == "assistant" and assistant["content"] == content
    assert {key: assistant[key] for key in expected} == expected


@pytest.mark.asyncio
async def test_every_row_a_real_tool_turn_stores_reads_back_on_the_next_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever way each tool call of a turn ended, the rows that turn stored are readable history for the next one."""
    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint)
    service, session_id = _composer_service_with_session(_mock_catalog(), _make_settings(data_dir=tmp_path))
    provider = exit_contract._ScriptedProvider(exit_contract._raw_batch(*exit_contract._EVERY_EXIT_BATCH))
    call_ids = [call_id for call_id, _name, _arguments in exit_contract._EVERY_EXIT_BATCH]
    with patch.object(service._provider_gateway, "_call_llm", new=provider):
        await service.compose("Exercise every exit", [], _stage1_valid_preview_state(), session_id=session_id)

    sessions = service._require_sessions_service()
    current = await sessions.add_message(UUID(session_id), "user", "Next request", writer_principal="route_user_message")
    records = await sessions.get_messages(UUID(session_id), limit=None, include_private_evidence=False)

    events = _events(session_id=session_id, current=current, request="Next request", records=records)

    answered = [event["tool_call_id"] for event in events if event["role"] == "tool" and event.get("tool_call_id") in set(call_ids)]
    assert answered == call_ids
    assert events[-1]["content_ref"] == "complete_original_user_request_section"
