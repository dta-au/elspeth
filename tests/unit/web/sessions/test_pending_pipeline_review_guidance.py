"""The proposal acceptance step advertised by planner handoff guidance."""

from uuid import UUID

import httpx
import pytest

from elspeth.contracts.hashing import stable_hash
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.interpretation_surfacing import InterpretationSurfacing
from elspeth.web.composer.pipeline_planner import PipelinePlanResult
from elspeth.web.composer.pipeline_proposal import (
    AbsentBase,
    PipelineProposal,
    owned_composition_state_authority,
    owned_composition_state_review_arguments,
)
from elspeth.web.composer.redaction import redact_tool_call_arguments
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.tools import execute_tool
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.routes._helpers import _initial_composition_state
from tests.integration.web.composer.test_pipeline_proposal_lifecycle import _runnable_pipeline
from tests.unit.web.coordination.test_composer_operation_authority import start
from tests.unit.web.sessions.test_pipeline_rejection_callers import mounted_app

pytest_plugins = ("tests.unit.web.coordination.test_composer_operation_authority",)


@pytest.mark.asyncio
async def test_accepting_pipeline_proposal_opens_candidate_interpretation_cards(operation_store, tmp_path):
    engine, repository, _authority, service, sid = operation_store
    record, running = start(operation_store)
    user = await service.add_message_with_transcript(
        sid,
        "user",
        "Build a pipeline using generated rows",
        operation_id=UUID(record.operation_id),
        requested_state_id=None,
        writer_principal="route_user_message",
        session_operation_context=running.session_operation_context,
        running=running,
    )
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    pipeline = _runnable_pipeline(tmp_path, sid)
    del pipeline["source"]["options"]["path"]
    pipeline["source"]["inline_blob"] = {"filename": "input.csv", "mime_type": "text/csv", "content": "value\n1\n"}
    result = execute_tool(
        "set_pipeline",
        pipeline,
        _initial_composition_state(),
        PolicyCatalogView.for_trained_operator(catalog, snapshot),
        plugin_snapshot=snapshot,
        data_dir=str(tmp_path),
        session_id=str(sid),
        session_engine=engine,
        session_operation_context=running.session_operation_context,
        session_operation_authority=service.session_operation_authority,
        user_id="alice",
        user_message_id=str(user.message.id),
        user_message_content=user.message.content,
        composer_model_identifier="model",
        composer_model_version="v1",
        composer_provider="test",
        composer_skill_hash=stable_hash("skill"),
        tool_arguments_hash=stable_hash(pipeline),
    )
    assert result.success, result.to_dict()
    state = result.updated_state
    proposal = PipelineProposal.create(
        pipeline=owned_composition_state_authority(state), base=AbsentBase(), repair_count=0, skill_hash=stable_hash("skill")
    )
    plan = PipelinePlanResult(
        proposal=proposal,
        tool_call_id="pending-source-plan",
        custody_result="not_required",
        model_identifier="model",
        model_version="v1",
        provider="test",
    )
    row = await service.create_pipeline_composition_proposal(
        session_id=sid,
        plan=plan,
        summary="Build pipeline",
        rationale="Requested",
        affects=("graph",),
        actor="composer-web:user:alice",
        arguments_redacted_json=redact_tool_call_arguments(
            "set_pipeline", owned_composition_state_review_arguments(proposal.pipeline), telemetry=NoopRedactionTelemetry()
        ),
        composer_model_identifier="model",
        composer_model_version="v1",
        composer_provider="test",
        session_operation_context=running.session_operation_context,
        user_message_id=user.message.id,
    )
    assert await service.list_interpretation_events(sid, status="pending") == []
    repository.release(running.session_operation_context)
    app = mounted_app(operation_store, tmp_path)
    app.state.interpretation_surfacing = InterpretationSurfacing(
        sessions_service=service,
        per_term_cap=5,
        per_session_day_cap=20,
        model_identifier="model",
        provider="test",
        composer_skill_hash=stable_hash("skill"),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/sessions/{sid}/proposals/{row.id}/accept", json={"draft_hash": proposal.draft_hash})
        assert response.status_code == 200, response.text
        events_response = await client.get(f"/api/sessions/{sid}/interpretations")
        assert events_response.status_code == 200, events_response.text
    pending = await service.list_interpretation_events(sid, status="pending")
    assert len(pending) == 1
    assert pending[0].kind.value == "invented_source"
    assert pending[0].composition_state_id == (await service.get_current_state(sid)).id
    assert str(pending[0].id) in events_response.text
