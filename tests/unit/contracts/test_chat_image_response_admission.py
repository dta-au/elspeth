"""Admission checks exact submitted image representations without rewriting output."""

import base64
import json
import tracemalloc

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


@pytest.mark.parametrize("variant", ["hex", "upper_hex", "separated_hex", "wrapped_hex", "array", "array_text"])
@pytest.mark.parametrize(
    "location", ["value", "header", "unicode", "malformed", "utf16_duplicate", "utf32_duplicate", "transport", "decoded"]
)
def test_image_response_admission_checks_complete_hex_and_byte_arrays(variant: str, location: str) -> None:
    data = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    image = ImagePart.from_bytes(format="png", data=data, blob_ref=None)
    messages = [ChatMessage(role="user", content=(image,))]
    value: str | list[int] = data.hex()
    if variant == "upper_hex":
        value = "0x" + data.hex().upper()
    elif variant == "separated_hex":
        value = ":".join(f"{byte:02X}" for byte in data)
    elif variant == "wrapped_hex":
        value = " \n\t".join(f"{byte:02x}" for byte in data)
    elif variant == "array":
        value = [0, *data, 255]
    elif variant == "array_text":
        value = "[\n" + ",\t".join(str(byte) for byte in data) + "\n]"
    rendered = json.dumps(value)
    if location == "value":
        assert not image_response_is_audit_safe(messages, RawCallPayload({"nested": {"image": value}}))
        return
    if location == "header":
        assert not image_response_is_audit_safe(messages, HTTPCallResponse(status_code=200, headers={"debug": rendered}))
        return
    body = '{"echo":' + rendered + "}"
    if location == "unicode":
        text = value if isinstance(value, str) else json.dumps(value)
        body = '{"echo":"' + "".join(f"\\u{ord(char):04x}" for char in text) + '"}'
    elif location == "malformed":
        body = '{"echo":' + rendered + ",invalid"
    elif location in {"utf16_duplicate", "utf32_duplicate"}:
        body = '{"padding":"' + "x" * 11000 + '","echo":' + rendered + ',"echo":"safe"}'
    raw = body.encode({"utf16_duplicate": "utf-16", "utf32_duplicate": "utf-32"}.get(location, "utf-8"))
    if location == "transport":
        candidate = HTTPCallResponse(
            status_code=200,
            headers={},
            transport=HTTPResponseTransport(body_b64=base64.b64encode(raw).decode(), headers=(), request_url="https://provider.example"),
        )
        assert not image_response_is_audit_safe(messages, candidate)
    elif location == "decoded":
        candidate = HTTPCallResponse(
            status_code=200,
            headers={},
            decoded_body=HTTPDecodedBodyEvidence(body_b64=base64.b64encode(raw).decode(), decoded_size=len(raw), complete=True),
        )
        assert not image_response_is_audit_safe(messages, candidate)
    else:
        assert not image_response_is_audit_safe(messages, RawCallPayload({"content": "safe"}), body=raw)


def test_image_response_admission_preserves_unrelated_hex_and_numeric_results() -> None:
    data = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    image = ImagePart.from_bytes(format="png", data=data, blob_ref=None)
    messages = [ChatMessage(role="user", content=(image,))]
    changed = data[:-1] + b"\x00"
    values = [
        changed.hex(),
        list(changed),
        data.hex()[:-2],
        list(data[:-1]),
        [byte + 0.5 for byte in data],
        [*data[:20], True, *data[21:]],
        [*data[:20], 256, *data[21:]],
        [*data[:20], -1, *data[21:]],
        [data.hex()[:20], data.hex()[20:]],
        [list(data[:20]), list(data[20:])],
        {"OCR": "137, 80, 78, 71", "values": [1, 2, 3], "checksum": image.sha256},
    ]
    for value in values:
        candidate = RawCallPayload({"output": value})
        before = json.dumps(candidate.to_dict())
        assert image_response_is_audit_safe(messages, candidate, body=before.encode())
        assert json.dumps(candidate.to_dict()) == before
    assert not image_response_is_audit_safe(messages, RawCallPayload({"output": data.hex()}))
    assert not image_response_is_audit_safe(messages, RawCallPayload({"output": list(data)}))
    # Canonical audit JSON renders integral floats as integers, so this would
    # retain the same complete byte array even though the SDK supplied floats.
    assert not image_response_is_audit_safe(messages, RawCallPayload({"output": [float(byte) for byte in data]}))


