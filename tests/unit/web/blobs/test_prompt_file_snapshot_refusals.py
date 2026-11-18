"""A prompt-file card refuses by type when its blob has no verified inline bytes.

Resolve: a blob whose status or hash moved after the card was shown is a
stale review (409 ``prompt_file_review_stale``), never a lookup failure.
Creation: a file over the inline cap is refused by size, naming the cap;
runtime substitution enforces the same cap, so no card is owed for it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, update

from elspeth.contracts.composer_interpretation import InterpretationKind, InterpretationSurfaceOrigin
from elspeth.core.blobs_inline import BLOB_INLINE_PER_REF_BYTE_CAP
from elspeth.web.composer.interpretation_surfacing import _auto_surface_prompt_template_reviews_for_state
from elspeth.web.composer.tools._common import _options_with_default_model_choice_review, _options_with_default_prompt_template_review
from elspeth.web.interpretation_state import prompt_review_draft_from_options
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import blobs_table, interpretation_events_table, prompt_file_approvals_table
from elspeth.web.sessions.prompt_file_approval import _blob_record, _read_custody_bytes
from elspeth.web.sessions.protocol import CompositionStateData
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web._sync_asgi_client import SyncASGITestClient
from tests.unit.web.blobs.test_prompt_editor import _live_prompt_setup
from tests.unit.web.blobs.test_routes import _create_session, _make_app, _upload_blob
from tests.unit.web.composer import test_blob_inline_tools as blob_tools


@pytest.mark.parametrize(
    ("column", "value"),
    [
        pytest.param("status", "error", id="status_error"),
        pytest.param("content_hash", "0" * 64, id="hash_changed"),
    ],
)
def test_resolve_route_refuses_unverifiable_prompt_file_as_stale(tmp_path: Path, column: str, value: str) -> None:
    client, _service, blob_service, session_id, first, _second, events = _live_prompt_setup(tmp_path)
    event_id = events["first"]
    # The card was shown (its snapshot anchor read) before the file moved.
    shown = client.get(f"/api/sessions/{session_id}/interpretations/{event_id}/prompt")
    assert shown.status_code == 200, shown.text
    with blob_service._engine.begin() as conn:
        conn.execute(update(blobs_table).where(blobs_table.c.id == first["id"]).values({column: value}))

    response = client.post(
        f"/api/sessions/{session_id}/interpretations/{event_id}/resolve",
        json={"choice": "accepted_as_drafted", "reviewed_anchor": shown.json()["anchor"]},
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "prompt_file_review_stale"
    with blob_service._engine.connect() as conn:
        choice = conn.execute(
            select(interpretation_events_table.c.choice).where(interpretation_events_table.c.id == str(event_id))
        ).scalar_one()
        approvals = conn.execute(
            select(prompt_file_approvals_table.c.blob_id).where(prompt_file_approvals_table.c.session_id == session_id)
        ).all()
    assert choice == "pending"
    assert approvals == []


def _oversized_prompt_state(tmp_path: Path) -> tuple[Any, Any, str, Any, str]:
    """A saved state whose LLM node binds a prompt file over the inline cap."""
    app, service, blob_service = _make_app(tmp_path)
    client = SyncASGITestClient(app)
    session_id = _create_session(client)
    blob = _upload_blob(client, session_id, b"x" * (BLOB_INLINE_PER_REF_BYTE_CAP + 1), "big.txt", "text/plain")
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
    del options["system_prompt"]
    options["prompt_template"] = {"blob_ref": blob["id"], "mode": "inline_content", "sha256": blob["content_hash"]}
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
    draft = prompt_review_draft_from_options(node["options"])
    assert draft is not None
    return service, engine, session_id, saved, draft


def _prompt_events(engine: Any, session_id: str) -> list[Any]:
    with engine.connect() as conn:
        return conn.execute(
            select(interpretation_events_table.c.id)
            .where(interpretation_events_table.c.session_id == session_id)
            .where(interpretation_events_table.c.kind == InterpretationKind.LLM_PROMPT_TEMPLATE.value)
        ).all()


def test_card_creation_refuses_oversized_prompt_file_naming_the_limit(tmp_path: Path) -> None:
    # Imported here so the red-on-base measurement shows the other tests'
    # failure modes instead of one collection error.
    from elspeth.web.sessions.protocol import PromptFileTooLargeError

    service, engine, session_id, saved, draft = _oversized_prompt_state(tmp_path)

    with fenced_operation_context(engine, session_id) as operation, pytest.raises(PromptFileTooLargeError) as refusal:
        asyncio.run(
            service.create_pending_interpretation_event(
                session_id=UUID(session_id),
                composition_state_id=saved.id,
                affected_node_id="first",
                tool_call_id="oversize-card",
                user_term="llm_prompt_template:first",
                kind=InterpretationKind.LLM_PROMPT_TEMPLATE,
                llm_draft=draft,
                model_identifier="test-model",
                model_version="1",
                provider="test-provider",
                composer_skill_hash="a" * 64,
                session_operation_context=operation,
            )
        )

    assert refusal.value.error_code == "prompt_file_too_large"
    assert refusal.value.limit_bytes == BLOB_INLINE_PER_REF_BYTE_CAP
    assert refusal.value.aggregate is False
    assert str(BLOB_INLINE_PER_REF_BYTE_CAP) in str(refusal.value)
    assert _prompt_events(engine, session_id) == []


@pytest.mark.parametrize("repair_mode", [False, True], ids=["settlement", "repair"])
def test_surfacing_skips_oversized_prompt_file_without_a_card(tmp_path: Path, repair_mode: bool) -> None:
    service, engine, session_id, saved, _draft = _oversized_prompt_state(tmp_path)
    state = state_from_record(saved)

    with fenced_operation_context(engine, session_id) as operation:
        asyncio.run(
            _auto_surface_prompt_template_reviews_for_state(
                state,
                sessions_service=service,
                session_id=session_id,
                current_state_id=str(saved.id),
                surface_origin=InterpretationSurfaceOrigin.COMPOSER_LLM,
                model_identifier="test-model",
                model_version="test-model",
                provider="test-provider",
                composer_skill_hash="a" * 64,
                session_operation_context=operation,
                repair_mode=repair_mode,
            )
        )

    assert _prompt_events(engine, session_id) == []
