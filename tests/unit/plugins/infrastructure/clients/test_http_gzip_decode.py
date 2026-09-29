"""Regression: a capped streaming response must not double-decompress gzip bodies.

``_consume_capped_response`` reads the body via ``iter_bytes()`` (which has ALREADY
applied content-decoding) but used to reconstruct the ``httpx.Response`` with the
original ``Content-Encoding`` header. On read, httpx then re-ran the gzip decoder on
the already-decoded HTML -> ``httpx.DecodingError`` ("Error -3 while decompressing
data: incorrect header check"). This surfaced live when GitHub Pages began serving
``content-encoding: gzip`` for the tutorial sample pages, failing every web_scrape row.

The companion ``test_capped_response_measures_decoded_size_not_compressed`` guards the
*decoded-size* body cap (the decompression-bomb-relevant metric): ``iter_bytes()`` is
kept deliberately so the cap counts decoded bytes, not compressed wire bytes.
"""

import gzip
import zlib
from unittest.mock import MagicMock

import httpx
import pytest

from elspeth.contracts.audit_protocols import CallRecorder
from elspeth.plugins.infrastructure.clients.http import (
    AuditedHTTPClient,
    HTTPResponseBodyTooLargeError,
    HTTPResponseEncodingLimitError,
)


def _make_client(*, max_response_body_bytes: int = 10 * 1024 * 1024) -> AuditedHTTPClient:
    recorder = MagicMock(spec=CallRecorder)
    recorder.allocate_call_index.return_value = 0
    return AuditedHTTPClient(
        execution=recorder,
        state_id="state-1",
        run_id="run-1",
        telemetry_emit=lambda event: None,
        timeout=5.0,
        max_response_body_bytes=max_response_body_bytes,
    )


def _stream_encoded(body: bytes, *, encoding: str) -> httpx.Response:
    return httpx.Response(200, stream=httpx.ByteStream(body), headers={"content-encoding": encoding, "content-type": "text/html"})


def _raw_deflate(body: bytes) -> bytes:
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return compressor.compress(body) + compressor.flush()


def test_encoded_byte_cap_and_ratio_are_audited_failures() -> None:
    decoded = b"x" * 10_000
    compressed = gzip.compress(decoded)
    recorder = MagicMock(spec=CallRecorder)
    recorder.allocate_call_index.return_value = 0
    for encoded_cap, ratio, expected in (
        (len(compressed) - 1, 1000, "encoded_body_too_large"),
        (len(compressed) + 1, 2, "decompression_ratio_exceeded"),
    ):
        client = AuditedHTTPClient(
            execution=recorder,
            state_id="state-1",
            run_id="run-1",
            telemetry_emit=lambda event: None,
            max_response_body_bytes=20_000,
            max_encoded_response_bytes=encoded_cap,
            max_decompression_ratio=ratio,
        )
        with (
            httpx.Client(transport=httpx.MockTransport(lambda request: _stream_encoded(compressed, encoding="gzip"))) as hc,
            hc.stream("GET", "https://example.test/page") as streaming,
            pytest.raises(HTTPResponseEncodingLimitError) as exc,
        ):
            client._consume_capped_response(streaming, full_url="https://example.test/page")
        assert exc.value.reason == expected
        assert exc.value.response_data["body"]["_reason"] == expected


def test_unsupported_content_encoding_refuses_before_body_read() -> None:
    client = _make_client()
    with (
        httpx.Client(transport=httpx.MockTransport(lambda request: _stream_encoded(b"anything", encoding="br"))) as hc,
        hc.stream("GET", "https://example.test/page") as streaming,
        pytest.raises(HTTPResponseEncodingLimitError) as exc,
    ):
        client._consume_capped_response(streaming, full_url="https://example.test/page")
    assert exc.value.reason == "unsupported_content_encoding"


def test_link_header_replay_transport_requires_audit_safe_exact_value() -> None:
    client = _make_client()
    request = httpx.Request("GET", "https://example.test/search")
    safe = httpx.Response(200, content=b"ok", headers={"Link": '</search?page=2>; rel="next"'}, request=request)
    unsafe = httpx.Response(200, content=b"ok", headers={"Link": '<https://example.test/search?token=SECRET>; rel="next"'}, request=request)
    assert client._build_replay_transport(safe, logical_url="https://example.test/search") is not None
    assert client._build_replay_transport(unsafe, logical_url="https://example.test/search") is None


