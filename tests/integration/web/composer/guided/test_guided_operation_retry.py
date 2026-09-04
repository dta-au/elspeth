"""Retry-custody contracts unique to guided-full planning."""

from __future__ import annotations

import asyncio
from uuid import UUID

from elspeth.web.sessions import protocol
from elspeth.web.sessions.routes._helpers import _state_from_record


def test_guided_full_staging_has_dedicated_exact_command_and_settlement() -> None:
    assert protocol.GuidedFullPipelineProposalStageCommand is not None
    assert protocol.GuidedFullPipelineProposalStageSettlement is not None
    assert "originating_message" in protocol.GuidedFullPipelineProposalStageCommand.__dataclass_fields__
    assert "checkpoint_state" in protocol.GuidedFullPipelineProposalStageSettlement.__dataclass_fields__


def test_session_service_exposes_dedicated_guided_full_settlement() -> None:
    assert "stage_guided_full_pipeline_proposal" in protocol.SessionServiceProtocol.__dict__


def test_every_profile_start_persists_exactly_one_verified_root_intent(composer_test_client) -> None:
    """Goal-first: BOTH profiles root their session, and both roots verify.

    The tutorial used to be the profile with no root at all. Now it carries
    the frozen lesson prompt through the same door, and the custody helper
    must accept it: the helper recovers the ``profile`` discriminator from the
    start CHECKPOINT rather than assuming ``"live"``, which is what makes a
    tutorial root re-derive the same request hash it was written under.
    """

    live = composer_test_client.post("/api/sessions", json={"title": "live root"}).json()
    body = {
        "profile": "live",
        "intent": "Build exactly this pipeline.",
        "operation_id": "00000000-0000-4000-8000-000000000021",
    }
    first = composer_test_client.post(f"/api/sessions/{live['id']}/guided/start", json=body)
    replay = composer_test_client.post(f"/api/sessions/{live['id']}/guided/start", json=body)
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    service = composer_test_client.app.state.session_service
    record = asyncio.run(service.get_current_state(UUID(live["id"])))
    assert record is not None
    guided = _state_from_record(record).guided_session
    assert guided is not None and guided.root_intent_message_id is not None
    messages = asyncio.run(service.get_messages(UUID(live["id"]), limit=None))
    roots = [message for message in messages if message.role == "user"]
    assert [(str(message.id), message.content, message.writer_principal) for message in roots] == [
        (guided.root_intent_message_id, body["intent"], "route_user_message")
    ]
    verified = asyncio.run(
        service.get_verified_guided_root_intent(
            session_id=UUID(live["id"]),
            root_message_id=UUID(guided.root_intent_message_id),
        )
    )
    assert verified == roots[0]

    tutorial = composer_test_client.post("/api/sessions", json={"title": "tutorial root"}).json()
    tutorial_intent = "Scrape each page, summarize it, and save the summaries."
    response = composer_test_client.post(
        f"/api/sessions/{tutorial['id']}/guided/start",
        json={"profile": "tutorial", "intent": tutorial_intent, "operation_id": "00000000-0000-4000-8000-000000000022"},
    )
    assert response.status_code == 200
    record = asyncio.run(service.get_current_state(UUID(tutorial["id"])))
    assert record is not None
    guided = _state_from_record(record).guided_session
    assert guided is not None and guided.root_intent_message_id is not None
    tutorial_roots = [message for message in asyncio.run(service.get_messages(UUID(tutorial["id"]), limit=None)) if message.role == "user"]
    assert [(str(message.id), message.content, message.writer_principal) for message in tutorial_roots] == [
        (guided.root_intent_message_id, tutorial_intent, "route_user_message")
    ]
    tutorial_verified = asyncio.run(
        service.get_verified_guided_root_intent(
            session_id=UUID(tutorial["id"]),
            root_message_id=UUID(guided.root_intent_message_id),
        )
    )
    assert tutorial_verified == tutorial_roots[0]
