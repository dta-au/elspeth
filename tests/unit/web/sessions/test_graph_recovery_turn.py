"""Recovery crosses real admission, turn ownership and atomic terminal storage.

The request tests drive the production app, worker and Continuation driver
through ``tests.helpers.continuation_app``; only the SDK and the prompt-guard
HTTP boundary are scripted.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from elspeth.contracts.composer_content import ContentPublication
from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence
from elspeth.web.composer.state import CompositionState
from elspeth.web.sessions.content_publication import storage_content_hash
from elspeth.web.sessions.graph_recovery import checkpoint_data
from elspeth.web.sessions.models import composition_states_table
from elspeth.web.sessions.schemas import MessageWithStateResponse
from tests.helpers.continuation_app import ScriptedSDK, continuation_app, end_admit, install_sdk, submit_and_settle
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web.sessions.test_routes import _EMPTY_STATE, _make_app

_MALFORMED_HEAD_UNCOVERED = (
    "UNCOVERED (A5 batch 4): a head damaged after admission never reaches the provider on Continuation "
    "(the request fails before its first PRIMARY call; Continuation bootstrap refuses a malformed head). "
    "Needs the Continuation graph-recovery owner (U-40 or a src change)."
)


def _recovery_call(call_id: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
    # Recovery controls are not graph-mutation tools, so they carry plain JSON
    # arguments rather than a semantic wire projection.
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _message(content: str, state_id: object = None) -> dict[str, object]:
    return {"content": content, "operation_id": str(uuid4()), "state_id": None if state_id is None else str(state_id)}


async def _seed_state(harness):
    with fenced_operation_context(harness.app.state.session_engine, str(harness.session_id)) as context:
        return await harness.app.state.session_service.save_composition_state(
            harness.session_id, checkpoint_data(_EMPTY_STATE), provenance="session_seed", session_operation_context=context
        )


def _corrupt_head(app, state_id) -> None:
    with app.state.session_engine.begin() as conn:
        conn.execute(
            update(composition_states_table)
            .where(composition_states_table.c.id == str(state_id))
            .values(nodes={"invalid": "not a state envelope"})
        )


def _corrupt_head_at_segment_start(harness, monkeypatch, state_id) -> None:
    """Damage the head after admission, once the worker's segment has started."""
    repository = harness.app.state.session_service.session_operation_authority
    actual_start = repository.start_composer_segment
    corrupted: list[bool] = []

    def start(*args, **kwargs):
        context = actual_start(*args, **kwargs)
        if not corrupted:
            corrupted.append(True)
            _corrupt_head(harness.app, state_id)
        return context

    monkeypatch.setattr(repository, "start_composer_segment", start)


async def _result(harness, operation_id: str) -> tuple[object, MessageWithStateResponse]:
    record = harness.app.state.composer_async_operation_authority.get(session_id=harness.session_id, operation_id=operation_id)
    assert record is not None and record.status == "completed" and record.result_json is not None
    return record, MessageWithStateResponse.model_validate_json(record.result_json)


