"""Admission checks exact submitted image representations without rewriting output."""

import base64
import json

import pytest

from elspeth.contracts.call_data import HTTPCallResponse, HTTPDecodedBodyEvidence, HTTPResponseTransport, RawCallPayload
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


@pytest.mark.parametrize("padding_bytes", [0, 1, 2])
@pytest.mark.parametrize("variant", ["standard", "unpadded", "urlsafe", "urlsafe_unpadded", "mixed", "partial_padding"])
@pytest.mark.parametrize("location", ["value", "unicode", "utf16_duplicate", "utf32_duplicate", "header", "transport", "decoded"])
def test_image_response_admission_checks_equivalent_base64(padding_bytes: int, variant: str, location: str) -> None:
    data = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) + b"x" * padding_bytes
    image = ImagePart.from_bytes(format="png", data=data, blob_ref=None)
    encoded = base64.b64encode(data).decode()
    assert "+" in encoded and "/" in encoded
    if variant == "unpadded":
        encoded = encoded.rstrip("=")
    elif variant in {"urlsafe", "urlsafe_unpadded"}:
        encoded = base64.urlsafe_b64encode(data).decode()
        if variant == "urlsafe_unpadded":
            encoded = encoded.rstrip("=")
    elif variant == "mixed":
        encoded = encoded.replace("+", "-", 1).replace("/", "_", 1).rstrip("=")
        assert all(char in encoded for char in "+/-_")
    elif variant == "partial_padding":
        encoded = encoded.removesuffix("=")
    restored = encoded.translate(str.maketrans("-_", "+/"))
    assert base64.b64decode(restored + "=" * (-len(restored) % 4), validate=True) == data
    wrapped = " \n\t".join(encoded[index : index + 4] for index in range(0, len(encoded), 4))
    messages = [ChatMessage(role="user", content=(image,))]
    if location == "value":
        assert not image_response_is_audit_safe(messages, RawCallPayload({"content": f"data:image/png;base64,{wrapped}"}))
        return
    if location == "header":
        assert not image_response_is_audit_safe(messages, HTTPCallResponse(status_code=200, headers={"debug": wrapped}))
        return
    escaped = "".join(f"\\u{ord(char):04x}" for char in wrapped)
    body = ('{"echo":"' + escaped + '"}').encode()
    if location in {"utf16_duplicate", "utf32_duplicate"}:
        body = ('{"padding":"' + "x" * 11000 + '","echo":"' + escaped + '","echo":"safe"}').encode(
            "utf-16" if location == "utf16_duplicate" else "utf-32"
        )
    if location == "transport":
        candidate = HTTPCallResponse(
            status_code=200,
            headers={},
            transport=HTTPResponseTransport(body_b64=base64.b64encode(body).decode(), headers=(), request_url="https://provider.example"),
        )
        assert not image_response_is_audit_safe(messages, candidate)
    elif location == "decoded":
        candidate = HTTPCallResponse(
            status_code=200,
            headers={},
            decoded_body=HTTPDecodedBodyEvidence(body_b64=base64.b64encode(body).decode(), decoded_size=len(body), complete=True),
        )
        assert not image_response_is_audit_safe(messages, candidate)
    else:
        assert not image_response_is_audit_safe(messages, RawCallPayload({"content": "safe"}), body=body)


@pytest.mark.parametrize("padding_bytes", [1, 2])
def test_image_response_admission_matches_used_bits_not_padding_bits(padding_bytes: int) -> None:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    data = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) + b"x" * padding_bytes
    image = ImagePart.from_bytes(format="png", data=data, blob_ref=None)
    messages = [ChatMessage(role="user", content=(image,))]
    encoded = base64.b64encode(data).decode()
    stem = encoded.rstrip("=")
    padding = len(encoded) - len(stem)
    for low_bits in range(1 << (2 * padding)):
        equivalent = stem[:-1] + alphabet[alphabet.index(stem[-1]) + low_bits]
        assert base64.b64decode(equivalent + "=" * padding, validate=True) == data
        for value in (equivalent, equivalent.translate(str.maketrans("+/", "-_"))):
            assert not image_response_is_audit_safe(messages, RawCallPayload({"content": value}))


@pytest.mark.parametrize("padding_bytes", [0, 1, 2])
def test_image_response_admission_preserves_different_bytes_and_separate_fields(padding_bytes: int) -> None:
    data = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) + b"x" * padding_bytes
    image = ImagePart.from_bytes(format="png", data=data, blob_ref=None)
    messages = [ChatMessage(role="user", content=(image,))]
    different = data[:-1] + bytes([data[-1] ^ 128])
    assert different != data
    unrelated = base64.urlsafe_b64encode(different).decode().rstrip("=")
    assert image_response_is_audit_safe(messages, RawCallPayload({"content": unrelated}))
    encoded = base64.urlsafe_b64encode(data).decode().rstrip("=")
    split = {"a": encoded[: len(encoded) // 2], "b": encoded[len(encoded) // 2 :]}
    assert image_response_is_audit_safe(messages, RawCallPayload(split), body=json.dumps(split).encode())


@pytest.mark.parametrize("padding_bytes", [1, 2])
def test_image_response_admission_checks_later_occurrence_after_changed_final_bits(padding_bytes: int) -> None:
    data = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) + b"x" * padding_bytes
    image = ImagePart.from_bytes(format="png", data=data, blob_ref=None)
    messages = [ChatMessage(role="user", content=(image,))]
    encoded = base64.urlsafe_b64encode(data).decode().rstrip("=")
    changed = base64.urlsafe_b64encode(data[:-1] + bytes([data[-1] ^ 1])).decode().rstrip("=")
    assert changed[:-1] == encoded[:-1] and changed[-1] != encoded[-1]
    assert image_response_is_audit_safe(messages, RawCallPayload({"content": changed}))
    assert not image_response_is_audit_safe(messages, RawCallPayload({"content": changed + ":" + encoded}))
    assert image_response_is_audit_safe(messages, RawCallPayload({"content": encoded[:-1]}))


def test_image_response_admission_preserves_periodic_prefix_mismatches_and_checks_later_echo() -> None:
    signature = b"\x89PNG\r\n\x1a\n"
    periodic = signature * 1001
    data = periodic[:-1] + bytes([periodic[-1] ^ 1])
    image = ImagePart.from_bytes(format="png", data=data, blob_ref=None)
    messages = [ChatMessage(role="user", content=(image,))]
    unrelated = base64.b64encode(signature * 10010).decode()
    encoded = base64.b64encode(data).decode()
    assert data not in signature * 10010
    assert image_response_is_audit_safe(messages, RawCallPayload({"content": unrelated}))
    assert not image_response_is_audit_safe(messages, RawCallPayload({"content": unrelated + ":" + encoded}))