def test_image_response_admission_checks_later_byte_array_after_invalid_token() -> None:
    data = b"\x89PNG\r\n\x1a\nprivate-image"
    image = ImagePart.from_bytes(format="png", data=data, blob_ref=None)
    messages = [ChatMessage(role="user", content=(image,))]
    body = "[256," + ",".join(str(byte) for byte in data) + ",false]"
    assert not image_response_is_audit_safe(messages, RawCallPayload({"content": body}))
    assert not image_response_is_audit_safe(messages, RawCallPayload({"content": body[:-7]}))


@pytest.mark.parametrize("encoding", ["base64", "hex", "array"])
@pytest.mark.parametrize("prefix", ['"broken ', '"broken \\" ', "\\", "ordinary "])
@pytest.mark.parametrize("location", ["raw", "transport", "decoded", "value"])
def test_image_response_admission_checks_escaped_echo_after_malformed_prefix(encoding: str, prefix: str, location: str) -> None:
    image = ImagePart.from_bytes(format="png", data=b"\x89PNG\r\n\x1a\nprivate-image", blob_ref=None)
    messages = [ChatMessage(role="user", content=(image,))]
    encoded = base64.b64encode(image.data).decode()
    if encoding == "hex":
        encoded = image.data.hex()
    elif encoding == "array":
        encoded = json.dumps(list(image.data))
    text = prefix + '"' + "".join(f"\\u{ord(char):04x}" for char in encoded) + '"'
    raw = text.encode()
    candidate: RawCallPayload | HTTPCallResponse = RawCallPayload({"content": "safe"})
    if location == "transport":
        candidate = HTTPCallResponse(
            status_code=200,
            headers={},
            transport=HTTPResponseTransport(body_b64=base64.b64encode(raw).decode(), headers=(), request_url="https://provider.example"),
        )
        assert not image_response_is_audit_safe(messages, candidate)
    elif location == "decoded":
        candidate = HTTPCallResponse(
            status_code=200,
            headers={},
            decoded_body=HTTPDecodedBodyEvidence(body_b64=base64.b64encode(raw).decode(), decoded_size=len(raw), complete=True),
        )
        assert not image_response_is_audit_safe(messages, candidate)
    elif location == "value":
        assert not image_response_is_audit_safe(messages, RawCallPayload({"content": text}))
    else:
        assert not image_response_is_audit_safe(messages, candidate, body=raw)
    assert image_response_is_audit_safe(messages, RawCallPayload({"content": prefix + '"ordinary"'}))
    assert image_response_is_audit_safe([ChatMessage(role="user", content="hello")], candidate, body=raw)


@pytest.mark.parametrize("as_text", [False, True])
def test_image_response_admission_bounds_matcher_memory_for_ordinary_numeric_metadata(as_text: bool) -> None:
    image = ImagePart.from_bytes(format="png", data=b"\x89PNG\r\n\x1a\nresource-control", blob_ref=None)
    messages = [ChatMessage(role="user", content=(image,))]
    numeric: list[int] | str = [0] * 131072
    if as_text:
        numeric = json.dumps(numeric, separators=(",", ":"))
    candidate = RawCallPayload({"metadata": numeric})
    text_size = len(json.dumps(candidate.to_dict(), separators=(",", ":")))
    tracemalloc.start()
    try:
        assert image_response_is_audit_safe(messages, candidate)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # Leave ample room for canonical serialization, while catching the numeric
    # repetition regex's measured >120x input-size backtracking allocation.
    assert peak < 64 * text_size
    assert not image_response_is_audit_safe(messages, RawCallPayload({"metadata": list(image.data)}))


@pytest.mark.parametrize("variant", ["base64", "hex", "byte_array"])
def test_image_response_admission_scans_escaped_quotes_and_backslashes_before_unicode_echo(variant: str) -> None:
    image = ImagePart.from_bytes(format="png", data=b"\x89PNG\r\n\x1a\nprivate-image", blob_ref=None)
    messages = [ChatMessage(role="user", content=(image,))]
    encoded = base64.b64encode(image.data).decode()
    if variant == "hex":
        encoded = image.data.hex()
    elif variant == "byte_array":
        encoded = json.dumps(list(image.data), separators=(",", ":"))
    prefix = 'quoted "before" \\ path \\\\ and \\"'
    literal = json.dumps(prefix + encoded + ' "after" \\ end')
    escaped = "".join(f"\\u{ord(char):04x}" for char in encoded)
    body = '{"before":"ordinary","echo":' + literal.replace(encoded, escaped) + ',"after":"ordinary"}'
    candidate = RawCallPayload({"content": "safe"})
    assert not image_response_is_audit_safe(messages, candidate, body=body.encode())
    assert image_response_is_audit_safe(messages, candidate, body=body.replace(escaped, "ordinary").encode())
