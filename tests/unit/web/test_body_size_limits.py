"""Received-byte limits at the ASGI boundary, including absent or false headers."""

from collections import deque

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from elspeth.web.app import _BodySizeLimitMiddleware


@pytest.mark.asyncio
@pytest.mark.parametrize("header", [None, b"1", b"64"])
@pytest.mark.parametrize("chunks", [[b"x" * 65], [b"x" * 32, b"x" * 32, b"x"]])
async def test_received_body_limit_precedes_downstream(header, chunks):
    entered = []
    output = []
    pending = deque(chunks)

    async def app(scope, receive, send):
        entered.append(True)
        body = await Request(scope, receive).body()
        await JSONResponse({"bytes": len(body)})(scope, receive, send)

    async def receive():
        chunk = pending.popleft()
        return {"type": "http.request", "body": chunk, "more_body": bool(pending)}

    async def send(message):
        output.append(message)

    middleware = _BodySizeLimitMiddleware(app)
    middleware._MAX_BODY_BYTES = 64
    scope = {"type": "http", "headers": [] if header is None else [(b"content-length", header)]}
    await middleware(scope, receive, send)
    assert entered == []
    assert output[0]["status"] == 413
    assert len(output) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("header", [None, b"64"])
async def test_exact_received_limit_replays_body_and_later_disconnect(header):
    output = []
    pending = deque(
        [
            {"type": "http.request", "body": b"x" * 31, "more_body": True},
            {"type": "http.request", "body": b"", "more_body": True},
            {"type": "http.request", "body": b"y" * 33, "more_body": False},
            {"type": "http.disconnect"},
        ]
    )

    async def app(scope, receive, send):
        assert await Request(scope, receive).body() == b"x" * 31 + b"y" * 33
        assert await receive() == {"type": "http.disconnect"}
        await JSONResponse({"ok": True})(scope, receive, send)

    async def receive():
        return pending.popleft()

    async def send(message):
        output.append(message)

    middleware = _BodySizeLimitMiddleware(app)
    middleware._MAX_BODY_BYTES = 64
    scope = {"type": "http", "headers": [] if header is None else [(b"content-length", header)]}
    await middleware(scope, receive, send)
    assert output[0]["status"] == 200
    assert not pending


@pytest.mark.asyncio
async def test_disconnect_during_body_never_dispatches_partial_request():
    pending = deque([{"type": "http.request", "body": b"partial", "more_body": True}, {"type": "http.disconnect"}])

    async def app(scope, receive, send):
        raise AssertionError("Disconnected body must not reach downstream")

    async def receive():
        return pending.popleft()

    async def send(message):
        raise AssertionError("Disconnected client must not receive a response")

    await _BodySizeLimitMiddleware(app)({"type": "http", "headers": []}, receive, send)
