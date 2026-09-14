"""The llm_prompt_template event draft is the ONE review derivation, and it is bounded.

``interpretation_state.prompt_review_draft_from_options`` is the single
derivation of the text an operator reviews for an LLM node's prompt:

* multi-query node -> the rendered prompt SURFACE (system prompt, every query
  template, node-level template with its in-use status). The event writer used
  to demand ``llm_draft == options.prompt_template`` instead, so a card
  carrying the staged surface was refused and only the dead node-level
  template could be persisted as the review draft.
* single-prompt node -> ``prompt_template`` itself, shortened with an inline
  marker when it is longer than ``PROMPT_SURFACE_REVIEW_MAX_CHARS``. Before the
  bound, a 9000-char template produced a 9000-char draft; the
  ``InterpretationEventResponse.llm_draft`` wire field caps at 8192, so the
  projection raised and ``GET /interpretations`` failed for the whole session.

The anchor and the node-level ``resolved_prompt_template_hash`` always cover
the complete text; only the displayed draft is bounded.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy.pool import StaticPool

from elspeth.contracts.composer_interpretation import (
    InterpretationChoice,
    InterpretationEventRecord,
    InterpretationKind,
)
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata
from elspeth.web.composer.tools._common import _options_with_default_prompt_template_review
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    PROMPT_SURFACE_REVIEW_MAX_CHARS,
    prompt_review_anchor_hash_from_options,
    prompt_review_draft_from_options,
)
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.protocol import InterpretationDraftMismatchError
from elspeth.web.sessions.routes._helpers import _interpretation_event_response
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.unit.web.sessions.guided_test_authority import DualFencedSessionServiceHarness
from tests.unit.web.sessions.test_interpretation_events_service import _seed_state_with_llm_node

_NODE_ID = "pair_colours"
_USER_TERM = f"llm_prompt_template:{_NODE_ID}"
_LONG_TEMPLATE_CHARS = 9000


@pytest.fixture
def session_service() -> SessionServiceImpl:
    engine = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(engine)
    return DualFencedSessionServiceHarness(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test"),
    )


@asynccontextmanager
async def _compose_operation(service: SessionServiceImpl, session_id: UUID) -> AsyncIterator[SessionOperationContext]:
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


def _multi_query_options() -> dict[str, Any]:
    return {
        "prompt_template": "Answer the question about the colour {{ row.colour }} in one short reply.",
        "system_prompt": "Reply with only the value asked for and nothing else.",
        "queries": {
            "good_pair": {
                "input_fields": {"colour": "colour"},
                "template": "What is a good colour pair for {{ row.colour }}? Reply with the single colour name only.",
            },
            "hex_code": {
                "input_fields": {"colour": "colour"},
                "template": "What is the approximate hex code of the colour {{ row.colour }}?",
            },
        },
        "required_input_fields": ["colour"],
    }


def _long_single_prompt_options() -> dict[str, Any]:
    head = "Classify the sentiment of {{ row.text }} as positive, negative or neutral. "
    body = "Consider tone, context and intent carefully before answering. "
    template = (head + body * (_LONG_TEMPLATE_CHARS // len(body) + 1))[:_LONG_TEMPLATE_CHARS]
    assert len(template) == _LONG_TEMPLATE_CHARS
    return {"prompt_template": template}


def _review_node(options: Mapping[str, Any], *, requirement_draft: str) -> dict[str, Any]:
    staged = dict(options)
    staged[INTERPRETATION_REQUIREMENTS_KEY] = [
        {
            "id": "prompt_template_review",
            "kind": InterpretationKind.LLM_PROMPT_TEMPLATE.value,
            "user_term": _USER_TERM,
            "status": "pending",
            "draft": requirement_draft,
            "event_id": None,
            "accepted_value": None,
            "accepted_artifact_hash": None,
            "resolved_prompt_template_hash": None,
        }
    ]
    state = CompositionState(
        source=None,
        nodes=(
            NodeSpec(
                id=_NODE_ID,
                node_type="transform",
                plugin="llm",
                input="rows",
                on_success="out",
                on_error="quarantine",
                options=staged,
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(name="Prompt review draft", description=""),
        version=1,
    )
    node: dict[str, Any] = state.to_dict()["nodes"][0]
    return node


async def _create_event(
    service: SessionServiceImpl,
    *,
    options: Mapping[str, Any],
    llm_draft: str,
) -> tuple[UUID, InterpretationEventRecord]:
    session_id = uuid4()
    review_draft = prompt_review_draft_from_options(options)
    assert review_draft is not None
    state = await _seed_state_with_llm_node(
        service,
        session_id=session_id,
        node=_review_node(options, requirement_draft=review_draft),
    )
    async with _compose_operation(service, session_id) as context:
        event = await service.create_pending_interpretation_event(
            session_id=session_id,
            composition_state_id=state.id,
            affected_node_id=_NODE_ID,
            tool_call_id="call_prompt_review",
            user_term=_USER_TERM,
            kind=InterpretationKind.LLM_PROMPT_TEMPLATE,
            llm_draft=llm_draft,
            model_identifier="anthropic/claude-opus-4-7",
            model_version="2026-05-01",
            provider="anthropic",
            composer_skill_hash="a" * 64,
            session_operation_context=context,
        )
    return session_id, event


# --------------------------------------------------------------------------- #
# (1) the writer admits the surface draft and refuses the dead node template
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_writer_accepts_a_multi_query_event_whose_draft_is_the_prompt_surface(session_service) -> None:
    options = _multi_query_options()
    surface_draft = prompt_review_draft_from_options(options)
    assert surface_draft is not None
    # Instrument control: the two candidate drafts genuinely differ for this shape.
    assert surface_draft != options["prompt_template"]

    _session_id, event = await _create_event(session_service, options=options, llm_draft=surface_draft)

    assert event.llm_draft == surface_draft
    assert event.choice is InterpretationChoice.PENDING


@pytest.mark.asyncio
async def test_writer_rejects_a_multi_query_event_whose_draft_is_the_dead_node_template(session_service) -> None:
    options = _multi_query_options()

    with pytest.raises(InterpretationDraftMismatchError, match="llm_prompt_template event draft"):
        await _create_event(session_service, options=options, llm_draft=options["prompt_template"])


@pytest.mark.asyncio
async def test_writer_still_accepts_a_single_prompt_event_whose_draft_is_the_template(session_service) -> None:
    options = {"prompt_template": "Read {{ row.html }} and return JSON."}
    assert prompt_review_draft_from_options(options) == options["prompt_template"]

    _session_id, event = await _create_event(session_service, options=options, llm_draft=options["prompt_template"])

    assert event.llm_draft == options["prompt_template"]


@pytest.mark.asyncio
async def test_writer_rejects_a_long_single_prompt_event_carrying_the_unbounded_template(session_service) -> None:
    options = _long_single_prompt_options()

    with pytest.raises(InterpretationDraftMismatchError, match="llm_prompt_template event draft"):
        await _create_event(session_service, options=options, llm_draft=options["prompt_template"])


# --------------------------------------------------------------------------- #
# (2) accepted_as_drafted resolves; the runtime contract is untouched
# --------------------------------------------------------------------------- #


def _resolved_node(nodes: Any) -> Mapping[str, Any]:
    matching = [node for node in nodes if node["id"] == _NODE_ID]
    assert len(matching) == 1
    node: Mapping[str, Any] = matching[0]
    return node


@pytest.mark.asyncio
async def test_resolve_accepted_as_drafted_succeeds_for_a_multi_query_node(session_service) -> None:
    options = _multi_query_options()
    surface_draft = prompt_review_draft_from_options(options)
    assert surface_draft is not None
    session_id, event = await _create_event(session_service, options=options, llm_draft=surface_draft)

    resolved, new_state = await session_service.resolve_interpretation_event(
        session_id=session_id,
        event_id=event.id,
        choice=InterpretationChoice.ACCEPTED_AS_DRAFTED,
        amended_value=None,
        actor="user:alice",
    )

    assert resolved.accepted_value == surface_draft
    node = _resolved_node(new_state.nodes)
    # accepted_value is never written into the prompt; the runtime hash covers the real template.
    assert node["options"]["prompt_template"] == options["prompt_template"]
    assert node["options"]["resolved_prompt_template_hash"] == stable_hash(options["prompt_template"])
    requirement = node["options"][INTERPRETATION_REQUIREMENTS_KEY][0]
    assert requirement["status"] == "resolved"
    assert requirement["accepted_value"] == surface_draft
    assert requirement["resolved_prompt_template_hash"] == prompt_review_anchor_hash_from_options(options)


@pytest.mark.asyncio
async def test_resolve_accepted_as_drafted_succeeds_for_a_long_single_prompt_node(session_service) -> None:
    options = _long_single_prompt_options()
    bounded_draft = prompt_review_draft_from_options(options)
    assert bounded_draft is not None
    assert len(bounded_draft) <= PROMPT_SURFACE_REVIEW_MAX_CHARS
    session_id, event = await _create_event(session_service, options=options, llm_draft=bounded_draft)

    resolved, new_state = await session_service.resolve_interpretation_event(
        session_id=session_id,
        event_id=event.id,
        choice=InterpretationChoice.ACCEPTED_AS_DRAFTED,
        amended_value=None,
        actor="user:alice",
    )

    assert resolved.accepted_value == bounded_draft
    node = _resolved_node(new_state.nodes)
    assert node["options"]["prompt_template"] == options["prompt_template"]
    full_template_hash = stable_hash(options["prompt_template"])
    assert node["options"]["resolved_prompt_template_hash"] == full_template_hash
    requirement = node["options"][INTERPRETATION_REQUIREMENTS_KEY][0]
    assert requirement["accepted_value"] == bounded_draft
    # The anchor covers the complete template, never the shortened display.
    assert requirement["resolved_prompt_template_hash"] == full_template_hash
    assert resolved.resolved_prompt_template_hash == full_template_hash


# --------------------------------------------------------------------------- #
# (3) the wire projection builds from the bounded draft
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_interpretation_event_response_builds_for_a_long_single_prompt_node(session_service) -> None:
    options = _long_single_prompt_options()
    review_draft = prompt_review_draft_from_options(options)
    assert review_draft is not None
    session_id, event = await _create_event(session_service, options=options, llm_draft=review_draft)

    pending_response = _interpretation_event_response(event)
    assert pending_response.llm_draft == review_draft

    resolved, _new_state = await session_service.resolve_interpretation_event(
        session_id=session_id,
        event_id=event.id,
        choice=InterpretationChoice.ACCEPTED_AS_DRAFTED,
        amended_value=None,
        actor="user:alice",
    )
    resolved_response = _interpretation_event_response(resolved)
    assert resolved_response.accepted_value == review_draft


# --------------------------------------------------------------------------- #
# (4) the single-prompt derivation is bounded; drafts within the bound are byte-identical
# --------------------------------------------------------------------------- #


def test_single_prompt_draft_within_the_bound_is_the_template_byte_for_byte() -> None:
    pinned = "Read {{ row.html }} and return JSON."
    assert prompt_review_draft_from_options({"prompt_template": pinned}) == "Read {{ row.html }} and return JSON."
    at_bound = "x" * PROMPT_SURFACE_REVIEW_MAX_CHARS
    assert prompt_review_draft_from_options({"prompt_template": at_bound}) == at_bound
    assert PROMPT_SURFACE_REVIEW_MAX_CHARS == 8000


def test_single_prompt_draft_over_the_bound_is_shortened_with_the_inline_marker() -> None:
    options = _long_single_prompt_options()
    template = options["prompt_template"]

    draft = prompt_review_draft_from_options(options)

    assert draft is not None
    assert len(draft) <= PROMPT_SURFACE_REVIEW_MAX_CHARS
    marker_start = draft.index(" […")
    kept = draft[:marker_start]
    assert template.startswith(kept)
    assert draft[marker_start:] == f" […{len(template) - len(kept)} more chars not shown; the review attests the full text]"
    # The anchor is unchanged by the bound: an unstructured single-prompt node still anchors to the full text.
    assert prompt_review_anchor_hash_from_options(options) is None


def test_single_prompt_draft_one_char_over_the_bound_is_shortened() -> None:
    template = "y" * (PROMPT_SURFACE_REVIEW_MAX_CHARS + 1)
    draft = prompt_review_draft_from_options({"prompt_template": template})
    assert draft is not None
    assert draft != template
    assert len(draft) <= PROMPT_SURFACE_REVIEW_MAX_CHARS
    assert draft.endswith("more chars not shown; the review attests the full text]")


def test_default_prompt_review_staging_stays_idempotent_for_a_long_single_prompt() -> None:
    options = _long_single_prompt_options()
    first = _options_with_default_prompt_template_review(node_id=_NODE_ID, plugin="llm", options=options)
    second = _options_with_default_prompt_template_review(node_id=_NODE_ID, plugin="llm", options=first, existing_options=first)

    first_requirements = first[INTERPRETATION_REQUIREMENTS_KEY]
    assert len(first_requirements) == 1
    assert first_requirements[0]["draft"] == prompt_review_draft_from_options(options)
    assert second[INTERPRETATION_REQUIREMENTS_KEY] == first_requirements
