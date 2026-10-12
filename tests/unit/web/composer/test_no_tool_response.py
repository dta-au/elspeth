"""Clarification protocol admission and public/history provenance."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from elspeth.web.composer.no_tool_policy import (
    AssistantTextSegment,
    TrustedSystemNoticeSegment,
    compose_clarification_message,
    compose_interpretation_review_handoff_message,
    visible_message_segments,
)
from elspeth.web.composer.no_tool_response import parse_no_tool_response
from elspeth.web.sessions.protocol import ChatMessageRecord
from elspeth.web.sessions.routes._helpers import _composer_history_content


@pytest.mark.parametrize(
    "content", ["Which input?", "clarify: Which input?", " CLARIFY: Which input?", "CLARIFY:   ", "DECLINE: Unsupported."]
)
def test_unmarked_or_malformed_prose_retains_original_meaning(content: str) -> None:
    response = parse_no_tool_response(content)
    assert response.kind == "reply"
    assert response.body == content


def test_exact_marker_admits_body_without_requiring_question_punctuation() -> None:
    response = parse_no_tool_response("CLARIFY: Please supply the required input file.")
    assert response.kind == "clarification"
    assert response.body == "Please supply the required input file."


def test_oversized_marker_body_uses_existing_provider_text_bound() -> None:
    content = "CLARIFY: " + "a" * 65_537
    response = parse_no_tool_response(content)
    assert response.kind == "reply"
    assert response.body == content


def test_clarification_body_preserves_trailing_provider_whitespace() -> None:
    assert parse_no_tool_response("CLARIFY: Which input?\n\n").body == "Which input?\n\n"


@pytest.mark.parametrize("composer", [compose_clarification_message, compose_interpretation_review_handoff_message])
def test_notice_round_trips_as_trusted_public_text_and_model_only_history(composer) -> None:
    prose = "Please confirm the categories."
    content = composer(prose)
    row = ChatMessageRecord(
        id=uuid4(),
        session_id=uuid4(),
        role="assistant",
        content=content,
        raw_content=prose,
        created_at=datetime.now(UTC),
        writer_principal="compose_loop",
        tool_calls=None,
        composition_state_id=None,
    )
    segments = visible_message_segments(content=row.content, raw_content=row.raw_content)
    assert len(segments) == 2
    assert segments[0] == AssistantTextSegment(prose)
    assert isinstance(segments[1], TrustedSystemNoticeSegment)
    assert _composer_history_content(row) == prose
