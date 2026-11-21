"""Carried terms on the paths around a wording edit: card bound, stale cards, renames, proposal accept.

Companion to ``test_carry_terms_across_wording_edits.py`` (same harness).
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from elspeth.contracts.freeze import deep_thaw
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.tools import execute_tool as production_execute_tool
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    PROMPT_SURFACE_REVIEW_MAX_CHARS,
    InterpretationReviewPending,
    materialize_state_for_execution,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.protocol import CompositionStateData, InterpretationEventAlreadyResolvedError
from elspeth.web.sessions.routes.composer import proposals as proposal_routes
from elspeth.web.sessions.service import SessionServiceImpl
from tests.unit.web.composer.test_prompt_patch_reviews import _approved_state
from tests.unit.web.composer.test_service import _make_settings
from tests.unit.web.composer.test_tools import _mock_catalog, execute_tool
from tests.unit.web.sessions.routes.composer.test_change_substring_proposal import _preview_proposal
from tests.unit.web.sessions.test_blob_proposal_accept import _save_composition_state
from tests.unit.web.sessions.test_carry_terms_across_wording_edits import (
    AUDIENCE,
    AUDIENCE_AMENDED,
    AUDIENCE_DRAFT,
    CATEGORY,
    CATEGORY_DRAFT,
    HASH_KEY,
    NODE,
    PROMPT_CARD,
    TWO_TERM_PARTS,
    TWO_TERM_ROWS,
    _approved_session,
    _assert_two_terms_resolved_as_the_user_left_them,
    _card_text,
    _cards,
    _edit_part,
    _errors,
    _padded_first_text,
    _patch,
    _prompt,
    _read,
    _rows,
    _sent_prompt,
    _state_context_node,
    _with_first_text,
    service,  # noqa: F401  (fixture)
)
from tests.unit.web.sessions.test_routes import _make_app


@pytest.mark.asyncio
async def test_terms_carried_by_one_call_stay_carried_when_a_second_call_shortens_the_card(
    service: SessionServiceImpl,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Two calls: a wording edit on a long prompt, then a system prompt that halves the card's share.

    There is no length gate, so the terms are carried by the first call and
    the second call has nothing to undo: one prompt card, cut short, and the
    run sends the full prompt with the amendment.
    """
    session = await _approved_session(service, tmp_path, system_prompt=False)
    original_rows = _rows(session.state)
    long_text = _padded_first_text(rendered_length=5080)
    edited = _patch(session.state, {"prompt_template_parts": _with_first_text(TWO_TERM_PARTS, long_text)})
    assert edited.success, _errors(edited)
    assert _cards(await session.commit(edited.updated_state)) == [PROMPT_CARD]

    with_system = _patch(session.state, {"system_prompt": "You classify rows."})
    assert with_system.success, _errors(with_system)
    await session.commit(with_system.updated_state)

    assert _rows(session.state)[CATEGORY] == original_rows[CATEGORY]
    assert _rows(session.state)[AUDIENCE] == original_rows[AUDIENCE]
    pending = await session.pending()
    assert _cards(pending) == [PROMPT_CARD]
    card = pending[0].llm_draft
    assert card is not None and "more chars not shown" in card and AUDIENCE_AMENDED not in card
    await session.resolve(pending[0])
    sent = _sent_prompt(session.state)
    assert len(sent) == 5080 and sent.endswith(f" into {CATEGORY_DRAFT} for {AUDIENCE_AMENDED}.")


@pytest.mark.asyncio
async def test_terms_carried_on_a_multi_query_node_stay_carried_when_more_queries_shorten_the_card(
    service: SessionServiceImpl,  # noqa: F811
    tmp_path: Path,
) -> None:
    session = await _approved_session(service, tmp_path, extra_options=MULTI_QUERY)
    original_rows = _rows(session.state)
    long_text = _padded_first_text(rendered_length=5080)
    edited = _patch(session.state, {"prompt_template_parts": _with_first_text(TWO_TERM_PARTS, long_text)})
    assert edited.success, _errors(edited)
    assert _cards(await session.commit(edited.updated_state)) == [PROMPT_CARD]

    many_queries = {f"query_number_{index:02d}_with_a_long_descriptive_name": {"input_fields": {"text": "text"}} for index in range(60)}
    more = _patch(session.state, {"queries": many_queries})
    assert more.success, _errors(more)
    await session.commit(more.updated_state)

    assert _rows(session.state)[CATEGORY] == original_rows[CATEGORY]
    assert _rows(session.state)[AUDIENCE] == original_rows[AUDIENCE]
    pending = await session.pending()
    assert _cards(pending) == [PROMPT_CARD]
    card = pending[0].llm_draft
    assert card is not None and len(card) <= PROMPT_SURFACE_REVIEW_MAX_CHARS
    assert "more chars not shown" in card and AUDIENCE_AMENDED not in card


