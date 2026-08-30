from __future__ import annotations

import asyncio
import json
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from structlog.testing import capture_logs

from elspeth.web.auth.models import UserIdentity
from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions.routes.composer import state as composer_state_routes
from elspeth.web.sessions.schemas import UpdateComposerPreferencesRequest
from elspeth.web.sessions.telemetry import observed_value


@pytest.mark.parametrize("failure_site", ["renewal_loss", "close_loss"])
def test_committed_preferences_transition_survives_late_fence_loss(
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    failure_site: str,
) -> None:
    session = test_client.post("/api/sessions", json={"title": "Committed preferences"}).json()
    original_close = SessionOperationLease.close
    observed_close_loss = False

    if failure_site == "renewal_loss":

        async def close_after_late_renewal_loss(lease: SessionOperationLease) -> None:
            nonlocal observed_close_loss
            lease._record_renewal_error(SessionOperationFenceLost(FenceLossReason.LEASE_EXPIRED))
            try:
                await original_close(lease)
            except SessionOperationFenceLost:
                observed_close_loss = True
                raise

        monkeypatch.setattr(SessionOperationLease, "close", close_after_late_renewal_loss)
    else:

        async def close_then_raise_lost(lease: SessionOperationLease) -> None:
            await original_close(lease)
            raise SessionOperationFenceLost(FenceLossReason.RELEASED)

        monkeypatch.setattr(SessionOperationLease, "close", close_then_raise_lost)

    client = TestClient(test_client.app, raise_server_exceptions=False)
    response = client.patch(
        f"/api/sessions/{session['id']}/composer/preferences",
        json={"trust_mode": "explicit_approve", "density_default": "medium"},
    )

    assert response.status_code == 200
    assert response.json()["trust_mode"] == "explicit_approve"
    if failure_site == "renewal_loss":
        assert observed_close_loss
    events = test_client.get(f"/api/sessions/{session['id']}/proposal-events").json()
    assert [event["event_type"] for event in events].count("trust_mode.changed") == 1
    assert observed_value(test_client.app.state.sessions_telemetry.session_switched_total) == 1


