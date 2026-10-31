"""Authorized legacy repair must retain the historical singular source."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from elspeth.contracts.freeze import deep_thaw
from elspeth.web.sessions.content_publication import read_content_publication
from elspeth.web.sessions.models import composition_states_table
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.state_envelope import envelope_state_column
from tests.unit.web._sync_asgi_client import SyncASGITestClient
from tests.unit.web.sessions.test_routes import _close_app_session_engines as _close_app_session_engines
from tests.unit.web.sessions.test_routes import _make_app, _save_test_composition_state


@pytest.mark.asyncio
async def test_revert_current_legacy_singular_source_preserves_authored_graph(tmp_path: Path) -> None:
    app, service = _make_app(tmp_path)
    session = await service.create_session("alice", "Repair historical source", "local")
    legacy_source = {
        "plugin": "csv",
        "on_success": "main",
        "on_validation_failure": "discard",
        "options": {"schema": {"mode": "observed"}},
    }
    original = await _save_test_composition_state(
        service,
        session.id,
        CompositionStateData(
            sources={"source": legacy_source},
            nodes=[],
            edges=[],
            outputs=[
                {
                    "name": "main",
                    "plugin": "csv",
                    "options": {"path": "outputs/retained.csv"},
                    "on_write_failure": "discard",
                }
            ],
            metadata_={"name": "Legacy café", "description": "authored graph"},
            is_valid=True,
        ),
        provenance="session_seed",
    )
    opaque_metadata = {"legacy_import": {"source": "manual café"}}
    # Genuine pre-plural storage shape: no publication and no sources map.
    with service._engine.begin() as connection:
        connection.execute(
            update(composition_states_table)
            .where(composition_states_table.c.id == str(original.id))
            .values(
                source=envelope_state_column(legacy_source),
                sources=None,
                composer_meta=envelope_state_column(opaque_metadata),
            )
        )
        original_row = connection.execute(select(composition_states_table).where(composition_states_table.c.id == str(original.id))).one()
    legacy = await service.get_current_state(session.id)
    assert legacy is not None and legacy.sources is None
    assert deep_thaw(legacy.source) == legacy_source
    assert read_content_publication(legacy) is None

    client = SyncASGITestClient(app)
    before = client.get(f"/api/sessions/{session.id}/state")
    assert before.status_code == 200, before.text
    assert before.json()["id"] == str(legacy.id)
    body = {"operation_id": str(uuid4()), "state_id": str(legacy.id)}
    with (
        patch(
            "litellm.acompletion", new_callable=AsyncMock, spec=True, side_effect=AssertionError("legacy repair must not dispatch")
        ) as dispatch,
        patch(
            "elspeth.web.composer.prompt_guard_clients.AzurePromptGuardClient.inspect",
            new_callable=AsyncMock,
            spec=True,
            side_effect=AssertionError("legacy repair must not require detector inspection"),
        ) as inspection,
    ):
        response = client.post(f"/api/sessions/{session.id}/state/revert", json=body)
        assert response.status_code == 200, response.text
        replay = client.post(f"/api/sessions/{session.id}/state/revert", json=body)
        dispatch.assert_not_awaited()
        inspection.assert_not_awaited()

    assert replay.status_code == 200, replay.text
    assert replay.json() == response.json()
    repaired = await service.get_current_state(session.id)
    assert repaired is not None and repaired.id != legacy.id and repaired.version == 2
    assert deep_thaw(repaired.sources) == {"source": legacy_source}
    assert response.json()["sources"]["source"] == legacy_source
    assert repaired.nodes == legacy.nodes
    assert repaired.edges == legacy.edges
    assert repaired.outputs == legacy.outputs
    assert repaired.metadata_ == legacy.metadata_
    assert repaired.composer_meta is not None
    assert deep_thaw(repaired.composer_meta["legacy_import"]) == opaque_metadata["legacy_import"]
    publication = read_content_publication(repaired)
    assert publication is not None and publication.state_id == repaired.id
    assert publication.state_version == 2
    assert publication.to_payload()["applicability"] == "requires_current_inspection"
    assert [record.version for record in await service.get_state_versions(session.id)] == [1, 2]
    with service._engine.connect() as connection:
        retained_row = connection.execute(select(composition_states_table).where(composition_states_table.c.id == str(legacy.id))).one()
    assert retained_row == original_row
