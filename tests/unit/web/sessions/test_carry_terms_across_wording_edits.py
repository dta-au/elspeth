"""An accepted vague term stays accepted when wording elsewhere in its prompt is edited.

A structured LLM prompt (``prompt_template_parts`` with ``interpretation_ref``
slots) keeps one review row per vague term. Editing a text part used to reset
every term row to pending at the edit's commit: each small wording edit
reopened the prompt card and every term card, the prompt card showed
``pending interpretation`` at the slots, and a term the user had amended came
back offering the model's draft, so the amendment was lost.

Reconciliation now carries a resolved term across such an edit unless the term
itself changed. Only the ``llm_prompt_template`` card reopens, and that card is
the review of the change: it shows the new wording with the accepted values in
place.

Driven for real: the tool dispatcher (``patch_node_options``,
``change_substring``, ``upsert_node``, ``set_pipeline``,
``get_pipeline_state``), the ``request_interpretation_review`` handler,
``SessionServiceImpl.save_composition_state``, the backend review surfacer
(``surface_pending_interpretation_reviews_for_state``),
``get_prompt_review_snapshot`` and ``resolve_interpretation_event`` against a
session database, and ``materialize_state_for_execution`` (the run gate).
Not driven: the compose loop that saves a tool's state and calls the surfacer
(the tests make those two calls themselves), the resolve HTTP route, the
execution service around the run gate, and the frontend card.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from itertools import pairwise
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import insert, select
from sqlalchemy.pool import StaticPool

from elspeth.contracts.composer_interpretation import InterpretationChoice, InterpretationKind, InterpretationSurfaceOrigin
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.interpretation_surfacing import surface_pending_interpretation_reviews_for_state
from elspeth.web.composer.prompts import build_context_string
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools import ToolResult
from elspeth.web.composer.tools.sessions import _handle_request_interpretation_review
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    PROMPT_SURFACE_REVIEW_MAX_CHARS,
    InterpretationReviewPending,
    materialize_state_for_execution,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import interpretation_events_table, session_operation_fences_table, sessions_table
from elspeth.web.sessions.protocol import CompositionStateData, InterpretationEventRecord
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.prompt_review import reviewed_anchor_kwargs
from tests.unit.web.composer import test_blob_inline_tools as blob_tools
from tests.unit.web.composer.test_tools import _mock_catalog, execute_tool
from tests.unit.web.sessions.session_test_authority import FencedSessionServiceHarness

NODE = "classify"
CATEGORY = "category:classify"
AUDIENCE = "audience:classify"
PROMPT_REVIEW = "prompt_template_review:classify"
CATEGORY_DRAFT = "broad support categories"
AUDIENCE_DRAFT = "everyone"
AUDIENCE_AMENDED = "new enterprise users"
PENDING_SLOT = "pending interpretation"
SYSTEM_PROMPT = "You classify each input row. Reply with one category label."
HASH_KEY = "approved_prompt_artifact_hash"
PROMPT_CARD = ("llm_prompt_template", f"llm_prompt_template:{NODE}")
TWO_TERM_PARTS: list[dict[str, str]] = [
    {"kind": "text", "text": "Classify {{ row.text }} into "},
    {"kind": "interpretation_ref", "requirement_id": CATEGORY},
    {"kind": "text", "text": " for "},
    {"kind": "interpretation_ref", "requirement_id": AUDIENCE},
    {"kind": "text", "text": "."},
]
TWO_TERM_ROWS: list[dict[str, str]] = [
    {"kind": "vague_term", "user_term": "category", "draft": CATEGORY_DRAFT},
    {"kind": "vague_term", "user_term": "audience", "draft": AUDIENCE_DRAFT},
]
ONE_TERM_PARTS: list[dict[str, str]] = [*TWO_TERM_PARTS[:2], {"kind": "text", "text": "."}]
ONE_TERM_ROWS: list[dict[str, str]] = TWO_TERM_ROWS[:1]
PROVENANCE = {"model_identifier": "test-model", "model_version": "1", "provider": "test-provider", "composer_skill_hash": "a" * 64}


@pytest.fixture
def service() -> SessionServiceImpl:
    engine = create_session_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    return FencedSessionServiceHarness(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("carry-terms-test"))


@asynccontextmanager
async def _compose(service: SessionServiceImpl, session_id: UUID) -> AsyncIterator[SessionOperationContext]:
    context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    try:
        yield context
    finally:
        await service._run_sync(service.session_operation_authority.release, context)


def _insert_session(service: SessionServiceImpl, session_id: UUID) -> None:
    created_at = datetime.now(UTC)
    with service._engine.begin() as conn:
        conn.execute(
            insert(sessions_table).values(
                id=str(session_id),
                user_id="alice",
                auth_provider_type="local",
                title="Carry terms across wording edits",
                created_at=created_at,
                updated_at=created_at,
            )
        )
        conn.execute(
            insert(session_operation_fences_table).values(
                session_id=str(session_id),
                operation_id=f"create-{session_id}",
                lease_token=f"create-token-{session_id}",
                operation_kind=SessionOperationKind.CREATE.value,
                owner_instance_id=service.session_operation_owner_instance_id,
                operation_epoch=1,
                lease_expires_at=created_at,
                released_at=created_at,
            )
        )


class _Session:
    """One session whose head is saved, surfaced and resolved through the real service."""

    def __init__(self, service: SessionServiceImpl) -> None:
        self.service = service
        self.id = uuid4()
        _insert_session(service, self.id)
        self.state_id = ""
        self.state: CompositionState

    async def commit(self, state: CompositionState) -> list[InterpretationEventRecord]:
        """Save ``state`` and run the backend surfacer, as a compose turn does. Returns the cards it opened."""
        before = {event.id for event in await self.service.list_interpretation_events(self.id, status="all")}
        document = state.to_dict()
        async with _compose(self.service, self.id) as context:
            saved = await self.service.save_composition_state(
                self.id,
                CompositionStateData(
                    sources=document["sources"],
                    nodes=document["nodes"],
                    edges=document["edges"],
                    outputs=document["outputs"],
                    metadata_=document["metadata"],
                    is_valid=True,
                ),
                provenance="tool_call",
                session_operation_context=context,
            )
            await surface_pending_interpretation_reviews_for_state(
                state,
                sessions_service=self.service,
                session_id=str(self.id),
                current_state_id=str(saved.id),
                surface_origin=InterpretationSurfaceOrigin.COMPOSER_LLM,
                session_operation_context=context,
                **PROVENANCE,
            )
        await self.reload()
        return [event for event in await self.pending() if event.id not in before]

    async def reload(self) -> CompositionState:
        record = await self.service.get_current_state(self.id)
        assert record is not None
        self.state_id = str(record.id)
        self.state = state_from_record(record)
        return self.state

    async def pending(self) -> list[InterpretationEventRecord]:
        return await self.service.list_interpretation_events(self.id, status="pending")

    async def resolve(self, event: InterpretationEventRecord, *, amended: str | None = None) -> None:
        """Approve a card the way the card does; a prompt card names its stored snapshot's anchor."""
        await self.service.resolve_interpretation_event(
            session_id=self.id,
            event_id=event.id,
            choice=InterpretationChoice.AMENDED if amended is not None else InterpretationChoice.ACCEPTED_AS_DRAFTED,
            amended_value=amended,
            actor="user:alice",
            runtime_model_identifier=None,
            runtime_model_version=None,
            **await reviewed_anchor_kwargs(self.service, self.id, event.id, event.kind),
        )
        await self.reload()

    async def approve_only_prompt_card(self, opened: list[InterpretationEventRecord]) -> None:
        assert _cards(opened) == [PROMPT_CARD]
        await self.resolve(opened[0])

    def term_events(self) -> list[dict[str, Any]]:
        """Every stored vague-term event row of this session, every column."""
        with self.service._engine.begin() as conn:
            rows = conn.execute(
                select(interpretation_events_table)
                .where(
                    interpretation_events_table.c.session_id == str(self.id),
                    interpretation_events_table.c.kind == InterpretationKind.VAGUE_TERM.value,
                )
                .order_by(interpretation_events_table.c.created_at, interpretation_events_table.c.id)
            ).all()
        return [dict(row._mapping) for row in rows]


