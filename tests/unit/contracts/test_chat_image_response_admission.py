"""Admission checks exact submitted image representations without rewriting output."""

import base64
import json

import pytest

from elspeth.contracts.call_data import HTTPCallResponse, HTTPResponseTransport
from elspeth.contracts.chat_parts import ChatMessage, ImagePart, image_response_is_audit_safe


@pytest.mark.parametrize("separator", ["", " ", "\n", "\r\n", "\t", "\f\v"])
@pytest.mark.parametrize(
    "form", ["raw", "base64", "slash", "unicode", "utf16", "malformed", "duplicate", "utf16_duplicate", "utf32_duplicate"]
)
def test_image_response_admission_normalizes_exact_echoes(form: str, separator: str) -> None:
    image = ImagePart.from_bytes(format="png", data=b"\x89PNG\r\n\x1a\n" + b"\xff" * 8, blob_ref=None)
    encoded = base64.b64encode(image.data).decode()
    encoded = separator.join(encoded[index : index + 4] for index in range(0, len(encoded), 4))
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
        body = ('{"echo":"' + escaped + '"}').encode()
    elif form == "utf16":
        body = body.decode().encode("utf-16")
    elif form == "malformed":
        body = ('{"echo":"' + escaped + '",invalid').encode()
    elif form == "duplicate":
        body = ('{"echo":"' + escaped + '","echo":"safe"}').encode()
    elif form in {"utf16_duplicate", "utf32_duplicate"}:
        body = ('{"padding":"' + "x" * 11000 + '","echo":"' + escaped + '","echo":"safe"}').encode(
            "utf-16" if form == "utf16_duplicate" else "utf-32"
        )
    candidate = HTTPCallResponse(status_code=200, headers={}, body_size=len(body), body={"echo": "safe"})
    messages = [ChatMessage(role="user", content=(image,))]
    assert not image_response_is_audit_safe(messages, candidate, body=body)
    assert image_response_is_audit_safe([ChatMessage(role="user", content="hello")], candidate, body=body)
    assert image_response_is_audit_safe(messages, candidate, body=b'{"content":"ordinary output"}')


@pytest.mark.parametrize("separator", ["", " ", "\n", "\t"])
def test_image_response_admission_checks_headers_and_archived_transport(separator: str) -> None:
    image = ImagePart.from_bytes(format="png", data=b"\x89PNG\r\n\x1a\nprivate-image", blob_ref=None)
    encoded = base64.b64encode(image.data).decode()
    encoded = separator.join(encoded[index : index + 4] for index in range(0, len(encoded), 4))
    messages = [ChatMessage(role="user", content=(image,))]
    for candidate in (
        HTTPCallResponse(status_code=200, headers={"x-debug": encoded}, body_size=2, body="ok"),
        HTTPCallResponse(status_code=200, headers={}, body_size=2, body={"nested": {encoded: "ok"}}),
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


def test_image_response_admission_preserves_unrelated_and_separate_strings() -> None:
    image = ImagePart.from_bytes(format="png", data=b"\x89PNG\r\n\x1a\nprivate-image", blob_ref=None)
    encoded = base64.b64encode(image.data).decode()
    messages = [ChatMessage(role="user", content=(image,))]
    ordinary = "OCR line one\nline two\twith spaces and a data URL: data:image/png;base64,b3RoZXI="
    candidate = HTTPCallResponse(status_code=200, headers={"x-debug": ordinary}, body_size=2, body=ordinary)
    assert image_response_is_audit_safe(messages, candidate, body=json.dumps({"content": ordinary}).encode())
    split = HTTPCallResponse(status_code=200, headers={}, body_size=2, body=[encoded[:8], encoded[8:]])
    assert image_response_is_audit_safe(messages, split, body=b"")
