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


_SESSION = "0b6c3f8e-2a4d-4f1e-9c7b-5d8a1e3f6b2c"
_UPLOAD_LIMIT = 50 * 1024 * 1024


async def _declared(middleware, method, path, length):
    """Dispatch a request that only declares its length; return (status, body, entered)."""
    entered = []
    output = []

    async def app(scope, receive, send):
        entered.append(True)
        await JSONResponse({"ok": True})(scope, receive, send)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        output.append(message)

    middleware.app = app
    scope = {"type": "http", "method": method, "path": path, "headers": [(b"content-length", str(length).encode())]}
    await middleware(scope, receive, send)
    return output[0]["status"], output[1]["body"], bool(entered)


@pytest.mark.asyncio
async def test_blob_upload_takes_the_upload_ceiling_plus_its_envelope():
    middleware = _BodySizeLimitMiddleware(None, max_upload_bytes=_UPLOAD_LIMIT)
    ceiling = _UPLOAD_LIMIT + 1024 * 1024
    path = f"/api/sessions/{_SESSION}/blobs"

    assert (await _declared(middleware, "POST", path, ceiling))[0] == 200
    status, body, entered = await _declared(middleware, "POST", path, ceiling + 1)
    assert (status, body, entered) == (413, b'{"error": "Request body too large (max 51 MB)"}', False)
    assert middleware._limits({"method": "POST", "path": path}) == (ceiling, 30.0 * ceiling / (10 * 1024 * 1024))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", f"/api/sessions/{_SESSION}/blobs/inline"),
        ("POST", f"/api/sessions/{_SESSION}/blobs/"),
        ("POST", f"/api/sessions/{_SESSION}/blobsx"),
        ("POST", "/api/sessions/not-a-uuid/blobs"),
        ("POST", f"/x/api/sessions/{_SESSION}/blobs"),
        ("PUT", f"/api/sessions/{_SESSION}/blobs"),
        ("POST", "/api/sessions"),
    ],
)
async def test_every_other_request_keeps_the_default_ceiling(method, path):
    middleware = _BodySizeLimitMiddleware(None, max_upload_bytes=_UPLOAD_LIMIT)
    default = 10 * 1024 * 1024

    assert (await _declared(middleware, method, path, default))[0] == 200
    status, body, entered = await _declared(middleware, method, path, default + 1)
    assert (status, body, entered) == (413, b'{"error": "Request body too large (max 10 MB)"}', False)
    assert middleware._limits({"method": method, "path": path}) == (default, 30.0)


@pytest.mark.asyncio
async def test_without_an_upload_setting_the_upload_path_keeps_the_default_ceiling():
    middleware = _BodySizeLimitMiddleware(None)
    status, _, _ = await _declared(middleware, "POST", f"/api/sessions/{_SESSION}/blobs", 10 * 1024 * 1024 + 1)
    assert status == 413


@pytest.mark.asyncio
async def test_a_small_upload_setting_never_lowers_the_default_ceiling():
    middleware = _BodySizeLimitMiddleware(None, max_upload_bytes=1)
    assert middleware._limits({"method": "POST", "path": f"/api/sessions/{_SESSION}/blobs"}) == (10 * 1024 * 1024, 30.0)


def test_the_upload_class_is_exactly_the_live_multipart_blob_create_route(tmp_path):
    """Walk the mounted app: only the multipart blob create endpoint may take the upload ceiling."""
    import re

    from fastapi.routing import APIRoute

    from elspeth.web.app import _BLOB_UPLOAD_PATH, create_app
    from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog
    from tests.unit.web.test_app import _settings

    app = create_app(_settings(tmp_path), process_watchdog_factory=OwnedTestProcessWatchdog)
    try:
        widened = set()
        posts = 0
        for route in app.routes:
            if not isinstance(route, APIRoute) or "POST" not in route.methods:
                continue
            posts += 1
            sample = re.sub(r"\{[^}]+\}", _SESSION, route.path)
            if _BLOB_UPLOAD_PATH.fullmatch(sample):
                widened.add(route.path)
        assert posts > 20, "the walk must see the mounted POST routes"
        assert widened == {"/api/sessions/{session_id}/blobs"}
    finally:
        app.state.session_engine.dispose()


_UPLOAD_PATH = f"/api/sessions/{_SESSION}/blobs"


def _small_limits(middleware, *, read_seconds=30.0):
    """Default ceiling 64 bytes, upload ceiling 256, one large-body slot."""
    middleware._MAX_BODY_BYTES = 64
    middleware._MAX_BODY_READ_SECONDS = read_seconds
    middleware._upload_body_bytes = 256
    middleware._MAX_LARGE_BODIES = 1
    return middleware


def _scope(declared=None):
    headers = [] if declared is None else [(b"content-length", str(declared).encode())]
    return {"type": "http", "method": "POST", "path": _UPLOAD_PATH, "headers": headers}