def _cards(events: list[InterpretationEventRecord]) -> list[tuple[str, str | None]]:
    return sorted((event.kind.value, event.user_term) for event in events)


def _options(state: CompositionState) -> Mapping[str, Any]:
    return next(node for node in state.nodes if node.id == NODE).options


def _rows(state: CompositionState) -> dict[str, dict[str, Any]]:
    return {row["id"]: deep_thaw(row) for row in _options(state)[INTERPRETATION_REQUIREMENTS_KEY]}


def _prompt(state: CompositionState) -> str:
    prompt = _options(state)["prompt_template"]
    assert type(prompt) is str
    return prompt


def _sent_prompt(state: CompositionState) -> str:
    """The prompt text a run would send: the run gate's materialized node."""
    materialized = materialize_state_for_execution(state)
    assert isinstance(materialized, CompositionState), materialized
    return _prompt(materialized)


def _card_text(prompt: str, *, system_prompt: bool = True) -> str:
    """The bounded text a prompt card carries for a prompt it shows whole."""
    return f"System prompt:\n{SYSTEM_PROMPT}\n\nPrompt template:\n{prompt}" if system_prompt else prompt


def _errors(result: ToolResult) -> list[dict[str, Any]]:
    return [entry.to_dict() for entry in result.validation.errors]


def _patch(state: CompositionState, patch: dict[str, Any]) -> ToolResult:
    return execute_tool("patch_node_options", {"node_id": NODE, "patch": patch}, state, _mock_catalog())


def _edit_part(session: _Session, old: str, new: str, *, part_index: int = 0) -> ToolResult:
    """``change_substring`` on one text part of the head, with the inspected hash and state id."""
    text = _options(session.state)["prompt_template_parts"][part_index]["text"]
    return execute_tool(
        "change_substring",
        {
            "target": {"kind": "prompt_part", "node_id": NODE, "part_index": part_index},
            "expected_sha256": sha256(text.encode("utf-8")).hexdigest(),
            "expected_state_id": session.state_id,
            "edits": [{"old": old, "replacement": new, "replace_all": False}],
        },
        session.state,
        _mock_catalog(),
        current_state_id=session.state_id,
    )


def _with_first_text(parts: Any, text: str) -> list[dict[str, str]]:
    return [{"kind": "text", "text": text}, *deep_thaw(parts)[1:]]


