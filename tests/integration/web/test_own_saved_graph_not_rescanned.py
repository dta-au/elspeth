"""A session's own saved graph is not re-scanned for credential material.

The graph a session saved is the session's own data. Credential admission runs
where text arrives from outside (messages, tool arguments, YAML import, library
fork) and on the bytes sent to a model; reading, validating, approving,
executing, sharing, publishing or composing on the saved graph does not walk
it again.

Two properties, each through the real application:

* A credential-shaped value planted straight into the saved graph does not stop
  any of the paths that used to re-scan it (``GET /state``, validate, approval
  request, execute, mark-ready-for-review, library publish).
* A graph past the scan's node limit opens on ``GET /state`` and a compose turn
  on it reaches the provider, with no whole-graph scan (and so no counted skip)
  on either path.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import insert

from elspeth.contracts.credential_material import find_credential_material, is_credential_match
from elspeth.web import credential_scan
from elspeth.web.app import create_app
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.composer.provider_gateway import _admit_composer_llm_completion
from elspeth.web.composer.service import ComposerAvailability
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.config import WebSettings
from elspeth.web.credential_guard import (
    CredentialMaterialRefused,
    credential_material_tool_arguments_projection,
    require_no_credential_material_in_state,
)
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import identity_roles_table
from elspeth.web.sessions.protocol import CompositionStateData
from tests.fixtures.identities import ensure_test_identity
from tests.integration.web.conftest import _passthrough_composition_state, _save_composition_state_with_compose_authority
from tests.unit.web.composer._helpers import _composer_service_with_session, _make_llm_response, _make_settings, _mock_catalog
from tests.unit.web.sessions.test_import_scans_bound_state import FIRST_OVER_SCAN, _import_body
from tests.unit.web.sessions.test_import_source_binding_prompt_file import _Env
from tests.unit.web.sessions.test_routes import _close_app_session_engines as _close_app_session_engines

# Built at runtime so the source carries no literal token. The admission detector matches it.
PLANTED_TOKEN = "sk-" + "a" * 24
# The surfaces that still scan during a compose turn on a held graph: the bytes sent to a model.
PROVIDER_SCAN_SURFACES = frozenset({"composer_provider_request"})


class _SkipRecorder:
    """Stands in for the OTel skip counter and keeps each skip's attributes."""

    def __init__(self) -> None:
        self.skips: list[dict[str, str]] = []

    def add(self, value: int, attributes: dict[str, str]) -> None:
        assert value == 1
        self.skips.append(dict(attributes))


# ── Test 2: a credential-shaped value planted in the saved graph ─────────────


@dataclass(frozen=True, slots=True)
class _PlantedApp:
    client: AsyncClient
    app: FastAPI
    settings: WebSettings


@pytest_asyncio.fixture
async def planted_app(tmp_path: Path) -> AsyncIterator[_PlantedApp]:
    """The closed-local app with workflow governance on, so approvals and library publish are routed."""
    for name in ("blobs", "outputs", "runs"):
        (tmp_path / name).mkdir()
    (tmp_path / "payloads").mkdir(mode=0o700)
    settings = WebSettings(
        data_dir=tmp_path,
        landscape_url=f"sqlite:///{tmp_path}/runs/audit.db",
        payload_store_path=tmp_path / "payloads",
        auth_provider="local",
        registration_mode="closed",
        workflow_governance="on",
        compartment_id="team-red",
        plugin_allowlist=("transform:passthrough",),
        composer_max_composition_turns=15,
        composer_context_max_prompt_tokens=200_000,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_boot_probe_enabled=False,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
    )
    app = create_app(settings=settings)
    with app.state.session_engine.begin() as conn:
        for identity_id in ("alice", "bob"):
            ensure_test_identity(conn, identity_id=identity_id)
        for identity_id, role in (("alice", "admin"), ("alice", "user"), ("bob", "approver")):
            conn.execute(
                insert(identity_roles_table).values(
                    role_id=str(uuid.uuid4()),
                    identity_id=identity_id,
                    role=role,
                    granted_by_identity_id="alice",
                    granted_at=datetime.now(UTC),
                )
            )
    for identity_id in ("alice", "bob"):
        app.state.auth_provider.create_user(identity_id, "password123", display_name=identity_id)
    async with (
        LifespanManager(app, startup_timeout=15, shutdown_timeout=30) as manager,
        AsyncClient(transport=ASGITransport(app=manager.app), base_url="http://test") as client,
    ):
        yield _PlantedApp(client, app, settings)


