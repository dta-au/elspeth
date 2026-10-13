"""Received-byte limits at the ASGI boundary, including absent or false headers."""

import asyncio
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


@pytest.mark.asyncio
@pytest.mark.parametrize("header", [None, b"1"])
@pytest.mark.parametrize("initial_chunk", [False, True])
async def test_stalled_body_times_out_before_dispatch_and_cancels_receive(header, initial_chunk):
    output = []
    receive_cancelled = asyncio.Event()
    reads = 0

    async def app(scope, receive, send):
        raise AssertionError("An incomplete request must not reach downstream")

    async def receive():
        nonlocal reads
        reads += 1
        if initial_chunk and reads == 1:
            return {"type": "http.request", "body": b"x", "more_body": True}
        try:
            await asyncio.Event().wait()
        finally:
            receive_cancelled.set()

    async def send(message):
        output.append(message)

    middleware = _BodySizeLimitMiddleware(app)
    middleware._MAX_BODY_READ_SECONDS = 0.02
    scope = {"type": "http", "headers": [] if header is None else [(b"content-length", header)]}
    await asyncio.wait_for(middleware(scope, receive, send), timeout=1)
    assert receive_cancelled.is_set()
    assert output[0]["status"] == 408
    assert (b"connection", b"close") in output[0]["headers"]
    assert len(output) == 2


@pytest.mark.asyncio
async def test_body_deadline_does_not_restart_with_each_chunk():
    output = []
    reads = 0

    async def app(scope, receive, send):
        raise AssertionError("A trickled body must not gain a fresh deadline per chunk")

    async def receive():
        nonlocal reads
        reads += 1
        await asyncio.sleep(0.01)
        return {"type": "http.request", "body": b"x", "more_body": reads < 20}

    async def send(message):
        output.append(message)

    middleware = _BodySizeLimitMiddleware(app)
    middleware._MAX_BODY_READ_SECONDS = 0.04
    await asyncio.wait_for(middleware({"type": "http", "headers": []}, receive, send), timeout=1)
    assert reads < 20
    assert output[0]["status"] == 408


@pytest.mark.asyncio
async def test_immediately_ready_empty_chunks_yield_and_obey_deadline():
    output = []
    heartbeat = asyncio.Event()
    reads = 0

    async def app(scope, receive, send):
        raise AssertionError("An unfinished body must not reach downstream")

    async def receive():
        nonlocal reads
        reads += 1
        assert reads < 10000, "Empty chunks must not bypass the body deadline or monopolize the event loop"
        return {"type": "http.request", "body": b"", "more_body": True}

    async def send(message):
        output.append(message)

    async def tick():
        await asyncio.sleep(0)
        heartbeat.set()

    middleware = _BodySizeLimitMiddleware(app)
    middleware._MAX_BODY_READ_SECONDS = 0.01
    tick_task = asyncio.create_task(tick())
    try:
        await asyncio.wait_for(middleware({"type": "http", "headers": []}, receive, send), timeout=1)
        assert heartbeat.is_set()
        assert output[0]["status"] == 408
    finally:
        await tick_task


@pytest.mark.asyncio
async def test_body_read_deadline_does_not_cancel_downstream_response():
    output = []

    async def app(scope, receive, send):
        assert await Request(scope, receive).body() == b"ok"
        await asyncio.sleep(0.1)
        await JSONResponse({"ok": True})(scope, receive, send)

    async def receive():
        return {"type": "http.request", "body": b"ok", "more_body": False}

    async def send(message):
        output.append(message)

    middleware = _BodySizeLimitMiddleware(app)
    middleware._MAX_BODY_READ_SECONDS = 0.05
    await middleware({"type": "http", "headers": []}, receive, send)
    assert output[0]["status"] == 200


@pytest.mark.asyncio
async def test_caller_cancellation_during_body_propagates_and_joins_receive():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def app(scope, receive, send):
        raise AssertionError("A cancelled request must not reach downstream")

    async def receive():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def send(message):
        raise AssertionError("Caller cancellation must not become a timeout response")

    task = asyncio.create_task(_BodySizeLimitMiddleware(app)({"type": "http", "headers": []}, receive, send))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_message", ["http.response.start", "http.response.body"])
async def test_oversize_response_send_timeout_propagates_without_second_response(failed_message):
    output = []

    async def app(scope, receive, send):
        raise AssertionError("An oversized body must not reach downstream")

    async def receive():
        return {"type": "http.request", "body": b"xxx", "more_body": False}

    async def send(message):
        output.append(message)
        if message["type"] == failed_message:
            raise TimeoutError("synthetic response-send timeout")

    middleware = _BodySizeLimitMiddleware(app)
    middleware._MAX_BODY_BYTES = 2
    with pytest.raises(TimeoutError, match="synthetic response-send timeout"):
        await middleware({"type": "http", "headers": []}, receive, send)
    assert [message["status"] for message in output if message["type"] == "http.response.start"] == [413]
