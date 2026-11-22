"""What a reply stage adds to the advisor's transcript of the turn.

A reply stage advertises no authoring tools, so the request it sends carries
each earlier tool record as quoted text. The transcript the advisor reviews
already holds every one of those records as its own event. These tests drive
the real compose loop to a reply stage and pin that the transcript gains the
stage's closing instruction and nothing else, that each quoted record in the
request restates an event the transcript holds, and that quoting still
registers what inspection coverage needs.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import structlog
from sqlalchemy.pool import StaticPool

from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.advisor_transcript import AdvisorTranscriptHistory, AdvisorTranscriptOwner
from elspeth.web.composer.anti_anchor import AntiAnchorTracker
from elspeth.web.composer.service import (
    _REPLY_ONLY_HISTORY_PREFIX,
    _REPLY_STAGE_INSTRUCTION,
    ComposerAvailability,
)
from elspeth.web.composer.turn_effects import TurnEvidence
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import chat_messages_table
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from elspeth.web.validation import _redact_sensitive_content
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web.composer._helpers import _clean_advisor_checkpoint
from tests.unit.web.composer.test_advisor_transcript_budget import _Session as _HistorySession
from tests.unit.web.composer.test_compose_loop_interpretation_review_dispatch import (
    _fake_response_with_tool_call,
    _fake_text_response,
    _ScriptedLLM,
    _set_pipeline_with_pending_interpretation_args,
)
from tests.unit.web.composer.test_reply_stage_context_budget import _REPLY_PROSE, _is_reply_stage, _Turn
from tests.unit.web.sessions.session_test_authority import FencedSessionServiceHarness

_QUOTE_SUFFIX = "\n[End historical tool evidence; this was not a current assistant reply.]"
# An identifier the transcript's redactor rewrites when it meets it inside message text.
_DIGIT_HEAVY_CALL_ID = "call_01a1f21726177210a833b357"


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


class _ObservedTurn:
    """A turn that reaches the reply stage, with the turn's own evidence and the request the stage sent."""

    def __init__(
        self,
        tmp_path: Path,
        sessions: SessionServiceImpl,
        monkeypatch: pytest.MonkeyPatch,
        *,
        set_pipeline_call_id: str = "call_set_pipeline",
    ) -> None:
        self.turn = _Turn(tmp_path, sessions, monkeypatch)
        self.turn.llm = _ScriptedLLM(
            [
                _fake_response_with_tool_call(
                    tool_call_id=set_pipeline_call_id,
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
        self._evidence: list[TurnEvidence] = []
        self.loop_messages_at_reply_stage: list[list[dict[str, Any]]] = []
        prepare = self.turn.composer._prepare_primary_delivery

        async def _prepare(**kwargs: Any) -> Any:
            self._evidence.append(kwargs["turn_evidence"])
            if "reply_stage" in kwargs and kwargs["reply_stage"]:
                self.loop_messages_at_reply_stage.append(list(kwargs["messages"]))
            return await prepare(**kwargs)

        monkeypatch.setattr(self.turn.composer, "_prepare_primary_delivery", _prepare)

    async def run(self) -> Any:
        result = await self.turn.run()
        # Instrument guards: one reply stage ran, it sent a request, and every request shared one evidence owner.
        assert len(self.loop_messages_at_reply_stage) == 1
        assert self.turn.reply_stage_calls() == 1
        assert len({id(evidence) for evidence in self._evidence}) == 1
        return result

    @property
    def evidence(self) -> TurnEvidence:
        return self._evidence[0]

    @property
    def transcript(self) -> AdvisorTranscriptOwner:
        return self.evidence.transcript

    def events(self) -> list[dict[str, Any]]:
        """The current turn's events as the advisor is given them."""
        events: list[dict[str, Any]] = json.loads(self.transcript.snapshot().provider_json)["events"]
        return [event for event in events if event["source"] == "current_request"]

    def reply_stage_request(self) -> list[dict[str, Any]]:
        """The messages the reply stage sent to the provider."""
        (request,) = [
            messages for messages, tools in zip(self.turn.llm.messages, self.turn.llm.tools, strict=True) if _is_reply_stage(tools)
        ]
        return request

    def quoted_records(self) -> list[dict[str, Any]]:
        """Each tool record the reply-stage request carried as quoted text, parsed back to its wire form."""
        records: list[dict[str, Any]] = []
        for message in self.reply_stage_request():
            content = message["content"]
            if type(content) is str and content.startswith(_REPLY_ONLY_HISTORY_PREFIX):
                assert content.endswith(_QUOTE_SUFFIX)
                records.append(json.loads(content[len(_REPLY_ONLY_HISTORY_PREFIX) : -len(_QUOTE_SUFFIX)]))
        return records


def _is_quoted_copy(event: dict[str, Any]) -> bool:
    content = event["content"]
    return type(content) is str and content.startswith(_REPLY_ONLY_HISTORY_PREFIX)


def _as_reviewed(text: str | None) -> str | None:
    """Text as a transcript event shows it: the advisor is given each event's text after redaction.

    A tool result can carry a generated identifier the redactor rewrites, so
    a comparison against an event's text applies the same redaction first.
    """
    return None if text is None else _redact_sensitive_content(text)


def _effective_arguments(call: dict[str, Any]) -> str:
    """The arguments a call event records as executed: the finalised ones when the backend changed them."""
    arguments: str = call["effective_arguments"] if "effective_arguments" in call else call["received_arguments"]
    return arguments


@pytest.mark.asyncio
async def test_a_reply_stage_adds_no_second_copy_of_a_tool_result(
    tmp_path: Path, sessions: SessionServiceImpl, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = _ObservedTurn(tmp_path, sessions, monkeypatch)

    await observed.run()

    # Instrument guard: the request the stage sent did quote the turn's tool records.
    assert len(observed.quoted_records()) == 4
    events = observed.events()
    assert [event for event in events if _is_quoted_copy(event)] == []
    # Each tool result is in the transcript once, as its own event, and its text is in no other event.
    results = [event for event in events if event["role"] == "tool"]
    assert [event["tool_call_id"] for event in results] == ["call_set_pipeline", "call_review"]
    # Both results report the same validation error; that text is in the transcript once per result.
    assert [event["event_id"] for event in events if "no_sinks_configured" in (event["content"] or "")] == [
        event["event_id"] for event in results
    ]


@pytest.mark.asyncio
async def test_no_transcript_event_carries_a_redacted_tool_call_id(
    tmp_path: Path, sessions: SessionServiceImpl, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A digit-heavy call identifier inside quoted text is rewritten by the redactor, which breaks the pairing."""
    observed = _ObservedTurn(tmp_path, sessions, monkeypatch, set_pipeline_call_id=_DIGIT_HEAVY_CALL_ID)

    await observed.run()

    provider_json = observed.transcript.snapshot().provider_json
    # Instrument guards: the identifier is in the transcript, on the call and on its result.
    assert provider_json.count(_DIGIT_HEAVY_CALL_ID) == 2
    assert {record["tool_call_id"] for record in observed.quoted_records() if record["role"] == "tool"} == {
        _DIGIT_HEAVY_CALL_ID,
        "call_review",
    }
    # Every appearance of the identifier is whole.
    assert provider_json.count(_DIGIT_HEAVY_CALL_ID[:10]) == provider_json.count(_DIGIT_HEAVY_CALL_ID)


@pytest.mark.asyncio
async def test_every_quoted_record_repeats_a_native_event(
    tmp_path: Path, sessions: SessionServiceImpl, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each record the reply-stage request quotes is already a transcript event of its own, field for field.

    A quoted call carries the arguments the backend finalised; the call's own
    event carries those and the arguments the provider sent.
    """
    observed = _ObservedTurn(tmp_path, sessions, monkeypatch)

    await observed.run()

    quoted = observed.quoted_records()
    assert [record["role"] for record in quoted] == ["assistant", "tool", "assistant", "tool"]
    native = [event for event in observed.events() if not _is_quoted_copy(event)]
    for record in quoted:
        if record["role"] == "tool":
            matches = [
                event
                for event in native
                if event["role"] == "tool"
                and event["tool_call_id"] == record["tool_call_id"]
                and event["content"] == _as_reviewed(record["content"])
            ]
        else:
            calls = [(call["id"], call["function"]["name"], _as_reviewed(call["function"]["arguments"])) for call in record["tool_calls"]]
            matches = [
                event
                for event in native
                if event["role"] == "assistant"
                and event["content"] == _as_reviewed(record["content"])
                and [(call["tool_call_id"], call["tool_name"], _effective_arguments(call)) for call in event["tool_calls"]] == calls
            ]
        assert len(matches) == 1, record
    # A quoted record has no field beyond the ones compared above.
    assert all(set(record) <= {"role", "content", "tool_calls", "tool_call_id"} for record in quoted)


@pytest.mark.asyncio
async def test_the_reply_stage_instruction_is_a_transcript_event(
    tmp_path: Path, sessions: SessionServiceImpl, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = _ObservedTurn(tmp_path, sessions, monkeypatch)

    await observed.run()

    instructions = [event for event in observed.events() if event["content"] == _REPLY_STAGE_INSTRUCTION]
    assert [(event["role"], event["authorship"]) for event in instructions] == [("system", "backend_control")]
    # The request the stage sent closed with the same instruction.
    assert observed.reply_stage_request()[-1]["content"] == _REPLY_STAGE_INSTRUCTION


@pytest.mark.asyncio
async def test_the_transcript_holds_the_loop_messages_and_the_instruction_in_order(
    tmp_path: Path, sessions: SessionServiceImpl, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The turn's events are the loop's own messages after the excluded setup, then the instruction, and no more."""
    observed = _ObservedTurn(tmp_path, sessions, monkeypatch)

    result = await observed.run()

    (loop_messages,) = observed.loop_messages_at_reply_stage
    # The loop's messages after the setup: the retained-history note, then two tool calls with their results.
    current = loop_messages[-5:]
    assert [message["role"] for message in current] == ["system", "assistant", "tool", "assistant", "tool"]
    assert [message["role"] for message in loop_messages[:-5]] == ["system", "user", "user", "user"]
    expected = [(message["role"], _as_reviewed(message["content"])) for message in current]
    expected.append(("system", _REPLY_STAGE_INSTRUCTION))
    assert [(event["role"], event["content"]) for event in observed.events()] == expected
    assert _REPLY_PROSE in result.assistant_message


@pytest.mark.asyncio
async def test_quoted_inspection_coverage_survives_a_reply_stage(
    tmp_path: Path, sessions: SessionServiceImpl, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quoting a tool result for the reply stage still registers it, once per result, with the text that was sent."""
    registered: list[dict[str, str]] = []
    register = TurnEvidence.register_quoted_inspection

    def _register(self: TurnEvidence, **kwargs: str) -> None:
        registered.append(kwargs)
        register(self, **kwargs)

    monkeypatch.setattr(TurnEvidence, "register_quoted_inspection", _register, raising=True)
    observed = _ObservedTurn(tmp_path, sessions, monkeypatch)

    await observed.run()

    assert [entry["tool_call_id"] for entry in registered] == ["call_set_pipeline", "call_review"]
    sent = [(message["role"], message["content"]) for message in observed.reply_stage_request()]
    results = {event["tool_call_id"]: event["content"] for event in observed.events() if event["role"] == "tool"}
    for entry in registered:
        assert (entry["quoted_role"], entry["quoted_content"]) in sent
        assert _as_reviewed(entry["original_content"]) == results[entry["tool_call_id"]]


@pytest.mark.asyncio
async def test_with_earlier_turns_the_advisor_is_sent_each_tool_record_of_this_turn_once(
    tmp_path: Path, sessions: SessionServiceImpl, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The age rule shapes earlier turns only; this turn's tool records go once, and the one copy is never shed.

    The turn carries an authenticated history of four earlier turns, each with
    a tool round. The transcript the advisor is sent is the one the age rule
    shapes (``by_age``).
    """
    observed = _ObservedTurn(tmp_path, sessions, monkeypatch)
    loop = observed.turn.composer._compose_loop

    async def _with_history(message: str, *args: Any, **kwargs: Any) -> Any:
        earlier = _HistorySession()
        earlier.sid = UUID(kwargs["session_id"])
        for n in range(4):
            earlier.turn(n)
        current = earlier.add("user", message, writer="route_user_message")
        history = AdvisorTranscriptHistory.from_records(
            session_id=kwargs["session_id"],
            actor_user_id="alice",
            current_request_id=str(current.id),
            request=message,
            records=tuple(earlier.rows),
        )
        kwargs["turn_evidence"] = TurnEvidence(
            session_id=kwargs["session_id"],
            # The compose lease the test adapter acquired for this turn.
            session_operation_context=kwargs["session_operation_context"],
            request_id=str(current.id),
            request=message,
            initial_state_id=kwargs["initial_current_state_id"],
            initial_state=args[1],
            actor_user_id="alice",
            advisor_history=history,
        )
        return await loop(message, *args, **kwargs)

    monkeypatch.setattr(observed.turn.composer, "_compose_loop", _with_history)

    await observed.run()

    snapshot = observed.transcript.snapshot()
    sent = snapshot.by_age()
    # Instrument guards: the history was there and the age rule shaped it.
    assert sent.history_by_age is not None
    assert sent.history_by_age.earlier_turns == 4
    assert len(observed.quoted_records()) == 4
    events = json.loads(sent.provider_json)["events"]
    assert [event for event in events if event["source"] == "current_request" and _is_quoted_copy(event)] == []
    # Earlier turns' tool traffic is left out by age; this turn's results go once each.
    assert [event["tool_call_id"] for event in events if event["role"] == "tool"] == ["call_set_pipeline", "call_review"]
    # None of this turn's events is sheddable, so no reduction can drop the one copy.
    current_indices = [
        index for index, event in enumerate(json.loads(snapshot.provider_json)["events"]) if event["source"] == "current_request"
    ]
    assert current_indices
    assert set(current_indices).isdisjoint(snapshot.sheddable_event_indices())


_FORCED_HINT = "[ELSPETH-SYSTEM-HINT] forced for this test"


@pytest.mark.asyncio
async def test_a_message_the_loop_adds_after_the_tool_round_is_a_transcript_event_before_the_instruction(
    tmp_path: Path, sessions: SessionServiceImpl, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The anti-anchor hint is appended after the round's messages were observed and can reach the reply stage.

    The tracker is forced to fire after the second (terminal) tool round, so
    the hint is in the loop's messages only from then on. The reply stage
    gives the transcript owner the loop's messages, so the hint is an event,
    and it comes before the stage's closing instruction.
    """
    fired: list[int] = []

    def _should_fire(self: AntiAnchorTracker) -> bool:
        fired.append(len(fired))
        return len(fired) == 2

    monkeypatch.setattr(AntiAnchorTracker, "should_fire", _should_fire, raising=True)
    monkeypatch.setattr(AntiAnchorTracker, "build_hint", lambda self: _FORCED_HINT, raising=True)
    observed = _ObservedTurn(tmp_path, sessions, monkeypatch)

    await observed.run()

    # Instrument guards: the tracker was asked after each of the two rounds, and the hint was in the loop's messages.
    assert fired == [0, 1]
    (loop_messages,) = observed.loop_messages_at_reply_stage
    assert loop_messages[-1] == {"role": "user", "content": _FORCED_HINT}
    contents = [event["content"] for event in observed.events()]
    assert contents.count(_FORCED_HINT) == 1
    assert contents.index(_FORCED_HINT) == contents.index(_REPLY_STAGE_INSTRUCTION) - 1


@pytest.mark.asyncio
async def test_control_the_stored_rows_of_a_reply_stage_turn_hold_no_quoted_record(
    tmp_path: Path, sessions: SessionServiceImpl, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later turn reads this turn through its stored rows; what the transcript owner is shown changes none of them.

    The rows are each call, its result and that round's audit row. The text a
    reply stage quotes for its request is in none of them.
    """
    observed = _ObservedTurn(tmp_path, sessions, monkeypatch)

    await observed.run()

    with sessions._engine.begin() as conn:
        rows = conn.execute(chat_messages_table.select().order_by(chat_messages_table.c.sequence_no)).mappings().all()
    assert [(row["role"], row["writer_principal"]) for row in rows] == [
        ("assistant", "compose_loop"),
        ("tool", "compose_loop"),
        ("audit", "compose_loop"),
        ("assistant", "compose_loop"),
        ("tool", "compose_loop"),
        ("audit", "compose_loop"),
    ]
    stored = [json.dumps([row["content"], row["raw_content"], row["tool_calls"]], default=str) for row in rows]
    # Instrument guard: the stored tool rows do hold the tool results.
    assert sum("no_sinks_configured" in text for text in stored) >= 2
    assert [text for text in stored if "Historical tool protocol record" in text] == []
    assert [row["tool_call_id"] for row in rows if row["role"] == "tool"] == ["call_set_pipeline", "call_review"]


_PLANNER_SHAPED_PROVIDER_JSON = (
    '{"events": [{"event_id": "current:1", "source": "current_request", "role": "assistant", "authorship": "composer", '
    '"content": null, "tool_calls": [{"tool_call_id": "call_a", "tool_name": "get_pipeline_state", "received_arguments": "{}", '
    '"argument_provenance": "provider_received", "effective_arguments_ref": "received_arguments"}]}, '
    '{"event_id": "current:2", "source": "current_request", "role": "tool", "authorship": "tool", '
    '"content": "{\\"success\\": true, \\"data\\": {\\"nodes\\": 3}}", "tool_call_id": "call_a", '
    '"primary_delivery": "not_yet_confirmed_by_a_successful_prepared_request"}, '
    '{"event_id": "provider:planner-call-1", "source": "current_request", "role": "assistant", "authorship": "composer", '
    '"content": "Three nodes are wired.", "tool_calls": [], "provider_call_id": "planner-call-1", "admission": "admitted"}], '
    '"availability_gaps": ["authenticated_persisted_history_not_supplied"], '
    '"current_request_ref": "complete_original_user_request_section", '
    '"scope": "same_session_visible_events_only; native_thinking_and_static_internal_prompts_excluded"}'
)


def test_control_a_ledger_used_as_the_planner_uses_it_snapshots_to_the_same_values() -> None:
    """The planner shares this ledger and reads ``snapshot()``; these values were measured before any reply-stage change.

    The ledger is driven the way the planner drives it: setup excluded, the
    loop's messages observed, one provider reply recorded. The four values of
    the snapshot, and of the snapshot with its first event omitted, are fixed.
    """
    setup = [{"role": "system", "content": "STATIC SETUP"}, {"role": "user", "content": "rate the pages"}]
    owner = AdvisorTranscriptOwner(request="rate the pages")
    owner.begin_messages(setup)
    call = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call_a", "type": "function", "function": {"name": "get_pipeline_state", "arguments": "{}"}}],
    }
    result = {"role": "tool", "tool_call_id": "call_a", "content": json.dumps({"success": True, "data": {"nodes": 3}})}
    owner.observe_messages([*setup, call, result])
    owner.record_provider_reply(provider_call_id="planner-call-1", message={"role": "assistant", "content": "Three nodes are wired."})

    snapshot = owner.snapshot()

    assert snapshot.request == "rate the pages"
    assert snapshot.binding == "8fbdc9df1871e2085e4da3e6d5bef5db891a7c4be0ad4abb5f3cb34c76ea787d"
    assert snapshot.semantic_binding == "c7fb9c043d0f40005a7edf2e4eb616da419df68e7eb5411461e4580a35a3dbb1"
    assert snapshot.provider_json == _PLANNER_SHAPED_PROVIDER_JSON
    # Every event of this ledger belongs to the turn in progress (it has no
    # earlier turn), so none can be shed and none is shaped by age.
    assert snapshot.sheddable_event_indices() == ()
    assert snapshot.earlier_turn_count() == 0
    assert snapshot.without_events(frozenset()) is snapshot
    assert snapshot.by_age() is snapshot
    with pytest.raises(ValueError, match="only sheddable transcript events can be omitted"):
        snapshot.without_events(frozenset({0}))


def test_control_every_ledger_call_the_planner_makes_gives_the_same_values() -> None:
    """The planner's other calls on the shared ledger and its snapshot, pinned to values measured on the unchanged tree.

    On the ledger: ``begin_messages``, ``observe_messages``, ``record_provider_reply`` as received and then
    ``admit_provider_reply``. On the snapshot: ``with_request_reference``, ``untrusted_content_parts``,
    ``event_bytes`` and ``event_wire_bytes``.
    """
    setup = [{"role": "system", "content": "STATIC SETUP"}, {"role": "user", "content": "rate the pages"}]
    owner = AdvisorTranscriptOwner(request="rate the pages")
    owner.begin_messages(setup)
    call = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call_a", "type": "function", "function": {"name": "get_pipeline_state", "arguments": "{}"}}],
    }
    result = {"role": "tool", "tool_call_id": "call_a", "content": json.dumps({"success": True, "data": {"nodes": 3}})}
    owner.observe_messages([*setup, call, result])
    owner.record_provider_reply(
        provider_call_id="planner-call-1",
        message={"role": "assistant", "content": "Three nodes are wired."},
        admission="received_unadmitted",
    )
    assert owner.snapshot().binding == "e9ba967e6a16cbbc69197b48b0f36041e89bdddbcfb9a901d09a24f4594f0dad"

    owner.admit_provider_reply("planner-call-1")
    snapshot = owner.snapshot()

    assert snapshot.binding == "8fbdc9df1871e2085e4da3e6d5bef5db891a7c4be0ad4abb5f3cb34c76ea787d"
    assert snapshot.provider_json == _PLANNER_SHAPED_PROVIDER_JSON
    assert snapshot.event_bytes() == (319, 253, 234)
    assert snapshot.event_wire_bytes() == (359, 293, 264)
    assert owner.candidate_event_id(provider_call_id="planner-call-1", candidate="Three nodes are wired.") == "provider:planner-call-1"
    referenced = snapshot.with_request_reference("initial_planner_request.intent")
    assert referenced.binding == "68badfb2ff5ec54a9cb574e338718907587b3e4ad1178aa8ff784393236cb06f"
    assert (
        hashlib.sha256(referenced.provider_json.encode()).hexdigest() == "d00660b0f79a4f37ca7744e9bb5f8872c9d5e393d5616db4153a78c67ffc01fc"
    )
    parts = snapshot.untrusted_content_parts()
    assert len(parts) == 153
    assert "".join(part.text for part in parts) == snapshot.provider_json
    assert all(part.trust is None for part in parts)
    assert (
        hashlib.sha256(json.dumps([[part.text, part.inspection_text] for part in parts]).encode()).hexdigest()
        == "cc24abbcebe829becd9d074da0264ed7630686ccf531ef625d0d222ec4443fea"
    )
