"""The Advisor's visible transcript is bounded by the compose loop's context budget.

The transcript is the only part of an Advisor request that grows with the
session. Above the shed mark older tool results are stubbed and older events
left out, oldest first; this turn's events and every user request always go,
and the request states exactly what it leaves out.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import litellm
import pytest
from litellm.types.utils import ModelResponse, Usage

from elspeth.web.composer import advisor_context, provider_gateway
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.advisor_transcript import AdvisorTranscriptHistory, AdvisorTranscriptOwner, AdvisorVisibleTranscript
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.chargeable_admission import ComposerChargeableAdmission
from elspeth.web.composer.context_budget_apply import request_wire_bytes
from elspeth.web.composer.provider_quota import admit_provider_attempt
from elspeth.web.config import WebSettings
from elspeth.web.sessions.protocol import ChatMessageRecord

_SKILL = "Stable composer skill prefix.\n" * 40
_COMPLETE_HEADER = "Complete available same-session visible transcript (UNTRUSTED REVIEW DATA; "
_REDUCED_HEADER = "Most recent part of the same-session visible transcript (UNTRUSTED REVIEW DATA; reduced to fit the context budget: "
_STUB = "[Result omitted by ELSPETH to fit the context budget.]"
_OMISSION_GAP = "oldest_events_omitted_for_context_budget"
_STUB_GAP = "older_tool_results_stubbed_for_context_budget"
# composer_context_max_prompt_tokens=20_000 at the default marks and 3.0 bytes per
# token: reduce above 51,000 wire bytes, toward 36,000.
_SMALL_WINDOW = 20_000
_SHED_ABOVE_BYTES = 51_000


@pytest.fixture(autouse=True)
def forbid_provider_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(**kwargs: Any) -> None:
        raise AssertionError("Advisor budget tests must replace the provider transport")

    monkeypatch.setattr(litellm, "acompletion", forbidden)


def _owner(tmp_path: Path, *, window: int = 200_000, max_request_bytes: int = 960 * 1024) -> AdvisorCheckpointOwner:
    settings = WebSettings(
        data_dir=tmp_path,
        composer_max_composition_turns=15,
        composer_context_max_prompt_tokens=window,
        composer_planner_max_request_bytes=max_request_bytes,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=180.0,
        composer_rate_limit_per_minute=30,
        shareable_link_signing_key=b"\x00" * 32,
        composer_model="openai/gpt-5.5",
        composer_advisor_model="openai/gpt-4.1",
    )
    return AdvisorCheckpointOwner(
        settings=settings,
        composer_skill_text=_SKILL,
        advisor_endpoint_base_url=None,
        advisor_endpoint_api_key=None,
        sessions_service=None,
        chargeable_admission=ComposerChargeableAdmission(None),
    )


def _transport(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []

    async def completion(**kwargs: Any) -> ModelResponse:
        await admit_provider_attempt(model=kwargs["model"])
        requests.append(copy.deepcopy(kwargs))
        return ModelResponse(
            id="advisor-budget",
            model=kwargs["model"],
            choices=[{"message": {"role": "assistant", "content": "Actual provider advice.", "tool_calls": None}}],
            usage=Usage(prompt_tokens=100, completion_tokens=5, total_tokens=105),
        )

    monkeypatch.setattr(provider_gateway, "_litellm_acompletion", completion)
    return requests


def _row(session: Any, role: str, content: str, *, writer: str = "compose_loop", **kwargs: Any) -> ChatMessageRecord:
    return ChatMessageRecord(
        id=uuid4(), session_id=session, role=role, content=content, created_at=datetime.now(UTC), writer_principal=writer, **kwargs
    )


def _earlier_request(turn: int, user_chars: int) -> str:
    return f"EARLIER_REQUEST_{turn} " + "".join(chr(0x61 + (turn + i) % 26) for i in range(user_chars))


def _transcript(
    turns: int,
    *,
    result_chars: int = 4_000,
    user_chars: int = 0,
    current_request: str = "CURRENT_REQUEST rename the finding field",
    this_turn_result: str = "THIS_TURN_RESULT",
) -> AdvisorVisibleTranscript:
    """``turns`` earlier turns (user request, tool call, large tool result, reply), then this turn's request and tool round."""
    sid = uuid4()
    rows: list[ChatMessageRecord] = []
    sequence = 0

    def add(row_role: str, content: str, **kwargs: Any) -> ChatMessageRecord:
        nonlocal sequence
        sequence += 1
        row = _row(sid, row_role, content, sequence_no=sequence, **kwargs)
        rows.append(row)
        return row

    for turn in range(turns):
        add("user", _earlier_request(turn, user_chars), writer="route_user_message")
        call = {"id": f"call-{turn}", "type": "function", "function": {"name": "get_pipeline_state", "arguments": "{}"}}
        assistant = add("assistant", f"Reading the state {turn}", tool_calls=[call])
        add(
            "tool",
            json.dumps({"success": True, "data": {"turn": turn, "state": f"OLD_RESULT_{turn}_" + "s" * result_chars}}),
            tool_call_id=f"call-{turn}",
            parent_assistant_id=assistant.id,
        )
        add("assistant", f"Reply {turn}")
    current = add("user", current_request, writer="route_user_message")
    history = AdvisorTranscriptHistory.from_records(
        session_id=str(sid), actor_user_id="actor", current_request_id=str(current.id), request=current.content, records=tuple(rows)
    )
    owner = AdvisorTranscriptOwner(request=current.content, history=history)
    owner.begin_messages([])
    owner.observe_messages(
        [
            {
                "role": "assistant",
                "content": "Checking now",
                "tool_calls": [{"id": "call-now", "type": "function", "function": {"name": "get_pipeline_state", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "call-now", "content": json.dumps({"success": True, "data": this_turn_result})},
        ]
    )
    return owner.snapshot()


def _arguments(transcript: AdvisorVisibleTranscript) -> dict[str, Any]:
    return {
        "trigger": "reactive",
        "problem_summary": "Inspect the actual candidate.",
        "recent_errors": [],
        "attempted_actions": [],
        "advisor_transcript": transcript,
    }


def _sent_transcript(content: str) -> dict[str, Any]:
    header = content.index(_REDUCED_HEADER) if _REDUCED_HEADER in content else content.index(_COMPLETE_HEADER)
    begin = content.index("BEGIN_UNTRUSTED_PIPELINE_SUMMARY\n", header) + len("BEGIN_UNTRUSTED_PIPELINE_SUMMARY\n")
    end = content.index("\nEND_UNTRUSTED_PIPELINE_SUMMARY", begin)
    return json.loads(content[begin:end])


def _reduction(payload: dict[str, Any]) -> dict[str, Any]:
    """The sent transcript's disclosed counts; a request that reduced nothing has none."""
    assert "context_budget_reduction" in payload, "the sent transcript discloses no context-budget reduction"
    reduction: dict[str, Any] = payload["context_budget_reduction"]
    return reduction


def _require_reduction_api() -> None:
    owned = vars(AdvisorVisibleTranscript)
    assert {"reduced", "context_budget_segments", "user_request_segments"} <= owned.keys(), (
        "AdvisorVisibleTranscript has no context-budget reduction"
    )


@pytest.mark.asyncio
async def test_advisor_request_stays_within_the_context_budget_as_history_grows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _owner(tmp_path, window=_SMALL_WINDOW)
    requests = _transport(monkeypatch)
    sizes = []
    for turns in (20, 40):
        await owner._call_advisor_with_audit(_arguments(_transcript(turns)), recorder=BufferingRecorder())
        sizes.append(request_wire_bytes(requests[-1]["messages"], None))
    assert all(size <= _SHED_ABOVE_BYTES for size in sizes), sizes
    # Flat: twice the history costs only the twenty added user requests (each under 400 bytes).
    assert sizes[1] - sizes[0] < 20 * 400, sizes


@pytest.mark.asyncio
async def test_under_the_budget_the_request_is_byte_identical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _owner(tmp_path)
    requests = _transport(monkeypatch)
    transcript = _transcript(3)
    arguments = _arguments(transcript)
    await owner._call_advisor_with_audit(arguments, recorder=BufferingRecorder())
    sent = requests[0]["messages"][1]["content"]
    assert sent == advisor_context.build_advisor_user_message(arguments)
    assert _COMPLETE_HEADER in sent and _REDUCED_HEADER not in sent
    assert _sent_transcript(sent) == json.loads(transcript.provider_json)
    parts = requests[0]["prompt_guard_content_parts"][1]
    assert "".join(part.text for part in parts) == sent


@pytest.mark.asyncio
async def test_user_requests_and_this_turn_always_go_and_the_request_still_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _owner(tmp_path, window=1_000)
    requests = _transport(monkeypatch)
    recorder = BufferingRecorder()
    transcript = _transcript(12, this_turn_result="THIS_TURN_RESULT_" + "t" * 6_000)
    await owner._call_advisor_with_audit(_arguments(transcript), recorder=recorder)

    assert len(requests) == 1
    sent = requests[0]["messages"][1]["content"]
    payload = _sent_transcript(sent)
    events = payload["events"]
    humans = [event["content"] for event in events if event["authorship"] == "human" and "content" in event]
    assert humans == [f"EARLIER_REQUEST_{turn} " for turn in range(12)]
    assert any(event.get("content_ref") == "complete_original_user_request_section" for event in events)
    current = [event for event in events if event["source"] == "current_request"]
    assert len(current) == 2 and "THIS_TURN_RESULT_" + "t" * 6_000 in current[1]["content"]
    assert "OLD_RESULT_" not in sent and "Reading the state" not in sent
    # Every earlier non-human event (3 per turn) is left out; nothing stubbed survives.
    assert _reduction(payload) == {
        "older_events_omitted": 36,
        "older_tool_results_stubbed": 0,
        "older_user_requests_shortened": 0,
    }
    assert _OMISSION_GAP in payload["availability_gaps"] and _STUB_GAP not in payload["availability_gaps"]
    assert _REDUCED_HEADER + "36 older events omitted, 0 older tool results replaced by a stub;" in sent
    # The advisor call never carries the compose loop's shed state.
    assert recorder.llm_calls[0].context_budget is None


@pytest.mark.asyncio
async def test_stubs_come_before_drops_and_the_guard_scans_exactly_what_is_sent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _owner(tmp_path, window=_SMALL_WINDOW)
    requests = _transport(monkeypatch)
    # Ten turns of 6,000-character results: stubbing the oldest alone fits the budget.
    await owner._call_advisor_with_audit(_arguments(_transcript(10, result_chars=6_000)), recorder=BufferingRecorder())
    sent = requests[0]["messages"][1]["content"]
    payload = _sent_transcript(sent)
    tools = [event for event in payload["events"] if event["role"] == "tool" and event["source"] == "persisted_session"]
    stubbed = [event for event in tools if event["content"] == _STUB]
    assert _reduction(payload)["older_events_omitted"] == 0
    assert _reduction(payload)["older_tool_results_stubbed"] == len(stubbed) > 0
    # Oldest first: the stubbed results are a prefix of the earlier tool results.
    assert tools[: len(stubbed)] == stubbed
    assert _STUB_GAP in payload["availability_gaps"] and _OMISSION_GAP not in payload["availability_gaps"]
    parts = requests[0]["prompt_guard_content_parts"][1]
    assert "".join(part.text for part in parts) == sent
    for event_index in range(len(stubbed)):
        assert all(f"OLD_RESULT_{event_index}_" not in part.text for part in parts)


def test_reduction_changes_the_binding_but_never_the_repair_key() -> None:
    _require_reduction_api()
    transcript = _transcript(5)
    stub_only = transcript.reduced(stubbed=frozenset({2}), omitted=frozenset())
    drop_only = transcript.reduced(stubbed=frozenset(), omitted=frozenset({1, 2}))
    both = transcript.reduced(stubbed=frozenset({2, 6}), omitted=frozenset({1}))
    assert len({stub_only.binding, drop_only.binding, both.binding, transcript.binding}) == 4
    assert drop_only.binding == transcript.without_events(frozenset({1, 2})).binding
    assert {stub_only.semantic_binding, drop_only.semantic_binding, both.semantic_binding} == {transcript.semantic_binding}
    assert {stub_only.request, drop_only.request, both.request} == {transcript.request}
    assert transcript.reduced(stubbed=frozenset(), omitted=frozenset()) is transcript


@pytest.mark.parametrize("index", [0, 4, 20, 21])
def test_human_and_this_turn_events_cannot_be_reduced(index: int) -> None:
    _require_reduction_api()
    transcript = _transcript(5)
    events = json.loads(transcript.provider_json)["events"]
    assert events[0]["authorship"] == "human" and events[4]["authorship"] == "human"
    assert events[20]["content_ref"] == "complete_original_user_request_section" and events[21]["source"] == "current_request"
    with pytest.raises(ValueError, match="only earlier, non-human transcript events"):
        transcript.reduced(stubbed=frozenset(), omitted=frozenset({index}))
    keys = {segment.key for segment in transcript.context_budget_segments()}
    assert f"drop:{index}" not in keys and f"stub:{index}" not in keys


@pytest.mark.asyncio
async def test_over_the_byte_maximum_everything_eligible_goes_and_the_request_is_still_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _owner(tmp_path, max_request_bytes=30_000)
    requests = _transport(monkeypatch)
    await owner._call_advisor_with_audit(
        _arguments(_transcript(6, this_turn_result="THIS_TURN_RESULT_" + "t" * 40_000)), recorder=BufferingRecorder()
    )
    assert len(requests) == 1
    payload = _sent_transcript(requests[0]["messages"][1]["content"])
    assert _reduction(payload) == {
        "older_events_omitted": 18,
        "older_tool_results_stubbed": 0,
        "older_user_requests_shortened": 0,
    }


@pytest.mark.asyncio
async def test_a_publication_review_sends_its_reduced_transcript_and_keeps_its_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from elspeth.web.composer.tools import ADVISOR_TRIGGER_DETERMINISTIC_END
    from elspeth.web.composer.turn_effects import AdvisorPublicationReview

    owner = _owner(tmp_path, window=_SMALL_WINDOW)
    requests = _transport(monkeypatch)
    transcript = _transcript(40)
    review = AdvisorPublicationReview(
        binding="publication-binding",
        semantic_repair_key="repair-key",
        payload_json="{}",
        provider_payload_json=json.dumps({"current_request": transcript.request, "candidate_reply": "Renamed it."}),
        transcript=transcript,
    )
    arguments = {
        "trigger": ADVISOR_TRIGGER_DETERMINISTIC_END,
        "problem_summary": "Review the candidate.",
        "recent_errors": [],
        "attempted_actions": [],
        "publication_review": review,
    }
    await owner._call_advisor_with_audit(arguments, recorder=BufferingRecorder(), structured_output=True)
    sent = requests[0]["messages"][1]["content"]
    assert _REDUCED_HEADER in sent and request_wire_bytes(requests[0]["messages"], None) <= _SHED_ABOVE_BYTES
    assert arguments["publication_review"] is review and review.transcript is transcript


_USER_GAP = "older_user_requests_shortened_for_context_budget"


def _marker(length: int) -> str:
    return f"\n[ELSPETH shortened this earlier user request to its first 2,000 of {length:,} characters to fit the context budget.]"


@pytest.mark.asyncio
async def test_user_requests_are_not_shortened_while_stubs_and_drops_suffice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _owner(tmp_path, window=_SMALL_WINDOW)
    requests = _transport(monkeypatch)
    await owner._call_advisor_with_audit(_arguments(_transcript(10, user_chars=2_500)), recorder=BufferingRecorder())
    sent = requests[0]["messages"][1]["content"]
    payload = _sent_transcript(sent)
    reduction = _reduction(payload)
    assert reduction["older_events_omitted"] + reduction["older_tool_results_stubbed"] > 0
    assert reduction["older_user_requests_shortened"] == 0 and _USER_GAP not in payload["availability_gaps"]
    humans = [event["content"] for event in payload["events"] if event["authorship"] == "human" and "content" in event]
    assert humans == [_earlier_request(turn, 2_500) for turn in range(10)]
    assert "older ones shortened" not in sent


@pytest.mark.asyncio
async def test_when_stubs_and_drops_cannot_fit_older_user_requests_are_shortened_oldest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _owner(tmp_path, window=_SMALL_WINDOW)
    requests = _transport(monkeypatch)
    current_request = "CURRENT_REQUEST " + "c" * 5_000
    this_turn = "THIS_TURN_RESULT_" + "t" * 3_000
    await owner._call_advisor_with_audit(
        _arguments(_transcript(12, user_chars=8_000, current_request=current_request, this_turn_result=this_turn)),
        recorder=BufferingRecorder(),
    )
    sent = requests[0]["messages"][1]["content"]
    payload = _sent_transcript(sent)
    shortened = _reduction(payload)["older_user_requests_shortened"]
    # Some, not all: shortening stops once the request is under the high mark.
    assert 0 < shortened < 12
    assert request_wire_bytes(requests[0]["messages"], None) <= _SHED_ABOVE_BYTES
    humans = [event["content"] for event in payload["events"] if event["authorship"] == "human" and "content" in event]
    assert len(humans) == 12
    for turn, content in enumerate(humans):
        original = _earlier_request(turn, 8_000)
        if turn < shortened:
            # Oldest first; the kept start is exact and the marker carries the original length.
            assert content == original[:2_000] + _marker(len(original))
        else:
            assert content == original
    assert _USER_GAP in payload["availability_gaps"]
    assert f"every user request is kept ({shortened} older ones shortened);" in sent
    # The current request and this turn's evidence are never shortened.
    assert current_request in sent
    current = [event for event in payload["events"] if event["source"] == "current_request"]
    assert this_turn in current[-1]["content"]
    parts = requests[0]["prompt_guard_content_parts"][1]
    assert "".join(part.text for part in parts) == sent


def test_only_earlier_user_requests_longer_than_the_kept_start_can_be_shortened() -> None:
    _require_reduction_api()
    transcript = _transcript(5, user_chars=2_500)
    with pytest.raises(ValueError, match="only earlier user requests"):
        transcript.reduced(stubbed=frozenset(), omitted=frozenset(), shortened=frozenset({1}))
    with pytest.raises(ValueError, match="only earlier user requests"):
        transcript.reduced(stubbed=frozenset(), omitted=frozenset(), shortened=frozenset({20}))
    with pytest.raises(ValueError, match="longer than the kept start"):
        _transcript(5).reduced(stubbed=frozenset(), omitted=frozenset(), shortened=frozenset({0}))
    assert [segment.key for segment in transcript.user_request_segments()] == [f"shorten:{4 * turn}" for turn in range(5)]
    one = transcript.reduced(stubbed=frozenset(), omitted=frozenset(), shortened=frozenset({0}))
    two = transcript.reduced(stubbed=frozenset(), omitted=frozenset(), shortened=frozenset({0, 4}))
    assert len({transcript.binding, one.binding, two.binding}) == 3
    assert one.semantic_binding == two.semantic_binding == transcript.semantic_binding


@pytest.mark.parametrize(
    ("reason_code", "disposition"),
    [("context_length_exceeded", "retained"), ("invalid_request", "retained"), ("unsupported_parameter", "retained"), (None, "absent")],
)
def test_a_context_length_rejection_suggests_the_context_budget_recourse(reason_code: Any, disposition: Any) -> None:
    from elspeth.contracts.composer_progress import CONTEXT_BUDGET_REFUSAL_DETAIL
    from elspeth.contracts.provider_rejection import SafeRequestRejection
    from elspeth.web.composer.advisor_policy import advisor_signoff_blocked_wording

    rejection = SafeRequestRejection(
        reason_code=reason_code,
        reason_disposition=disposition,
        reason_source="sdk_body" if reason_code is not None else "unavailable",
    )
    detail, suggestion = advisor_signoff_blocked_wording(reason="request_rejected", findings="", request_rejection=rejection)
    assert detail == rejection.public_detail
    if reason_code == "context_length_exceeded":
        assert suggestion == CONTEXT_BUDGET_REFUSAL_DETAIL
    else:
        assert suggestion == (
            "An unchanged rejected request will not be retried automatically. Review the rejection reason before sending another message."
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected"), [("context_length_exceeded", "context_length_exceeded"), ("invalid_request", "BadRequestError")]
)
async def test_the_advisor_call_row_names_a_context_length_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str, expected: str
) -> None:
    from litellm.exceptions import BadRequestError

    owner = _owner(tmp_path)

    async def rejected(**kwargs: Any) -> None:
        await admit_provider_attempt(model=kwargs["model"])
        raise BadRequestError("PRIVATE_PROVIDER_BODY", model=kwargs["model"], llm_provider="openai", body={"code": code})

    monkeypatch.setattr(provider_gateway, "_litellm_acompletion", rejected)
    recorder = BufferingRecorder()
    with pytest.raises(BadRequestError):
        await owner._call_advisor_with_audit(_arguments(_transcript(1)), recorder=recorder)
    assert len(recorder.llm_calls) == 1
    assert recorder.llm_calls[0].error_class == "BadRequestError"
    assert recorder.llm_calls[0].error_message == expected


def test_a_reduced_transcript_keeps_its_disclosure_or_refuses_a_second_reduction() -> None:
    _require_reduction_api()
    reduced = _transcript(5).reduced(stubbed=frozenset({2}), omitted=frozenset({1}))
    referenced = reduced.with_request_reference("initial_planner_request.intent")
    # Renaming the reference keeps the counts, so the reduced header still renders.
    assert referenced.reduction == reduced.reduction
    assert _REDUCED_HEADER in advisor_context.build_advisor_user_message(_arguments(referenced))
    with pytest.raises(ValueError, match="already reduced"):
        reduced.without_events(frozenset({3}))
    with pytest.raises(ValueError, match="already reduced"):
        reduced.reduced(stubbed=frozenset(), omitted=frozenset({3}))


@pytest.mark.asyncio
async def test_the_hint_path_stays_within_the_context_budget_as_history_grows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.web.composer.tools.sessions import RequestAdvisorHintArgumentsModel

    owner = _owner(tmp_path, window=_SMALL_WINDOW)
    requests = _transport(monkeypatch)
    sizes = []
    for turns in (20, 40):
        await owner._call_advisor_for_tool(
            RequestAdvisorHintArgumentsModel(
                trigger="proactive_security_safety", problem_summary="Check the change.", recent_errors=[], attempted_actions=[]
            ),
            recorder=BufferingRecorder(),
            advisor_transcript=_transcript(turns),
        )
        sizes.append(request_wire_bytes(requests[-1]["messages"], None))
    assert len(requests) == 2
    assert all(size <= _SHED_ABOVE_BYTES for size in sizes), sizes
    assert sizes[1] - sizes[0] < 20 * 400, sizes
    assert _REDUCED_HEADER in requests[-1]["messages"][1]["content"]
