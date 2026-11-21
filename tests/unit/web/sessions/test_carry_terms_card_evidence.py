"""The event a prompt card's slot names holds the value the run uses.

A prompt review snapshot lists each slot as ``{id, event_id, user_term}`` and
stores the prompt rendered with accepted values. These tests settle whether a
card can show a carried term's definition from the EVENT its slot names: for
every slot of every prompt card, that event's ``accepted_value`` is the row's
``accepted_value`` and the text the snapshot and the run-bound prompt carry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts.composer_interpretation import InterpretationChoice, InterpretationKind
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    InterpretationReviewPending,
    materialize_state_for_execution,
)
from elspeth.web.sessions.protocol import InterpretationEventRecord
from elspeth.web.sessions.service import SessionServiceImpl
from tests.unit.web.sessions.test_carry_terms_across_wording_edits import (
    AUDIENCE,
    AUDIENCE_AMENDED,
    AUDIENCE_DRAFT,
    CATEGORY,
    CATEGORY_DRAFT,
    ONE_TERM_PARTS,
    ONE_TERM_ROWS,
    PROMPT_CARD,
    PROMPT_REVIEW,
    TWO_TERM_PARTS,
    TWO_TERM_ROWS,
    _approved_session,
    _cards,
    _edit_part,
    _errors,
    _patch,
    _rows,
    _sent_prompt,
    _Session,
    service,  # noqa: F401  (fixture)
)


async def _slot_values(session: _Session, card: InterpretationEventRecord) -> dict[str, str | None]:
    """For one prompt card: each slot's accepted value, read from the EVENT the snapshot names.

    ``None`` where the snapshot names no event for the slot. Asserts on the way
    that a named event, the stored row and the snapshot agree.
    """
    snapshot = await session.service.get_prompt_review_snapshot(session.id, card.id)
    assert snapshot is not None
    surface = snapshot["authored_surface"]
    events = {str(event.id): event for event in await session.service.list_interpretation_events(session.id, status="all")}
    rows = _rows(session.state)
    values: dict[str, str | None] = {}
    for slot in surface[INTERPRETATION_REQUIREMENTS_KEY]:
        assert set(slot) == {"id", "event_id", "user_term"}
        if slot["event_id"] is None:
            values[slot["id"]] = None
            continue
        row = rows[slot["id"]]
        assert row["event_id"] == slot["event_id"]
        event = events[slot["event_id"]]
        assert event.kind is InterpretationKind.VAGUE_TERM and event.user_term == slot["user_term"]
        assert event.accepted_value == row["accepted_value"]
        assert event.accepted_value is not None and event.accepted_value in surface["prompt_template"]
        values[slot["id"]] = event.accepted_value
    return values


@pytest.mark.asyncio
async def test_the_slot_event_holds_the_run_value_for_drafted_and_amended_terms_before_and_after_a_carried_edit(
    service: SessionServiceImpl,  # noqa: F811
    tmp_path: Path,
) -> None:
    session = await _approved_session(service, tmp_path)
    events = {event.user_term: event for event in await service.list_interpretation_events(session.id, status="all")}
    assert events["category"].choice is InterpretationChoice.ACCEPTED_AS_DRAFTED
    assert events["audience"].choice is InterpretationChoice.AMENDED
    # The first prompt card was created while both terms were still pending. Its snapshot is
    # frozen at creation, so its slots name no event even after the terms were resolved.
    first_card = events[PROMPT_CARD[1]]
    assert await _slot_values(session, first_card) == {CATEGORY: None, AUDIENCE: None}
    first_snapshot = await service.get_prompt_review_snapshot(session.id, first_card.id)
    assert first_snapshot is not None
    assert "pending interpretation" in first_snapshot["authored_surface"]["prompt_template"]

    edited = _edit_part(session, "Classify", "Sort")
    assert edited.success, _errors(edited)
    opened = await session.commit(edited.updated_state)

    assert _cards(opened) == [PROMPT_CARD]
    # The new card's slots name the ORIGINAL events; their values are what the new prompt carries.
    assert await _slot_values(session, opened[0]) == {CATEGORY: CATEGORY_DRAFT, AUDIENCE: AUDIENCE_AMENDED}
    snapshot = await service.get_prompt_review_snapshot(session.id, opened[0].id)
    assert snapshot is not None
    assert {slot["event_id"] for slot in snapshot["authored_surface"][INTERPRETATION_REQUIREMENTS_KEY]} == {
        str(events["category"].id),
        str(events["audience"].id),
    }
    expected = f"Sort {{{{ row.text }}}} into {CATEGORY_DRAFT} for {AUDIENCE_AMENDED}."
    assert snapshot["authored_surface"]["prompt_template"] == expected
    await session.resolve(opened[0])
    assert _sent_prompt(session.state) == expected


@pytest.mark.asyncio
async def test_a_term_reviewed_twice_is_named_by_its_latest_event(
    service: SessionServiceImpl,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Removed, added back and resolved again with a different amendment: two events for one term."""
    session = await _approved_session(service, tmp_path)
    first_event_id = _rows(session.state)[AUDIENCE]["event_id"]
    removed = _patch(session.state, {"prompt_template_parts": ONE_TERM_PARTS, INTERPRETATION_REQUIREMENTS_KEY: ONE_TERM_ROWS})
    assert removed.success, _errors(removed)
    await session.commit(removed.updated_state)
    restored = _patch(session.state, {"prompt_template_parts": TWO_TERM_PARTS, INTERPRETATION_REQUIREMENTS_KEY: TWO_TERM_ROWS})
    assert restored.success, _errors(restored)
    await session.commit(restored.updated_state)
    term_card = next(event for event in await session.pending() if event.user_term == "audience")
    await session.resolve(term_card, amended="first-time buyers")
    prompt_card = next(event for event in await session.pending() if event.kind is InterpretationKind.LLM_PROMPT_TEMPLATE)

    values = await _slot_values(session, prompt_card)

    # This prompt card was created while ``audience`` was pending again: its snapshot names the
    # carried ``category`` event and no event for ``audience``. The ROW names the latest event.
    assert values == {CATEGORY: CATEGORY_DRAFT, AUDIENCE: None}
    second_event_id = _rows(session.state)[AUDIENCE]["event_id"]
    assert second_event_id == str(term_card.id) != first_event_id
    await session.resolve(prompt_card)
    sent = _sent_prompt(session.state)
    assert sent.endswith("for first-time buyers.") and AUDIENCE_AMENDED not in sent and AUDIENCE_DRAFT not in sent


