"""Recompose labels a compose-heartbeat cancel as a server fault, not a user Stop.

Finding #28 (wf2) was fixed on ``send_message`` only: the ``/recompose`` cancel
arm still published ``client_cancelled`` ("Stopped") and recorded terminal
status ``cancelled`` when ``_track_compose_inflight``'s heartbeat cancelled the
request after losing its lease. The heartbeat's cancel carries a
``_ComposerHeartbeatCancel`` marker; the route must publish the server-fault
progress event, record ``failed`` and leave the structured 503 to the
dependency. A plain task cancel (a user Stop reaching the task) stays
``cancelled``.

Progression is driven by events: the heartbeat's interval wait is replaced by
a wait on "the composer is running", so no test sleeps or asserts a duration.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from elspeth.contracts.composer_progress import ComposerProgressEvent
from elspeth.web.composer.progress import ComposerRequestLease
from elspeth.web.coordination.composer_progress_authority import ComposerRequestLeaseLost
from elspeth.web.sessions.routes import _helpers
from tests.unit.web.sessions.test_routes import _make_progress_route_app


class _HangingComposer:
    """``compose`` parks until cancelled; ``started`` gates the heartbeat failure."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def compose(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable: compose never completes")


def _capture_recompose_terminal(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    emitted: list[dict[str, str]] = []

    class _Counter:
        def add(self, value: int, attributes: dict[str, str]) -> None:
            assert value == 1
            emitted.append(dict(attributes))

    monkeypatch.setattr(_helpers, "_COMPOSER_REQUEST_TERMINAL_COUNTER", _Counter())
    return emitted


def _heartbeat_loses_lease_once_running(monkeypatch: pytest.MonkeyPatch, registry: Any, running: asyncio.Event) -> None:
    """The first heartbeat renewal after ``running`` reports the lease lost."""
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


def _snapshot_event(snapshot: Any) -> ComposerProgressEvent:
    return ComposerProgressEvent(
        phase=snapshot.phase,
        headline=snapshot.headline,
        evidence=snapshot.evidence,
        likely_next=snapshot.likely_next,
        reason=snapshot.reason,
    )


@pytest.mark.asyncio
async def test_recompose_heartbeat_cancel_records_failed_and_answers_503(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    app, service = _make_progress_route_app(tmp_path)
    await service.add_message(service.session.id, "user", "Recompose me", writer_principal="route_user_message")
    composer = _HangingComposer()
    app.state.composer_service = composer
    registry = app.state.composer_progress_registry
    _heartbeat_loses_lease_once_running(monkeypatch, registry, composer.started)
    terminal = _capture_recompose_terminal(monkeypatch)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/sessions/{service.session.id}/recompose")

    assert composer.cancelled is True
    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "error_type": "composer_request_lease_lost",
            "detail": "The server lost this composer request's lease before it finished. Please resubmit.",
        }
    }
    snapshot = await registry.get_latest(str(service.session.id))
    assert _snapshot_event(snapshot) == _helpers._composer_heartbeat_failed_progress_event()
    assert snapshot.inflight_requests == 0
    assert [event for event in terminal if event["endpoint"] == "recompose"] == [{"endpoint": "recompose", "status": "failed"}]


@pytest.mark.asyncio
async def test_recompose_plain_task_cancel_is_still_a_client_cancel(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: a cancel that carries no heartbeat marker keeps the Stop labelling."""
    app, service = _make_progress_route_app(tmp_path)
    await service.add_message(service.session.id, "user", "Recompose me", writer_principal="route_user_message")
    composer = _HangingComposer()
    app.state.composer_service = composer
    registry = app.state.composer_progress_registry
    terminal = _capture_recompose_terminal(monkeypatch)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        request_task = asyncio.create_task(client.post(f"/api/sessions/{service.session.id}/recompose"))
        await asyncio.wait_for(composer.started.wait(), timeout=5)
        request_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request_task

    assert composer.cancelled is True
    snapshot = await registry.get_latest(str(service.session.id))
    assert snapshot.phase == "cancelled"
    assert snapshot.reason == "client_cancelled"
    assert [event for event in terminal if event["endpoint"] == "recompose"] == [{"endpoint": "recompose", "status": "cancelled"}]
