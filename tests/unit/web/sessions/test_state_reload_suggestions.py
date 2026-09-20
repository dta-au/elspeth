"""Reload derives suggestion rows from the current persisted composition."""

from pathlib import Path
from uuid import uuid4

import pytest

from elspeth.web.composer.guided.state_machine import GuidedSession
from elspeth.web.composer.redaction import REDACTED_BLOB_SOURCE_PATH
from elspeth.web.sessions.protocol import CompositionStateData
from tests.unit.web._sync_asgi_client import SyncASGITestClient
from tests.unit.web.sessions.test_routes import _make_app, _save_test_composition_state


@pytest.mark.asyncio
@pytest.mark.parametrize("guided", [False, True], ids=["freeform", "guided"])
async def test_reload_recomputes_suggestions_and_discards_previous_state_advice(tmp_path: Path, guided: bool) -> None:
    app, service = _make_app(tmp_path)
    client = SyncASGITestClient(app)
    session = await service.create_session("alice", "Reload advice", "local")
    composer_meta = {"guided_session": GuidedSession.initial().to_dict()} if guided else None
    private_source_path = str(tmp_path / "private-input.csv")
    initial = CompositionStateData(
        sources={
            "source": {
                "plugin": "csv",
                "options": {"path": private_source_path, "blob_ref": str(uuid4())},
                "on_success": "main",
                "on_validation_failure": "discard",
            }
        },
        outputs=[
            {
                "name": "main",
                "plugin": "csv",
                "options": {"path": "output.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            }
        ],
        metadata_={"name": "Reload advice", "description": ""},
        composer_meta=composer_meta,
        is_valid=False,
    )
    first = await _save_test_composition_state(service, session.id, initial, provenance="session_seed")

    response = client.get(f"/api/sessions/{session.id}/state")
    assert response.status_code == 200
    assert response.json()["id"] == str(first.id)
    assert response.json()["sources"]["source"]["options"]["path"] == REDACTED_BLOB_SOURCE_PATH
    assert private_source_path not in response.text
    assert response.json()["validation_suggestions"] == [
        {
            "component": "source",
            "message": "Source has no explicit schema. Downstream field references depend on runtime column names.",
            "severity": "low",
            "error_code": None,
        }
    ]
    assert response.json()["is_valid"] is False

    updated = CompositionStateData(
        sources={
            "source": {
                "plugin": "csv",
                "options": {"path": "input.csv", "schema": {"mode": "observed"}},
                "on_success": "main",
                "on_validation_failure": "discard",
            }
        },
        outputs=initial.outputs,
        metadata_=initial.metadata_,
        composer_meta=composer_meta,
        is_valid=True,
    )
    second = await _save_test_composition_state(service, session.id, updated, provenance="session_seed")
    response = client.get(f"/api/sessions/{session.id}/state")
    assert response.status_code == 200
    assert response.json()["id"] == str(second.id)
    assert response.json()["validation_suggestions"] == []
    assert response.json()["is_valid"] is True
    assert await service.get_current_state(session.id) == second

    history = client.get(f"/api/sessions/{session.id}/state/versions")
    assert history.status_code == 200
    assert [row["validation_suggestions"] for row in history.json()] == [None, None]
