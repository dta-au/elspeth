"""COMPOSE lease release on the early-failure paths of the state-write routes.

``POST /state/yaml`` and ``POST /state/e2e-seed`` each take a self-renewing
COMPOSE ``SessionOperationLease``. Anything that raises between the acquire
and the ``try/finally`` that closes it leaks that lease for the life of the
process, so every later COMPOSE writer on the session (chat, export, import,
seed) is refused as "already active". These tests make the pre-body lookups
raise and then prove the session still accepts a COMPOSE writer.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient
from tests.unit.web.sessions.test_e2e_state_seed_route import _make_app as _make_seed_app
from tests.unit.web.sessions.test_e2e_state_seed_route import _ready_readiness, _valid_state
from tests.unit.web.sessions.test_routes import _make_app

from elspeth.web.execution.schemas import ValidationResult

_IMPORT_YAML = """
sources:
  source:
    plugin: csv
    on_success: main
    options:
      schema:
        mode: observed
sinks:
  main:
    plugin: csv
    options:
      path: outputs/out.csv
    on_write_failure: discard
"""


async def _pass_preflight(state, *, settings, secret_service, user_id, session_id, **_policy_context):
    return ValidationResult(is_valid=True, checks=[], errors=[], readiness=_ready_readiness())


class _LockLookupFailedError(RuntimeError):
    """Stand-in for a registry failure while the route resolves its compose lock."""


class _FailingComposeLockRegistry:
    async def get_lock(self, session_id: str) -> object:
        raise _LockLookupFailedError(session_id)


@pytest.mark.asyncio
async def test_import_policy_context_failure_does_not_strand_the_compose_lease(tmp_path: Path) -> None:
    app, service = _make_app(tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    session = await service.create_session("alice", "Import lease", "local")

    with patch(
        "elspeth.web.sessions.routes.composer.state._request_plugin_policy_context",
        side_effect=HTTPException(status_code=503, detail="plugin policy snapshot unavailable"),
    ):
        failed = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": _IMPORT_YAML})
    assert failed.status_code == 503, failed.text

    # Export is another COMPOSE writer: with a stranded lease it is refused
    # instead of reporting that no state exists.
    exported = client.get(f"/api/sessions/{session.id}/state/yaml")
    assert exported.status_code == 404, exported.text
    assert exported.json()["detail"] == "No composition state exists"

    with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
        retried = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": _IMPORT_YAML})
    assert retried.status_code == 200, retried.text
    assert await service.get_current_state(session.id) is not None


@pytest.mark.asyncio
async def test_import_compose_lock_lookup_failure_does_not_strand_the_compose_lease(tmp_path: Path) -> None:
    app, service = _make_app(tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    session = await service.create_session("alice", "Import lock lease", "local")

    with patch(
        "elspeth.web.sessions.routes.composer.state._get_session_compose_lock_registry",
        return_value=_FailingComposeLockRegistry(),
    ):
        failed = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": _IMPORT_YAML})
    assert failed.status_code == 500

    with patch("elspeth.web.sessions.routes.composer.state._runtime_preflight_for_state", side_effect=_pass_preflight):
        retried = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": _IMPORT_YAML})
    assert retried.status_code == 200, retried.text


@pytest.mark.asyncio
async def test_seed_compose_lock_lookup_failure_does_not_strand_the_compose_lease(tmp_path: Path) -> None:
    app, service = _make_seed_app(tmp_path, e2e_state_seed_enabled=True)
    client = TestClient(app, raise_server_exceptions=False)
    session = await service.create_session("alice", "Seed lease", "local")
    body = {"state": _valid_state(tmp_path, session_id=str(session.id)).to_dict()}

    with patch(
        "elspeth.web.sessions.routes.composer.state._get_session_compose_lock_registry",
        return_value=_FailingComposeLockRegistry(),
    ):
        failed = client.post(f"/api/sessions/{session.id}/state/e2e-seed", json=body)
    assert failed.status_code == 500

    with patch("elspeth.web.sessions.routes._helpers._runtime_preflight_for_state", side_effect=_pass_preflight):
        retried = client.post(f"/api/sessions/{session.id}/state/e2e-seed", json=body)
    assert retried.status_code == 200, retried.text
    assert await service.get_current_state(session.id) is not None


@pytest.mark.asyncio
async def test_seed_invalid_body_does_not_strand_the_compose_lease(tmp_path: Path) -> None:
    app, service = _make_seed_app(tmp_path, e2e_state_seed_enabled=True)
    client = TestClient(app, raise_server_exceptions=False)
    session = await service.create_session("alice", "Seed body lease", "local")

    rejected = client.post(f"/api/sessions/{session.id}/state/e2e-seed", json={"not_state": 1})
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["detail"] == "Invalid seed request JSON"

    body = {"state": _valid_state(tmp_path, session_id=str(session.id)).to_dict()}
    with patch("elspeth.web.sessions.routes._helpers._runtime_preflight_for_state", side_effect=_pass_preflight):
        retried = client.post(f"/api/sessions/{session.id}/state/e2e-seed", json=body)
    assert retried.status_code == 200, retried.text
