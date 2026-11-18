"""A review opt-out never stands in for a person reading a prompt file.

Opting out is a session preference that lets inline interpretations
auto-resolve. A file-backed prompt has no inline text the planner authored
for the user to have seen, so its card stays pending and must be approved
before a run admits it; other kinds keep following the opt-out.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID

from elspeth.contracts.composer_interpretation import InterpretationChoice, InterpretationKind, InterpretationSource
from elspeth.web.composer.tools._common import _options_with_default_model_choice_review, _options_with_default_prompt_template_review
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY, PROMPT_TEMPLATE_PARTS_KEY, prompt_review_draft_from_options
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.prompt_file_approval import _blob_record, _read_custody_bytes
from elspeth.web.sessions.protocol import CompositionStateData
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web._sync_asgi_client import SyncASGITestClient
from tests.unit.web.blobs.test_prompt_editor import _admit
from tests.unit.web.blobs.test_routes import _create_session, _make_app, _upload_blob
from tests.unit.web.composer import test_blob_inline_tools as blob_tools

_RENDERED = "Rate pending interpretation this is."


def _opted_out_file_prompt_session(tmp_path: Path, *, vague_term: bool) -> tuple[Any, Any, Any, str, Any, dict[str, Any]]:
    app, service, blob_service = _make_app(tmp_path)
    client = SyncASGITestClient(app)
    session_id = _create_session(client)
    opted = client.post(f"/api/sessions/{session_id}/interpretations/opt_out")
    assert opted.status_code == 200, opted.text
    blob = _upload_blob(client, session_id, b"Classify each row.", "prompt.txt", "text/plain")
    engine = blob_service._engine

    def read_blob(_context: Any, blob_id: UUID) -> tuple[Any, bytes]:
        with engine.connect() as conn:
            record = _blob_record(conn, UUID(session_id), blob_id)
        return record, _read_custody_bytes(record)

    service._inline_blob_read = read_blob
    state = blob_tools._inline_ref_state().to_dict()
    node = state["nodes"][0]
    node["id"] = "first"
    options = dict(node["options"])
    marker = {"blob_ref": blob["id"], "mode": "inline_content", "sha256": blob["content_hash"]}
    if vague_term:
        # The file is the system prompt; the inline prompt carries a vague term.
        options["system_prompt"] = marker
        options["prompt_template"] = _RENDERED
        options[PROMPT_TEMPLATE_PARTS_KEY] = [
            {"kind": "text", "text": "Rate "},
            {"kind": "interpretation_ref", "requirement_id": "cool"},
            {"kind": "text", "text": " this is."},
        ]
        options[INTERPRETATION_REQUIREMENTS_KEY] = [
            {
                "id": "cool",
                "user_term": "cool",
                "status": "pending",
                "draft": "A draft of cool",
                "event_id": None,
                "accepted_value": None,
                "resolved_prompt_template_hash": None,
            }
        ]
    else:
        del options["system_prompt"]
        options["prompt_template"] = marker
    options = _options_with_default_model_choice_review(node_id="first", plugin="llm", options=options)
    node["options"] = _options_with_default_prompt_template_review(node_id="first", plugin="llm", options=options)
    with fenced_operation_context(engine, session_id) as operation:
        saved = asyncio.run(
            service.save_composition_state(
                UUID(session_id),
                CompositionStateData(
                    sources=state["sources"],
                    nodes=state["nodes"],
                    edges=state["edges"],
                    outputs=state["outputs"],
                    metadata_=state["metadata"],
                    is_valid=True,
                ),
                provenance="tool_call",
                session_operation_context=operation,
            )
        )
    return client, service, blob_service, session_id, saved, node


def _create(service: Any, engine: Any, session_id: str, state_id: UUID, *, kind: InterpretationKind, user_term: str, draft: str) -> Any:
    with fenced_operation_context(engine, session_id) as operation:
        return asyncio.run(
            service.create_pending_interpretation_event(
                session_id=UUID(session_id),
                composition_state_id=state_id,
                affected_node_id="first",
                tool_call_id=f"opt-out-{user_term}",
                user_term=user_term,
                kind=kind,
                llm_draft=draft,
                model_identifier="test-model",
                model_version="1",
                provider="test-provider",
                composer_skill_hash="a" * 64,
                session_operation_context=operation,
            )
        )


def _current(service: Any, session_id: str) -> Any:
    record = asyncio.run(service.get_current_state(UUID(session_id)))
    assert record is not None
    return record


def _settle_model_choice(service: Any, engine: Any, session_id: str) -> None:
    """The model-choice card follows the opt-out: it auto-resolves."""
    record = _current(service, session_id)
    node = next(n for n in record.nodes if n["id"] == "first")
    event = _create(
        service,
        engine,
        session_id,
        record.id,
        kind=InterpretationKind.LLM_MODEL_CHOICE,
        user_term="llm_model_choice:first",
        draft=node["options"]["model"],
    )
    assert event.choice is InterpretationChoice.OPTED_OUT


def _file_card(service: Any, engine: Any, session_id: str) -> Any:
    record = _current(service, session_id)
    node = next(n for n in record.nodes if n["id"] == "first")
    draft = prompt_review_draft_from_options(node["options"])
    assert draft is not None
    return _create(
        service,
        engine,
        session_id,
        record.id,
        kind=InterpretationKind.LLM_PROMPT_TEMPLATE,
        user_term="llm_prompt_template:first",
        draft=draft,
    )


def _approve_and_admit(client: Any, service: Any, blob_service: Any, session_id: str, event_id: UUID) -> None:
    resolved = client.post(f"/api/sessions/{session_id}/interpretations/{event_id}/resolve", json={"choice": "accepted_as_drafted"})
    assert resolved.status_code == 200, resolved.text
    assert [snapshot.node_id for snapshot in _admit(service, blob_service, session_id)] == ["first"]


def test_opted_out_session_still_gets_an_approvable_file_prompt_card(tmp_path: Path) -> None:
    client, service, blob_service, session_id, _saved, _node = _opted_out_file_prompt_session(tmp_path, vague_term=False)
    engine = blob_service._engine
    _settle_model_choice(service, engine, session_id)

    card = _file_card(service, engine, session_id)

    assert card.choice is InterpretationChoice.PENDING
    assert card.interpretation_source is InterpretationSource.USER_APPROVED
    _approve_and_admit(client, service, blob_service, session_id, card.id)


def test_vague_term_follows_the_opt_out_while_the_file_card_stays_pending(tmp_path: Path) -> None:
    client, service, blob_service, session_id, saved, node = _opted_out_file_prompt_session(tmp_path, vague_term=True)
    engine = blob_service._engine

    vague = _create(service, engine, session_id, saved.id, kind=InterpretationKind.VAGUE_TERM, user_term="cool", draft="A draft of cool")

    assert vague.choice is InterpretationChoice.OPTED_OUT
    assert vague.interpretation_source is InterpretationSource.AUTO_INTERPRETED_OPT_OUT
    requirements = next(n for n in _current(service, session_id).nodes if n["id"] == "first")["options"][INTERPRETATION_REQUIREMENTS_KEY]
    assert next(r for r in requirements if r["id"] == "cool")["status"] == "resolved"
    _settle_model_choice(service, engine, session_id)
    card = _file_card(service, engine, session_id)
    assert card.choice is InterpretationChoice.PENDING
    _approve_and_admit(client, service, blob_service, session_id, card.id)
    assert (
        state_from_record(_current(service, session_id)).nodes[0].options["system_prompt"]["blob_ref"]
        == node["options"]["system_prompt"]["blob_ref"]
    )
