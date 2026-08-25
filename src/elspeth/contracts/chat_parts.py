"""Owned chat-message content parts for multimodal LLM calls.

Layer: L0. No upward imports.

One authority for what an LLM message IS inside ELSPETH: a role plus either a
plain string (text-only — byte-identical audit behavior to the pre-image tree)
or an ordered tuple of typed parts. Two projections derive from it and are the
ONLY sanctioned exits: ``wire_messages`` (OpenAI content-parts dialect — the
single wire dialect; litellm translates it for Bedrock/Converse) and
``audit_messages`` (bytes-free — the only image representation permitted in
audit, tracing, hashing, and logs). Image bytes never leave this module any
other way.

Signature validation follows binary_documents doctrine: the byte signature
proves agreement with the declared format; it never chooses one.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, get_args

from elspeth.contracts.binary_documents import binary_document_signature_matches
from elspeth.contracts.hashing import canonical_json

ImageFormat = Literal["jpeg", "png"]
"""Closed set of LLM-input image formats. Subset of BinaryDocumentFormat;
widening to pdf is a deliberate later change, not a config knob."""

IMAGE_FORMATS: frozenset[str] = frozenset(get_args(ImageFormat))

_IMAGE_MIME_BY_FORMAT = {"jpeg": "image/jpeg", "png": "image/png"}


@dataclass(frozen=True, slots=True)
class TextPart:
    """One text segment of a message's content."""

    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError(f"TextPart.text must be str, got {type(self.text).__name__}")


@dataclass(frozen=True, slots=True)
class ImagePart:
    """One image segment. Construct via from_bytes(); __post_init__ re-asserts
    every invariant so a hand-built instance cannot lie."""

    format: ImageFormat
    data: bytes
    sha256: str
    byte_count: int
    blob_ref: str | None

    def __post_init__(self) -> None:
        if self.format not in IMAGE_FORMATS:
            raise ValueError(f"ImagePart.format must be one of {sorted(IMAGE_FORMATS)}, got {self.format!r}")
        if not isinstance(self.data, bytes) or not self.data:
            raise ValueError("ImagePart.data must be non-empty bytes")
        if not binary_document_signature_matches(self.format, self.data):
            raise ValueError(f"ImagePart data does not carry the {self.format} byte signature")
        actual_hash = hashlib.sha256(self.data).hexdigest()
        if self.sha256 != actual_hash:
            raise ValueError("ImagePart.sha256 does not match data")
        if self.byte_count != len(self.data):
            raise ValueError("ImagePart.byte_count does not match data")
        if self.blob_ref is not None and not isinstance(self.blob_ref, str):
            raise ValueError("ImagePart.blob_ref must be str or None")

    @classmethod
    def from_bytes(cls, *, format: ImageFormat, data: bytes, blob_ref: str | None) -> ImagePart:
        if not isinstance(data, bytes) or not data:
            raise ValueError("ImagePart.from_bytes requires non-empty bytes")
        return cls(
            format=format,
            data=data,
            sha256=hashlib.sha256(data).hexdigest(),
            byte_count=len(data),
            blob_ref=blob_ref,
        )

    def audit_view(self) -> dict[str, str | int | None]:
        """The bytes-free projection — the ONLY image shape audit may hold."""
        return {
            "type": "image",
            "format": self.format,
            "sha256": self.sha256,
            "byte_count": self.byte_count,
            "blob_ref": self.blob_ref,
        }


ContentPart = TextPart | ImagePart


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One chat message. content is str for text-only (audit byte-identical to
    the pre-image tree) or an ordered non-empty tuple of parts."""

    role: Literal["system", "user", "assistant"]
    content: str | tuple[ContentPart, ...]

    def __post_init__(self) -> None:
        if self.role not in ("system", "user", "assistant"):
            raise ValueError(f"ChatMessage.role invalid: {self.role!r}")
        if isinstance(self.content, str):
            return
        if not isinstance(self.content, tuple) or not self.content:
            raise ValueError("ChatMessage.content must be str or a non-empty tuple of parts")
        for part in self.content:
            if not isinstance(part, (TextPart, ImagePart)):
                raise ValueError(f"ChatMessage part must be TextPart or ImagePart, got {type(part).__name__}")


def _wire_content(content: str | tuple[ContentPart, ...]) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    out: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, TextPart):
            out.append({"type": "text", "text": part.text})
        else:
            b64 = base64.b64encode(part.data).decode("ascii")
            mime = _IMAGE_MIME_BY_FORMAT[part.format]
            out.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    return out


def _audit_content(content: str | tuple[ContentPart, ...]) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    out: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, TextPart):
            out.append({"type": "text", "text": part.text})
        else:
            out.append(part.audit_view())
    return out


def wire_messages(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    """OpenAI-dialect wire form. The only projection that may contain bytes
    (base64-encoded), and it goes to the provider ONLY — never to audit."""
    return [{"role": m.role, "content": _wire_content(m.content)} for m in messages]


def audit_messages(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    """Bytes-free audit form for LLMCallRequest recording."""
    return [{"role": m.role, "content": _audit_content(m.content)} for m in messages]


def parts_hash(content: tuple[ContentPart, ...]) -> str:
    """Order-sensitive SHA-256 over the audit views of a parts tuple."""
    views: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, TextPart):
            views.append({"type": "text", "sha256": hashlib.sha256(part.text.encode("utf-8")).hexdigest()})
        else:
            views.append(part.audit_view())
    return hashlib.sha256(canonical_json(views).encode("utf-8")).hexdigest()
