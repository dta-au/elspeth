"""Actual V1 finalization route and retained source/budget custody controls."""

from __future__ import annotations

import pytest

from elspeth.web.sessions import composer_finalization, composer_turn
from elspeth.web.sessions.schemas import MessageWithStateResponse
from tests.unit.web.sessions.test_routes import _make_app
from tests.unit.web.sessions.test_run_composer_turn import _run, _running_job


@pytest.mark.asyncio
async def test_actual_v1_turn_calls_shared_finalizer_with_original_owners_and_durable_response(tmp_path, monkeypatch):
    app, service = _make_app(tmp_path)
    session = await service.create_session("alice", "Finalization custody", "local")
    observed = []
    original = composer_finalization.settle_composer_result

    async def settle(inputs, result):
        assert type(inputs) is composer_finalization.ComposerFinalizationInputs
        assert inputs.service is service
        assert inputs.session.id == session.id
        assert inputs.turn is job.turn
        assert inputs.lease is job.lease
        assert inputs.running is job.running
        assert inputs.observation is job.observation
        assert inputs.budget_anchor is job.budget_anchor
        assert inputs.required_work is job.observation.required_work
        assert result is job.observation.compose_result
        assert inputs.provider_binding.coordinator is inputs.required_work
        observed.append((inputs, result))
        return await original(inputs, result)

    monkeypatch.setattr(composer_turn, "settle_composer_result", settle)
    async with _running_job(app, service, session.id, kind="compose_message", content="Give a plain reply.") as job:
        terminal = await _run(app, job)
        assert len(observed) == 1
        assert terminal.status == "completed"
        assert job.observation.audit_cohort_durable
        persisted = job.authority.get(session_id=session.id, operation_id=job.turn.operation_id)
        assert persisted is not None and persisted.result_json == terminal.result_json
        response = MessageWithStateResponse.model_validate_json(persisted.result_json)
        rows = await service.get_messages(session.id, limit=None)
        assert [row.content for row in rows if str(row.id) == response.message.id] == [response.message.content]
