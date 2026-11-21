"""Carried vague terms across a routing-only upsert_node.

A routing-only edit re-sends a transform's routes and keeps its authored options
byte-identical, so its prompt parts are unchanged and a carried term keeps its
accepted value on the ordinary path. A rewire opens no card of its own: before
the reopened prompt card is approved the term is still carried and that card
is still the one pending; after approval the rewire changes nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from elspeth.web.interpretation_state import InterpretationReviewPending, materialize_state_for_execution
from elspeth.web.sessions.service import SessionServiceImpl
from tests.unit.web.composer.test_tools import _mock_catalog, execute_tool
from tests.unit.web.sessions.test_carry_terms_across_wording_edits import (
    AUDIENCE,
    AUDIENCE_AMENDED,
    CATEGORY,
    CATEGORY_DRAFT,
    NODE,
    PROMPT_CARD,
    _approved_session,
    _cards,
    _edit_part,
    _errors,
    _rows,
    _sent_prompt,
)

pytest_plugins = ("tests.unit.web.sessions.test_carry_terms_across_wording_edits",)

ROUTES = (
    {"id": NODE, "node_type": "transform", "input": "rows", "routing_only": True},
    {"id": NODE, "node_type": "transform", "input": "rows", "on_error": "classified", "routing_only": True},
)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES, ids=["same-input", "on_error-changed"])
async def test_carried_terms_survive_a_rewire_before_the_prompt_card_is_approved(
    service: SessionServiceImpl,
    tmp_path: Path,
    route: dict[str, object],
) -> None:
    session = await _approved_session(service, tmp_path)
    original = _rows(session.state)
    edited = _edit_part(session, "Classify", "Sort")
    assert edited.success, _errors(edited)
    assert _cards(await session.commit(edited.updated_state)) == [PROMPT_CARD]
    carried = _rows(session.state)
    assert carried[CATEGORY] == original[CATEGORY] and carried[AUDIENCE] == original[AUDIENCE]

    rerouted = execute_tool("upsert_node", dict(route), session.state, _mock_catalog())
    assert rerouted.success, _errors(rerouted)
    opened_after = await session.commit(rerouted.updated_state)

    rows = _rows(session.state)
    assert rows[CATEGORY] == original[CATEGORY], rows[CATEGORY]
    assert rows[AUDIENCE] == original[AUDIENCE], rows[AUDIENCE]
    assert _cards(opened_after) == []
    assert _cards(await session.pending()) == [PROMPT_CARD]
    assert isinstance(materialize_state_for_execution(session.state), InterpretationReviewPending)

    (card,) = await session.pending()
    await session.resolve(card)
    assert await session.pending() == []
    assert _sent_prompt(session.state) == f"Sort {{{{ row.text }}}} into {CATEGORY_DRAFT} for {AUDIENCE_AMENDED}."


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES, ids=["same-input", "on_error-changed"])
async def test_a_rewire_after_the_prompt_card_is_approved_reopens_nothing(
    service: SessionServiceImpl,
    tmp_path: Path,
    route: dict[str, object],
) -> None:
    session = await _approved_session(service, tmp_path)
    edited = _edit_part(session, "Classify", "Sort")
    assert edited.success, _errors(edited)
    await session.approve_only_prompt_card(await session.commit(edited.updated_state))
    approved = _rows(session.state)

    rerouted = execute_tool("upsert_node", dict(route), session.state, _mock_catalog())
    assert rerouted.success, _errors(rerouted)
    opened_after = await session.commit(rerouted.updated_state)

    assert _rows(session.state) == approved
    assert _cards(opened_after) == []
    assert _sent_prompt(session.state) == f"Sort {{{{ row.text }}}} into {CATEGORY_DRAFT} for {AUDIENCE_AMENDED}."
