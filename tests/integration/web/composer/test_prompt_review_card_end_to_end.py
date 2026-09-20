"""Prompt-review cards survive every seam from staging to execution, over a real sessions DB.

Each lane that hardened the ``llm_prompt_template`` review pinned its own
seam in isolation: the auto-stager's draft, the backend surfacer's draft, the
pending-event writer's draft check, the resolve anchor, and the execution
materializer. This module drives ONE state through all of them in order and
checks that they agree:

1. the real auto-stager (``tools._common._options_with_default_prompt_template_review``)
   stages the pending requirement on an LLM node;
2. the state is persisted under a real COMPOSE lease;
3. the real surfacer (``composer.service.surface_pending_interpretation_reviews_for_state``,
   which derives the card through ``_backend_surface_args_for_site``) writes the
   pending event through the strict writer boundary;
4. the operator accepts the card as drafted (``resolve_interpretation_event``);
5. ``materialize_state_for_execution`` over the resolved persisted state
   returns a ``CompositionState``, not ``InterpretationReviewPending``.

Two shapes:

* a multi-query node, whose card must carry the rendered prompt SURFACE, not
  the node-level ``prompt_template`` the multi-query path never sends alone;
* a 9000-char single-prompt node, whose card draft is bounded to
  ``PROMPT_SURFACE_REVIEW_MAX_CHARS`` while the anchor covers the full text.

Out of scope here: inline blob markers in prompt fields. Those create no
review site when no review exists (user-uploaded blob prompts, ADR-034), and
LLM-authored blobs are refused at the wire tool and at run admission; their
coverage lives with those lanes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

import pytest
import structlog
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from elspeth.contracts.composer_interpretation import (
    InterpretationChoice,
    InterpretationEventRecord,
    InterpretationKind,
    InterpretationSurfaceOrigin,
)
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer.service import surface_pending_interpretation_reviews_for_state
from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata
from elspeth.web.composer.tools._common import _options_with_default_prompt_template_review
from elspeth.web.coordination.contracts import SessionOperationKind
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    PROMPT_SURFACE_REVIEW_MAX_CHARS,
    InterpretationReviewPending,
    approved_prompt_artifact_hash_from_options,
    materialize_state_for_execution,
    prompt_review_anchor_hash_from_options,
    prompt_review_draft_from_options,
)
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.protocol import CompositionStateData, CompositionStateRecord
from elspeth.web.sessions.routes._helpers import _interpretation_event_response
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity
from tests.integration.web.conftest import _save_composition_state_with_compose_authority
from tests.unit.web.sessions.guided_test_authority import DualFencedSessionServiceHarness

_NODE_ID = "pair_colours"
_PROMPT_TERM = f"llm_prompt_template:{_NODE_ID}"
_LONG_TEMPLATE_CHARS = 9000
# The InterpretationEventResponse.llm_draft wire bound the draft must fit.
_WIRE_DRAFT_MAX_CHARS = 8192


@pytest.fixture
def engine() -> Engine:
    eng = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(eng)
    with eng.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    return eng


@pytest.fixture
def sessions_service(engine: Engine) -> SessionServiceImpl:
    return DualFencedSessionServiceHarness(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.prompt_review_card_end_to_end"),
    )


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


def _state_with_node_options(options: Mapping[str, Any], *, version: int = 1) -> CompositionState:
    return CompositionState(
        source=None,
        nodes=(
            NodeSpec(
                id=_NODE_ID,
                node_type="transform",
                plugin="llm",
                input="rows",
                on_success="out",
                on_error="discard",
                options=options,
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
        metadata=PipelineMetadata(name="Prompt review card end to end", description=""),
        version=version,
    )


def _node_options(state: CompositionState) -> dict[str, Any]:
    nodes = state.to_dict()["nodes"]
    assert len(nodes) == 1
    options: dict[str, Any] = nodes[0]["options"]
    return options


def _prompt_requirement(options: Mapping[str, Any]) -> Mapping[str, Any]:
    rows = [row for row in options[INTERPRETATION_REQUIREMENTS_KEY] if row["kind"] == InterpretationKind.LLM_PROMPT_TEMPLATE.value]
    assert len(rows) == 1
    requirement: Mapping[str, Any] = rows[0]
    return requirement


async def _new_session(sessions_service: SessionServiceImpl) -> UUID:
    session = await sessions_service.create_session(
        user_id="alice",
        title="prompt review card end to end",
        auth_provider_type="local",
    )
    return session.id


async def _persist(
    sessions_service: SessionServiceImpl,
    session_id: UUID,
    state: CompositionState,
) -> CompositionStateRecord:
    state_dict = state.to_dict()
    return await _save_composition_state_with_compose_authority(
        sessions_service,
        session_id,
        CompositionStateData(
            nodes=state_dict["nodes"],
            sources=state_dict["sources"],
            edges=state_dict["edges"],
            outputs=state_dict["outputs"],
            metadata_=state_dict["metadata"],
            is_valid=False,
        ),
        provenance="tool_call",
    )


async def _run_surfacer(
    sessions_service: SessionServiceImpl,
    session_id: UUID,
    record: CompositionStateRecord,
) -> None:
    """The production settlement surfacing pass over the PERSISTED state."""
    lease = await SessionOperationLease.acquire(
        sessions_service.session_operation_authority,
        session_id=session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=sessions_service.session_operation_owner_instance_id,
        lease_seconds=sessions_service.session_operation_lease_seconds,
    )
    async with lease:
        await surface_pending_interpretation_reviews_for_state(
            state_from_record(record),
            sessions_service=sessions_service,
            session_id=str(session_id),
            current_state_id=str(record.id),
            surface_origin=InterpretationSurfaceOrigin.COMPOSER_LLM,
            model_identifier="anthropic/claude-opus-4-7",
            model_version="2026-05-01",
            provider="anthropic",
            composer_skill_hash="a" * 64,
            session_operation_context=lease.context,
        )


async def _single_pending_prompt_card(
    sessions_service: SessionServiceImpl,
    session_id: UUID,
    record: CompositionStateRecord,
) -> InterpretationEventRecord:
    events = await sessions_service.list_interpretation_events(
        session_id,
        status="all",
        composition_state_id=record.id,
    )
    assert [(event.kind, event.affected_node_id, event.user_term) for event in events] == [
        (InterpretationKind.LLM_PROMPT_TEMPLATE, _NODE_ID, _PROMPT_TERM)
    ]
    [event] = events
    assert event.choice is InterpretationChoice.PENDING
    return event


async def _accept_as_drafted(
    sessions_service: SessionServiceImpl,
    session_id: UUID,
    event: InterpretationEventRecord,
) -> tuple[InterpretationEventRecord, CompositionState]:
    lease = await SessionOperationLease.acquire(
        sessions_service.session_operation_authority,
        session_id=session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=sessions_service.session_operation_owner_instance_id,
        lease_seconds=sessions_service.session_operation_lease_seconds,
    )
    async with lease:
        resolved, new_record = await sessions_service.resolve_interpretation_event(
            session_id=session_id,
            event_id=event.id,
            choice=InterpretationChoice.ACCEPTED_AS_DRAFTED,
            amended_value=None,
            actor="user:alice",
            session_operation_context=lease.context,
        )
    return resolved, state_from_record(new_record)


def _assert_pending_prompt_site_blocks_execution(state: CompositionState) -> None:
    blocked = materialize_state_for_execution(state)
    assert isinstance(blocked, InterpretationReviewPending)
    assert [(site.component_id, site.kind) for site in blocked.sites] == [(_NODE_ID, InterpretationKind.LLM_PROMPT_TEMPLATE)]


# --------------------------------------------------------------------------- #
# Chain A: multi-query node -> surface draft -> surface anchor -> executable
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("with_fallback", [True, False])
async def test_multi_query_card_carries_the_surface_and_resolves_to_an_executable_state(
    sessions_service: SessionServiceImpl,
    with_fallback: bool,
) -> None:
    options = _multi_query_options()
    unused_template = options["prompt_template"]
    if not with_fallback:
        del options["prompt_template"]
    surface_draft = prompt_review_draft_from_options(options)
    surface_anchor = prompt_review_anchor_hash_from_options(options)
    assert surface_draft is not None and surface_anchor is not None
    # Instrument controls: the surface and the dead node template differ, and
    # the surface anchor is not the node-level template's text hash.
    assert surface_draft != unused_template
    assert surface_anchor != stable_hash(unused_template)

    staged = _options_with_default_prompt_template_review(node_id=_NODE_ID, plugin="llm", options=options)
    staged_requirement = _prompt_requirement(staged)
    assert staged_requirement["status"] == "pending"
    assert staged_requirement["draft"] == surface_draft

    session_id = await _new_session(sessions_service)
    record = await _persist(sessions_service, session_id, _state_with_node_options(staged))
    _assert_pending_prompt_site_blocks_execution(state_from_record(record))

    await _run_surfacer(sessions_service, session_id, record)

    event = await _single_pending_prompt_card(sessions_service, session_id, record)
    # Stager, surfacer and writer agree: the persisted card is the rendered
    # prompt surface, never the node-level template.
    assert event.llm_draft == surface_draft
    assert event.llm_draft != unused_template
    assert _interpretation_event_response(event).llm_draft == surface_draft

    resolved, resolved_state = await _accept_as_drafted(sessions_service, session_id, event)

    assert resolved.accepted_value == surface_draft
    resolved_options = _node_options(resolved_state)
    requirement = _prompt_requirement(resolved_options)
    assert requirement["status"] == "resolved"
    assert requirement["accepted_value"] == surface_draft
    assert requirement["resolved_prompt_template_hash"] == surface_anchor
    # Accepting never rewrites the prompt; the runtime hash covers the real template.
    if with_fallback:
        assert resolved_options["prompt_template"] == unused_template
    else:
        assert "prompt_template" not in resolved_options
    assert resolved_options["approved_prompt_artifact_hash"] == approved_prompt_artifact_hash_from_options(options)
    assert resolved.approved_prompt_artifact_hash == resolved_options["approved_prompt_artifact_hash"]

    materialized = materialize_state_for_execution(resolved_state)

    assert isinstance(materialized, CompositionState)
    assert _node_options(materialized)["queries"] == options["queries"]


# --------------------------------------------------------------------------- #
# Chain B: 9000-char single prompt -> bounded draft -> full-text anchor -> executable
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_long_single_prompt_card_is_bounded_and_resolves_to_an_executable_state(
    sessions_service: SessionServiceImpl,
) -> None:
    options = _long_single_prompt_options()
    template = options["prompt_template"]
    full_template_hash = stable_hash(template)
    bounded_draft = prompt_review_draft_from_options(options)
    assert bounded_draft is not None
    # Instrument controls: the template really is over both bounds, and the
    # single-prompt anchor falls back to the full-text hash.
    assert len(template) > _WIRE_DRAFT_MAX_CHARS > PROMPT_SURFACE_REVIEW_MAX_CHARS
    assert prompt_review_anchor_hash_from_options(options) is None

    staged = _options_with_default_prompt_template_review(node_id=_NODE_ID, plugin="llm", options=options)
    assert _prompt_requirement(staged)["draft"] == bounded_draft

    session_id = await _new_session(sessions_service)
    record = await _persist(sessions_service, session_id, _state_with_node_options(staged))
    _assert_pending_prompt_site_blocks_execution(state_from_record(record))

    await _run_surfacer(sessions_service, session_id, record)

    event = await _single_pending_prompt_card(sessions_service, session_id, record)
    assert event.llm_draft == bounded_draft
    assert len(event.llm_draft) <= PROMPT_SURFACE_REVIEW_MAX_CHARS
    assert event.llm_draft != template
    # The wire projection (GET /interpretations) builds from the bounded draft.
    assert _interpretation_event_response(event).llm_draft == bounded_draft

    resolved, resolved_state = await _accept_as_drafted(sessions_service, session_id, event)

    assert _interpretation_event_response(resolved).accepted_value == bounded_draft
    assert resolved.approved_prompt_artifact_hash == approved_prompt_artifact_hash_from_options(options)
    resolved_options = _node_options(resolved_state)
    requirement = _prompt_requirement(resolved_options)
    assert requirement["status"] == "resolved"
    # The anchor covers the complete template, never the shortened display.
    assert requirement["resolved_prompt_template_hash"] == full_template_hash
    assert resolved_options["prompt_template"] == template

    materialized = materialize_state_for_execution(resolved_state)

    assert isinstance(materialized, CompositionState)
    # The run receives the full template, not the bounded card text.
    assert _node_options(materialized)["prompt_template"] == template


@pytest.mark.asyncio
async def test_review_acceptance_keeps_no_full_artifact_for_blob_backed_system(sessions_service: SessionServiceImpl) -> None:
    options = _multi_query_options()
    options["system_prompt"] = {"blob_ref": "5b7a4e0e-9e4a-4f0b-8d3e-2c0e1f0d3a4b", "mode": "inline_content", "sha256": "a" * 64}
    options["approved_prompt_artifact_hash"] = "b" * 64
    staged = _options_with_default_prompt_template_review(node_id=_NODE_ID, plugin="llm", options=options)
    session_id = await _new_session(sessions_service)
    record = await _persist(sessions_service, session_id, _state_with_node_options(staged))
    await _run_surfacer(sessions_service, session_id, record)
    event = await _single_pending_prompt_card(sessions_service, session_id, record)
    resolved, state = await _accept_as_drafted(sessions_service, session_id, event)
    assert resolved.approved_prompt_artifact_hash is None
    assert _node_options(state)["approved_prompt_artifact_hash"] is None
    assert _prompt_requirement(_node_options(state))["status"] == "resolved"
    materialized = materialize_state_for_execution(state)
    assert isinstance(materialized, CompositionState)
    assert "approved_prompt_artifact_hash" not in _node_options(materialized)
