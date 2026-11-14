"""A turn whose reply stage the context budget refuses, sent through the session route.

The composer-level tests stop at the composer's result. Here the turn is
submitted to ``POST /api/sessions/{id}/messages``: the real compose loop runs
inside the route's operation, the real decision refuses the reply stage, and
the route settles and persists the finished turn. The test then reads the
session back through every reader that sees its rows on the next turn.

The route's composer is the real ``ComposerServiceImpl`` entered at its
compose loop (``_run_one_turn_for_test``) rather than at ``compose``, so the
turn reaches the staged-review branch without scripting the planner. Only the
provider is scripted, and the byte ceiling is lowered for the reply stage's
decision alone so the request cannot fit.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.web.composer import no_tool_policy
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.advisor_transcript import AdvisorTranscriptHistory
from elspeth.web.composer.context_budget_history import ContextBudgetHistory
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.composer.reply_stage_refusal import COMPOSER_REPLY_STAGE_REFUSAL_KIND
from elspeth.web.composer.service import ComposerAvailability
from elspeth.web.sessions.models import chat_messages_table, composer_async_operations_table
from tests.helpers.composer_operations import current_head_state_id_sync, message_body
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient
from tests.unit.web.composer._helpers import _clean_advisor_checkpoint
from tests.unit.web.composer.test_compose_loop_interpretation_review_dispatch import (
    _build_composer,
    _fake_response_with_tool_call,
    _fake_text_response,
    _ScriptedLLM,
    _set_pipeline_with_pending_interpretation_args,
)
from tests.unit.web.sessions.test_routes import _make_app, _make_composer_mock, _settled_composer_response

_REPLY_TOOLS = {"handoff_to_user", "restore_parked_graph"}
_REPLY_PROSE = "A fork suits independent personas; distinct answer columns preserve both results."
_REPLY_STAGE_BYTE_CEILING = 1024


def _is_reply_stage(tools: list[dict[str, Any]]) -> bool:
    return {tool["function"]["name"] for tool in tools} == _REPLY_TOOLS


def test_a_turn_whose_reply_stage_is_refused_completes_persists_and_reads_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint, raising=True)
    monkeypatch.setattr(
        "elspeth.web.composer.service.compute_availability",
        lambda **kwargs: ComposerAvailability(available=True, model=str(kwargs["model"]), provider="anthropic"),
    )
    app, service = _make_app(tmp_path)
    composer = _build_composer(tmp_path, service)
    scripted = _ScriptedLLM(
        [
            _fake_response_with_tool_call(
                tool_call_id="call_set_pipeline",
                tool_name="set_pipeline",
                arguments=_set_pipeline_with_pending_interpretation_args(),
            ),
            _fake_response_with_tool_call(
                tool_call_id="call_review",
                tool_name="request_interpretation_review",
                arguments={
                    "affected_node_id": "rate_node",
                    "kind": "vague_term",
                    "user_term": "cool",
                    "llm_draft": "modern, useful, engaging, and clear for the public.",
                },
            ),
            _fake_text_response(_REPLY_PROSE),
        ]
    )
    loop_results: list[ComposerResult] = []
    original_loop = composer._compose_loop
    decide = composer._shed_to_context_budget
    policy = composer._context_budget_policy
    reply_stage_decisions: list[bool] = []

    async def _recording_loop(*args: Any, **kwargs: Any) -> ComposerResult:
        completed = await original_loop(*args, **kwargs)
        loop_results.append(completed)
        return completed

    def _decide_under_a_ceiling_the_reply_cannot_fit(**kwargs: Any) -> Any:
        if not _is_reply_stage(kwargs["tools"]):
            return decide(**kwargs)
        reply_stage_decisions.append(True)
        monkeypatch.setattr(composer, "_context_budget_policy", replace(policy, max_request_bytes=_REPLY_STAGE_BYTE_CEILING))
        try:
            return decide(**kwargs)
        finally:
            monkeypatch.setattr(composer, "_context_budget_policy", policy)

    monkeypatch.setattr(composer, "_compose_loop", _recording_loop)
    monkeypatch.setattr(composer, "_shed_to_context_budget", _decide_under_a_ceiling_the_reply_cannot_fit)

    async def _compose_through_the_real_loop(message: str, messages: Any, state: Any, **kwargs: Any) -> ComposerResult:
        del messages
        with patch.object(composer._provider_gateway, "_call_llm", new=scripted):
            await composer._run_one_turn_for_test(
                session_id=kwargs["session_id"],
                current_state_id=kwargs["current_state_id"],
                initial_state=state,
                message=message,
                session_operation_context=kwargs["session_operation_context"],
            )
        (completed,) = loop_results
        return completed

    app.state.composer_service = SimpleNamespace(compose=_compose_through_the_real_loop)
    client = TestClient(app, raise_server_exceptions=False)
    session_id = client.post("/api/sessions", json={"title": "Reply stage refusal"}).json()["id"]
    operation_id = str(uuid.uuid4())

    response = _settled_composer_response(
        client,
        app,
        path=f"/api/sessions/{session_id}/messages",
        body=message_body(
            "create a workflow that rates how cool pages are",
            state_id=current_head_state_id_sync(client, session_id),
            operation_id=operation_id,
        ),
    )

    # The turn completed: the refusal did not fail it.
    assert response.status_code == 200, response.text
    with app.state.session_engine.connect() as conn:
        operation = (
            conn.execute(select(composer_async_operations_table).where(composer_async_operations_table.c.operation_id == operation_id))
            .mappings()
            .one()
        )
        rows = (
            conn.execute(
                chat_messages_table.select()
                .where(chat_messages_table.c.session_id == session_id)
                .order_by(chat_messages_table.c.sequence_no)
            )
            .mappings()
            .all()
        )
    assert operation["status"] == "completed"

    # No provider call was made for the reply: the real decision refused it first.
    assert reply_stage_decisions == [True]
    assert [_is_reply_stage(tools) for tools in scripted.tools] == [False, False]
    assert len(scripted._responses) == 1
    (completed,) = loop_results
    assert [call.status for call in completed.llm_calls] == [ComposerLLMCallStatus.SUCCESS, ComposerLLMCallStatus.SUCCESS]

    # The refusal row is stored once, with the decision's own figures.
    refusals = [
        (row, envelope)
        for row in rows
        if row["role"] == "audit"
        for envelope in (row["tool_calls"] or [])
        if envelope.get("_kind") == COMPOSER_REPLY_STAGE_REFUSAL_KIND
    ]
    ((refusal_row, envelope),) = refusals
    assert refusal_row["writer_principal"] == "compose_loop"
    assert json.loads(refusal_row["content"]) == envelope
    assert set(envelope) == {
        "_kind",
        "schema",
        "reason",
        "ceiling",
        "estimated_tokens",
        "request_wire_bytes",
        "max_prompt_tokens",
        "max_request_bytes",
        "largest_component",
    }
    assert envelope["schema"] == "composer.reply-stage-refusal.v1"
    assert envelope["reason"] == "context_budget_exceeded"
    assert envelope["ceiling"] == "bytes"
    assert envelope["max_request_bytes"] == _REPLY_STAGE_BYTE_CEILING
    assert envelope["max_prompt_tokens"] == policy.max_prompt_tokens
    assert envelope["request_wire_bytes"] > _REPLY_STAGE_BYTE_CEILING
    assert envelope["estimated_tokens"] > 0
    assert envelope["largest_component"] == "system"

    # The visible message carries the fixed notice once, and no reply prose.
    assistant_rows = [row for row in rows if row["role"] == "assistant"]
    visible = assistant_rows[-1]["content"]
    assert visible.count(no_tool_policy._REVIEW_REPLY_CONTEXT_BUDGET_NOTICE) == 1
    assert no_tool_policy._REVIEW_REPLY_UNAVAILABLE_NOTICE not in visible
    assert "Interpretation review cards are ready" in visible
    assert _REPLY_PROSE not in visible

    # The staged work is intact: the pipeline is the session's state and its review card is pending.
    state = client.get(f"/api/sessions/{session_id}/state")
    assert state.status_code == 200, state.text
    assert "rate_node" in state.text
    pending = client.get(f"/api/sessions/{session_id}/interpretations", params={"status": "pending"})
    assert pending.status_code == 200, pending.text
    assert len(pending.json()) == 1

    # Reading the session back: the message view omits the audit row and keeps the notice.
    listed = client.get(f"/api/sessions/{session_id}/messages")
    assert listed.status_code == 200, listed.text
    assert COMPOSER_REPLY_STAGE_REFUSAL_KIND not in listed.text
    assert no_tool_policy._REVIEW_REPLY_CONTEXT_BUDGET_NOTICE in listed.text

    # The next turn reads the same rows into its history projections without the refusal row breaking any of them.
    next_composer = _make_composer_mock(response_text="Next turn")
    app.state.composer_service = next_composer
    next_response = _settled_composer_response(
        client,
        app,
        path=f"/api/sessions/{session_id}/messages",
        body=message_body("and now?", state_id=current_head_state_id_sync(client, session_id), operation_id=str(uuid.uuid4())),
    )
    assert next_response.status_code == 200, next_response.text
    next_kwargs = next_composer.compose.await_args.kwargs
    history = next_composer.compose.await_args.args[1]
    assert COMPOSER_REPLY_STAGE_REFUSAL_KIND not in json.dumps(history, default=str)
    assert isinstance(next_kwargs["context_budget_history"], ContextBudgetHistory)
    assert isinstance(next_kwargs["advisor_history"], AdvisorTranscriptHistory)
    assert COMPOSER_REPLY_STAGE_REFUSAL_KIND not in repr(next_kwargs["advisor_history"])
