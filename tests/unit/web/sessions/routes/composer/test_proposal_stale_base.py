"""An obsolete proposal base is distinct from retryable lease contention."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

from tests.unit.web._sync_asgi_client import SyncASGITestClient
from tests.unit.web.sessions.test_routes import (
    _create_test_composition_proposal,
    _make_app,
    _save_test_composition_state,
)

from elspeth.web.sessions.protocol import CompositionStateData


def test_stale_base_has_named_refusal_and_remains_rejectable(tmp_path: Path) -> None:
    app, service = _make_app(tmp_path)
    client = SyncASGITestClient(app)
    session = client.post("/api/sessions", json={"title": "Obsolete proposal"}).json()
    session_id = UUID(session["id"])
    base = asyncio.run(_save_test_composition_state(service, session_id, CompositionStateData(), provenance="session_seed"))
    proposal = asyncio.run(
        _create_test_composition_proposal(
            service,
            session_id=session_id,
            tool_call_id="stale-base-call",
            tool_name="set_metadata",
            summary="Rename pipeline",
            rationale="Requested by the user",
            affects=("metadata",),
            arguments_json={"patch": {"name": "Requested name"}},
            arguments_redacted_json={"patch": "<metadata-patch:name>"},
            base_state_id=base.id,
            actor="composer-web:user:alice",
        )
    )
    newer = asyncio.run(_save_test_composition_state(service, session_id, CompositionStateData(), provenance="session_seed"))
    response = client.post(f"/api/sessions/{session_id}/proposals/{proposal.id}/accept")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_type"] == "proposal_base_state_changed"
    current = asyncio.run(service.get_current_state(session_id))
    assert current is not None
    assert current.id == newer.id
    assert asyncio.run(service.list_composition_proposals(session_id))[0].status == "pending"
    rejected = client.post(f"/api/sessions/{session_id}/proposals/{proposal.id}/reject", json={})
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
