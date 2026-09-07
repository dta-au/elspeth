"""Tests for tutorial telemetry helpers and routes."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from structlog.testing import capture_logs

from elspeth.contracts.errors import AuditIntegrityError, FrameworkBugError
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.composer import tutorial_telemetry as tutorial_telemetry_module
from elspeth.web.composer.tutorial_abandon_routes import create_tutorial_abandon_router
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient


class _RecordingCounter:
    def __init__(self) -> None:
        self.calls: list[tuple[int, dict[str, object]]] = []

    def add(self, amount: int, *, attributes: dict[str, object]) -> None:
        self.calls.append((amount, dict(attributes)))


class _FailingCounter:
    def add(self, amount: int, *, attributes: dict[str, object]) -> None:
        raise RuntimeError("exporter unavailable")


def test_record_tutorial_completed_rejects_unknown_path() -> None:
    try:
        tutorial_telemetry_module.record_tutorial_completed_path("made_up")  # type: ignore[arg-type]
    except ValueError as exc:
        assert "completion_path" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_abandon_route_increments_counter(monkeypatch) -> None:
    counter = _RecordingCounter()
    monkeypatch.setattr(tutorial_telemetry_module, "_TUTORIAL_ABANDON_COUNTER", counter)
    app = FastAPI()
    identity = UserIdentity(user_id="alice", username="alice")

    async def _mock_user() -> UserIdentity:
        return identity

    app.dependency_overrides[get_current_user] = _mock_user
    app.include_router(create_tutorial_abandon_router())
    client = TestClient(app)

    response = client.post("/api/tutorial/abandon")

    assert response.status_code == 204
    assert counter.calls == [(1, {})]


def test_completed_telemetry_does_not_replace_committed_outcome(monkeypatch) -> None:
    monkeypatch.setattr(tutorial_telemetry_module, "_TUTORIAL_COMPLETED_COUNTER", _FailingCounter())

    with capture_logs() as logs:
        assert tutorial_telemetry_module.record_tutorial_completed_path("first_time") is None
    assert logs == [{"event": "tutorial_telemetry_failed", "operation": "completed", "error_type": "RuntimeError", "log_level": "error"}]


def test_abandon_telemetry_is_best_effort(monkeypatch) -> None:
    monkeypatch.setattr(tutorial_telemetry_module, "_TUTORIAL_ABANDON_COUNTER", _FailingCounter())

    with capture_logs() as logs:
        assert tutorial_telemetry_module.record_tutorial_abandoned() is None
    assert logs == [{"event": "tutorial_telemetry_failed", "operation": "abandoned", "error_type": "RuntimeError", "log_level": "error"}]


@pytest.mark.parametrize("error_class", [FrameworkBugError, AuditIntegrityError])
@pytest.mark.parametrize("site", ["completed", "abandoned", "fallback_logger"])
def test_tutorial_metric_boundaries_propagate_integrity_failures(monkeypatch, error_class, site) -> None:
    failure = error_class("tutorial telemetry integrity failure")

    class FailingBoundary:
        def add(self, *args, **kwargs) -> None:
            raise failure

        def error(self, *args, **kwargs) -> None:
            raise failure

    with pytest.raises(error_class) as caught:
        if site == "completed":
            monkeypatch.setattr(tutorial_telemetry_module, "_TUTORIAL_COMPLETED_COUNTER", FailingBoundary())
            tutorial_telemetry_module.record_tutorial_completed_path("first_time")
        elif site == "abandoned":
            monkeypatch.setattr(tutorial_telemetry_module, "_TUTORIAL_ABANDON_COUNTER", FailingBoundary())
            tutorial_telemetry_module.record_tutorial_abandoned()
        else:
            monkeypatch.setattr(tutorial_telemetry_module, "_log", FailingBoundary())
            tutorial_telemetry_module._log_telemetry_failure(operation="completed", error_type="RuntimeError")
    assert caught.value is failure
