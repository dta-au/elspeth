"""Actual create_app ordering, independently of a manually assembled test stack."""

import pytest
from starlette.datastructures import Headers

from elspeth.web.app import create_app
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog
from tests.unit.web.test_app import _settings


@pytest.mark.asyncio
async def test_actual_create_app_body_rejection_precedes_request_id(tmp_path):
    app = create_app(_settings(tmp_path), process_watchdog_factory=OwnedTestProcessWatchdog)
    messages = []

    async def receive():
        raise AssertionError("Actual mounted rejection consumed body")

    async def send(message):
        messages.append(message)

    scope = {"type": "http", "method": "POST", "path": "/api/sessions", "headers": [(b"content-length", b"10485761")], "query_string": b""}
    try:
        await app(scope, receive, send)
        assert len(messages) == 2
        assert messages[0]["status"] == 413
        headers = Headers(scope=messages[0])
        assert headers["content-type"] == "application/json"
        assert headers["x-elspeth-instance"] == app.state.instance_id
        assert "x-request-id" not in headers
        assert messages[1] == {"type": "http.response.body", "body": b'{"error": "Request body too large (max 10 MB)"}'}
    finally:
        app.state.session_engine.dispose()