@pytest.mark.asyncio
async def test_the_status_words_the_model_reads_are_resolved_and_pending(
    service: SessionServiceImpl,  # noqa: F811
    tmp_path: Path,
) -> None:
    """After one edit that re-drafts ``category`` and leaves ``audience`` alone: the literal row statuses."""
    session = await _approved_session(service, tmp_path)
    rows = [{"kind": "vague_term", "user_term": "category", "draft": "narrow categories"}, TWO_TERM_ROWS[1]]
    edited = _patch(
        session.state,
        {"prompt_template_parts": _with_first_text(TWO_TERM_PARTS, "Sort {{ row.text }} into "), INTERPRETATION_REQUIREMENTS_KEY: rows},
    )
    assert edited.success, _errors(edited)
    await session.commit(edited.updated_state)

    context_rows = {row["id"]: row for row in _state_context_node(session.state)["options"][INTERPRETATION_REQUIREMENTS_KEY]}
    node_rows = {row["id"]: row for row in _read(session.state, NODE)["node"]["options"][INTERPRETATION_REQUIREMENTS_KEY]}
    for read_rows in (context_rows, node_rows):
        assert read_rows[AUDIENCE]["status"] == "resolved"
        assert read_rows[CATEGORY]["status"] == "pending"
    # The state context shows a pending term's draft as ``draft`` and a resolved term's as ``historical_draft``.
    assert context_rows[CATEGORY]["draft"] == "narrow categories"
    assert context_rows[AUDIENCE]["historical_draft"] == AUDIENCE_DRAFT


@pytest.mark.asyncio
async def test_a_prompt_card_replaced_by_a_later_edit_cannot_approve_the_later_text(
    service: SessionServiceImpl,  # noqa: F811
    tmp_path: Path,
) -> None:
    session = await _approved_session(service, tmp_path)
    first = _edit_part(session, "Classify", "Sort")
    assert first.success, _errors(first)
    stale_card = (await session.commit(first.updated_state))[0]
    second = _edit_part(session, "Sort", "Label")
    assert second.success, _errors(second)
    current_card = (await session.commit(second.updated_state))[0]
    assert current_card.id != stale_card.id
    assert _cards(await session.pending()) == [PROMPT_CARD]

    # The replaced card was closed when the later edit opened its own.
    with pytest.raises(InterpretationEventAlreadyResolvedError):
        await session.resolve(stale_card)

    await session.reload()
    assert isinstance(materialize_state_for_execution(session.state), InterpretationReviewPending)
    assert [event.id for event in await session.pending()] == [current_card.id]
    _assert_two_terms_resolved_as_the_user_left_them(session.state)

    await session.resolve(current_card)
    assert _sent_prompt(session.state) == f"Label {{{{ row.text }}}} into {CATEGORY_DRAFT} for {AUDIENCE_AMENDED}."


