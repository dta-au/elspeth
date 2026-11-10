"""The reply stage after staged review cards, under the context budget.

The reply stage makes one optional provider call once the turn's work is
done. These tests drive the real compose loop to that stage and pin three
behaviours: a context-budget refusal there leaves the finished turn standing
with the reply unavailable and no provider call; a reply stage that is out of
time prepares nothing and announces nothing; and the shed-history notice,
when the reply call is the first of the turn to shed history, is still the
latest progress when the provider is called.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args
from uuid import uuid4

import pytest
import structlog
from sqlalchemy import insert
from sqlalchemy.pool import StaticPool

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus, ComposerLLMRequestByteSplit
from elspeth.contracts.composer_progress import ComposerProgressEvent
from elspeth.web.composer import no_tool_policy
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.context_budget import ContextBudgetRefusal
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.composer.reply_stage_refusal import (
    COMPOSER_REPLY_STAGE_REFUSAL_KIND,
    ComposeLoopContextBudgetComponent,
    ReplyStageRefusal,
)
from elspeth.web.composer.service import ComposerAvailability, ComposerServiceImpl, _context_budget_component_bytes
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import chat_messages_table, sessions_table
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web.composer._helpers import _clean_advisor_checkpoint
from tests.unit.web.composer.test_compose_loop_interpretation_review_dispatch import (
    _build_composer,
    _fake_response_with_tool_call,
    _fake_text_response,
    _ScriptedLLM,
    _set_pipeline_with_pending_interpretation_args,
)
from tests.unit.web.sessions.session_test_authority import FencedSessionServiceHarness


@pytest.fixture(autouse=True)
def _advisor_end_gate_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """The advisor gates are not under test; they return CLEAN without a provider call."""
    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint, raising=True)


@pytest.fixture(autouse=True)
def _composer_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the tests independent of local API keys."""

    def _available(**kwargs: object) -> ComposerAvailability:
        return ComposerAvailability(available=True, model=str(kwargs["model"]), provider="anthropic")

    monkeypatch.setattr("elspeth.web.composer.service.compute_availability", _available)