@pytest.mark.asyncio
async def test_a_duplicated_slot_names_one_event_whose_value_fills_both_places(
    service: SessionServiceImpl,  # noqa: F811
    tmp_path: Path,
) -> None:
    session = await _approved_session(service, tmp_path)
    parts: list[dict[str, Any]] = [
        *TWO_TERM_PARTS[:4],
        {"kind": "text", "text": "; write for "},
        TWO_TERM_PARTS[3],
        TWO_TERM_PARTS[4],
    ]
    edited = _patch(session.state, {"prompt_template_parts": parts})
    assert edited.success, _errors(edited)
    opened = await session.commit(edited.updated_state)

    assert _cards(opened) == [PROMPT_CARD]
    assert await _slot_values(session, opened[0]) == {CATEGORY: CATEGORY_DRAFT, AUDIENCE: AUDIENCE_AMENDED}
    await session.resolve(opened[0])
    assert _sent_prompt(session.state).count(AUDIENCE_AMENDED) == 2


@pytest.mark.asyncio
async def test_a_prompt_card_can_be_approved_while_a_term_is_pending_and_is_not_reissued(
    service: SessionServiceImpl,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Behaviour that predates the carry, pinned because the card design depends on it.

    The prompt card is approved showing ``pending interpretation`` at the open
    term's slot. Resolving the term afterwards opens no new prompt card, and
    the run is admitted with the term's value.
    """
    session = await _approved_session(service, tmp_path, leave_pending=("category",))
    pending = await session.pending()
    prompt_card = next(event for event in pending if event.kind is InterpretationKind.LLM_PROMPT_TEMPLATE)
    term_card = next(event for event in pending if event.user_term == "category")
    assert prompt_card.llm_draft is not None and "into pending interpretation for" in prompt_card.llm_draft

    await session.resolve(prompt_card)

    statuses = {row_id: row["status"] for row_id, row in _rows(session.state).items()}
    assert (statuses[CATEGORY], statuses[PROMPT_REVIEW]) == ("pending", "resolved")
    assert _cards(await session.pending()) == [("vague_term", "category")]
    assert isinstance(materialize_state_for_execution(session.state), InterpretationReviewPending)

    await session.resolve(term_card)

    assert all(row["status"] == "resolved" for row in _rows(session.state).values())
    assert await session.pending() == []
    assert await session.commit(session.state) == []
    assert _sent_prompt(session.state) == f"Classify {{{{ row.text }}}} into {CATEGORY_DRAFT} for {AUDIENCE_AMENDED}."