async def _approved_session(
    service: SessionServiceImpl,
    tmp_path: Path,
    *,
    parts: list[dict[str, str]] = TWO_TERM_PARTS,
    rows: list[dict[str, str]] = TWO_TERM_ROWS,
    system_prompt: bool = True,
    leave_pending: tuple[str, ...] = (),
    extra_options: Mapping[str, Any] | None = None,
) -> _Session:
    """A structured prompt whose reviews the user resolved: ``audience`` amended, the rest as drafted.

    ``leave_pending`` names user terms whose cards are left open; the prompt
    card is then left open too.
    """
    session = _Session(service)
    state = blob_tools._field_declaring_sink_state(tmp_path)
    if not system_prompt:
        node = state.nodes[0]
        state = replace(state, nodes=(replace(node, options={k: v for k, v in node.options.items() if k != "system_prompt"}),))
    staged = _patch(state, {"prompt_template_parts": parts, "interpretation_requirements": rows, **(extra_options or {})})
    assert staged.success, _errors(staged)
    opened = await session.commit(staged.updated_state)
    assert _cards(opened) == sorted(
        [("llm_model_choice", f"llm_model_choice:{NODE}"), PROMPT_CARD, *(("vague_term", row["user_term"]) for row in rows)]
    )
    # Terms and the model first, the prompt card last.
    for event in sorted(opened, key=lambda event: event.kind is InterpretationKind.LLM_PROMPT_TEMPLATE):
        if event.user_term in leave_pending or (leave_pending and event.kind is InterpretationKind.LLM_PROMPT_TEMPLATE):
            continue
        await session.resolve(event, amended=AUDIENCE_AMENDED if event.user_term == "audience" else None)
    return session


def _assert_two_terms_resolved_as_the_user_left_them(state: CompositionState) -> None:
    rows = _rows(state)
    category, audience = rows[CATEGORY], rows[AUDIENCE]
    assert (category["status"], category["accepted_value"], category["draft"]) == ("resolved", CATEGORY_DRAFT, CATEGORY_DRAFT)
    assert (audience["status"], audience["accepted_value"], audience["draft"]) == ("resolved", AUDIENCE_AMENDED, AUDIENCE_DRAFT)


# --------------------------------------------------------------------------- #
# What a resolved term's attestation binds
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_resolved_term_attests_its_accepted_value_alone(service: SessionServiceImpl, tmp_path: Path) -> None:
    """The stored row binds ``stable_hash(accepted_value)``: nothing about position, neighbours or the skeleton."""
    first = await _approved_session(service, tmp_path)
    moved_parts = [
        {"kind": "text", "text": "An entirely different sentence about {{ row.text }}: audience "},
        {"kind": "interpretation_ref", "requirement_id": AUDIENCE},
        {"kind": "text", "text": ", categories "},
        {"kind": "interpretation_ref", "requirement_id": CATEGORY},
    ]
    second = await _approved_session(service, tmp_path, parts=moved_parts)
    assert _prompt(first.state) != _prompt(second.state)
    for session in (first, second):
        rows = _rows(session.state)
        for row_id, accepted in ((CATEGORY, CATEGORY_DRAFT), (AUDIENCE, AUDIENCE_AMENDED)):
            row = rows[row_id]
            assert set(row) == {
                "id",
                "kind",
                "user_term",
                "status",
                "draft",
                "event_id",
                "accepted_value",
                "accepted_artifact_hash",
                "resolved_prompt_template_hash",
            }
            assert row["status"] == "resolved"
            assert row["accepted_value"] == accepted
            assert row["resolved_prompt_template_hash"] == stable_hash(accepted)
            assert row["accepted_artifact_hash"] is None
    # Same value, different surrounding text and slot order: identical attestation.
    for row_id in (CATEGORY, AUDIENCE):
        assert _rows(first.state)[row_id]["resolved_prompt_template_hash"] == _rows(second.state)[row_id]["resolved_prompt_template_hash"]


# --------------------------------------------------------------------------- #
# Lead: wording edits reopen one card and keep the amendment
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_each_wording_edit_reopens_only_the_prompt_card_and_the_amendment_survives(
    service: SessionServiceImpl, tmp_path: Path
) -> None:
    session = await _approved_session(service, tmp_path)
    _assert_two_terms_resolved_as_the_user_left_them(session.state)
    original_rows = _rows(session.state)
    original_term_events = session.term_events()
    assert [(row["user_term"], row["choice"], row["accepted_value"], row["actor"]) for row in original_term_events] == [
        ("category", "accepted_as_drafted", CATEGORY_DRAFT, "user:alice"),
        ("audience", "amended", AUDIENCE_AMENDED, "user:alice"),
    ]
    assert _sent_prompt(session.state) == f"Classify {{{{ row.text }}}} into {CATEGORY_DRAFT} for {AUDIENCE_AMENDED}."

    for old, new in (("Classify", "Sort"), ("Sort", "Label"), ("Label", "Tag")):
        edited = _edit_part(session, old, new)
        assert edited.success, _errors(edited)
        expected_prompt = f"{new} {{{{ row.text }}}} into {CATEGORY_DRAFT} for {AUDIENCE_AMENDED}."

        opened = await session.commit(edited.updated_state)

        # Exactly one card per edit: the prompt card.
        assert _cards(opened) == [PROMPT_CARD]
        assert _cards(await session.pending()) == [PROMPT_CARD]
        # Both terms keep their rows unchanged: status, value, draft, event id, hash.
        rows = _rows(session.state)
        assert rows[CATEGORY] == original_rows[CATEGORY]
        assert rows[AUDIENCE] == original_rows[AUDIENCE]
        assert rows[PROMPT_REVIEW]["status"] == "pending"
        # The stored prompt and the card show the new wording with the accepted values at the slots.
        assert _prompt(session.state) == expected_prompt
        card = opened[0]
        assert card.llm_draft == _card_text(expected_prompt)
        snapshot = await service.get_prompt_review_snapshot(session.id, card.id)
        assert snapshot is not None
        surface = snapshot["authored_surface"]
        assert surface["prompt_template"] == expected_prompt
        assert surface["prompt_template_parts"] == deep_thaw(_options(session.state)["prompt_template_parts"])
        # The card links each slot to the ORIGINAL approval, so the slot reads as accepted.
        assert surface["interpretation_requirements"] == [
            {"id": CATEGORY, "event_id": original_rows[CATEGORY]["event_id"], "user_term": "category"},
            {"id": AUDIENCE, "event_id": original_rows[AUDIENCE]["event_id"], "user_term": "audience"},
        ]
        # Nothing recorded a new approval of either term at edit time.
        assert session.term_events() == original_term_events
        # While the prompt card is open: authoring-valid against the declaring sink, and the run is refused.
        validation = session.state.validate()
        assert validation.is_valid, [entry.to_dict() for entry in validation.errors]
        refusal = materialize_state_for_execution(session.state)
        assert isinstance(refusal, InterpretationReviewPending)
        assert [(site.kind, site.component_id) for site in refusal.sites] == [(InterpretationKind.LLM_PROMPT_TEMPLATE, NODE)]

        await session.approve_only_prompt_card(opened)

        assert await session.pending() == []
        assert _rows(session.state)[CATEGORY] == original_rows[CATEGORY]
        assert _rows(session.state)[AUDIENCE] == original_rows[AUDIENCE]
        # After approval the run is admitted and sends the amended definition.
        assert _sent_prompt(session.state) == expected_prompt
        assert session.state.validate().is_valid
        assert session.term_events() == original_term_events


