"""Integrity failures cannot settle as user rejection or rejoin winner success."""

from unittest.mock import patch
from uuid import UUID

import pytest

from elspeth.contracts.composer_llm_audit import ComposerChatTurnStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.guided.errors import InvariantError
from elspeth.web.composer.guided.stage_transitions import SchemaFormAuthority, _knob_fields
from elspeth.web.sessions._guided_step_chat import GuidedStepChatOnlyResult, StepChatResult
from elspeth.web.sessions.protocol import GuidedOperationFenceLostError
from elspeth.web.sessions.routes.composer import guided_chat_atomic
from elspeth.web.sessions.schemas import GuidedChatResponse, GuidedRespondRequest
from tests.unit.web.sessions.test_routes import TestClient, _guided_chat_body, _make_app, _start_live_guided_session


async def _advisory(*args, **kwargs):
    return GuidedStepChatOnlyResult(
        chat=StepChatResult(
            assistant_message="Use the source form.",
            status=ComposerChatTurnStatus.SUCCESS,
            latency_ms=1,
            error_class=None,
        )
    )


def test_owned_schema_invariant_is_not_a_successful_chat_rejection(tmp_path):
    app, service = _make_app(tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    session_id = UUID(client.post("/api/sessions", json={"title": "Integrity"}).json()["id"])
    start = _start_live_guided_session(client, session_id, "Help me")
    body = _guided_chat_body(start.json(), "Help me")
    invalid_authority = SchemaFormAuthority(knobs={"fields": "broken owned schema"}, model_validated_options={})

    def malformed_owned_schema(*args, **kwargs):
        return _knob_fields(invalid_authority, plugin_kind="source", plugin_name="csv")

    with (
        patch("elspeth.web.sessions.routes.composer.guided._run_guided_chat_provider_attempt", new=_advisory),
        patch.object(
            guided_chat_atomic,
            "_transition_request",
            return_value=GuidedRespondRequest.model_validate(
                {"operation_id": body["operation_id"], "turn_token": body["turn_token"], "chosen": ["csv"]}
            ),
        ),
        patch(
            "elspeth.web.sessions.routes.composer.guided._schema8_answer_and_project_next", side_effect=malformed_owned_schema
        ) as transition,
        patch.object(service, "settle_guided_state_operation", wraps=service.settle_guided_state_operation) as settle,
    ):
        response = client.post(f"/api/sessions/{session_id}/guided/chat", json=body)
    transition.assert_called_once()
    settle.assert_not_called()
    assert response.status_code == 500
    assert "broken owned schema" not in response.text


@pytest.mark.parametrize("primary_type", [AuditIntegrityError, InvariantError, RuntimeError])
def test_integrity_primary_survives_settlement_fence_loss_while_ordinary_failure_rejoins(tmp_path, primary_type):
    app, service = _make_app(tmp_path)
    client = TestClient(app)
    session_id = UUID(client.post("/api/sessions", json={"title": "Integrity"}).json()["id"])
    start = _start_live_guided_session(client, session_id, "Help me")
    with patch("elspeth.web.sessions.routes.composer.guided._run_guided_chat_provider_attempt", new=_advisory):
        success = client.post(f"/api/sessions/{session_id}/guided/chat", json=_guided_chat_body(start.json(), "First message"))
    assert success.status_code == 200
    winner = GuidedChatResponse.model_validate_json(success.text)
    body = _guided_chat_body(success.json(), "Second message")
    primary = primary_type("owned operation failed")
    reserve = guided_chat_atomic.reserve_or_replay_guided_operation
    joins = []
    settlement_errors = []

    async def reserve_or_winner(**kwargs):
        if settlement_errors and kwargs.get("reserve_if_absent") is False:
            joins.append(True)
            return winner
        return await reserve(**kwargs)

    async def lose_settlement(command, **kwargs):
        error = GuidedOperationFenceLostError(command.fence)
        settlement_errors.append(error)
        raise error

    async def fail_provider(*args, **kwargs):
        raise primary

    async def lose_guard_settlement(fence, **kwargs):
        raise GuidedOperationFenceLostError(fence)

    with (
        patch.object(guided_chat_atomic, "reserve_or_replay_guided_operation", new=reserve_or_winner),
        patch("elspeth.web.sessions.routes.composer.guided._run_guided_chat_provider_attempt", new=fail_provider),
        patch.object(service, "fail_guided_operation_with_audit", new=lose_settlement),
        patch.object(service, "fail_guided_operation", new=lose_guard_settlement),
    ):
        if primary_type is RuntimeError:
            response = client.post(f"/api/sessions/{session_id}/guided/chat", json=body)
            assert response.status_code == 200
            assert response.json() == success.json()
            assert joins == [True]
        else:
            with pytest.raises(primary_type) as caught:
                client.post(f"/api/sessions/{session_id}/guided/chat", json=body)
            assert caught.value is primary
            assert caught.value.__cause__ is settlement_errors[0]
            assert joins == []
    assert len(settlement_errors) == 1
