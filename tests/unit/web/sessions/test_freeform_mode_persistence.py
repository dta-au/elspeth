"""Freeform proposals and lazy composition persist through ordinary session APIs."""

import asyncio
import json
import uuid
from unittest.mock import MagicMock

import pytest

from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.composer import prompts as composer_prompts
from tests.helpers.continuation_app import ScriptedSDK, continuation_app, end_admit, install_sdk, submit_and_settle
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient
from tests.unit.web.composer.test_primary_rejection_gateway import _sdk_rejection
from tests.unit.web.sessions.test_routes import (
    ValidationResult,
    _async_return,
    _create_test_composition_proposal,
    _make_app,
)


@pytest.mark.parametrize("tool_name", ["set_source", "set_pipeline"])
def test_rootless_freeform_proposal_survives_reload(tmp_path, monkeypatch, tool_name) -> None:
    from elspeth.web.catalog.schemas import PluginSchemaInfo, PluginSummary

    app, service = _make_app(tmp_path)
    app.state.session_engine = service._engine
    catalog = MagicMock(spec=CatalogService)
    catalog.list_sources.return_value = [
        PluginSummary(name="csv", description="CSV source", plugin_type="source", config_fields=[]),
    ]
    catalog.list_transforms.return_value = [
        PluginSummary(name="passthrough", description="Passthrough", plugin_type="transform", config_fields=[]),
    ]
    catalog.list_sinks.return_value = [
        PluginSummary(name="csv", description="CSV sink", plugin_type="sink", config_fields=[]),
    ]
    catalog.get_schema.return_value = PluginSchemaInfo(
        name="csv",
        plugin_type="source",
        description="CSV source",
        json_schema={"title": "Config", "properties": {}},
        knob_schema={"fields": []},
    )
    app.state.catalog_service = catalog
    monkeypatch.setattr(
        "elspeth.web.sessions.routes._helpers._runtime_preflight_for_state",
        _async_return(ValidationResult(is_valid=True, checks=[], errors=[])),
    )
    client = TestClient(app)
    session = client.post("/api/sessions", json={"title": "Accept"}).json()
    session_id = uuid.UUID(session["id"])
    assert asyncio.run(service.get_current_state(session_id)) is None
    input_path = tmp_path / "blobs" / str(session_id) / "input.csv"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text("value\n1\n", encoding="utf-8")
    proposal = asyncio.run(
        _create_test_composition_proposal(
            service,
            session_id=session_id,
            tool_call_id="call_set_pipeline",
            tool_name=tool_name,
            summary="Replace the pipeline.",
            rationale="Requested by the current composer turn.",
            affects=("graph", "validation", "yaml"),
            arguments_json={
                "source_name": "primary",
                "plugin": "csv",
                "on_success": "source_out",
                "options": {"path": str(input_path), "schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            }
            if tool_name == "set_source"
            else {
                "sources": {
                    "primary": {
                        "plugin": "csv",
                        "on_success": "source_out",
                        "options": {"path": str(input_path), "schema": {"mode": "observed"}},
                        "on_validation_failure": "quarantine",
                    }
                },
                "nodes": [
                    {
                        "id": "t1",
                        "node_type": "transform",
                        "plugin": "passthrough",
                        "input": "source_out",
                        "on_success": "main",
                        "on_error": "discard",
                        "options": {"schema": {"mode": "observed"}},
                    }
                ],
                "edges": [
                    {
                        "id": "e1",
                        "from_node": "primary",
                        "to_node": "t1",
                        "edge_type": "on_success",
                        "label": None,
                    }
                ],
                "outputs": [
                    {
                        "sink_name": "main",
                        "plugin": "csv",
                        "options": {
                            "path": "outputs/output.csv",
                            "schema": {"mode": "observed"},
                            "mode": "write",
                            "collision_policy": "auto_increment",
                        },
                        "on_write_failure": "discard",
                    }
                ],
                "metadata": {"name": "accepted-proposal"},
            },
            arguments_redacted_json={"summary": "redacted"},
            base_state_id=None,
            actor="composer-web:user:alice",
        )
    )

    response = client.post(f"/api/sessions/{session['id']}/proposals/{proposal.id}/accept")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "committed"
    assert body["committed_state_id"] is not None
    persisted = asyncio.run(service.get_current_state(session_id))
    assert persisted is not None
    assert persisted.sources["primary"]["plugin"] == "csv"
    assert persisted.sources["primary"]["options"]["path"] == str(input_path)
    assert persisted.sources["primary"]["options"]["schema"]["mode"] == "observed"
    if tool_name == "set_pipeline":
        assert [(edge["from_node"], edge["to_node"]) for edge in persisted.edges] == [("primary", "t1")]
    reloaded = client.get(f"/api/sessions/{session_id}/state")
    assert reloaded.status_code == 200, reloaded.json()
    assert reloaded.json()["id"] == str(persisted.id)
    assert asyncio.run(service.get_state(persisted.id)) == persisted


def _state_wire(messages) -> str:
    return next(
        row["content"]
        for row in messages
        if isinstance(row["content"], str) and row["content"].startswith(composer_prompts.STATE_CONTEXT_PREFIX)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["messages", "recompose"])
async def test_lazy_freeform_composition_passes_empty_state(tmp_path, monkeypatch, endpoint) -> None:
    steps: list = [("primary", ("Please choose your input.", None)), ("end", end_admit())]
    if endpoint == "recompose":
        # The recompose precondition: the conversation ends at a failed user
        # turn whose original ingress receipt the retry reuses.
        steps.insert(0, ("raise", _sdk_rejection()))
    sdk = ScriptedSDK(steps)
    install_sdk(monkeypatch, sdk)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        sessions = harness.app.state.session_service
        assert await sessions.get_current_state(harness.session_id) is None
        first_primary = 0
        if endpoint == "recompose":
            failed = await submit_and_settle(harness, {"content": "Summarize my CSV", "operation_id": str(uuid.uuid4()), "state_id": None})
            assert failed.status == "failed", failed.model_dump_json()
            assert await sessions.get_current_state(harness.session_id) is None
            users = [row for row in await sessions.get_messages(harness.session_id, limit=None) if row.role == "user"]
            assert [row.content for row in users] == ["Summarize my CSV"]
            body = {"expected_user_message_id": str(users[0].id), "operation_id": str(uuid.uuid4()), "state_id": None}
            first_primary = 1
        else:
            body = {"content": "Summarize my CSV", "operation_id": str(uuid.uuid4()), "state_id": None}
        snapshot = await submit_and_settle(harness, body, route=endpoint)
        # The planner's first request for this operation carries the lazy, empty composition.
        assert sdk.roles[first_primary : first_primary + 1] == ["primary"], snapshot.model_dump_json()
        view = json.loads(_state_wire(sdk.requests[first_primary]["messages"]).split("\n", 1)[1])["current_state"]
        supplied_state = view["data"]
        assert supplied_state["sources"] == {}
        assert supplied_state["nodes"] == []
        # Integration completion point (U-40): the clarifying reply completes.
        assert snapshot.status == "completed", snapshot.model_dump_json()
        assert snapshot.result is not None and snapshot.result.message.content == "Please choose your input."
