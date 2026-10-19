"""Recovery crosses real admission, turn ownership and atomic terminal storage."""

from __future__ import annotations

import json
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from elspeth.web.composer.availability import ComposerAvailability
from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence
from elspeth.web.composer.graph_recovery import GraphRecoveryIntent
from elspeth.web.composer.protocol import ComposerResult, ComposerService, GraphRecoveryResponse
from elspeth.web.composer.state import CompositionState
from elspeth.web.sessions.graph_recovery import checkpoint_data
from elspeth.web.sessions.models import composition_states_table
from elspeth.web.sessions.schemas import MessageWithStateResponse
from tests.unit.web.composer.test_compose_loop_interpretation_review_dispatch import _build_composer
from tests.unit.web.composer.test_compose_loop_persistence import _tool_batch_response
from tests.unit.web.sessions.test_routes import _EMPTY_STATE, _make_app
from tests.unit.web.sessions.test_run_composer_turn import _run, _running_job, _seed_output_only_draft


@pytest.mark.asyncio
async def test_malformed_head_reaches_recovery_and_commits_chat_without_validation(tmp_path, monkeypatch):
    app, service = _make_app(tmp_path)
    session = await service.create_session("alice", "Recovery", "local")
    initial = await service.save_composition_state(session.id, checkpoint_data(_EMPTY_STATE), provenance="session_seed")
    composer = create_autospec(ComposerService, instance=True)
    intent = GraphRecoveryIntent("reset_to_safe_checkpoint", "Which input should we use?", None, "recover-call")
    composer.compose_recovery.return_value = GraphRecoveryResponse(intent=intent, llm_calls=(), tool_invocations=())
    app.state.composer_service = composer

    def forbid_validation(_state):
        raise AssertionError("Recovery must not invoke construction validation")

    async with _running_job(app, service, session.id, kind="compose_message", content="Recover and let us discuss the input.") as job:
        with app.state.session_engine.begin() as conn:
            conn.execute(
                update(composition_states_table)
                .where(composition_states_table.c.id == str(initial.id))
                .values(nodes={"invalid": "not a state envelope"})
            )
        monkeypatch.setattr(CompositionState, "validate", forbid_validation)
        record = await _run(app, job)
        assert job.observation.audit_cohort_durable

    assert record.status == "completed"
    response = MessageWithStateResponse.model_validate_json(record.result_json)
    assert response.message.content == intent.message
    assert response.recovery_notice is not None
    assert "empty authoring workspace" in response.recovery_notice.content
    assert response.state is not None
    assert response.state.id != str(initial.id)
    assert response.state.version == initial.version + 1
    assert response.state.is_valid is False
    assert response.state.composer_meta is None
    composer.compose.assert_not_called()
    composer.compose_recovery.assert_awaited_once()
    assert composer.compose_recovery.await_args.kwargs["recovery_context"].malformed_head
    with app.state.session_engine.begin() as conn:
        original = conn.execute(
            select(composition_states_table.c.nodes).where(composition_states_table.c.id == str(initial.id))
        ).scalar_one()
    assert original == {"invalid": "not a state envelope"}
    messages = await service.get_messages(session.id, limit=None)
    assert len([row for row in messages if str(row.id) == response.message.id]) == 1
    assert len([row for row in messages if str(row.id) == response.recovery_notice.id]) == 1


