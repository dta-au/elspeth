"""Freeform persistence must not create an authoritative guided checkpoint."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.composer.protocol import ComposerResult, ComposerService
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient
from tests.unit.web.sessions.test_routes import (
    ValidationResult,
    _async_return,
    _create_test_composition_proposal,
    _make_app,
    _start_live_guided_session,
)


@pytest.mark.parametrize("tool_name", ["set_source", "set_pipeline"])
def test_rootless_freeform_proposal_stays_freeform_after_reload_and_can_explicitly_convert(tmp_path, monkeypatch, tool_name) -> None:
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
                        "from_node": "source",
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
    reloaded = client.get(f"/api/sessions/{session_id}/guided")
    assert reloaded.status_code == 400, reloaded.json()
    assert persisted.composer_meta is None or "guided_session" not in persisted.composer_meta

    converted = client.post(
        f"/api/sessions/{session_id}/guided/convert",
        json={"operation_id": str(uuid.uuid4()), "intent": "Summarize this CSV and save the result"},
    )
    assert converted.status_code == 200, converted.json()
    resumed = client.get(f"/api/sessions/{session_id}/guided")
    assert resumed.status_code == 200, resumed.json()
    assert resumed.json()["composition_state"]["composer_meta"]["guided_session"]["root_intent_message_id"] is not None
    assert resumed.json()["next_turn"] is not None
    assert asyncio.run(service.get_state(persisted.id)) == persisted


def test_deliberate_guided_start_remains_guided_after_reload(tmp_path) -> None:
    app, service = _make_app(tmp_path)
    client = TestClient(app)
    session = client.post("/api/sessions", json={"title": "Deliberate guided"}).json()
    session_id = uuid.UUID(session["id"])
    started = _start_live_guided_session(client, session_id, "Summarize this CSV and save the result")
    reloaded = client.get(f"/api/sessions/{session_id}/guided")
    assert reloaded.status_code == 200, reloaded.json()
    assert reloaded.json()["guided_session"] == started.json()["guided_session"]
    persisted = asyncio.run(service.get_current_state(session_id))
    assert persisted is not None
    assert persisted.composer_meta["guided_session"]["root_intent_message_id"] is not None
    assert reloaded.json()["next_turn"] is not None


@pytest.mark.parametrize("endpoint", ["messages", "recompose"])
def test_lazy_freeform_composition_does_not_pass_a_guided_checkpoint(tmp_path, endpoint) -> None:
    app, service = _make_app(tmp_path)
    client = TestClient(app)
    session = client.post("/api/sessions", json={"title": "Freeform request"}).json()
    session_id = uuid.UUID(session["id"])
    composer = MagicMock(spec=ComposerService)
    composer.surface_pending_interpretation_reviews = AsyncMock(
        spec=ComposerService.surface_pending_interpretation_reviews, return_value=None
    )
    composer.compose = AsyncMock(
        spec=ComposerService.compose,
        return_value=ComposerResult(
            message="Please choose your input.",
            state=CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1),
        ),
    )
    app.state.composer_service = composer
    if endpoint == "recompose":
        asyncio.run(service.add_message(session_id, "user", "Summarize my CSV", writer_principal="route_user_message"))
        response = client.post(f"/api/sessions/{session_id}/recompose")
    else:
        response = client.post(f"/api/sessions/{session_id}/messages", json={"content": "Summarize my CSV"})
    assert response.status_code == 200, response.json()
    composer.compose.assert_awaited_once()
    supplied_state = composer.compose.call_args.args[2]
    assert isinstance(supplied_state, CompositionState)
    assert supplied_state.guided_session is None
