"""Physical send and original exception preservation through pure ASGI middleware."""

import asyncio

import pytest
from starlette.datastructures import Headers

from elspeth.web.app import _SPA_CSP, _BodySizeLimitMiddleware, _BrowserDocumentHeadersMiddleware


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before-start", "after-start"])
@pytest.mark.parametrize("kind", ["exception", "cancellation"])
async def test_original_downstream_failure_identity(phase, kind):
    original = RuntimeError("owned-original") if kind == "exception" else asyncio.CancelledError("owned-original")
    delivered = []

    async def downstream(scope, receive, send):
        if phase == "after-start":
            await send({"type": "http.response.start", "status": 200, "headers": []})
        raise original

    async def receive():
        raise AssertionError("Unexpected body read")

    async def send(message):
        delivered.append(message)

    app = _BrowserDocumentHeadersMiddleware(_BodySizeLimitMiddleware(downstream))
    with pytest.raises(type(original)) as caught:
        await app({"type": "http", "headers": []}, receive, send)
    assert caught.value is original
    assert len(delivered) == (1 if phase == "after-start" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_type", ["websocket", "lifespan"])
async def test_non_http_forwards_exact_owned_arguments(scope_type):
    scope = {"type": scope_type}
    seen = []

    async def receive():
        raise AssertionError("Middleware consumed non-HTTP input")

    async def send(message):
        raise AssertionError("Middleware emitted non-HTTP output")

    async def downstream(actual_scope, actual_receive, actual_send):
        seen.append((actual_scope, actual_receive, actual_send))

    await _BrowserDocumentHeadersMiddleware(_BodySizeLimitMiddleware(downstream))(scope, receive, send)
    assert len(seen) == 1
    actual_scope, actual_receive, actual_send = seen[0]
    assert actual_scope is scope
    assert actual_receive is receive
    assert actual_send is send


@pytest.mark.asyncio
@pytest.mark.parametrize("values,status", [(["\u0661"], 400), (["9" * 5000], 400), (["1", "11000000"], 204), (["11000000", "1"], 413)])
async def test_content_length_first_header_and_no_read(values, status):
    messages = []
    calls = []

    async def receive():
        raise AssertionError("Guard read body")

    async def send(message):
        messages.append(message)

    async def downstream(scope, receive, send):
        calls.append(scope)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    await _BodySizeLimitMiddleware(downstream)(
        {"type": "http", "headers": [(b"content-length", v.encode("utf-8")) for v in values]}, receive, send
    )
    assert messages[0]["status"] == status
    assert len(calls) == (1 if status == 204 else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 404])
@pytest.mark.parametrize("content_type", [b"TeXt/HtMl; charset=utf-8", b"application/json", b"text/event-stream"])
async def test_document_headers_replace_duplicates_preserve_cookies_and_body(content_type, status):
    messages = []
    policies = {
        "content-security-policy": _SPA_CSP,
        "x-frame-options": "DENY",
        "referrer-policy": "no-referrer",
        "cache-control": "no-store",
    }
    prior = [(key.encode(), b"old") for key in policies for _ in range(2)]
    cookies = [(b"set-cookie", b"a=1"), (b"set-cookie", b"b=2")]
    start = {"type": "http.response.start", "status": status, "headers": [(b"content-type", content_type), *prior, *cookies]}
    body = {"type": "http.response.body", "body": b"exact\x00bytes", "more_body": True}
    final = {"type": "http.response.body", "body": b"final-bytes", "more_body": False}

    async def receive():
        raise AssertionError("Header middleware read body")

    async def send(message):
        messages.append(message)

    async def downstream(scope, receive, send):
        await send(start)
        await send(body)
        await send(final)

    await _BrowserDocumentHeadersMiddleware(downstream)({"type": "http", "headers": []}, receive, send)
    assert len(messages) == 3
    assert messages[0] is start and messages[1] is body and messages[2] is final
    assert messages[0]["status"] == status
    assert messages[1] == {"type": "http.response.body", "body": b"exact\x00bytes", "more_body": True}
    assert messages[2] == {"type": "http.response.body", "body": b"final-bytes", "more_body": False}
    assert [item for item in start["headers"] if item[0] == b"set-cookie"] == cookies
    for name, value in policies.items():
        values = Headers(scope=start).getlist(name)
        assert values == ([value] if content_type.lower().startswith(b"text/html") else ["old", "old"])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["exception", "cancellation"])
