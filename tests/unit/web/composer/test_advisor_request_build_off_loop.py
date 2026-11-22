"""History-proportional Advisor work runs once per request, off the event loop.

Before this change ``_call_advisor_with_audit_body`` rendered prompt-guard parts
for the whole unshaped session transcript on the event loop, then shaped the
transcript by age and rendered the parts again for what it sent, discarding the
first result; on the gold-3 battery session the discarded render held the loop
for 14-20 s per Advisor attempt. Every ``AdvisorTranscriptOwner.snapshot`` (a
turn takes several) also decoded, re-read and re-redacted the whole stored
history on the loop.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.composer import advisor_context
from elspeth.web.composer import advisor_transcript as transcript_module
from elspeth.web.composer.advisor_transcript import AdvisorTranscriptHistory, AdvisorTranscriptOwner
from elspeth.web.composer.audit import BufferingRecorder
from tests.unit.web.composer.test_advisor_transcript_budget import _arguments, _owner, _probe, _Session, _transport


def _record_renders(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, Any, tuple[Any, ...]]]:
    real = advisor_context.advisor_user_content_parts
    renders: list[tuple[int, Any, tuple[Any, ...]]] = []

    def recording(arguments):
        parts = real(arguments)
        renders.append((threading.get_ident(), advisor_context.advisor_request_transcript(arguments), parts))
        return parts

    monkeypatch.setattr(advisor_context, "advisor_user_content_parts", recording)
    return renders


@pytest.mark.asyncio
async def test_a_shaped_request_renders_its_parts_once_off_the_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = _Session()
    for n in range(49):
        session.turn(n, request=f"REQUEST_{n} " + "r" * 3_000)
    transcript = session.snapshot()
    renders = _record_renders(monkeypatch)
    requests = _transport(monkeypatch)
    await _owner(tmp_path)._call_advisor_with_audit(_arguments(transcript), recorder=BufferingRecorder())
    assert len(requests) == 1
    assert len(renders) == 1, "the unshaped transcript is no longer rendered and discarded"
    thread, rendered, parts = renders[0]
    assert thread != threading.get_ident(), "the render runs in a worker thread, not on the event loop"
    assert rendered is not transcript, "the render is of the transcript after shaping by age"
    sent = requests[0]["messages"][1]["content"]
    assert "".join(part.text for part in parts) == sent
    assert requests[0]["prompt_guard_content_parts"][1] == parts


@pytest.mark.asyncio
async def test_an_unshaped_request_is_unchanged_and_rendered_once_off_the_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    transcript = _probe(0)
    renders = _record_renders(monkeypatch)
    requests = _transport(monkeypatch)
    arguments = _arguments(transcript)
    await _owner(tmp_path)._call_advisor_with_audit(arguments, recorder=BufferingRecorder())
    assert len(requests) == 1 and len(renders) == 1
    thread, rendered, parts = renders[0]
    assert thread != threading.get_ident()
    assert rendered is transcript
    assert requests[0]["messages"][1]["content"] == advisor_context.build_advisor_user_message(arguments)
    assert requests[0]["prompt_guard_content_parts"][1] == parts


def _owner_with_history(turns: int) -> AdvisorTranscriptOwner:
    session = _Session()
    for n in range(turns):
        session.turn(n)
    current = "CURRENT_REQUEST rename the finding field"
    request = session.add("user", current, writer="route_user_message")
    history = AdvisorTranscriptHistory.from_records(
        session_id=str(session.sid), actor_user_id="actor", current_request_id=str(request.id), request=current, records=tuple(session.rows)
    )
    owner = AdvisorTranscriptOwner(request=current, history=history)
    owner.begin_messages([])
    return owner


def _observe_round(owner: AdvisorTranscriptOwner, n: int) -> None:
    owner.observe_messages(
        [
            {
                "role": "assistant",
                "content": f"THIS_TURN_PROSE_{n}",
                "tool_calls": [{"id": f"call-now-{n}", "type": "function", "function": {"name": "get_pipeline_state", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": f"call-now-{n}", "content": json.dumps({"success": True, "data": f"THIS_TURN_RESULT_{n}"})},
        ]
    )


def _redactions_for_a_turn(turns: int, snapshots: int, monkeypatch: pytest.MonkeyPatch) -> int:
    """Redaction calls made by one request's snapshots, as a turn takes them between tool rounds."""
    owner = _owner_with_history(turns)
    real = transcript_module._redact_sensitive_content
    calls = 0

    def counting(text):
        nonlocal calls
        calls += 1
        return real(text)

    monkeypatch.setattr(transcript_module, "_redact_sensitive_content", counting)
    try:
        for n in range(snapshots):
            _observe_round(owner, n)
            owner.snapshot()
    finally:
        monkeypatch.setattr(transcript_module, "_redact_sensitive_content", real)
    return calls


def test_a_turns_snapshots_redact_the_stored_history_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seven snapshots cost about one history redaction, not seven; work stays linear in history size."""
    one = _redactions_for_a_turn(40, 1, monkeypatch)
    seven = _redactions_for_a_turn(40, 7, monkeypatch)
    larger = _redactions_for_a_turn(160, 7, monkeypatch)
    assert seven / one < 2.0, f"7 snapshots made {seven} redactions, 1 snapshot {one}: the history is redacted again each time"
    assert larger / seven < 5.0, f"4x the history made {larger / seven:.1f}x the redactions"


def test_repeated_snapshots_equal_a_fresh_owners_snapshot() -> None:
    reused = _owner_with_history(12)
    for n in range(3):
        _observe_round(reused, n)
        reused.snapshot()
    # A fresh owner over the same stored history, never snapshotted before.
    fresh = AdvisorTranscriptOwner(request=reused.request, history=reused._history)
    fresh.begin_messages([])
    for n in range(3):
        _observe_round(fresh, n)
    assert fresh._history_redaction is None
    first = fresh.snapshot()
    again = reused.snapshot()
    assert (again.provider_json, again.binding, again.semantic_binding) == (first.provider_json, first.binding, first.semantic_binding)


@pytest.mark.asyncio
async def test_a_turn_prepares_the_history_off_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _owner_with_history(12)
    threads = []
    real = owner._projected_history

    def recording():
        threads.append(threading.get_ident())
        return real()

    monkeypatch.setattr(owner, "_projected_history", recording)
    await run_sync_in_worker(owner.prepare_history)
    assert threads and threads[0] != threading.get_ident()
    assert owner._history_redaction is not None