@pytest.mark.parametrize("fault_site", ["telemetry", "cleanup"])
def test_committed_preferences_transition_logs_sanitized_postcommit_failure_without_masking_success(
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fault_site: str,
) -> None:
    session = test_client.post("/api/sessions", json={"title": "Postcommit fault"}).json()
    original_close = SessionOperationLease.close
    secret = "postgresql://operator:secret@example.invalid/elspeth"  # secret-scan: allow-this-line

    if fault_site == "telemetry":

        def fail_telemetry(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError(secret)

        monkeypatch.setattr(composer_state_routes, "record_session_switched", fail_telemetry)
        expected_event = "composer_preferences_postcommit_telemetry_failed"
    else:

        async def close_then_fail(lease: SessionOperationLease) -> None:
            await original_close(lease)
            raise RuntimeError(secret)

        monkeypatch.setattr(SessionOperationLease, "close", close_then_fail)
        expected_event = "composer_preferences_postcommit_cleanup_failed"
    client = TestClient(test_client.app, raise_server_exceptions=False)
    with capture_logs() as logs:
        response = client.patch(
            f"/api/sessions/{session['id']}/composer/preferences",
            json={"trust_mode": "explicit_approve", "density_default": "medium"},
        )

    assert response.status_code == 200
    assert response.json()["trust_mode"] == "explicit_approve"
    assert secret not in json.dumps(logs)
    assert {
        "event": expected_event,
        "session_id": session["id"],
        "exc_class": "RuntimeError",
        "log_level": "error",
    } in logs


def test_precommit_preferences_failure_preserves_primary_and_attaches_cleanup_type(
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = test_client.post("/api/sessions", json={"title": "Primary failure"}).json()
    service = test_client.app.state.session_service
    original_close = SessionOperationLease.close
    primary = ValueError("primary service failure")

    async def fail_update(*_args: object, **_kwargs: object) -> object:
        raise primary

    async def close_then_fail(lease: SessionOperationLease) -> None:
        await original_close(lease)
        raise RuntimeError("sensitive cleanup failure")

    monkeypatch.setattr(service, "update_composer_preferences", fail_update)
    monkeypatch.setattr(SessionOperationLease, "close", close_then_fail)

    with pytest.raises(ValueError) as exc_info:
        test_client.patch(
            f"/api/sessions/{session['id']}/composer/preferences",
            json={"trust_mode": "explicit_approve", "density_default": "medium"},
        )

    assert exc_info.value is primary
    assert exc_info.value.__notes__ == ["Composer preferences lease cleanup also failed with RuntimeError."]
    events = test_client.get(f"/api/sessions/{session['id']}/proposal-events").json()
    assert [event["event_type"] for event in events].count("trust_mode.changed") == 0


@pytest.mark.asyncio
async def test_cancellation_after_preferences_commit_drains_close_and_preserves_one_transition(
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = test_client.post("/api/sessions", json={"title": "Cancelled after commit"}).json()
    service = test_client.app.state.session_service
    original_update = type(service).update_composer_preferences
    original_close = SessionOperationLease.close
    close_started = asyncio.Event()
    close_finished = asyncio.Event()

    async def update_then_cancel(self: object, *args: object, **kwargs: object) -> object:
        transition = await original_update(self, *args, **kwargs)
        task = asyncio.current_task()
        assert task is not None
        asyncio.get_running_loop().call_soon(task.cancel)
        return transition

    async def observed_close(lease: SessionOperationLease) -> None:
        close_started.set()
        try:
            await original_close(lease)
        finally:
            close_finished.set()

    monkeypatch.setattr(type(service), "update_composer_preferences", update_then_cancel)
    monkeypatch.setattr(SessionOperationLease, "close", observed_close)
    request = Request({"type": "http", "app": test_client.app})

    with pytest.raises(asyncio.CancelledError):
        await composer_state_routes.update_composer_preferences(
            UUID(session["id"]),
            UpdateComposerPreferencesRequest(trust_mode="explicit_approve", density_default="medium"),
            request,
            UserIdentity(user_id="alice", username="alice"),
        )

    assert close_started.is_set()
    assert close_finished.is_set()
    events = await service.list_proposal_events(UUID(session["id"]))
    assert [event.event_type for event in events].count("trust_mode.changed") == 1
    preferences = await service.get_composer_preferences(UUID(session["id"]))
    assert preferences.trust_mode == "explicit_approve"
    assert preferences.density_default == "medium"
    assert observed_value(test_client.app.state.sessions_telemetry.session_switched_total) == 1


@pytest.mark.asyncio
async def test_precommit_cleanup_cancellation_propagates_instead_of_being_replaced_by_primary(
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = test_client.post("/api/sessions", json={"title": "Cleanup cancellation"}).json()
    service = test_client.app.state.session_service
    original_close = SessionOperationLease.close
    primary = ValueError("primary service failure")
    cleanup_cancellation = asyncio.CancelledError()

    async def fail_update(*_args: object, **_kwargs: object) -> object:
        raise primary

    async def close_then_cancel(lease: SessionOperationLease) -> None:
        await original_close(lease)
        raise cleanup_cancellation

    monkeypatch.setattr(service, "update_composer_preferences", fail_update)
    monkeypatch.setattr(SessionOperationLease, "close", close_then_cancel)
    request = Request({"type": "http", "app": test_client.app})

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await composer_state_routes.update_composer_preferences(
            UUID(session["id"]),
            UpdateComposerPreferencesRequest(trust_mode="explicit_approve", density_default="medium"),
            request,
            UserIdentity(user_id="alice", username="alice"),
        )

    assert exc_info.value is cleanup_cancellation
    assert exc_info.value.__notes__ == ["Composer preferences operation also failed with ValueError."]
    events = await service.list_proposal_events(UUID(session["id"]))
    assert [event.event_type for event in events].count("trust_mode.changed") == 0