async def test_external_send_failure_preserves_original_identity(kind):
    original = RuntimeError("physical-original") if kind == "exception" else asyncio.CancelledError("physical-original")
    entered = []

    async def receive():
        raise AssertionError("Unexpected body read")

    async def send(message):
        entered.append(asyncio.current_task())
        raise original

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})

    app = _BrowserDocumentHeadersMiddleware(_BodySizeLimitMiddleware(downstream))
    caller = asyncio.current_task()
    with pytest.raises(type(original)) as caught:
        await app({"type": "http", "headers": []}, receive, send)
    assert caught.value is original
    assert entered == [caller]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, b"0", b"00", b"01", b"10485760"])
async def test_accepted_body_forwards_exact_arguments(value):
    scope = {"type": "http", "headers": [] if value is None else [(b"content-length", value)]}
    seen = []

    async def receive():
        raise AssertionError("Guard read accepted body")

    async def send(message):
        raise AssertionError("Guard emitted accepted response")

    async def downstream(actual_scope, actual_receive, actual_send):
        seen.append((actual_scope, actual_receive, actual_send))

    await _BodySizeLimitMiddleware(downstream)(scope, receive, send)
    assert len(seen) == 1
    actual_scope, actual_receive, actual_send = seen[0]
    assert actual_scope is scope
    assert actual_receive is receive
    assert actual_send is send


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value,status,body",
    [
        (b"", 400, b'{"error": "Invalid Content-Length"}'),
        (b"-1", 400, b'{"error": "Invalid Content-Length"}'),
        (b"+1", 400, b'{"error": "Invalid Content-Length"}'),
        (b"1_000", 400, b'{"error": "Invalid Content-Length"}'),
        (b"1.0", 400, b'{"error": "Invalid Content-Length"}'),
        (b" 1", 400, b'{"error": "Invalid Content-Length"}'),
        (b"1 ", 400, b'{"error": "Invalid Content-Length"}'),
        (b"10485761", 413, b'{"error": "Request body too large (max 10 MB)"}'),
    ],
)
async def test_mounted_rejection_exact_wire_and_order(value, status, body):
    from fastapi import FastAPI

    from elspeth.web.middleware.instance_identity import InstanceIdentityMiddleware
    from elspeth.web.middleware.request_id import RequestIdMiddleware

    app = FastAPI()
    reached = []

    @app.post("/proof")
    async def proof():
        reached.append(True)
        return {"ok": True}

    # Same relative registration order as create_app, including the two wrappers.
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(_BodySizeLimitMiddleware)
    app.add_middleware(_BrowserDocumentHeadersMiddleware)
    app.add_middleware(InstanceIdentityMiddleware, instance_id="owned-proof")
    messages = []

    async def receive():
        raise AssertionError("Mounted rejection read body")

    async def send(message):
        messages.append(message)

    scope = {"type": "http", "method": "POST", "path": "/proof", "headers": [(b"content-length", value)], "query_string": b""}
    await app(scope, receive, send)
    assert reached == []
    assert len(messages) == 2
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == status
    headers = Headers(scope=messages[0])
    assert headers["content-type"] == "application/json"
    assert headers["content-length"] == str(len(body))
    assert headers["x-elspeth-instance"] == "owned-proof"
    assert "x-request-id" not in headers
    assert messages[1] == {"type": "http.response.body", "body": body}
