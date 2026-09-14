"""Guided routes record a compose-heartbeat cancel as a server fault, not a user Stop.

Finding #28 (wf2) was fixed on ``send_message`` only. ``_track_compose_inflight``
now cancels a request whose lease renewal fails, with a
``_ComposerHeartbeatCancel`` marker, and answers the structured 503 itself. The
guided PLAN, CHAT and RESPOND cancel arms read ``Task.cancelling() > 0``, which
is true for that cancel too, so they filed it in the durable guided operation
row as ``request_cancelled`` and (PLAN, CHAT) published ``client_cancelled``.

Every heartbeat test is paired with the cancels that must stay user
cancellations: a plain task cancel and, where the route runs the disconnect
watcher (PLAN, CHAT), a client disconnect.

Progression is driven by events: the heartbeat's interval wait is replaced by a
wait on "the route's parked await is running", so no test sleeps or asserts a
duration. ``asyncio.wait_for`` bounds only turn a hang into a red test.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from elspeth.contracts.composer_progress import ComposerProgressEvent
from elspeth.web.composer.progress import ComposerRequestLease
from elspeth.web.coordination.composer_progress_authority import ComposerRequestLeaseLost
from elspeth.web.sessions.routes import _helpers
from elspeth.web.sessions.routes.composer.guided_plan import _guided_full_failed_progress_event
from tests.integration.web.composer.guided.test_chat_schema8_atomic import (
    _blocking_provider,
    _chat_body,
    _guided_operation_row,
)
from tests.integration.web.composer.guided.test_chat_schema8_atomic import (
    _create_session as _create_started_session,
)
from tests.integration.web.composer.guided.test_respond_schema8_atomic import (
    _create_session as _create_rootless_session,
)
from tests.integration.web.composer.guided.test_respond_schema8_atomic import _live_body
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient

_LEASE_LOST_503 = {
    "detail": {
        "error_type": "composer_request_lease_lost",
        "detail": "The server lost this composer request's lease before it finished. Please resubmit.",
    }
}


def _heartbeat_loses_lease_once_running(monkeypatch: pytest.MonkeyPatch, client: TestClient, running: asyncio.Event) -> None:
    """The first heartbeat renewal after ``running`` reports the lease lost.

    Only the first renewal after ``running`` fails, so progress claims made
    earlier in the request renew normally.
    """
    registry = client.app.state.composer_progress_registry
    real_renew = registry.renew_request
    failed = False

    async def renew_request(lease: ComposerRequestLease) -> None:
        nonlocal failed
        if running.is_set() and not failed:
            failed = True
            raise ComposerRequestLeaseLost("Composer request lease cannot be renewed")
        await real_renew(lease)

    async def wait_until_running(seconds: float) -> None:
        del seconds
        await running.wait()

    monkeypatch.setattr(registry, "renew_request", renew_request)
    monkeypatch.setattr(
        _helpers,
        "_COMPOSER_HEARTBEAT_TIMER",
        _helpers._ComposerHeartbeatTimer(now=lambda: 0.0, wait=wait_until_running),
    )


def _capture_request_terminal_status(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    statuses: list[str] = []
    real_finish = _helpers.finish_composer_request_metrics

    def finish(token: Any, *, status: Any, primary_error: BaseException | None) -> None:
        statuses.append(status)
        real_finish(token, status=status, primary_error=primary_error)

    monkeypatch.setattr(_helpers, "finish_composer_request_metrics", finish)
    return statuses


def _latest_progress(client: TestClient, session_id: str) -> Any:
    return asyncio.run(client.app.state.composer_progress_registry.get_latest(session_id))


def _snapshot_event(snapshot: Any) -> ComposerProgressEvent:
    return ComposerProgressEvent(
        phase=snapshot.phase,
        headline=snapshot.headline,
        evidence=snapshot.evidence,
        likely_next=snapshot.likely_next,
        reason=snapshot.reason,
    )


async def _post_heartbeat_cancelled(client: TestClient, path: str, body: dict[str, Any]) -> Any:
    async with AsyncClient(transport=ASGITransport(app=client.app), base_url="http://test") as async_client:
        return await asyncio.wait_for(async_client.post(path, json=body), timeout=10)


async def _post_then_cancel_task(client: TestClient, path: str, body: dict[str, Any], running: asyncio.Event) -> None:
    async with AsyncClient(transport=ASGITransport(app=client.app), base_url="http://test") as async_client:
        request_task = asyncio.create_task(async_client.post(path, json=body))
        await asyncio.wait_for(running.wait(), timeout=10)
        request_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request_task


async def _post_then_disconnect(client: TestClient, path: str, body: dict[str, Any], running: asyncio.Event) -> int:
    """Drive the ASGI app directly and report the client gone once ``running``."""
    request_messages = [{"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if request_messages:
            return request_messages.pop(0)
        await running.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    await asyncio.wait_for(client.app(scope, receive, send), timeout=10)
    return int(next(message["status"] for message in sent if message["type"] == "http.response.start"))


# --------------------------------------------------------------------------- PLAN


class _BlockingPlanner:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def plan_guided_full_pipeline(self, **kwargs: Any) -> Any:
        del kwargs
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable: the parked planner never completes")


def _plan_setup(client: TestClient) -> tuple[str, str, dict[str, str], _BlockingPlanner]:
    planner = _BlockingPlanner()
    client.app.state.composer_service = planner
    session_id = str(client.post("/api/sessions", json={"title": "guided plan heartbeat"}).json()["id"])
    body = {"operation_id": "00000000-0000-4000-8000-0000000000a1", "intent": "Plan until the heartbeat fails."}
    return session_id, f"/api/sessions/{session_id}/guided/plan", body, planner


def test_guided_plan_heartbeat_cancel_records_operation_failed_and_answers_503(
    composer_test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = composer_test_client
    session_id, path, body, planner = _plan_setup(client)
    _heartbeat_loses_lease_once_running(monkeypatch, client, planner.started)
    statuses = _capture_request_terminal_status(monkeypatch)

    response = asyncio.run(_post_heartbeat_cancelled(client, path, body))

    assert response.status_code == 503
    assert response.json() == _LEASE_LOST_503
    operation = _guided_operation_row(client, session_id, body["operation_id"])
    assert operation["status"] == "failed"
    assert operation["failure_code"] == "operation_failed"
    snapshot = _latest_progress(client, session_id)
    assert _snapshot_event(snapshot) == _guided_full_failed_progress_event("operation_failed")
    assert snapshot.inflight_requests == 0
    assert statuses == ["failed"]


def test_guided_plan_plain_task_cancel_is_still_request_cancelled(
    composer_test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = composer_test_client
    session_id, path, body, planner = _plan_setup(client)
    statuses = _capture_request_terminal_status(monkeypatch)

    asyncio.run(_post_then_cancel_task(client, path, body, planner.started))

    operation = _guided_operation_row(client, session_id, body["operation_id"])
    assert operation["status"] == "failed"
    assert operation["failure_code"] == "request_cancelled"
    snapshot = _latest_progress(client, session_id)
    assert (snapshot.phase, snapshot.reason) == ("cancelled", "client_cancelled")
    assert statuses == ["cancelled"]


def test_guided_plan_client_disconnect_is_still_request_cancelled(
    composer_test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = composer_test_client
    session_id, path, body, planner = _plan_setup(client)
    statuses = _capture_request_terminal_status(monkeypatch)

    status = asyncio.run(_post_then_disconnect(client, path, body, planner.started))

    assert status == 499
    operation = _guided_operation_row(client, session_id, body["operation_id"])
    assert operation["status"] == "failed"
    assert operation["failure_code"] == "request_cancelled"
    snapshot = _latest_progress(client, session_id)
    assert (snapshot.phase, snapshot.reason) == ("cancelled", "client_cancelled")
    assert statuses == ["cancelled"]


# --------------------------------------------------------------------------- CHAT


def _chat_setup(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str, dict[str, str], asyncio.Event]:
    session_id = _create_started_session(client)
    turn = client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    started = _blocking_provider(monkeypatch)
    return session_id, f"/api/sessions/{session_id}/guided/chat", _chat_body(turn), started


def test_guided_chat_heartbeat_cancel_records_operation_failed_and_answers_503(
    composer_test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = composer_test_client
    session_id, path, body, started = _chat_setup(client, monkeypatch)
    _heartbeat_loses_lease_once_running(monkeypatch, client, started)
    statuses = _capture_request_terminal_status(monkeypatch)

    response = asyncio.run(_post_heartbeat_cancelled(client, path, body))

    assert response.status_code == 503
    assert response.json() == _LEASE_LOST_503
    operation = _guided_operation_row(client, session_id, body["operation_id"])
    assert operation["status"] == "failed"
    assert operation["failure_code"] == "operation_failed"
    snapshot = _latest_progress(client, session_id)
    assert _snapshot_event(snapshot) == _helpers._composer_heartbeat_failed_progress_event()
    assert snapshot.inflight_requests == 0
    assert statuses == ["failed"]


def test_guided_chat_plain_task_cancel_is_still_request_cancelled(
    composer_test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = composer_test_client
    session_id, path, body, started = _chat_setup(client, monkeypatch)
    statuses = _capture_request_terminal_status(monkeypatch)

    asyncio.run(_post_then_cancel_task(client, path, body, started))

    operation = _guided_operation_row(client, session_id, body["operation_id"])
    assert operation["status"] == "failed"
    assert operation["failure_code"] == "request_cancelled"
    snapshot = _latest_progress(client, session_id)
    assert (snapshot.phase, snapshot.reason) == ("cancelled", "client_cancelled")
    assert statuses == ["cancelled"]


def test_guided_chat_client_disconnect_is_still_request_cancelled(
    composer_test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = composer_test_client
    session_id, path, body, started = _chat_setup(client, monkeypatch)
    statuses = _capture_request_terminal_status(monkeypatch)

    status = asyncio.run(_post_then_disconnect(client, path, body, started))

    assert status == 499
    operation = _guided_operation_row(client, session_id, body["operation_id"])
    assert operation["status"] == "failed"
    assert operation["failure_code"] == "request_cancelled"
    snapshot = _latest_progress(client, session_id)
    assert (snapshot.phase, snapshot.reason) == ("cancelled", "client_cancelled")
    assert statuses == ["cancelled"]


# --------------------------------------------------------------------------- RESPOND
#
# RESPOND runs no disconnect watcher (``_cancel_on_client_disconnect`` is not
# entered anywhere in guided.py), so a client disconnect never reaches its
# cancel arm; there is no disconnect control for it. RESPOND publishes no
# terminal progress on any cancellation, so the tests assert none.


def _respond_setup(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str, dict[str, Any], asyncio.Event]:
    session_id = _create_rootless_session(client)
    turn = client.get(f"/api/sessions/{session_id}/guided").json()["next_turn"]
    body = _live_body(turn, chosen=[turn["payload"]["options"][0]["id"]])
    settling = asyncio.Event()
    service = client.app.state.session_service

    async def parked_settlement(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        settling.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable: the parked settlement never completes")

    monkeypatch.setattr(service, "settle_guided_state_operation", parked_settlement)
    return session_id, f"/api/sessions/{session_id}/guided/respond", body, settling


def test_guided_respond_heartbeat_cancel_records_operation_failed_and_answers_503(
    composer_test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = composer_test_client
    session_id, path, body, settling = _respond_setup(client, monkeypatch)
    _heartbeat_loses_lease_once_running(monkeypatch, client, settling)
    statuses = _capture_request_terminal_status(monkeypatch)

    response = asyncio.run(_post_heartbeat_cancelled(client, path, body))

    assert response.status_code == 503
    assert response.json() == _LEASE_LOST_503
    operation = _guided_operation_row(client, session_id, body["operation_id"])
    assert operation["status"] == "failed"
    assert operation["failure_code"] == "operation_failed"
    assert statuses == ["failed"]


def test_guided_respond_plain_task_cancel_is_still_request_cancelled(
    composer_test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = composer_test_client
    session_id, path, body, settling = _respond_setup(client, monkeypatch)
    statuses = _capture_request_terminal_status(monkeypatch)

    asyncio.run(_post_then_cancel_task(client, path, body, settling))

    operation = _guided_operation_row(client, session_id, body["operation_id"])
    assert operation["status"] == "failed"
    assert operation["failure_code"] == "request_cancelled"
    assert statuses == ["cancelled"]