@pytest.mark.asyncio
async def test_a_large_body_beyond_the_slot_limit_is_refused_until_the_holder_returns():
    held = asyncio.Event()
    release = asyncio.Event()
    entered = []

    async def app(scope, receive, send):
        body = await Request(scope, receive).body()
        entered.append(len(body))
        if len(body) > 64:
            held.set()
            await release.wait()
        await JSONResponse({"bytes": len(body)})(scope, receive, send)

    middleware = _small_limits(_BodySizeLimitMiddleware(app))

    async def run(chunks, declared=None):
        pending = deque(chunks)
        output = []

        async def receive():
            chunk = pending.popleft()
            return {"type": "http.request", "body": chunk, "more_body": bool(pending)}

        async def send(message):
            output.append(message)

        await middleware(_scope(declared), receive, send)
        return output

    holder = asyncio.create_task(run([b"x" * 50, b"x" * 50]))
    await held.wait()
    assert middleware._large_bodies == 1

    # Bounded: without the cap these would enter downstream and wait on ``release``.
    refused_streamed = await asyncio.wait_for(run([b"y" * 50, b"y" * 50]), timeout=5)
    refused_declared = await asyncio.wait_for(run([b"z" * 100], declared=100), timeout=5)
    small = await asyncio.wait_for(run([b"s" * 64]), timeout=5)
    for refused in (refused_streamed, refused_declared):
        assert refused[0]["status"] == 503
        assert (b"retry-after", b"30") in refused[0]["headers"]
    assert small[0]["status"] == 200
    assert entered == [100, 64], "refused large bodies never reach downstream; a default-class body is not blocked"

    release.set()
    assert (await holder)[0]["status"] == 200
    assert middleware._large_bodies == 0
    assert (await run([b"y" * 50, b"y" * 50]))[0]["status"] == 200


async def _never():
    await asyncio.Event().wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_path", ["success", "too_large", "timeout", "disconnect", "cancelled", "downstream_raises"])
async def test_every_exit_releases_the_large_body_slot(exit_path):
    claimed = []

    async def app(scope, receive, send):
        await Request(scope, receive).body()
        if exit_path == "downstream_raises":
            raise RuntimeError("downstream failure")
        await JSONResponse({"ok": True})(scope, receive, send)

    middleware = _small_limits(_BodySizeLimitMiddleware(app), read_seconds=0.05 if exit_path == "timeout" else 30.0)
    crossing = [{"type": "http.request", "body": b"x" * 65, "more_body": True}]
    tails = {
        "success": [{"type": "http.request", "body": b"x", "more_body": False}],
        "too_large": [{"type": "http.request", "body": b"x" * 200, "more_body": False}],
        "downstream_raises": [{"type": "http.request", "body": b"x", "more_body": False}],
        "disconnect": [{"type": "http.disconnect"}],
        "timeout": [],
        "cancelled": [],
    }
    pending = deque(crossing + tails[exit_path])

    async def receive():
        if len(pending) < len(crossing + tails[exit_path]):
            claimed.append(middleware._large_bodies)
        if not pending:
            await _never()
        return pending.popleft()

    output = []

    async def send(message):
        output.append(message)

    task = asyncio.create_task(middleware(_scope(), receive, send))
    if exit_path == "cancelled":
        while not claimed:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif exit_path == "downstream_raises":
        with pytest.raises(RuntimeError, match="downstream failure"):
            await task
    else:
        await task

    assert claimed and claimed[0] == 1, "the crossing chunk must have claimed the slot"
    assert middleware._large_bodies == 0
    expected = {"success": 200, "too_large": 413, "timeout": 408}
    if exit_path in expected:
        assert output[0]["status"] == expected[exit_path]


@pytest.mark.asyncio
async def test_a_declared_large_body_claims_the_slot_before_reading():
    seen = []

    async def app(scope, receive, send):
        await Request(scope, receive).body()
        await JSONResponse({"ok": True})(scope, receive, send)

    middleware = _small_limits(_BodySizeLimitMiddleware(app))

    async def receive():
        seen.append(middleware._large_bodies)
        return {"type": "http.request", "body": b"x" * 100, "more_body": False}

    async def send(message):
        pass

    await middleware(_scope(declared=100), receive, send)
    assert seen == [1]
    assert middleware._large_bodies == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chunks", "replayed"),
    [
        ([b"a" * 40, b"b" * 40, b"c" * 40], [(b"a" * 40, True), (b"b" * 40, True), (b"c" * 40, False)]),
        ([b"a" * 30, b"", b"b" * 30], [(b"a" * 30 + b"b" * 30, False)]),
        ([b""], [(b"", False)]),
    ],
    ids=["large-replayed-as-received", "default-replayed-whole", "empty-body"],
)
async def test_replay_shape(chunks, replayed):
    downstream = []

    async def app(scope, receive, send):
        while True:
            message = await receive()
            downstream.append((message["body"], message["more_body"]))
            if not message["more_body"]:
                break
        await JSONResponse({"ok": True})(scope, receive, send)

    middleware = _small_limits(_BodySizeLimitMiddleware(app))
    pending = deque(chunks)

    async def receive():
        chunk = pending.popleft()
        return {"type": "http.request", "body": chunk, "more_body": bool(pending)}

    async def send(message):
        pass

    await middleware(_scope(), receive, send)
    assert downstream == replayed