@pytest.mark.asyncio
async def test_single_term_edit_reopens_one_card(service: SessionServiceImpl, tmp_path: Path) -> None:
    session = await _approved_session(service, tmp_path, parts=ONE_TERM_PARTS, rows=ONE_TERM_ROWS)
    original = _rows(session.state)[CATEGORY]
    assert original["status"] == "resolved"

    edited = _edit_part(session, "Classify", "Sort")
    assert edited.success, _errors(edited)
    opened = await session.commit(edited.updated_state)

    assert _cards(opened) == [PROMPT_CARD]
    assert _rows(session.state)[CATEGORY] == original
    assert opened[0].llm_draft == _card_text(f"Sort {{{{ row.text }}}} into {CATEGORY_DRAFT}.")


@pytest.mark.asyncio
async def test_parts_patch_that_preserves_refs_carries_like_a_substring_edit(service: SessionServiceImpl, tmp_path: Path) -> None:
    session = await _approved_session(service, tmp_path)
    original_rows = _rows(session.state)
    parts = deep_thaw(_options(session.state)["prompt_template_parts"])
    parts[0]["text"] = "Group {{ row.text }} into "
    parts[2]["text"] = ", written for "

    edited = _patch(session.state, {"prompt_template_parts": parts})
    assert edited.success, _errors(edited)
    opened = await session.commit(edited.updated_state)

    expected_prompt = f"Group {{{{ row.text }}}} into {CATEGORY_DRAFT}, written for {AUDIENCE_AMENDED}."
    assert _cards(opened) == [PROMPT_CARD]
    assert _rows(session.state)[CATEGORY] == original_rows[CATEGORY]
    assert _rows(session.state)[AUDIENCE] == original_rows[AUDIENCE]
    assert opened[0].llm_draft == _card_text(expected_prompt)
    await session.approve_only_prompt_card(opened)
    assert _sent_prompt(session.state) == expected_prompt


@pytest.mark.asyncio
async def test_multi_query_node_carries_and_its_card_ends_with_the_whole_shared_prompt(service: SessionServiceImpl, tmp_path: Path) -> None:
    """Every query uses the structured node-level template; the card lists the queries, then shows that template."""
    queries = {"first": {"input_fields": {"text": "text"}}, "second": {"input_fields": {"text": "text"}}}
    session = await _approved_session(service, tmp_path, extra_options={"queries": queries, "max_capacity_retry_seconds": 20})
    original_rows = _rows(session.state)

    edited = _edit_part(session, "Classify", "Sort")
    assert edited.success, _errors(edited)
    opened = await session.commit(edited.updated_state)

    expected_prompt = f"Sort {{{{ row.text }}}} into {CATEGORY_DRAFT} for {AUDIENCE_AMENDED}."
    assert _cards(opened) == [PROMPT_CARD]
    assert _rows(session.state)[CATEGORY] == original_rows[CATEGORY]
    assert _rows(session.state)[AUDIENCE] == original_rows[AUDIENCE]
    card = opened[0].llm_draft
    assert card is not None and card.startswith("Multi-query LLM node:")
    assert card.endswith(f"used by queries without their own template: first, second\n{expected_prompt}")


