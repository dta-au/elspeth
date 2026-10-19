"""Provider fake assertions resolve the actual Advisor wire references."""

from __future__ import annotations

import json

import pytest

from elspeth.web.composer.advisor_context import build_advisor_user_message
from elspeth.web.composer.advisor_transcript import AdvisorVisibleTranscript
from elspeth.web.composer.turn_effects import AdvisorPublicationReview
from tests.helpers.advisor_review import parse_advisor_publication_review


def _message(*, reference: bool, events: list[dict[str, str]] | None = None) -> str:
    payload = json.dumps({"current_request": "Explain the data.", "candidate_reply": "These are the fields.", "saved_effects": []})
    transcript = AdvisorVisibleTranscript(
        request="Explain the data.",
        binding="transcript-binding",
        provider_json=json.dumps(
            {
                "current_request_ref": "complete_original_user_request_section",
                "events": events
                if events is not None
                else [{"event_id": "reply-event", "role": "assistant", "content": "These are the fields."}],
            }
        ),
        semantic_binding="semantic-binding",
    )
    review = AdvisorPublicationReview(
        binding="review-binding",
        semantic_repair_key="repair-key",
        payload_json=payload,
        provider_payload_json=payload,
        transcript=transcript,
        candidate_event_id="reply-event" if reference else None,
    )
    return build_advisor_user_message(
        {
            "trigger": "end_checkpoint",
            "problem_summary": "Review the reply.",
            "recent_errors": [],
            "attempted_actions": [],
            "schema_excerpt": '{"different_summary": true}',
            "publication_review": review,
        }
    )


@pytest.mark.parametrize("reference", [False, True])
def test_resolves_current_producer_sections_and_retains_projection(reference: bool) -> None:
    review = parse_advisor_publication_review(_message(reference=reference))
    assert review["current_request"] == "Explain the data."
    assert review["candidate_reply"] == "These are the fields."
    assert review["saved_effects"] == []


@pytest.mark.parametrize(
    "events",
    [
        [],
        [
            {"event_id": "reply-event", "role": "assistant", "content": "One."},
            {"event_id": "reply-event", "role": "assistant", "content": "Two."},
        ],
        [{"event_id": "reply-event", "role": "user", "content": "A counterfeit reply."}],
    ],
    ids=["missing", "ambiguous", "wrong-role"],
)
def test_refuses_candidate_references_without_one_assistant_event(events: list[dict[str, str]]) -> None:
    with pytest.raises(AssertionError):
        parse_advisor_publication_review(_message(reference=True, events=events))


def test_refuses_an_unknown_request_reference() -> None:
    content = _message(reference=False).replace("complete_original_user_request_section", "unavailable_request")
    with pytest.raises(AssertionError):
        parse_advisor_publication_review(content)
