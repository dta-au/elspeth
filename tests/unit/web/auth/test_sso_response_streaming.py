"""SSO response limits stop reading remote bodies, not just JSON parsing."""

import gzip
from collections.abc import AsyncIterator
from typing import Literal

import httpx
import pytest

from elspeth.web.auth.sso import (
    SsoDiscoveryFailed,
    SsoTokenExchangeFailed,
    SsoUserinfoInvalid,
    fetch_discovery_endpoints,
    fetch_userinfo,
    redeem_authorization_code,
)

Endpoint = Literal["discovery", "token", "userinfo"]
ISSUER = "https://idp.example.gov.au"


class MeasuredStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.bytes_read = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.bytes_read += len(chunk)
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


async def request_endpoint(endpoint: Endpoint, transport: httpx.AsyncBaseTransport) -> None:
    if endpoint == "discovery":
        await fetch_discovery_endpoints(issuer=ISSUER, expected_origins=frozenset({ISSUER}), transport=transport)
    elif endpoint == "token":
        await redeem_authorization_code(
            "fixture-code",
            verifier="fixture-verifier",
            token_endpoint=f"{ISSUER}/token",
            client_id="fixture-client",
            client_secret="fixture-secret",
            redirect_uri="https://app.example.gov.au/api/auth/sso/callback",
            transport=transport,
        )
    else:
        await fetch_userinfo(
            userinfo_endpoint=f"{ISSUER}/userinfo",
            access_token="fixture-access",
            expected_subject="fixture-subject",
            transport=transport,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint,limit", [("discovery", 256 * 1024), ("token", 64 * 1024), ("userinfo", 64 * 1024)])
async def test_oversized_body_stops_at_first_chunk_over_limit(endpoint: Endpoint, limit: int) -> None:
    stream = MeasuredStream([b"x" * limit, b"x", b"unread remainder"])

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)

    with pytest.raises((SsoDiscoveryFailed, SsoTokenExchangeFailed, SsoUserinfoInvalid), match="maximum accepted size"):
        await request_endpoint(endpoint, httpx.MockTransport(respond))
    assert stream.bytes_read == limit + 1
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["discovery", "token", "userinfo"])
async def test_refused_http_status_does_not_download_body(endpoint: Endpoint) -> None:
    stream = MeasuredStream([b"unneeded error body"])

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, stream=stream)

    with pytest.raises((SsoDiscoveryFailed, SsoTokenExchangeFailed, SsoUserinfoInvalid)):
        await request_endpoint(endpoint, httpx.MockTransport(respond))
    assert stream.bytes_read == 0
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["discovery", "token", "userinfo"])
async def test_exact_limit_valid_document_is_accepted(endpoint: Endpoint) -> None:
    if endpoint == "discovery":
        document = (
            b'{"issuer":"https://idp.example.gov.au",'
            b'"authorization_endpoint":"https://idp.example.gov.au/authorize",'
            b'"token_endpoint":"https://idp.example.gov.au/token",'
            b'"jwks_uri":"https://idp.example.gov.au/jwks"}'
        )
        limit = 256 * 1024
    elif endpoint == "token":
        document = b'{"token_type":"Bearer","id_token":"fixture-id","access_token":"fixture-access"}'
        limit = 64 * 1024
    else:
        document = b'{"sub":"fixture-subject"}'
        limit = 64 * 1024
    stream = MeasuredStream([document, b" " * (limit - len(document))])

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)

    await request_endpoint(endpoint, httpx.MockTransport(respond))
    assert stream.bytes_read == limit
    assert stream.closed


@pytest.mark.asyncio
async def test_userinfo_limit_counts_decoded_bytes() -> None:
    compressed = gzip.compress(b"x" * (64 * 1024 + 1))
    assert len(compressed) < 64 * 1024
    stream = MeasuredStream([compressed, b"unread remainder"])

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json", "content-encoding": "gzip"}, stream=stream)

    with pytest.raises(SsoUserinfoInvalid, match="maximum accepted size"):
        await request_endpoint("userinfo", httpx.MockTransport(respond))
    assert stream.bytes_read == len(compressed)
    assert stream.closed


@pytest.mark.asyncio
async def test_userinfo_wrong_media_type_does_not_download_body() -> None:
    stream = MeasuredStream([b"unneeded HTML response"])

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, stream=stream)

    with pytest.raises(SsoUserinfoInvalid, match="not application/json"):
        await request_endpoint("userinfo", httpx.MockTransport(respond))
    assert stream.bytes_read == 0
    assert stream.closed