@pytest.mark.asyncio
async def test_five_edits_in_a_row_without_approval_between_stay_coherent(service: SessionServiceImpl, tmp_path: Path) -> None:
    session = await _approved_session(service, tmp_path)
    original_rows = _rows(session.state)
    original_term_events = session.term_events()
    opened: list[InterpretationEventRecord] = []
    words = ("Classify", "Sort", "Label", "Tag", "Bucket", "File")

    for old, new in pairwise(words):
        edited = _edit_part(session, old, new)
        assert edited.success, _errors(edited)
        opened = await session.commit(edited.updated_state)
        # One open card at a time: each edit's prompt card replaces the previous one.
        assert _cards(opened) == [PROMPT_CARD]
        assert _cards(await session.pending()) == [PROMPT_CARD]
        assert _rows(session.state)[CATEGORY] == original_rows[CATEGORY]
        assert _rows(session.state)[AUDIENCE] == original_rows[AUDIENCE]
        assert _prompt(session.state) == f"{new} {{{{ row.text }}}} into {CATEGORY_DRAFT} for {AUDIENCE_AMENDED}."
        assert session.state.validate().is_valid
        assert isinstance(materialize_state_for_execution(session.state), InterpretationReviewPending)

    expected_prompt = f"File {{{{ row.text }}}} into {CATEGORY_DRAFT} for {AUDIENCE_AMENDED}."
    assert opened[0].llm_draft == _card_text(expected_prompt)
    await session.approve_only_prompt_card(opened)
    assert _sent_prompt(session.state) == expected_prompt
    assert session.term_events() == original_term_events
    # And the loop still works afterwards.
    again = _edit_part(session, "File", "Classify")
    assert again.success, _errors(again)
    assert _cards(await session.commit(again.updated_state)) == [PROMPT_CARD]
    _assert_two_terms_resolved_as_the_user_left_them(session.state)


# --------------------------------------------------------------------------- #
# What is carried, and what is not
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_carry_never_upgrades_a_pending_term(service: SessionServiceImpl, tmp_path: Path) -> None:
    session = await _approved_session(service, tmp_path, leave_pending=("audience",))
    before = _rows(session.state)
    assert (before[CATEGORY]["status"], before[AUDIENCE]["status"]) == ("resolved", "pending")

    edited = _edit_part(session, "Classify", "Sort")
    assert edited.success, _errors(edited)
    await session.commit(edited.updated_state)

    after = _rows(session.state)
    assert after[CATEGORY] == before[CATEGORY]
    assert after[AUDIENCE] == before[AUDIENCE]
    assert after[AUDIENCE]["status"] == "pending" and after[AUDIENCE]["accepted_value"] is None
    assert _prompt(session.state) == f"Sort {{{{ row.text }}}} into {CATEGORY_DRAFT} for {PENDING_SLOT}."
    assert _cards(await session.pending()) == [PROMPT_CARD, ("vague_term", "audience")]


@pytest.mark.asyncio
async def test_a_moved_slot_with_the_same_ref_is_carried(service: SessionServiceImpl, tmp_path: Path) -> None:
    session = await _approved_session(service, tmp_path)
    original_rows = _rows(session.state)
    moved = [
        {"kind": "text", "text": "For "},
        {"kind": "interpretation_ref", "requirement_id": AUDIENCE},
        {"kind": "text", "text": ", classify {{ row.text }} into "},
        {"kind": "interpretation_ref", "requirement_id": CATEGORY},
        {"kind": "text", "text": "."},
    ]

    edited = _patch(session.state, {"prompt_template_parts": moved})
    assert edited.success, _errors(edited)
    opened = await session.commit(edited.updated_state)

    assert _cards(opened) == [PROMPT_CARD]
    assert _rows(session.state)[CATEGORY] == original_rows[CATEGORY]
    assert _rows(session.state)[AUDIENCE] == original_rows[AUDIENCE]
    assert opened[0].llm_draft == _card_text(f"For {AUDIENCE_AMENDED}, classify {{{{ row.text }}}} into {CATEGORY_DRAFT}.")


@pytest.mark.asyncio
async def test_a_duplicated_slot_with_the_same_ref_is_carried(service: SessionServiceImpl, tmp_path: Path) -> None:
    session = await _approved_session(service, tmp_path)
    original_rows = _rows(session.state)
    duplicated = [
        *TWO_TERM_PARTS,
        {"kind": "text", "text": " Explain the choice to "},
        {"kind": "interpretation_ref", "requirement_id": AUDIENCE},
        {"kind": "text", "text": "."},
    ]

    edited = _patch(session.state, {"prompt_template_parts": duplicated})
    assert edited.success, _errors(edited)
    opened = await session.commit(edited.updated_state)

    assert _cards(opened) == [PROMPT_CARD]
    assert _rows(session.state)[AUDIENCE] == original_rows[AUDIENCE]
    expected_prompt = f"Classify {{{{ row.text }}}} into {CATEGORY_DRAFT} for {AUDIENCE_AMENDED}. Explain the choice to {AUDIENCE_AMENDED}."
    assert opened[0].llm_draft == _card_text(expected_prompt)
    await session.approve_only_prompt_card(opened)
    assert _sent_prompt(session.state) == expected_prompt


@pytest.mark.asyncio
async def test_removing_a_slot_but_keeping_its_row_is_refused(service: SessionServiceImpl, tmp_path: Path) -> None:
    session = await _approved_session(service, tmp_path)

    refused = _patch(session.state, {"prompt_template_parts": ONE_TERM_PARTS})

    assert not refused.success and refused.updated_state is session.state
    assert [entry.error_code for entry in refused.validation.errors] == ["review_reconciliation_failed"]


