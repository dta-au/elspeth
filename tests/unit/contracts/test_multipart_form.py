"""Deterministic, bounded multipart request evidence."""

import hashlib

import pytest

from elspeth.contracts.call_data import HTTPCallRequest, MultipartPart, encode_multipart_form


def test_multipart_preserves_order_and_records_exact_wire_hash() -> None:
    blob = b"%PDF-1.7\r\nexample"
    ref = hashlib.sha256(blob).hexdigest()
    parts = (
        MultipartPart(name="q", value="A B"),
        MultipartPart(name="document", blob_ref=ref, filename="form.pdf", content_type="application/pdf"),
        MultipartPart(name="q", value="Café"),
    )
    body, metadata = encode_multipart_form(parts, {ref: blob}, max_body_bytes=4096)
    repeated_body, repeated_metadata = encode_multipart_form(parts, {ref: blob}, max_body_bytes=4096)

    assert body == repeated_body
    assert metadata == repeated_metadata
    assert body.count(b'name="q"') == 2
    assert body.index(b"A B") < body.index(blob) < body.index("Café".encode())
    assert body.endswith(f"--{metadata.boundary}--\r\n".encode())
    assert metadata.body_sha256 == hashlib.sha256(body).hexdigest()
    assert metadata.body_size == len(body)

    request = HTTPCallRequest(
        method="POST",
        url="https://example.gov.au/upload",
        headers={"Content-Type": f"multipart/form-data; boundary={metadata.boundary}"},
        multipart=metadata,
    ).to_dict()
    assert request["json"] is None
    assert request["body_encoding"] == "multipart-v1"
    assert request["body_sha256"] == metadata.body_sha256
    assert request["body_size"] == len(body)
    assert request["multipart"] == [part.to_dict() for part in parts]


def test_multipart_refuses_body_over_limit() -> None:
    parts = (MultipartPart(name="q", value="large"),)
    with pytest.raises(ValueError, match="exceeds max_body_bytes"):
        encode_multipart_form(parts, {}, max_body_bytes=4)


@pytest.mark.parametrize(
    "part",
    [
        {"name": "q", "value": "x", "blob_ref": "0" * 64},
        {"name": "q\r\nX-Injected: yes", "value": "x"},
        {"name": "file", "blob_ref": "a" * 64},
        {"name": "file", "blob_ref": "a" * 64, "filename": "x\r\nsecret", "content_type": "application/pdf"},
    ],
)
def test_multipart_rejects_ambiguous_or_unsafe_parts(part: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        MultipartPart(**part)