@pytest.mark.asyncio
async def test_requested_missing_park_finishes_with_trusted_refusal_not_model_success(tmp_path):
    app, service = _make_app(tmp_path)
    session = await service.create_session("alice", "Recovery refusal", "local")
    composer = create_autospec(ComposerService, instance=True)
    intent = GraphRecoveryIntent("restore_parked_graph", "I restored it successfully.", str(uuid4()), "restore-call")
    composer.compose.return_value = ComposerResult(message=intent.message, state=_EMPTY_STATE, graph_recovery_intent=intent)
    app.state.composer_service = composer
    async with _running_job(app, service, session.id, kind="compose_message", content="Restore the parked graph.") as job:
        record = await _run(app, job)
    response = MessageWithStateResponse.model_validate_json(record.result_json)
    assert response.state is None
    assert intent.message not in response.message.content
    assert response.message.raw_content is None
    stored = await service.get_messages(session.id, limit=None)
    assert next(row for row in stored if str(row.id) == response.message.id).raw_content == ""
    assert all(segment.kind == "trusted_system_notice" for segment in response.message.segments)
    assert response.recovery_notice is not None
    assert "did not complete" in response.recovery_notice.content
    composer.compose.assert_awaited_once()
    composer.compose_recovery.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_head", [False, True])
async def test_refused_provider_recovery_retains_exact_private_control_and_physical_binding(tmp_path, monkeypatch, malformed_head):
    app, service = _make_app(tmp_path)
    session = await service.create_session("alice", "Private recovery audit", "local")
    initial = await _seed_output_only_draft(app, service, session.id)
    composer = _build_composer(tmp_path, service)
    monkeypatch.setattr(composer, "_availability", ComposerAvailability(True, "anthropic/claude-opus-4-7", "anthropic"))
    app.state.composer_service = composer
    arguments = {"park_id": str(uuid4()), "message": "I restored the previous draft successfully."}

    async def provider(*args, **kwargs):
        return _tool_batch_response(("private-restore", "restore_parked_graph", arguments))

    monkeypatch.setattr("litellm.acompletion", provider)
    async with _running_job(app, service, session.id, kind="compose_message", content="Restore the parked graph.") as job:
        if malformed_head:
            with app.state.session_engine.begin() as conn:
                conn.execute(
                    update(composition_states_table)
                    .where(composition_states_table.c.id == str(initial.id))
                    .values(nodes={"invalid": "not a state envelope"})
                )
        record = await _run(app, job)
    response = MessageWithStateResponse.model_validate_json(record.result_json)
    assert arguments["message"] not in response.message.content
    rows = await service.get_messages(session.id, limit=None)
    private = [
        ComposerDiagnosticEvidence.from_envelope(row.tool_calls[0], expected_session_id=str(session.id))
        for row in rows
        if row.tool_calls and row.tool_calls[0].get("_kind") == "composer_diagnostic_evidence"
    ]
    (received,) = [item for item in private if item.tool_name == "restore_parked_graph"]
    assert json.loads(received.arguments.content) == arguments
    assert received.operation_id == record.session_operation_id
    assert received.operation_epoch == record.session_operation_epoch
    facts = json.loads(received.result.content)
    assert facts["provider_tool_call_id"] == "private-restore"
    assert facts["physical_call_id"]
    public = await service.get_messages(session.id, limit=None, include_private_evidence=False)
    assert all(not row.tool_calls or all(slot.get("_kind") != "composer_diagnostic_evidence" for slot in row.tool_calls) for row in public)


@pytest.mark.asyncio
async def test_reload_exposes_only_scalar_head_when_graph_cannot_hydrate(tmp_path):
    app, service = _make_app(tmp_path)
    session = await service.create_session("alice", "Broken graph", "local")
    state = await service.save_composition_state(session.id, checkpoint_data(_EMPTY_STATE), provenance="session_seed")
    with app.state.session_engine.begin() as conn:
        conn.execute(
            update(composition_states_table).where(composition_states_table.c.id == str(state.id)).values(nodes={"sensitive": "bad"})
        )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/sessions/{session.id}/state")
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["error_type"] == "composition_state_unavailable"
        assert detail["state_id"] == str(state.id)
        assert detail["version"] == state.version
        assert "sensitive" not in response.text
        head = await client.get(f"/api/sessions/{session.id}/state/head")
        assert head.status_code == 200
        assert head.json() == {"state_id": str(state.id), "version": state.version}