@pytest.mark.asyncio
async def test_a_removed_term_is_gone_and_comes_back_pending_with_the_authored_draft(service: SessionServiceImpl, tmp_path: Path) -> None:
    session = await _approved_session(service, tmp_path)
    original_rows = _rows(session.state)

    removed = _patch(
        session.state,
        {
            "prompt_template_parts": _with_first_text(ONE_TERM_PARTS, "Sort {{ row.text }} into "),
            "interpretation_requirements": ONE_TERM_ROWS,
        },
    )
    assert removed.success, _errors(removed)
    opened = await session.commit(removed.updated_state)
    assert _cards(opened) == [PROMPT_CARD]
    rows = _rows(session.state)
    assert AUDIENCE not in rows
    # The term that stayed is carried across the same edit.
    assert rows[CATEGORY] == original_rows[CATEGORY]

    restored = _patch(session.state, {"prompt_template_parts": TWO_TERM_PARTS, "interpretation_requirements": TWO_TERM_ROWS})
    assert restored.success, _errors(restored)
    opened = await session.commit(restored.updated_state)
    row = _rows(session.state)[AUDIENCE]
    assert (row["status"], row["accepted_value"], row["draft"], row["event_id"]) == ("pending", None, AUDIENCE_DRAFT, None)
    assert next(event for event in opened if event.user_term == "audience").llm_draft == AUDIENCE_DRAFT


@pytest.mark.asyncio
async def test_a_renamed_term_is_a_new_pending_row(service: SessionServiceImpl, tmp_path: Path) -> None:
    session = await _approved_session(service, tmp_path)
    original_rows = _rows(session.state)
    renamed_id = f"reader:{NODE}"
    parts = [*TWO_TERM_PARTS[:3], {"kind": "interpretation_ref", "requirement_id": renamed_id}, TWO_TERM_PARTS[4]]
    rows = [TWO_TERM_ROWS[0], {"kind": "vague_term", "user_term": "reader", "draft": AUDIENCE_DRAFT}]

    edited = _patch(session.state, {"prompt_template_parts": parts, "interpretation_requirements": rows})
    assert edited.success, _errors(edited)
    await session.commit(edited.updated_state)

    after = _rows(session.state)
    assert AUDIENCE not in after
    assert (after[renamed_id]["status"], after[renamed_id]["accepted_value"], after[renamed_id]["event_id"]) == ("pending", None, None)
    assert after[CATEGORY] == original_rows[CATEGORY]
    assert AUDIENCE_AMENDED not in _prompt(session.state)


@pytest.mark.asyncio
async def test_a_ref_pointed_at_an_unknown_requirement_is_refused(service: SessionServiceImpl, tmp_path: Path) -> None:
    session = await _approved_session(service, tmp_path)
    parts = [*TWO_TERM_PARTS[:3], {"kind": "interpretation_ref", "requirement_id": f"reader:{NODE}"}, TWO_TERM_PARTS[4]]

    refused = _patch(session.state, {"prompt_template_parts": parts})

    assert not refused.success and refused.updated_state is session.state
    assert [entry.error_code for entry in refused.validation.errors] == ["review_reconciliation_failed"]


@pytest.mark.asyncio
async def test_a_changed_draft_with_a_wording_edit_reopens_that_term_only(service: SessionServiceImpl, tmp_path: Path) -> None:
    session = await _approved_session(service, tmp_path)
    original_rows = _rows(session.state)
    rows = [{"kind": "vague_term", "user_term": "category", "draft": "narrow billing categories"}, TWO_TERM_ROWS[1]]

    edited = _patch(
        session.state,
        {"prompt_template_parts": _with_first_text(TWO_TERM_PARTS, "Sort {{ row.text }} into "), "interpretation_requirements": rows},
    )
    assert edited.success, _errors(edited)
    opened = await session.commit(edited.updated_state)

    category = _rows(session.state)[CATEGORY]
    assert (category["status"], category["accepted_value"], category["draft"], category["event_id"]) == (
        "pending",
        None,
        "narrow billing categories",
        None,
    )
    assert _rows(session.state)[AUDIENCE] == original_rows[AUDIENCE]
    assert _cards(opened) == [PROMPT_CARD, ("vague_term", "category")]
    assert _prompt(session.state) == f"Sort {{{{ row.text }}}} into {PENDING_SLOT} for {AUDIENCE_AMENDED}."


@pytest.mark.parametrize(
    ("damage", "code"),
    [
        ("value", "review_reconciliation_failed"),
        ("hash", "review_reconciliation_failed"),
        ("stored_prompt", "review_reconciliation_failed"),
        ("event_id", "interpretation_requirements_invalid"),
    ],
)
@pytest.mark.asyncio
async def test_altered_or_missing_stored_evidence_refuses_the_edit(
    service: SessionServiceImpl, tmp_path: Path, damage: str, code: str
) -> None:
    session = await _approved_session(service, tmp_path)
    node = next(node for node in session.state.nodes if node.id == NODE)
    options = deep_thaw(node.options)
    audience = next(row for row in options[INTERPRETATION_REQUIREMENTS_KEY] if row["id"] == AUDIENCE)
    if damage == "value":
        audience["accepted_value"] = "everyone on the internet"
    elif damage == "hash":
        audience["resolved_prompt_template_hash"] = stable_hash("something else")
    elif damage == "event_id":
        audience["event_id"] = None
    else:
        options["prompt_template"] = options["prompt_template"].replace(AUDIENCE_AMENDED, "everyone on the internet")
    damaged = session.state.with_node(replace(node, options=options))

    refused = _patch(damaged, {"prompt_template_parts": _with_first_text(TWO_TERM_PARTS, "Sort {{ row.text }} into ")})

    assert not refused.success and refused.updated_state is damaged
    assert refused.validation.errors[0].error_code == code


# --------------------------------------------------------------------------- #
# The bounded prompt card decides the carry
# --------------------------------------------------------------------------- #