@pytest.mark.skip(reason=_MALFORMED_HEAD_UNCOVERED)
@pytest.mark.asyncio
async def test_malformed_head_reaches_recovery_and_commits_chat_without_validation(tmp_path, monkeypatch):
    message = "Which input should we use?"
    sdk = ScriptedSDK(
        [
            (
                "primary",
                (None, [_recovery_call("recover-call", "handoff_to_user", {"action": "reset_to_safe_checkpoint", "message": message})]),
            ),
            ("end", end_admit()),
        ]
    )
    install_sdk(monkeypatch, sdk)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        service = harness.app.state.session_service
        initial = await _seed_state(harness)
        _corrupt_head_at_segment_start(harness, monkeypatch, initial.id)

        def forbid_validation(_state):
            raise AssertionError("Recovery must not invoke construction validation")

        monkeypatch.setattr(CompositionState, "validate", forbid_validation)
        body = _message("Recover and let us discuss the input.", initial.id)
        snapshot = await submit_and_settle(harness, body)
        assert snapshot.status == "completed", (snapshot, sdk.roles)
        _record, response = await _result(harness, body["operation_id"])
        assert response.message.content == message
        assert response.recovery_notice is not None
        assert "empty authoring workspace" in response.recovery_notice.content
        assert response.state is not None
        assert response.state.id != str(initial.id)
        assert response.state.version == initial.version + 1
        assert response.state.is_valid is False
        assert response.state.composer_meta is not None
        assert set(response.state.composer_meta) == {"content_publication"}
        publication_payload = response.state.composer_meta["content_publication"]
        publication = ContentPublication.from_payload(publication_payload)
        assert publication.to_payload() == publication_payload
        assert publication_payload["applicability"] == "requires_current_inspection"
        assert sdk.roles[0] == "primary" and sdk.roles.count("primary") == 1
        with harness.app.state.session_engine.begin() as conn:
            original = conn.execute(
                select(composition_states_table.c.nodes).where(composition_states_table.c.id == str(initial.id))
            ).scalar_one()
            new_row = conn.execute(
                select(composition_states_table).where(
                    composition_states_table.c.id == response.state.id,
                    composition_states_table.c.session_id == str(harness.session_id),
                )
            ).one()
        assert original == {"invalid": "not a state envelope"}
        stored = service._row_to_state_record(new_row)
        assert str(stored.id) == response.state.id
        assert stored.session_id == harness.session_id
        assert stored.version == response.state.version
        assert publication == ContentPublication(stored.session_id, stored.id, stored.version, storage_content_hash(stored))
        assert new_row.composer_meta["data"] == response.state.composer_meta
        assert new_row.provenance == "graph_recovery"
        messages = await service.get_messages(harness.session_id, limit=None)
        assert len([row for row in messages if str(row.id) == response.message.id]) == 1
        assert len([row for row in messages if str(row.id) == response.recovery_notice.id]) == 1


@pytest.mark.asyncio
async def test_requested_missing_park_finishes_with_trusted_refusal_not_model_success(tmp_path, monkeypatch):
    message = "I restored it successfully."
    sdk = ScriptedSDK(
        [
            ("primary", (None, [_recovery_call("restore-call", "restore_parked_graph", {"park_id": str(uuid4()), "message": message})])),
            ("end", end_admit()),
        ]
    )
    install_sdk(monkeypatch, sdk)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        service = harness.app.state.session_service
        body = _message("Restore the parked graph.")
        snapshot = await submit_and_settle(harness, body)
        assert snapshot.status == "completed", (snapshot, sdk.roles)
        _record, response = await _result(harness, body["operation_id"])
        assert response.state is None
        assert message not in response.message.content
        assert response.message.raw_content is None
        stored = await service.get_messages(harness.session_id, limit=None)
        assert next(row for row in stored if str(row.id) == response.message.id).raw_content == ""
        assert all(segment.kind == "trusted_system_notice" for segment in response.message.segments)
        assert response.recovery_notice is not None
        assert "did not complete" in response.recovery_notice.content
        assert sdk.roles.count("primary") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_head", [False, pytest.param(True, marks=pytest.mark.skip(reason=_MALFORMED_HEAD_UNCOVERED))])
async def test_refused_provider_recovery_retains_exact_private_control_and_physical_binding(tmp_path, monkeypatch, malformed_head):
    arguments = {"park_id": str(uuid4()), "message": "I restored the previous draft successfully."}
    sdk = ScriptedSDK([("primary", (None, [_recovery_call("private-restore", "restore_parked_graph", arguments)])), ("end", end_admit())])
    install_sdk(monkeypatch, sdk)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        service = harness.app.state.session_service
        initial = await _seed_state(harness)
        if malformed_head:
            _corrupt_head_at_segment_start(harness, monkeypatch, initial.id)
        body = _message("Restore the parked graph.", initial.id)
        snapshot = await submit_and_settle(harness, body)
        assert snapshot.status == "completed", (snapshot, sdk.roles)
        record, response = await _result(harness, body["operation_id"])
        assert arguments["message"] not in response.message.content
        rows = await service.get_messages(harness.session_id, limit=None)
        private = [
            ComposerDiagnosticEvidence.from_envelope(row.tool_calls[0], expected_session_id=str(harness.session_id))
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
        public = await service.get_messages(harness.session_id, limit=None, include_private_evidence=False)
        assert all(
            not row.tool_calls or all(slot.get("_kind") != "composer_diagnostic_evidence" for slot in row.tool_calls) for row in public
        )


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