@pytest.fixture
def sessions() -> SessionServiceImpl:
    engine = create_session_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    return FencedSessionServiceHarness(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test.sessions"))


class _CallableLLM:
    """A scripted LLM whose calls go through ``call`` while its recorded requests stay on ``scripted``."""

    def __init__(self, call: Any, scripted: _ScriptedLLM) -> None:
        self._call = call
        self.messages = scripted.messages
        self.tools = scripted.tools

    async def __call__(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **kwargs: Any) -> Any:
        return await self._call(messages, tools, **kwargs)


_REPLY_TOOLS = {"handoff_to_user", "restore_parked_graph"}
_REPLY_PROSE = "A fork suits independent personas; distinct answer columns preserve both results."


def _is_reply_stage(tools: list[dict[str, Any]]) -> bool:
    return {tool["function"]["name"] for tool in tools} == _REPLY_TOOLS


class _Turn:
    """One turn that stages a review card and reaches the reply stage, with its progress events and result."""

    def __init__(self, tmp_path: Path, sessions: SessionServiceImpl, monkeypatch: pytest.MonkeyPatch) -> None:
        self.composer: ComposerServiceImpl = _build_composer(tmp_path, sessions)
        self.sessions = sessions
        self.events: list[ComposerProgressEvent] = []
        self.results: list[ComposerResult] = []
        self.latest_event_at_reply_call: list[ComposerProgressEvent] = []
        self.llm = _ScriptedLLM(
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
        original_loop = self.composer._compose_loop

        async def _sink(event: ComposerProgressEvent) -> None:
            self.events.append(event)

        async def _loop(*args: Any, **kwargs: Any) -> ComposerResult:
            kwargs["progress"] = _sink
            completed = await original_loop(*args, **kwargs)
            self.results.append(completed)
            return completed

        monkeypatch.setattr(self.composer, "_compose_loop", _loop)

    async def _observed_llm(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **kwargs: Any) -> Any:
        if _is_reply_stage(tools):
            self.latest_event_at_reply_call.append(self.events[-1])
        return await self.llm(messages, tools, **kwargs)

    async def run(self) -> Any:
        session_id = uuid4()
        with self.sessions._engine.begin() as conn:
            conn.execute(
                insert(sessions_table).values(
                    id=str(session_id),
                    user_id="alice",
                    auth_provider_type="local",
                    title="Reply stage context budget",
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
        result = await self.composer._run_one_turn_for_test(
            llm=self._observed_llm,
            session_id=str(session_id),
            current_state_id=None,
            message="create a workflow that rates how cool pages are",
        )
        # Instrument guard: the turn reached the staged-review branch.
        assert [invocation.tool_name for invocation in result.tool_invocations] == ["set_pipeline", "request_interpretation_review"]
        return result

    def reply_stage_calls(self) -> int:
        return sum(1 for tools in self.llm.tools if _is_reply_stage(tools))

    def notices(self) -> list[ComposerProgressEvent]:
        return [event for event in self.events if event.reason == "context_reduced"]

    def refusal_records(self) -> list[tuple[str, dict[str, Any]]]:
        """Each stored reply-stage refusal as (row content, envelope), read back from the session's audit rows."""
        with self.sessions._engine.begin() as conn:
            rows = conn.execute(chat_messages_table.select().order_by(chat_messages_table.c.sequence_no)).mappings().all()
        return [
            (row["content"], envelope)
            for row in rows
            if row["role"] == "audit"
            for envelope in (row["tool_calls"] or [])
            if envelope.get("_kind") == COMPOSER_REPLY_STAGE_REFUSAL_KIND
        ]


@pytest.mark.asyncio
async def test_control_the_reply_stage_calls_the_provider_and_returns_its_reply(
    tmp_path: Path, sessions: SessionServiceImpl, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn = _Turn(tmp_path, sessions, monkeypatch)

    result = await turn.run()

    assert turn.reply_stage_calls() == 1
    assert _REPLY_PROSE in result.assistant_message
    assert "final reply is unavailable" not in result.assistant_message
    (completed,) = turn.results
    assert len(completed.llm_calls) == 3
    assert turn.notices() == []
    (latest,) = turn.latest_event_at_reply_call
    assert latest.headline == "I'm asking the model to prepare your reply."
    assert turn.refusal_records() == []


@pytest.mark.asyncio
async def test_a_context_budget_refusal_at_the_reply_stage_leaves_the_finished_turn_standing(
    tmp_path: Path, sessions: SessionServiceImpl, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn = _Turn(tmp_path, sessions, monkeypatch)
    composer = turn.composer
    decide = composer._shed_to_context_budget
    refused: list[bool] = []

    def _refuse_the_reply_request(**kwargs: Any) -> Any:
        if not _is_reply_stage(kwargs["tools"]):
            return decide(**kwargs)
        refused.append(True)
        raise composer._context_budget_refusal(
            ContextBudgetRefusal(ceiling="tokens", estimated_tokens=10**9, request_bytes=10**9),
            {"latest_tool_round": 10**9},
            kwargs["refusal_scope"],
        )

    monkeypatch.setattr(composer, "_shed_to_context_budget", _refuse_the_reply_request)

    result = await turn.run()

    assert refused == [True]
    # No provider call was made for the reply, and no call record claims one.
    assert turn.reply_stage_calls() == 0
    assert len(turn.llm.messages) == 2
    (completed,) = turn.results
    assert len(completed.llm_calls) == 2
    # The turn's work stands: the cards are staged and there is no reply.
    assert "Interpretation review cards are ready" in result.assistant_message
    assert result.raw_assistant_content == ""
    assert _REPLY_PROSE not in result.assistant_message
    # The user is told why, in one fixed trusted notice, and is not told to retry.
    assert result.assistant_message.count(no_tool_policy._REVIEW_REPLY_CONTEXT_BUDGET_NOTICE) == 1
    assert "retry" not in result.assistant_message.lower()
    assert no_tool_policy._REVIEW_REPLY_UNAVAILABLE_NOTICE not in result.assistant_message
    segments = no_tool_policy.visible_message_segments(content=result.assistant_message, raw_content=result.raw_assistant_content)
    assert no_tool_policy.TrustedSystemNoticeSegment(no_tool_policy._REVIEW_REPLY_CONTEXT_BUDGET_NOTICE) in segments
    # Nothing announced a model request that was never made.
    assert [event for event in turn.events if event.headline == "I'm asking the model to prepare your reply."] == []
    # The refusal is on the turn's audit trail: one audit row, sizes and closed-vocabulary names only.
    ((content, envelope),) = turn.refusal_records()
    policy = composer._context_budget_policy
    assert envelope == {
        "_kind": "composer_reply_stage_refusal",
        "schema": "composer.reply-stage-refusal.v1",
        "reason": "context_budget_exceeded",
        "ceiling": "tokens",
        "estimated_tokens": 10**9,
        "request_wire_bytes": 10**9,
        "max_prompt_tokens": policy.max_prompt_tokens,
        "max_request_bytes": policy.max_request_bytes,
        "largest_component": "latest_tool_round",
    }
    assert json.loads(content) == envelope


@pytest.mark.asyncio
async def test_control_a_provider_failure_at_the_reply_stage_keeps_its_call_record_and_its_notice(
    tmp_path: Path, sessions: SessionServiceImpl, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn = _Turn(tmp_path, sessions, monkeypatch)
    scripted = turn.llm

    async def _provider_times_out_on_the_reply(messages: list[dict[str, Any]], tools: list[dict[str, Any]], **kwargs: Any) -> Any:
        if _is_reply_stage(tools):
            scripted.tools.append(tools)
            raise TimeoutError("reply call timed out")
        return await scripted(messages, tools, **kwargs)

    monkeypatch.setattr(turn, "llm", _CallableLLM(_provider_times_out_on_the_reply, scripted))

    result = await turn.run()

    assert turn.reply_stage_calls() == 1
    # The existing notice and the existing failed call record; no context-budget refusal anywhere.
    assert result.assistant_message.count(no_tool_policy._REVIEW_REPLY_UNAVAILABLE_NOTICE) == 1
    assert no_tool_policy._REVIEW_REPLY_CONTEXT_BUDGET_NOTICE not in result.assistant_message
    (completed,) = turn.results
    assert [call.status for call in completed.llm_calls] == [
        ComposerLLMCallStatus.SUCCESS,
        ComposerLLMCallStatus.SUCCESS,
        ComposerLLMCallStatus.TIMEOUT,
    ]
    assert turn.refusal_records() == []


@pytest.mark.asyncio
async def test_a_reply_stage_that_is_out_of_time_prepares_no_request_and_announces_nothing(
    tmp_path: Path, sessions: SessionServiceImpl, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn = _Turn(tmp_path, sessions, monkeypatch)
    composer = turn.composer
    timeout_seconds = 4.0
    monkeypatch.setattr(composer, "_timeout_seconds", timeout_seconds)
    surface = composer._interpretation_surfacing.surface_pending_interpretation_reviews
    decide = composer._shed_to_context_budget
    reply_stage_preparations: list[bool] = []

    async def _surface_then_run_out_of_time(*args: Any, **kwargs: Any) -> Any:
        surfaced = await surface(*args, **kwargs)
        # The reply stage reads the remaining time right after surfacing.
        await asyncio.sleep(timeout_seconds)
        return surfaced

    def _shedding_history(**kwargs: Any) -> Any:
        messages, record, bytes_per_token = decide(**kwargs)
        if _is_reply_stage(kwargs["tools"]):
            reply_stage_preparations.append(True)
            # Were the request prepared, its decision would announce shed history.
            record = replace(record, shed_history_indices=(0,))
        return messages, record, bytes_per_token

    monkeypatch.setattr(composer._interpretation_surfacing, "surface_pending_interpretation_reviews", _surface_then_run_out_of_time)
    monkeypatch.setattr(composer, "_shed_to_context_budget", _shedding_history)

    try:
        result = await turn.run()
    except Exception as outcome:
        # Whatever the turn does once it is out of time, it is not a context-budget outcome.
        assert "context_budget" not in repr(outcome)
        result = None

    assert reply_stage_preparations == []
    assert turn.reply_stage_calls() == 0
    assert turn.notices() == []
    # Out of time is not a context-budget refusal: no refusal record, and the existing notice.
    assert turn.refusal_records() == []
    if result is not None:
        assert no_tool_policy._REVIEW_REPLY_UNAVAILABLE_NOTICE in result.assistant_message
        assert no_tool_policy._REVIEW_REPLY_CONTEXT_BUDGET_NOTICE not in result.assistant_message


@pytest.mark.asyncio
async def test_the_shed_notice_from_the_reply_call_is_the_latest_progress_when_the_provider_is_called(
    tmp_path: Path, sessions: SessionServiceImpl, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn = _Turn(tmp_path, sessions, monkeypatch)
    composer = turn.composer
    decide = composer._shed_to_context_budget

    def _reply_call_is_the_first_to_shed_history(**kwargs: Any) -> Any:
        messages, record, bytes_per_token = decide(**kwargs)
        if _is_reply_stage(kwargs["tools"]):
            record = replace(record, shed_history_indices=(0,))
        return messages, record, bytes_per_token

    monkeypatch.setattr(composer, "_shed_to_context_budget", _reply_call_is_the_first_to_shed_history)

    result = await turn.run()

    assert _REPLY_PROSE in result.assistant_message
    (notice,) = turn.notices()
    # The notice follows the reply-stage announcement, so nothing replaced it before the call.
    (latest,) = turn.latest_event_at_reply_call
    assert latest is notice
    position = turn.events.index(notice)
    assert turn.events[position - 1].headline == "I'm asking the model to prepare your reply."


# --- The refusal record's closed shape ----------------------------------------------------------


def _refusal(**overrides: Any) -> ReplyStageRefusal:
    values: dict[str, Any] = {
        "ceiling": "bytes",
        "estimated_tokens": 10,
        "request_wire_bytes": 30,
        "max_prompt_tokens": 5,
        "max_request_bytes": 20,
        "largest_component": "tools",
    }
    return ReplyStageRefusal(**{**values, **overrides})


def test_the_refusal_envelope_carries_sizes_and_closed_vocabulary_names_only() -> None:
    assert _refusal().envelope() == {
        "_kind": "composer_reply_stage_refusal",
        "schema": "composer.reply-stage-refusal.v1",
        "reason": "context_budget_exceeded",
        "ceiling": "bytes",
        "estimated_tokens": 10,
        "request_wire_bytes": 30,
        "max_prompt_tokens": 5,
        "max_request_bytes": 20,
        "largest_component": "tools",
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"ceiling": "words"},
        {"largest_component": "the user's secret text"},
        {"largest_component": "request"},  # a planner component, not a compose-loop one
        {"estimated_tokens": -1},
        {"request_wire_bytes": 1.5},
        {"max_prompt_tokens": True},
        {"max_request_bytes": "20"},
    ],
)
def test_a_refusal_outside_its_closed_shape_is_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="reply stage refusal"):
        _refusal(**overrides)


def test_the_refusal_component_vocabulary_is_the_compose_loops() -> None:
    split = ComposerLLMRequestByteSplit(message_bytes=(1, 1, 1, 1), tools_bytes=1, total_bytes=5)
    named = _context_budget_component_bytes(split, state_index=2, latest_start=None, sheddable_bytes=0)
    assert set(named) == set(get_args(ComposeLoopContextBudgetComponent))