def _card_budget(*, system_prompt: bool) -> int:
    """Characters of prompt template the bounded card shows in full."""
    if not system_prompt:
        return PROMPT_SURFACE_REVIEW_MAX_CHARS
    return (PROMPT_SURFACE_REVIEW_MAX_CHARS - len("System prompt:\n\n\nPrompt template:\n")) // 2


def _padded_first_text(*, rendered_length: int) -> str:
    """First text part that makes the two-term prompt, with its accepted values, exactly ``rendered_length`` chars."""
    head, tail = "Sort {{ row.text }} ", " into "
    rest = f"{CATEGORY_DRAFT} for {AUDIENCE_AMENDED}."
    padding = rendered_length - len(head) - len(tail) - len(rest)
    assert padding > 0
    return head + "x" * padding + tail


@pytest.mark.parametrize("system_prompt", [True, False])
@pytest.mark.asyncio
async def test_prompt_that_fills_the_card_exactly_is_carried_and_shown_whole(
    service: SessionServiceImpl, tmp_path: Path, system_prompt: bool
) -> None:
    session = await _approved_session(service, tmp_path, system_prompt=system_prompt)
    original_rows = _rows(session.state)
    budget = _card_budget(system_prompt=system_prompt)

    edited = _patch(session.state, {"prompt_template_parts": _with_first_text(TWO_TERM_PARTS, _padded_first_text(rendered_length=budget))})
    assert edited.success, _errors(edited)
    opened = await session.commit(edited.updated_state)

    prompt = _prompt(session.state)
    assert len(prompt) == budget
    assert prompt.endswith(f" into {CATEGORY_DRAFT} for {AUDIENCE_AMENDED}.")
    assert _cards(opened) == [PROMPT_CARD]
    assert _rows(session.state)[CATEGORY] == original_rows[CATEGORY]
    assert _rows(session.state)[AUDIENCE] == original_rows[AUDIENCE]
    card = opened[0].llm_draft
    assert card is not None and len(card) <= PROMPT_SURFACE_REVIEW_MAX_CHARS
    assert card == _card_text(prompt, system_prompt=system_prompt)
    assert "more chars not shown" not in card


@pytest.mark.parametrize("system_prompt", [True, False])
@pytest.mark.asyncio
async def test_prompt_one_char_past_the_card_is_not_carried(service: SessionServiceImpl, tmp_path: Path, system_prompt: bool) -> None:
    """The card would cut the text, so the terms reopen as before; the card is not widened."""
    session = await _approved_session(service, tmp_path, system_prompt=system_prompt)
    budget = _card_budget(system_prompt=system_prompt)

    edited = _patch(
        session.state, {"prompt_template_parts": _with_first_text(TWO_TERM_PARTS, _padded_first_text(rendered_length=budget + 1))}
    )
    assert edited.success, _errors(edited)
    opened = await session.commit(edited.updated_state)

    rows = _rows(session.state)
    for row_id, draft in ((CATEGORY, CATEGORY_DRAFT), (AUDIENCE, AUDIENCE_DRAFT)):
        row = rows[row_id]
        assert (row["status"], row["accepted_value"], row["event_id"], row["draft"]) == ("pending", None, None, draft)
    assert _cards(opened) == [PROMPT_CARD, ("vague_term", "audience"), ("vague_term", "category")]
    assert HASH_KEY not in _options(session.state)
    card = next(event for event in opened if event.kind is InterpretationKind.LLM_PROMPT_TEMPLATE).llm_draft
    assert card is not None and len(card) <= PROMPT_SURFACE_REVIEW_MAX_CHARS
    assert AUDIENCE_AMENDED not in card


# --------------------------------------------------------------------------- #
# What the model reads after a carried edit, and what happens when it writes that back
# --------------------------------------------------------------------------- #


async def _session_after_one_carried_edit(service: SessionServiceImpl, tmp_path: Path) -> _Session:
    session = await _approved_session(service, tmp_path)
    edited = _edit_part(session, "Classify", "Sort")
    assert edited.success, _errors(edited)
    # The edit's own result carries no requirement rows and no prompt text.
    assert set(edited.data) == {"target", "changed", "replacement_count", "length_chars", "size_bytes", "sha256"}
    payload = json.dumps(deep_thaw(edited.to_dict()), default=str)
    assert "interpretation_requirements" not in payload and AUDIENCE_AMENDED not in payload
    await session.commit(edited.updated_state)
    return session


def _state_context_node(state: CompositionState) -> dict[str, Any]:
    catalog = _mock_catalog()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    policy = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    context = json.loads(build_context_string(state, policy, plugin_snapshot=snapshot).split("\n", 1)[1])
    node: dict[str, Any] = context["current_state"]["nodes"][0]
    return node


def _read(state: CompositionState, component: str) -> Any:
    result = execute_tool("get_pipeline_state", {"component": component}, state, _mock_catalog())
    assert result.success
    return deep_thaw(result.data)


