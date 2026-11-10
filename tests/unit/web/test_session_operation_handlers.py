"""Fence-loss diagnostics must survive without exposing session existence."""

from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from structlog.testing import capture_logs

from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost
from elspeth.web.session_operation_handlers import register_session_operation_exception_handlers
from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error


def _fence_loss_app(reason: FenceLossReason) -> FastAPI:
    app = FastAPI()
    register_session_operation_exception_handlers(app)

    @app.post("/sessions/{session_id}/blobs")
    async def upload(session_id: str) -> None:
        raise SessionOperationFenceLost(reason)

    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", list(FenceLossReason))
async def test_every_fence_loss_keeps_the_exact_nonleaking_response(reason: FenceLossReason) -> None:
    app = _fence_loss_app(reason)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/sessions/{uuid4()}/blobs")

    assert response.status_code == 404
    assert response.content == b'{"detail":"Session not found"}'
    assert response.json() == {"detail": "Session not found"}
    assert reason.value not in repr(dict(response.headers))


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", list(FenceLossReason))
async def test_fence_loss_records_only_bounded_operator_fields(reason: FenceLossReason) -> None:
    app = _fence_loss_app(reason)
    with capture_logs() as logs:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/sessions/{uuid4()}/blobs")

    assert response.status_code == 404
    assert logs == [
        {
            "event": "http_session_operation_fence_lost",
            "reason": reason.value,
            "method": "POST",
            "route": "/sessions/{session_id}/blobs",
            "log_level": "warning",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", list(FenceLossReason))
async def test_fence_loss_never_logs_or_returns_request_identity(reason: FenceLossReason) -> None:
    app = _fence_loss_app(reason)
    session_id = str(uuid4())
    user_header = "private-user-identity"
    with capture_logs() as logs:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                f"/sessions/{session_id}/blobs?token=SECRET",
                headers={"X-User": user_header},
            )

    assert response.status_code == 404
    assert logs, "The leak control must exercise an actual diagnostic record"
    for marker in (session_id, "SECRET", user_header):
        assert marker not in repr(logs)
        assert marker not in response.text
        assert marker not in repr(dict(response.headers))


@pytest.mark.asyncio
async def test_fence_loss_without_a_matched_route_does_not_log_the_raw_path() -> None:
    app = FastAPI()
    register_session_operation_exception_handlers(app)
    request = Request({"type": "http", "method": "POST", "path": "/private-id", "headers": []})

    with capture_logs() as logs:
        response = await app.exception_handlers[SessionOperationFenceLost](
            request, SessionOperationFenceLost(FenceLossReason.LEASE_EXPIRED)
        )

    assert response.status_code == 404
    assert logs == [
        {
            "event": "http_session_operation_fence_lost",
            "reason": "lease_expired",
            "method": "POST",
            "route": None,
            "log_level": "warning",
        }
    ]


@pytest.mark.parametrize("reason", list(FenceLossReason))
def test_detached_fence_loss_projection_remains_pure_and_nonleaking(reason: FenceLossReason) -> None:
    with capture_logs() as logs:
        projected = project_composer_operation_error(SessionOperationFenceLost(reason), request_id="private-request-id")

    assert projected.http_status == 404
    assert projected.body == {"detail": "Session not found"}
    assert logs == []
