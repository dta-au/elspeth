"""Public HTTP transforms preserve shared refusals and canonical DNS identity."""

import hashlib
import socket
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from elspeth.contracts import CallStatus, CallType
from elspeth.contracts.audit import Call
from elspeth.plugins.transforms.blob_fetch import BlobFetch
from elspeth.plugins.transforms.web_scrape import WebScrapeTransform
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_context

_IP = "93.184.216.34"


class _Payloads:
    def __init__(self) -> None:
        self.stored: list[bytes] = []

    def store(self, body: bytes) -> str:
        self.stored.append(body)
        return hashlib.sha256(body).hexdigest()


class _Limiters:
    def get_limiter(self, _name: str) -> None:
        return None


def _context():
    ctx = make_context()
    ctx.payload_store = _Payloads()
    ctx.rate_limit_registry = _Limiters()
    ctx.landscape.allocate_call_index.return_value = 0
    ctx.landscape.record_call.return_value = Call(
        call_id="boundary-call",
        call_index=0,
        call_type=CallType.HTTP,
        status=CallStatus.SUCCESS,
        request_hash="request-hash",
        request_ref="request-ref",
        response_hash="response-hash",
        response_ref="response-ref",
        created_at=datetime.now(UTC),
        state_id=ctx.state_id,
    )
    return ctx


def _transport(monkeypatch: pytest.MonkeyPatch, response: httpx.Response | tuple[httpx.Response, ...]):
    requests: list[httpx.Request] = []
    original_client = httpx.Client

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response[len(requests) - 1] if isinstance(response, tuple) else response

    def client(*args, **kwargs):
        return original_client(*args, **kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(httpx, "Client", client)
    return requests


@pytest.mark.parametrize(
    ("encoding", "body", "expected_reason", "expected_encoding_error"),
    [
        ("identity", b"ok", None, None),
        ("compress", b"ok", "body_too_large", "unsupported_content_encoding"),
        ("gzip", b"not-gzip", "body_too_large", "invalid_content_encoding"),
        ("identity", b"too large", "body_too_large", None),
    ],
)
def test_blob_fetch_shared_response_refusals_are_audited_row_errors(
    monkeypatch: pytest.MonkeyPatch,
    encoding: str,
    body: bytes,
    expected_reason: str | None,
    expected_encoding_error: str | None,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_IP, 0))])
    requests = _transport(
        monkeypatch,
        httpx.Response(200, headers={"content-type": "text/plain", "content-encoding": encoding}, stream=httpx.ByteStream(body)),
    )
    ctx = _context()
    transform = BlobFetch(
        {
            "schema": {"mode": "observed"},
            "url_field": "url",
            "http": {"abuse_contact": "test@example.com", "fetch_reason": "boundary regression", "max_body_bytes": 4},
        }
    )
    transform.on_start(ctx)
    result = transform.process(make_pipeline_row({"url": "https://example.com/data"}), ctx)

    assert len(requests) == 1
    ctx.landscape.record_call.assert_called_once()
    audit = ctx.landscape.record_call.call_args.kwargs
    assert audit["status"] == (CallStatus.SUCCESS if expected_reason is None else CallStatus.ERROR)
    if expected_reason is None:
        assert result.status == "success"
        assert ctx.payload_store.stored == [body]
    else:
        assert result.status == "error"
        assert result.reason["reason"] == expected_reason
        assert ctx.payload_store.stored == []
        assert audit["response_data"].to_dict()["body"]["_captured_body"] is False
        if expected_encoding_error is not None:
            assert result.reason["error_type"] == expected_encoding_error