async def _bearer(planted: _PlantedApp, identity_id: str) -> dict[str, str]:
    response = await planted.client.post("/api/auth/login", json={"username": identity_id, "password": "password123"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _planted_state(data_dir: Path, session_id: uuid.UUID) -> CompositionState:
    """The passthrough fixture graph with the token planted in its pipeline description."""
    state = _passthrough_composition_state(data_dir, session_id)
    return replace(state, metadata=PipelineMetadata(name=state.metadata.name, description=PLANTED_TOKEN))


def _assert_the_removed_scan_would_refuse(state: CompositionState) -> None:
    """Control: the planted value is credential material to the detector and to the removed state guard."""
    finding = find_credential_material(state.to_dict(), purpose="admission")
    assert finding is not None
    assert is_credential_match(finding)
    with pytest.raises(CredentialMaterialRefused):
        require_no_credential_material_in_state(state, surface="composer_library_fork_state")


async def _planted_session(planted: _PlantedApp, alice: dict[str, str]) -> tuple[str, str]:
    """Create a session through the route and save the planted graph straight into it."""
    created = await planted.client.post("/api/sessions", headers=alice, json={"title": "planted state"})
    assert created.status_code == 201, created.text
    session_id = uuid.UUID(created.json()["id"])
    blob_dir = planted.settings.data_dir / "blobs" / str(session_id)
    blob_dir.mkdir(parents=True, exist_ok=True)
    (planted.settings.data_dir / "outputs" / str(session_id)).mkdir(parents=True, exist_ok=True)
    (blob_dir / "audit_readiness_fixture.csv").write_text("id,name,value\n1,one,10\n", encoding="utf-8")
    state = _planted_state(planted.settings.data_dir, session_id)
    _assert_the_removed_scan_would_refuse(state)
    payload = state.to_dict()
    record = await _save_composition_state_with_compose_authority(
        planted.app.state.session_service,
        session_id,
        CompositionStateData(
            sources=payload["sources"],
            nodes=payload["nodes"],
            edges=payload["edges"],
            outputs=payload["outputs"],
            metadata_=payload["metadata"],
            is_valid=True,
            validation_errors=None,
        ),
        provenance="session_seed",
    )
    return str(session_id), str(record.id)


async def _request_approval(planted: _PlantedApp, session_id: str, state_id: str, alice: dict[str, str]) -> Response:
    return await planted.client.post(
        f"/api/sessions/{session_id}/approvals",
        headers=alice,
        json={"state_id": state_id, "approver_identity_id": "bob"},
    )


@pytest.mark.asyncio
async def test_state_read_returns_a_saved_graph_holding_a_planted_token(planted_app: _PlantedApp) -> None:
    alice = await _bearer(planted_app, "alice")
    session_id, state_id = await _planted_session(planted_app, alice)

    response = await planted_app.client.get(f"/api/sessions/{session_id}/state", headers=alice)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == state_id
    assert body["metadata"]["description"] == PLANTED_TOKEN


@pytest.mark.asyncio
async def test_validate_accepts_a_saved_graph_holding_a_planted_token(planted_app: _PlantedApp) -> None:
    alice = await _bearer(planted_app, "alice")
    session_id, _state_id = await _planted_session(planted_app, alice)

    response = await planted_app.client.post(f"/api/sessions/{session_id}/validate", headers=alice)

    assert response.status_code == 200, response.text
    assert response.json()["is_valid"] is True


@pytest.mark.asyncio
async def test_approval_request_compiles_a_saved_graph_holding_a_planted_token(planted_app: _PlantedApp) -> None:
    alice = await _bearer(planted_app, "alice")
    session_id, state_id = await _planted_session(planted_app, alice)

    response = await _request_approval(planted_app, session_id, state_id, alice)

    assert response.status_code == 201, response.text
    assert response.json()["state_id"] == state_id


@pytest.mark.asyncio
async def test_execute_admits_a_saved_graph_holding_a_planted_token(planted_app: _PlantedApp) -> None:
    alice = await _bearer(planted_app, "alice")
    bob = await _bearer(planted_app, "bob")
    session_id, state_id = await _planted_session(planted_app, alice)
    # Governance is on: an approved request for this exact state lets execute reach the run start.
    requested = await _request_approval(planted_app, session_id, state_id, alice)
    assert requested.status_code == 201, requested.text
    decided = await planted_app.client.post(
        f"/api/approvals/{requested.json()['approval_id']}/decide", headers=bob, json={"decision": "approved"}
    )
    assert decided.status_code == 200, decided.text

    response = await planted_app.client.post(f"/api/sessions/{session_id}/execute", headers=alice)

    # 202 is the run admitted; the run itself proceeds in the background and its outcome is not asserted here.
    assert response.status_code == 202, response.text
    assert response.json()["run_id"]


@pytest.mark.asyncio
async def test_mark_ready_for_review_validates_a_saved_graph_holding_a_planted_token(planted_app: _PlantedApp) -> None:
    alice = await _bearer(planted_app, "alice")
    session_id, _state_id = await _planted_session(planted_app, alice)

    response = await planted_app.client.post(f"/api/sessions/{session_id}/mark-ready-for-review", headers=alice)

    assert response.status_code == 200, response.text
    assert response.json()["payload_digest"].startswith("sha256:")


@pytest.mark.asyncio
async def test_library_publish_accepts_a_saved_graph_holding_a_planted_token(planted_app: _PlantedApp) -> None:
    alice = await _bearer(planted_app, "alice")
    session_id, _state_id = await _planted_session(planted_app, alice)

    response = await planted_app.client.post(
        f"/api/sessions/{session_id}/library/publish", headers=alice, json={"title": "planted pipeline"}
    )

    assert response.status_code == 201, response.text
    assert response.json()["state"] == "pending"


# ── Test 1: a graph past the scan's node limit ───────────────────────────────


@pytest.fixture
def env(tmp_path: Path) -> _Env:
    return _Env(tmp_path)


def test_a_graph_past_the_scan_limit_opens_and_its_compose_turn_reaches_the_provider(
    env: _Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The graph is stored through the real YAML import (which still scans, best effort), then opened and composed on.
    session_id = asyncio.run(env.session())
    imported = env.import_yaml(session_id, asyncio.run(_import_body(env, session_id, FIRST_OVER_SCAN)))
    assert imported.status_code == 200, imported.text
    record = asyncio.run(env.service.get_current_state(session_id))
    assert record is not None
    state = state_from_record(record)
    # Control: the whole-graph scan the removed sites ran stops at its node limit on this graph.
    limit = find_credential_material(credential_material_tool_arguments_projection("set_pipeline", state.to_dict()), purpose="admission")
    assert limit is not None
    assert (limit.category, limit.bound) == ("traversal_limit", "nodes")

    recorder = _SkipRecorder()
    monkeypatch.setattr(credential_scan, "_CREDENTIAL_SCAN_SKIPPED_COUNTER", recorder)

    opened = env.client.get(f"/api/sessions/{session_id}/state")

    assert opened.status_code == 200, opened.text
    assert opened.json()["id"] == str(record.id)
    assert len(opened.json()["nodes"][0]["options"]["queries"]) == FIRST_OVER_SCAN
    # No whole-graph scan ran on the read, so none stopped at the limit.
    assert recorder.skips == []

    # The route harnesses replace compose() or enter below it; the removed turn-start scan lived in compose(),
    # so the turn is driven through the real ComposerServiceImpl.compose with only the provider call mocked.
    def _available(*, model: str, **_kwargs: object) -> ComposerAvailability:
        return ComposerAvailability(available=True, model=model, provider="test")

    monkeypatch.setattr("elspeth.web.composer.service.compute_availability", _available)
    service, composer_session_id = _composer_service_with_session(_mock_catalog(), _make_settings(data_dir=tmp_path))

    async def _compose() -> tuple[object, int]:
        with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as provider:
            provider.return_value = _admit_composer_llm_completion(_make_llm_response(content="Here is a review of the pipeline."))
            result = await service.compose("Review this pipeline", [], state, session_id=composer_session_id)
        return result, provider.await_count

    result, provider_calls = asyncio.run(_compose())

    assert isinstance(result, ComposerResult)
    assert provider_calls == 1
    assert {skip["surface"] for skip in recorder.skips} <= PROVIDER_SCAN_SURFACES
