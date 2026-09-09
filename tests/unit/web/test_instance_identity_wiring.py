"""Instance identity on the wire — the app.py wiring (6b-3, elspeth-31878c9787).

``web/deployment_profiles.py`` and ``web/middleware/instance_identity.py`` are
unit-tested on their own; these tests pin that ``create_app`` actually wires
them: one identity per process, stamped on EVERY response, reported by
``/api/system/status``, and handed to the session service as the owner of the
fences it acquires — the three places the multi-replica probes read it from.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretBytes

from elspeth.web.app import create_app
from elspeth.web.config import WebSettings
from elspeth.web.deployment_profiles import is_valid_instance_id
from elspeth.web.middleware.instance_identity import INSTANCE_HEADER
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient


def _settings(tmp_path: Path, *, instance_id: str | None = None) -> WebSettings:
    return WebSettings(
        data_dir=tmp_path,
        instance_id=instance_id,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=SecretBytes(b"\x00" * 32),
        operator_metrics_bearer_token="operator-metrics-token-for-tests-0001",
    )


def test_every_response_carries_the_minted_instance_id(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    instance_id = app.state.instance_id
    assert isinstance(instance_id, str) and instance_id.startswith("web-") and is_valid_instance_id(instance_id)
    client = TestClient(app)
    ok = client.get("/api/system/status")
    missing = client.get("/api/this-route-does-not-exist")
    assert ok.status_code == 200
    assert missing.status_code == 404
    assert ok.headers[INSTANCE_HEADER] == instance_id
    assert missing.headers[INSTANCE_HEADER] == instance_id


def test_status_fence_owner_and_header_are_one_identity(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, instance_id="replica-a"))
    assert app.state.instance_id == "replica-a"
    assert app.state.session_service.session_operation_owner_instance_id == "replica-a"
    response = TestClient(app).get("/api/system/status")
    body = response.json()
    assert response.headers[INSTANCE_HEADER] == "replica-a"
    assert body["instance_id"] == "replica-a"
    assert body["deployment_target"] == "default"
    # No platform publishes identity through the environment for this target.
    assert body["deployment_revision"] is None
    assert body["deployment_replica"] is None


def test_two_processes_without_a_pinned_id_are_distinguishable(tmp_path: Path) -> None:
    first = create_app(_settings(tmp_path / "a"))
    second = create_app(_settings(tmp_path / "b"))
    assert first.state.instance_id != second.state.instance_id
    assert TestClient(first).get("/api/system/status").json()["instance_id"] == first.state.instance_id
    assert TestClient(second).get("/api/system/status").json()["instance_id"] == second.state.instance_id