@pytest.mark.parametrize("transform_class", [BlobFetch, WebScrapeTransform])
@pytest.mark.parametrize(
    ("host", "wire_host"),
    [("example.com", "example.com"), ("bücher.example", "xn--bcher-kva.example"), ("faß.example", "xn--fa-hia.example")],
)
@pytest.mark.parametrize("port", [443, 8443])
def test_public_transforms_use_one_canonical_dns_host_for_wire_and_audit(
    monkeypatch: pytest.MonkeyPatch, transform_class, host: str, wire_host: str, port: int
) -> None:
    dns_hosts: list[str] = []

    def dns(hostname: str, *_args):
        dns_hosts.append(hostname)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_IP, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    requests = _transport(monkeypatch, httpx.Response(200, headers={"content-type": "text/plain"}, stream=httpx.ByteStream(b"ok")))
    ctx = _context()
    url = f"https://{host}:{port}/data"
    reason_key = "fetch_reason" if transform_class is BlobFetch else "scraping_reason"
    config = {
        "schema": {"mode": "observed"},
        "url_field": "url",
        "http": {"abuse_contact": "test@example.com", reason_key: "boundary regression", "allowed_origins": [f"https://{host}:{port}"]},
    }
    if transform_class is WebScrapeTransform:
        config.update(content_field="content", fingerprint_field="fingerprint")
    transform = transform_class(config)
    transform.on_start(ctx)
    result = transform.process(make_pipeline_row({"url": url}), ctx)

    assert result.status == "success"
    assert dns_hosts == [wire_host]
    assert len(requests) == 1
    assert requests[0].url.host == _IP
    expected_host = wire_host if port == 443 else f"{wire_host}:{port}"
    assert requests[0].headers["host"] == expected_host
    assert requests[0].extensions["sni_hostname"] == wire_host
    ctx.landscape.record_call.assert_called_once()
    audit = ctx.landscape.record_call.call_args.kwargs
    assert audit["status"] is CallStatus.SUCCESS
    assert audit["request_data"].to_dict()["url"] == url
    assert audit["request_data"].to_dict()["resolved_ip"] == _IP


@pytest.mark.parametrize("transform_class", [BlobFetch, WebScrapeTransform])
def test_unicode_origin_redirect_rechecks_same_ascii_dns_identity(monkeypatch: pytest.MonkeyPatch, transform_class) -> None:
    dns_hosts: list[str] = []

    def dns(hostname: str, *_args):
        dns_hosts.append(hostname)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_IP, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    requests = _transport(
        monkeypatch,
        (
            httpx.Response(302, headers={"location": "https://xn--bcher-kva.example/second"}, stream=httpx.ByteStream(b"")),
            httpx.Response(200, headers={"content-type": "text/plain"}, stream=httpx.ByteStream(b"ok")),
        ),
    )
    ctx = _context()
    parent_call = ctx.landscape.record_call.return_value
    ctx.landscape.allocate_call_index.side_effect = iter(range(2))
    ctx.landscape.record_call.side_effect = [
        replace(parent_call, call_id="redirect-call", call_index=1, call_type=CallType.HTTP_REDIRECT),
        parent_call,
    ]
    reason_key = "fetch_reason" if transform_class is BlobFetch else "scraping_reason"
    config = {
        "schema": {"mode": "observed"},
        "url_field": "url",
        "http": {"abuse_contact": "test@example.com", reason_key: "boundary regression", "allowed_origins": ["https://bücher.example"]},
    }
    if transform_class is WebScrapeTransform:
        config.update(content_field="content", fingerprint_field="fingerprint")
    transform = transform_class(config)
    transform.on_start(ctx)
    result = transform.process(make_pipeline_row({"url": "https://bücher.example/first"}), ctx)

    assert result.status == "success"
    assert dns_hosts == ["xn--bcher-kva.example", "xn--bcher-kva.example"]
    assert len(requests) == 2
    assert [request.headers["host"] for request in requests] == ["xn--bcher-kva.example"] * 2
    assert [request.url.host for request in requests] == [_IP] * 2
    assert ctx.landscape.record_call.call_count == 2
    audits = [call.kwargs for call in ctx.landscape.record_call.call_args_list]
    assert [call["call_type"] for call in audits] == [CallType.HTTP_REDIRECT, CallType.HTTP]
    assert [call["call_index"] for call in audits] == [1, 0]