@pytest.mark.parametrize(
    ("encoding", "compress"),
    [
        ("gzip", gzip.compress),
        ("deflate", zlib.compress),
        ("deflate", _raw_deflate),
    ],
)
def test_capped_response_decodes_once_not_double(encoding: str, compress) -> None:
    body = b"<html><body>" + b"hello world " * 200 + b"</body></html>"
    encoded = compress(body)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-encoding": encoding,
                "content-type": "text/html; charset=utf-8",
            },
            content=encoded,
        )

    client = _make_client()
    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as hc, hc.stream("GET", "https://example.test/page") as streaming:
        result = client._consume_capped_response(streaming, full_url="https://example.test/page")

    assert result.status_code == 200
    # Content-Encoding must be stripped so .text/.content do not re-decode the
    # already-decoded body.
    assert "content-encoding" not in result.headers
    # The stale compressed length is replaced with the decoded length.
    assert result.headers["content-length"] == str(len(body))
    assert len(encoded) < len(body)
    assert result.text == body.decode()
    assert result.content == body


def test_raw_deflate_still_obeys_decoded_cap() -> None:
    encoded = _raw_deflate(b"A" * (256 * 1024))
    client = _make_client(max_response_body_bytes=4096)
    with (
        httpx.Client(transport=httpx.MockTransport(lambda request: _stream_encoded(encoded, encoding="deflate"))) as hc,
        hc.stream("GET", "https://example.test/page") as streaming,
        pytest.raises(HTTPResponseBodyTooLargeError) as exc,
    ):
        client._consume_capped_response(streaming, full_url="https://example.test/page")
    assert exc.value.body_size > 4096


def test_raw_deflate_stream_succeeds_with_encoded_and_decoded_limits() -> None:
    body = b"<html><body>raw deflate works</body></html>" * 20
    encoded = _raw_deflate(body)
    recorder = MagicMock(spec=CallRecorder)
    recorder.allocate_call_index.return_value = 0
    client = AuditedHTTPClient(
        execution=recorder,
        state_id="state-1",
        run_id="run-1",
        telemetry_emit=lambda event: None,
        max_response_body_bytes=len(body),
        max_encoded_response_bytes=len(encoded),
        max_decompression_ratio=200,
    )
    with (
        httpx.Client(transport=httpx.MockTransport(lambda request: _stream_encoded(encoded, encoding="deflate"))) as hc,
        hc.stream("GET", "https://example.test/page") as streaming,
    ):
        result = client._consume_capped_response(streaming, full_url="https://example.test/page")
    assert result.content == body


def test_capped_response_identity_success_path_preserved() -> None:
    """The header-stripping reconstruction must not corrupt the common
    uncompressed (no content-encoding) success path."""
    body = b"<html><body>plain identity body</body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=body,
        )

    client = _make_client()
    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as hc, hc.stream("GET", "https://example.test/page") as streaming:
        result = client._consume_capped_response(streaming, full_url="https://example.test/page")

    assert result.status_code == 200
    assert result.content == body
    assert result.text == body.decode()


def test_capped_response_measures_decoded_size_not_compressed() -> None:
    """The body cap is the decompression-bomb-relevant metric: it must count the
    DECODED size, not the compressed wire size.

    Sizes are chosen so that ``len(gz) < cap < decoded_size``. A small gzip
    payload that inflates past the cap must trip ``HTTPResponseBodyTooLargeError``
    with ``body_size`` reflecting decoded bytes. If the cap measured compressed
    bytes instead, the ~260-byte payload would slip under the 4096-byte cap and
    the error would never fire.
    """
    cap = 4096
    decoded = b"A" * (256 * 1024)  # 262144 decoded bytes
    gz = gzip.compress(decoded)
    assert len(gz) < cap < len(decoded)  # discriminator precondition

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-encoding": "gzip",
                "content-type": "text/html; charset=utf-8",
            },
            content=gz,
        )

    client = _make_client(max_response_body_bytes=cap)
    transport = httpx.MockTransport(handler)
    with (
        httpx.Client(transport=transport) as hc,
        hc.stream("GET", "https://example.test/page") as streaming,
        pytest.raises(HTTPResponseBodyTooLargeError) as exc_info,
    ):
        client._consume_capped_response(streaming, full_url="https://example.test/page")

    exc = exc_info.value
    assert exc.body_size > cap  # cap tripped on decoded bytes
    # Load-bearing: body_size exceeding the ENTIRE compressed payload proves the
    # cap measured decoded, not compressed, bytes.
    assert exc.body_size > len(gz)
    assert exc.max_body_bytes == cap
    truncated = exc.response_data["body"]
    assert truncated["_truncated"] is True
    assert truncated["_reason"] == "body_too_large"
    assert truncated["_observed_body_size"] == exc.body_size
