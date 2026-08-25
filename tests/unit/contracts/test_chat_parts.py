"""ImagePart invariants, ChatMessage projections. Guards are mutation-tested:
every tampered field must raise, not just the happy path pass."""

import base64
import hashlib

import pytest

from elspeth.contracts.chat_parts import (
    ChatMessage,
    ImagePart,
    TextPart,
    audit_messages,
    parts_hash,
    wire_messages,
)

# Smallest valid 1x1 PNG (signature-correct real image).
PNG_BYTES = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 16  # signature-correct prefix


def _part(data: bytes = PNG_BYTES, fmt: str = "png") -> ImagePart:
    return ImagePart.from_bytes(format=fmt, data=data, blob_ref="a" * 64)


class TestImagePartFromBytes:
    def test_computes_hash_and_count(self) -> None:
        part = _part()
        assert part.sha256 == hashlib.sha256(PNG_BYTES).hexdigest()
        assert part.byte_count == len(PNG_BYTES)
        assert part.format == "png"
        assert part.blob_ref == "a" * 64

    def test_jpeg_signature_accepted(self) -> None:
        assert _part(JPEG_BYTES, "jpeg").format == "jpeg"

    def test_signature_mismatch_rejected(self) -> None:
        with pytest.raises(ValueError, match="signature"):
            ImagePart.from_bytes(format="jpeg", data=PNG_BYTES, blob_ref=None)

    def test_empty_data_rejected(self) -> None:
        with pytest.raises(ValueError):
            ImagePart.from_bytes(format="png", data=b"", blob_ref=None)


class TestImagePartInvariants:
    """A hand-built ImagePart cannot lie — __post_init__ re-asserts everything."""

    def test_tampered_sha256_rejected(self) -> None:
        with pytest.raises(ValueError, match="sha256"):
            ImagePart(format="png", data=PNG_BYTES, sha256="0" * 64, byte_count=len(PNG_BYTES), blob_ref=None)

    def test_tampered_byte_count_rejected(self) -> None:
        good = hashlib.sha256(PNG_BYTES).hexdigest()
        with pytest.raises(ValueError, match="byte_count"):
            ImagePart(format="png", data=PNG_BYTES, sha256=good, byte_count=1, blob_ref=None)

    def test_tampered_format_rejected(self) -> None:
        good = hashlib.sha256(PNG_BYTES).hexdigest()
        with pytest.raises(ValueError, match="signature"):
            ImagePart(format="jpeg", data=PNG_BYTES, sha256=good, byte_count=len(PNG_BYTES), blob_ref=None)

    def test_audit_view_has_no_bytes(self) -> None:
        view = _part().audit_view()
        assert view == {
            "type": "image",
            "format": "png",
            "sha256": hashlib.sha256(PNG_BYTES).hexdigest(),
            "byte_count": len(PNG_BYTES),
            "blob_ref": "a" * 64,
        }
        assert not any(isinstance(v, bytes) for v in view.values())


class TestProjections:
    def test_str_content_passes_through_both(self) -> None:
        msgs = [ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="hi")]
        assert wire_messages(msgs) == [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]
        assert audit_messages(msgs) == wire_messages(msgs)

    def test_wire_parts_are_openai_dialect(self) -> None:
        part = _part()
        msgs = [ChatMessage(role="user", content=(TextPart(text="describe"), part))]
        wire = wire_messages(msgs)
        b64 = base64.b64encode(PNG_BYTES).decode("ascii")
        assert wire == [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }
        ]

    def test_audit_parts_carry_no_bytes(self) -> None:
        part = _part()
        msgs = [ChatMessage(role="user", content=(TextPart(text="describe"), part))]
        audit = audit_messages(msgs)
        assert audit == [{"role": "user", "content": [{"type": "text", "text": "describe"}, part.audit_view()]}]

    def test_parts_hash_is_order_sensitive_and_bytes_free(self) -> None:
        a, b = _part(), _part(JPEG_BYTES, "jpeg")
        t = TextPart(text="x")
        h1 = parts_hash((t, a, b))
        h2 = parts_hash((t, b, a))
        assert h1 != h2
        assert len(h1) == 64