@pytest.mark.asyncio
async def test_wording_changed_and_changed_back_still_needs_the_prompt_card_and_keeps_the_terms(
    service: SessionServiceImpl,  # noqa: F811
    tmp_path: Path,
) -> None:
    session = await _approved_session(service, tmp_path)
    original_rows = _rows(session.state)
    original_prompt = _prompt(session.state)

    away = _edit_part(session, "Classify", "Sort")
    assert away.success, _errors(away)
    await session.commit(away.updated_state)
    back = _edit_part(session, "Sort", "Classify")
    assert back.success, _errors(back)
    opened = await session.commit(back.updated_state)

    assert _prompt(session.state) == original_prompt
    assert _rows(session.state)[CATEGORY] == original_rows[CATEGORY]
    assert _rows(session.state)[AUDIENCE] == original_rows[AUDIENCE]
    # The earlier approval of this same text is not revived: the open card must be approved.
    pending = await session.pending()
    assert _cards(pending) == [PROMPT_CARD]
    assert isinstance(materialize_state_for_execution(session.state), InterpretationReviewPending)
    assert pending[0].llm_draft == _card_text(original_prompt)
    await session.resolve(pending[0])
    assert _sent_prompt(session.state) == original_prompt
    assert opened == [] or _cards(opened) == [PROMPT_CARD]


