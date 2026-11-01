"""The no-tool repair gate never re-asks for a vague_term review the rate cap refuses.

Once a vague_term site's review request is refused by an interpretation rate
cap, ``request_interpretation_review`` for that site is certain to be refused
again, and the ``AUTO_INTERPRETED_NO_SURFACES`` row the refusal writes carries
no node or term, so the site stays "missing". The repair gate must therefore
not spend its finite repair turns asking the planner to call the tool again for
that site; it gives the documented fallback instead (write the interpretation
into ``options.prompt_template`` and remove the requirement and its wiring).
The orphan gate stays unfiltered, so a site the planner leaves unresolved still
fails closed.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import Engine

from elspeth.contracts.composer_mutation import ComposerMutationCeiling
from elspeth.web.composer._compose_loop_carriers import _AdmittedAssistantMessage, _CallModelOutcome
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.anti_anchor import AntiAnchorTracker
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.provider_gateway import _admit_composer_llm_completion
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.turn_effects import TurnEvidence
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.helpers.session_fences import acquire_compose_context
from tests.unit.web.composer._helpers import _clean_advisor_checkpoint
from tests.unit.web.composer.test_compose_loop_interpretation_review_dispatch import (
    _build_composer,
    _fake_response_with_tool_call,
    _llm_node_spec_with_id,
    _seed_session_and_state,
)
from tests.unit.web.composer.test_preview_policy_snapshot import _dispatch_tool_batch
from tests.unit.web.sessions.session_test_authority import FencedSessionServiceHarness

_ORDINARY_ASK = "call request_interpretation_review with the listed affected_node_id"
_FALLBACK_INSTRUCTION = "write the interpretation into options.prompt_template"


@pytest.fixture(autouse=True)
def _advisor_end_gate_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _clean_advisor_checkpoint, raising=True)


@pytest.fixture
def sessions_service(engine: Engine) -> SessionServiceImpl:
    return FencedSessionServiceHarness(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.sessions.rate_capped_repair_gate"),
    )


def _composer_system_messages(llm_messages: list[dict[str, Any]]) -> list[str]:
    return [
        message["content"]
        for message in llm_messages
        if message["role"] == "user" and isinstance(message["content"], str) and message["content"].startswith("[composer-system]")
    ]


async def _review_call(
    composer: ComposerServiceImpl,
    sessions_service: SessionServiceImpl,
    *,
    session_id: UUID,
    state_id: UUID,
    state: CompositionState,
    call_id: str,
    node_id: str,
    draft: str,
) -> None:
    """Dispatch one real request_interpretation_review through the shared batch owner."""
    completion = _admit_composer_llm_completion(
        _fake_response_with_tool_call(
            tool_call_id=call_id,
            tool_name="request_interpretation_review",
            arguments={"affected_node_id": node_id, "kind": "vague_term", "user_term": "cool", "llm_draft": draft},
        )
    )
    plugin_snapshot, policy_catalog = composer._policy_context.build(None)
    preferences = await sessions_service.get_composer_preferences(session_id)
    async with acquire_compose_context(sessions_service, session_id) as context:
        ceiling = ComposerMutationCeiling(str(session_id), str(uuid4()), preferences.trust_mode)
        await _dispatch_tool_batch(
            composer,
            call_model=_CallModelOutcome(completion=completion),
            mutation_ceiling=ceiling,
            state=state,
            last_validation=None,
            last_runtime_preflight=None,
            llm_messages=[],
            recorder=BufferingRecorder(),
            anti_anchor=AntiAnchorTracker(),
            discovery_cache={},
            runtime_preflight_cache=composer._preflight.new_cache(),
            session_id=str(session_id),
            user_id="alice",
            user_message_id=None,
            user_message_content=None,
            current_state_id=str(state_id),
            actor="test",
            initial_version=state.version,
            deadline=asyncio.get_running_loop().time() + 60,
            progress=None,
            session_scope=str(session_id),
            advisor_calls_used=0,
            composition_turns_used=0,
            discovery_turns_used=0,
            failed_turn=None,
            cancellation_requested=asyncio.Event(),
            plugin_snapshot=plugin_snapshot,
            policy_catalog=policy_catalog,
            session_operation_context=context,
        )


async def _no_tool_repair_messages(
    composer: ComposerServiceImpl,
    sessions_service: SessionServiceImpl,
    *,
    session_id: UUID,
    state_id: UUID,
    state: CompositionState,
    content: str,
) -> list[str]:
    """Run the shared no-tool terminal owner once and return its repair messages."""
    llm_messages: list[dict[str, Any]] = []
    async with acquire_compose_context(sessions_service, session_id) as context:
        outcome = await composer._completion._try_terminate_no_tools(
            turn_evidence=TurnEvidence(
                session_id=str(session_id),
                session_operation_context=context,
                request_id=str(uuid4()),
                request="rate how cool the pages are",
                initial_state_id=str(state_id),
                initial_state=state,
            ),
            assistant_message=_AdmittedAssistantMessage(content=content),
            message="rate how cool the pages are",
            llm_messages=llm_messages,
            state=state,
            session_id=str(session_id),
            current_state_id=str(state_id),
            initial_version=0,
            user_id="alice",
            last_runtime_preflight=None,
            runtime_preflight_cache=composer._preflight.new_cache(),
            session_scope=str(session_id),
            mutation_success_seen=False,
            recorder=BufferingRecorder(),
            progress=None,
            repair_turns_used=0,
            persisted_assistant_message_id=None,
            persisted_assistant_content=None,
            persisted_tool_call_turn=False,
            advisor_checkpoint_passes_used=0,
            session_operation_context=context,
        )
    assert outcome.action == "continue" and outcome.repair_turns_delta == 1
    return _composer_system_messages(llm_messages)


@pytest.mark.asyncio
async def test_repair_gate_gives_the_fallback_instead_of_re_asking_a_rate_capped_site(
    tmp_path: Path,
    sessions_service: SessionServiceImpl,
) -> None:
    composer = _build_composer(tmp_path, sessions_service)
    state = CompositionState(
        source=None,
        nodes=tuple(_llm_node_spec_with_id(f"rate_node_{i}", term="cool") for i in range(4)),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )
    session_id, state_id = await _seed_session_and_state(sessions_service, state=state)

    # Positive control for the instrument, before any cap is touched: a planner
    # that stops with uncapped missing sites IS asked for them with the
    # ordinary repair message.
    control = await _no_tool_repair_messages(
        composer, sessions_service, session_id=session_id, state_id=state_id, state=state, content="Done."
    )
    control_asks = [content for content in control if _ORDINARY_ASK in content]
    assert control_asks, "the ordinary repair ask must be detectable for an uncapped site"
    assert "vague_term:rate_node_3:cool" in control_asks[0]
    assert not [content for content in control if _FALLBACK_INSTRUCTION in content]

    # Three reviews of "cool" on distinct sites use up the per-term cap (default 3).
    for i in range(3):
        await _review_call(
            composer,
            sessions_service,
            session_id=session_id,
            state_id=state_id,
            state=state,
            call_id=f"call_{i}",
            node_id=f"rate_node_{i}",
            draft=f"Visually appealing {i}.",
        )

    # The fourth site is refused by the per-term cap, then the planner stops.
    await _review_call(
        composer,
        sessions_service,
        session_id=session_id,
        state_id=state_id,
        state=state,
        call_id="call_capped",
        node_id="rate_node_3",
        draft="Visually appealing 3.",
    )
    repair_messages = await _no_tool_repair_messages(
        composer, sessions_service, session_id=session_id, state_id=state_id, state=state, content="Stopping."
    )
    assert repair_messages, "the capped site is still unresolved, so the repair gate must fire"
    capped_site = "vague_term:rate_node_3:cool"
    # Never asked to request the capped review again ...
    assert not [content for content in repair_messages if _ORDINARY_ASK in content and capped_site in content]
    # ... and told the documented fallback for exactly that site instead.
    fallbacks = [content for content in repair_messages if _FALLBACK_INSTRUCTION in content]
    assert fallbacks
    assert all(capped_site in content for content in fallbacks)
    assert not [content for content in fallbacks if _ORDINARY_ASK in content]


def _repair_parts(messages: list[str]) -> list[str]:
    """Split each composer-system repair message into its ask and fallback parts."""
    return [f"[composer-system]{part}" for content in messages for part in content.split("[composer-system]") if part.strip()]


@pytest.mark.asyncio
async def test_repair_gate_asks_a_site_whose_review_is_already_pending_even_when_its_term_is_capped(
    tmp_path: Path,
    sessions_service: SessionServiceImpl,
) -> None:
    """A site with a user-approved event on this state is answered by the dedup gate, not the cap.

    The handler checks dedup before the rate cap, so a request for such a site
    is never refused by the cap. Once its wiring is gone the site is
    unresolvable, and the repair gate must give it the ordinary ask (patch the
    node, then request the review) rather than the capped-site fallback, even
    though the term's per-term cap is full.
    """
    composer = _build_composer(tmp_path, sessions_service)
    state = CompositionState(
        source=None,
        nodes=tuple(_llm_node_spec_with_id(f"rate_node_{i}", term="cool") for i in range(4)),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )
    session_id, state_id = await _seed_session_and_state(sessions_service, state=state)

    # Three reviews of "cool" on distinct sites fill the per-term cap (default 3)
    # and leave a pending user-approved event on rate_node_0..2 for this state.
    for i in range(3):
        await _review_call(
            composer,
            sessions_service,
            session_id=session_id,
            state_id=state_id,
            state=state,
            call_id=f"call_{i}",
            node_id=f"rate_node_{i}",
            draft=f"Visually appealing {i}.",
        )
    pending = await sessions_service.list_interpretation_events(session_id, status="pending")
    assert len(pending) == 3
    assert {event.affected_node_id for event in pending} == {"rate_node_0", "rate_node_1", "rate_node_2"}

    # rate_node_0 loses its prompt wiring; rate_node_3 never had a review.
    unwired = replace(state.nodes[0], options={"prompt_template": "Rate this row."})
    drifted_state = replace(state, nodes=(unwired, *state.nodes[1:]))
    parts = _repair_parts(
        await _no_tool_repair_messages(
            composer, sessions_service, session_id=session_id, state_id=state_id, state=drifted_state, content="Stopping."
        )
    )
    # Every repair message the gate emits must split the sites the same way.
    asks = [part for part in parts if _ORDINARY_ASK in part]
    fallbacks = [part for part in parts if _FALLBACK_INSTRUCTION in part]
    assert asks, parts
    assert fallbacks, parts
    assert len(asks) + len(fallbacks) == len(parts), parts
    pending_site = "vague_term:rate_node_0:cool"
    capped_site = "vague_term:rate_node_3:cool"
    for ask in asks:
        # The already-pending site is asked for, never told to delete its requirement ...
        assert pending_site in ask
        assert capped_site not in ask
        assert _FALLBACK_INSTRUCTION not in ask
    for fallback in fallbacks:
        # ... while the capped site gets only the fallback.
        assert capped_site in fallback
        assert pending_site not in fallback
        assert _ORDINARY_ASK not in fallback
