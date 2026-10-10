"""Admission checks exact submitted image representations without rewriting output."""

import base64
import json

import pytest

from elspeth.contracts.call_data import HTTPCallResponse, HTTPResponseTransport
from elspeth.contracts.chat_parts import ChatMessage, ImagePart, image_response_is_audit_safe


@pytest.mark.parametrize("form", ["raw", "base64", "slash", "unicode", "utf16", "malformed", "duplicate"])
def test_image_response_admission_normalizes_exact_echoes(form: str) -> None:
    image = ImagePart.from_bytes(format="png", data=b"\x89PNG\r\n\x1a\n" + b"\xff" * 8, blob_ref=None)
    encoded = base64.b64encode(image.data).decode()
    escaped = "".join(f"\\u{ord(char):04x}" for char in encoded)
    body = json.dumps({"echo": encoded}).encode()
    if form == "raw":
        body = image.data
    elif form == "base64":
        body = encoded.encode()
    elif form == "slash":
        assert "/" in encoded
        body = body.replace(b"/", b"\\/")
    elif form == "unicode":
        body = body.replace(encoded.encode(), escaped.encode())
    elif form == "utf16":
        body = body.decode().encode("utf-16")
    elif form == "malformed":
        body = ('{"echo":"' + escaped + '",invalid').encode()
    elif form == "duplicate":
        body = ('{"echo":"' + escaped + '","echo":"safe"}').encode()
    candidate = HTTPCallResponse(status_code=200, headers={}, body_size=len(body), body={"echo": "safe"})
    messages = [ChatMessage(role="user", content=(image,))]
    assert not image_response_is_audit_safe(messages, candidate, body=body)
    assert image_response_is_audit_safe([ChatMessage(role="user", content="hello")], candidate, body=body)
    assert image_response_is_audit_safe(messages, candidate, body=b'{"content":"ordinary output"}')


def test_image_response_admission_checks_headers_and_archived_transport() -> None:
    image = ImagePart.from_bytes(format="png", data=b"\x89PNG\r\n\x1a\nprivate-image", blob_ref=None)
    encoded = base64.b64encode(image.data).decode()
    messages = [ChatMessage(role="user", content=(image,))]
    for candidate in (
        HTTPCallResponse(status_code=200, headers={"x-debug": encoded}, body_size=2, body="ok"),
        HTTPCallResponse(
            status_code=200,
            headers={},
            body_size=2,
            body="ok",
            transport=HTTPResponseTransport(
                body_b64=base64.b64encode(encoded.encode()).decode(), headers=(), request_url="https://provider.example"
            ),
        ),
    ):
        assert not image_response_is_audit_safe(messages, candidate)