@pytest.mark.asyncio
async def test_a_term_renamed_and_renamed_back_is_reviewed_again_with_the_authored_draft(
    service: SessionServiceImpl,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Renaming a term drops its row; the old name coming back is a new pending row, not the old approval."""
    session = await _approved_session(service, tmp_path)
    original_rows = _rows(session.state)
    renamed_id = f"reader:{NODE}"
    renamed_parts = [*TWO_TERM_PARTS[:3], {"kind": "interpretation_ref", "requirement_id": renamed_id}, TWO_TERM_PARTS[4]]
    renamed_rows = [TWO_TERM_ROWS[0], {"kind": "vague_term", "user_term": "reader", "draft": AUDIENCE_DRAFT}]
    renamed = _patch(session.state, {"prompt_template_parts": renamed_parts, INTERPRETATION_REQUIREMENTS_KEY: renamed_rows})
    assert renamed.success, _errors(renamed)
    await session.commit(renamed.updated_state)

    back = _patch(session.state, {"prompt_template_parts": TWO_TERM_PARTS, INTERPRETATION_REQUIREMENTS_KEY: TWO_TERM_ROWS})
    assert back.success, _errors(back)
    opened = await session.commit(back.updated_state)

    rows = _rows(session.state)
    assert renamed_id not in rows
    audience = rows[AUDIENCE]
    assert (audience["status"], audience["accepted_value"], audience["event_id"], audience["draft"]) == (
        "pending",
        None,
        None,
        AUDIENCE_DRAFT,
    )
    assert rows[CATEGORY] == original_rows[CATEGORY]
    assert _prompt(session.state) == f"Classify {{{{ row.text }}}} into {CATEGORY_DRAFT} for pending interpretation."
    audience_card = next(event for event in opened if event.user_term == "audience")
    assert audience_card.llm_draft == AUDIENCE_DRAFT
    assert AUDIENCE_AMENDED not in _prompt(session.state)


@pytest.mark.asyncio
async def test_a_wording_edit_accepted_as_a_proposal_carries_the_term_like_the_direct_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit-approve mode: the edit is previewed as a proposal, then accepted through the accept route."""
    app, sessions = _make_app(tmp_path)
    catalog = _mock_catalog()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    policy_catalog = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    composer = ComposerServiceImpl.for_trained_operator(
        catalog=catalog, settings=_make_settings(data_dir=tmp_path), sessions_service=sessions, session_engine=sessions._engine
    )

    def trained_dispatch(name: str, arguments: dict[str, object], state: object, _route_catalog: object, **kwargs: Any) -> object:
        return production_execute_tool(name, arguments, state, policy_catalog, **{**kwargs, "plugin_snapshot": snapshot})

    monkeypatch.setattr(proposal_routes, "execute_tool", trained_dispatch)
    session = await sessions.create_session("alice", "Carry through proposal accept", "local")
    await sessions.update_composer_preferences(session.id, trust_mode="explicit_approve", density_default="high", actor="user:alice")
    state = _approved_state(structured=True)
    document = state.to_dict()
    saved = await _save_composition_state(
        sessions,
        session.id,
        CompositionStateData(
            sources=document["sources"],
            nodes=document["nodes"],
            edges=document["edges"],
            outputs=document["outputs"],
            metadata_=document["metadata"],
            is_valid=False,
        ),
        provenance="session_seed",
    )
    stored_term = next(deep_thaw(row) for row in state.nodes[0].options[INTERPRETATION_REQUIREMENTS_KEY] if row["kind"] == "vague_term")
    assert stored_term["status"] == "resolved"
    arguments: dict[str, object] = {
        "target": {"kind": "prompt_part", "node_id": state.nodes[0].id, "part_index": 0},
        "expected_sha256": sha256(b"Classify ").hexdigest(),
        "expected_state_id": str(saved.id),
        "edits": [{"old": "Classify", "replacement": "Assess", "replace_all": False}],
    }

    direct = execute_tool("change_substring", arguments, state, catalog, current_state_id=str(saved.id))
    assert direct.success, _errors(direct)
    direct_options = direct.updated_state.nodes[0].options

    proposal = await _preview_proposal(composer, sessions, session.id, state, saved.id, arguments, "carry-edit")
    assert (await sessions.get_current_state(session.id)).id == saved.id
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        accepted = await client.post(f"/api/sessions/{session.id}/proposals/{proposal.id}/accept")
        assert accepted.status_code == 200, accepted.text

    final = await sessions.get_current_state(session.id)
    assert final is not None and final.id != saved.id
    accepted_options = state_from_record(final).nodes[0].options
    for options in (direct_options, accepted_options):
        rows = {row["id"]: deep_thaw(row) for row in options[INTERPRETATION_REQUIREMENTS_KEY]}
        assert rows["category"] == stored_term
        assert next(row for row in rows.values() if row["kind"] == "llm_prompt_template")["status"] == "pending"
        assert options["prompt_template"] == "Assess support categories for {{ row.text }}."
        # Pre-existing: the artifact hash is re-derived for the new text while its review is pending.
        assert options[HASH_KEY] != state.nodes[0].options[HASH_KEY]
    assert deep_thaw(accepted_options[INTERPRETATION_REQUIREMENTS_KEY]) == deep_thaw(direct_options[INTERPRETATION_REQUIREMENTS_KEY])


MULTI_QUERY = {
    "queries": {"first": {"input_fields": {"text": "text"}}, "second": {"input_fields": {"text": "text"}}},
    "max_capacity_retry_seconds": 20,
}


@pytest.mark.asyncio
async def test_a_wording_edit_sent_with_a_query_template_that_bypasses_the_slots_is_still_refused(
    service: SessionServiceImpl,  # noqa: F811
    tmp_path: Path,
) -> None:
    """A query with its own template does not use the node-level slots. The edit is refused, as before the carry."""
    session = await _approved_session(service, tmp_path, extra_options=MULTI_QUERY)
    queries = {
        "first": {"input_fields": {"text": "text"}, "template": "Summarise {{ row.text }}."},
        "second": {"input_fields": {"text": "text"}},
    }

    refused = _patch(
        session.state,
        {"prompt_template_parts": _with_first_text(TWO_TERM_PARTS, "Sort {{ row.text }} into "), "queries": queries},
    )

    assert not refused.success
    assert "vague_term_query_override" in [entry["error_code"] for entry in _errors(refused)]


@pytest.mark.asyncio
async def test_parts_re_split_without_a_text_change_carry_the_terms_behind_a_reopened_prompt_card(
    service: SessionServiceImpl,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Splitting one text part in two changes the parts skeleton and no text.

    On a multi-query node too, the prompt review reopens for the changed
    skeleton, so the terms are carried behind that card like any wording edit.
    """
    session = await _approved_session(service, tmp_path, extra_options=MULTI_QUERY)
    original_rows = _rows(session.state)
    original_prompt = _prompt(session.state)
    re_split = [{"kind": "text", "text": "Classify "}, {"kind": "text", "text": "{{ row.text }} into "}, *TWO_TERM_PARTS[1:]]

    edited = _patch(session.state, {"prompt_template_parts": re_split})
    assert edited.success, _errors(edited)
    opened = await session.commit(edited.updated_state)

    assert _cards(opened) == [PROMPT_CARD]
    assert _rows(session.state)[CATEGORY] == original_rows[CATEGORY]
    assert _rows(session.state)[AUDIENCE] == original_rows[AUDIENCE]
    assert _prompt(session.state) == original_prompt
    assert isinstance(materialize_state_for_execution(session.state), InterpretationReviewPending)