@pytest.mark.asyncio
async def test_reads_after_a_carried_edit_show_the_term_resolved(service: SessionServiceImpl, tmp_path: Path) -> None:
    session = await _session_after_one_carried_edit(service, tmp_path)
    stored = _rows(session.state)
    expected_prompt = f"Sort {{{{ row.text }}}} into {CATEGORY_DRAFT} for {AUDIENCE_AMENDED}."

    # get_pipeline_state for the node (and the full state) returns the stored rows as they are.
    for options in (_read(session.state, NODE)["node"]["options"], _read(session.state, "full")["nodes"][0]["options"]):
        assert options["prompt_template"] == expected_prompt
        read_rows = {row["id"]: row for row in options[INTERPRETATION_REQUIREMENTS_KEY]}
        assert read_rows[AUDIENCE] == stored[AUDIENCE]
        assert read_rows[CATEGORY] == stored[CATEGORY]
        assert read_rows[PROMPT_REVIEW]["status"] == "pending"

    # The per-turn state context shows the term resolved, with its draft marked historical.
    context_options = _state_context_node(session.state)["options"]
    assert context_options["prompt_template"] == expected_prompt
    context_rows = {row["id"]: row for row in context_options[INTERPRETATION_REQUIREMENTS_KEY]}
    assert context_rows[AUDIENCE] == {
        "id": AUDIENCE,
        "kind": "vague_term",
        "user_term": "audience",
        "historical_draft": AUDIENCE_DRAFT,
        "status": "resolved",
    }

    # The set_pipeline replay form is the one read that shows author shells: no status,
    # the model's draft, and the pending wording in every slot.
    replay_options = _read(session.state, "set_pipeline_arguments")["nodes"][0]["options"]
    assert replay_options["prompt_template"] == f"Sort {{{{ row.text }}}} into {PENDING_SLOT} for {PENDING_SLOT}."
    assert replay_options[INTERPRETATION_REQUIREMENTS_KEY][:2] == [
        {"kind": "vague_term", "user_term": "category", "draft": CATEGORY_DRAFT},
        {"kind": "vague_term", "user_term": "audience", "draft": AUDIENCE_DRAFT},
    ]


@pytest.mark.parametrize("wording_edit", [False, True])
@pytest.mark.parametrize("form", ["node_read", "state_context", "author_shells", "set_pipeline_replay", "upsert_node_replay"])
@pytest.mark.asyncio
async def test_writing_a_read_form_back_keeps_the_carried_amendment(
    service: SessionServiceImpl, tmp_path: Path, form: str, wording_edit: bool
) -> None:
    session = await _session_after_one_carried_edit(service, tmp_path)
    stored = _rows(session.state)
    first_text = "Group {{ row.text }} into " if wording_edit else "Sort {{ row.text }} into "
    catalog = _mock_catalog()

    if form in ("node_read", "state_context", "author_shells"):
        if form == "node_read":
            source = _read(session.state, NODE)["node"]["options"]
        elif form == "state_context":
            source = _state_context_node(session.state)["options"]
        else:
            source = _read(session.state, "set_pipeline_arguments")["nodes"][0]["options"]
        result = _patch(
            session.state,
            {
                INTERPRETATION_REQUIREMENTS_KEY: source[INTERPRETATION_REQUIREMENTS_KEY],
                "prompt_template_parts": _with_first_text(source["prompt_template_parts"], first_text),
            },
        )
    else:
        arguments = _read(session.state, "set_pipeline_arguments")
        node = arguments["nodes"][0]
        node["options"]["prompt_template_parts"] = _with_first_text(node["options"]["prompt_template_parts"], first_text)
        if form == "set_pipeline_replay":
            result = execute_tool("set_pipeline", arguments, session.state, catalog)
        else:
            result = execute_tool("upsert_node", node, session.state, catalog)

    assert result.success, _errors(result)
    opened = await session.commit(result.updated_state)
    after = _rows(session.state)
    assert after[AUDIENCE] == stored[AUDIENCE]
    assert after[CATEGORY] == stored[CATEGORY]
    assert _prompt(session.state) == f"{first_text}{CATEGORY_DRAFT} for {AUDIENCE_AMENDED}."
    assert "vague_term" not in [kind for kind, _term in _cards(opened)]
    assert _cards(await session.pending()) == [PROMPT_CARD]


@pytest.mark.parametrize(("term", "row_id"), [("category", CATEGORY), ("audience", AUDIENCE)])
@pytest.mark.parametrize("draft", ["omitted", "same", "different"])
@pytest.mark.asyncio
async def test_re_requesting_review_of_a_carried_term_is_refused_and_changes_nothing(
    service: SessionServiceImpl, tmp_path: Path, term: str, row_id: str, draft: str
) -> None:
    """The live model re-requested each term after every edit, sending ``llm_draft: null``.

    The strict wire strips a null at an optional position, so that call reaches
    the handler with ``llm_draft`` omitted. A resolved term is not a pending
    review site: the request is an argument error, no card opens, and the row
    keeps its value. A different draft is refused the same way.
    """
    session = await _session_after_one_carried_edit(service, tmp_path)
    stored = _rows(session.state)
    pending_before = _cards(await session.pending())
    events_before = session.term_events()
    arguments = {"affected_node_id": NODE, "kind": "vague_term", "user_term": term}
    if draft == "same":
        arguments["llm_draft"] = stored[row_id]["draft"]
    elif draft == "different":
        arguments["llm_draft"] = "a different definition the model proposes now"

    with pytest.raises(ToolArgumentError) as refused:
        await _handle_request_interpretation_review(
            arguments,
            session.state,
            session_id=session.id,
            composition_state_id=UUID(session.state_id),
            tool_call_id=f"re-request-{term}-{draft}",
            now=datetime.now(UTC),
            per_term_cap=3,
            per_session_day_cap=10,
            create_pending_interpretation_event=service.create_pending_interpretation_event,
            list_interpretation_events=service.list_interpretation_events,
            **PROVENANCE,
        )

    assert "pending vague_term interpretation requirement" in str(refused.value)
    await session.reload()
    assert _rows(session.state)[row_id] == stored[row_id]
    assert _cards(await session.pending()) == pending_before == [PROMPT_CARD]
    assert session.term_events() == events_before
