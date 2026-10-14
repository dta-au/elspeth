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
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, get_args

from elspeth.contracts.binary_documents import (
    BINARY_DOCUMENT_MIME_BY_FORMAT,
    binary_document_signature_matches,
)
from elspeth.contracts.call_data import CallPayload, HTTPCallResponse
from elspeth.contracts.freeze import freeze_fields
from elspeth.contracts.hashing import canonical_json

ImageFormat = Literal["jpeg", "png"]
"""Closed set of LLM-input image formats. Subset of BinaryDocumentFormat;
widening to pdf is a deliberate later change, not a config knob."""

IMAGE_FORMATS: frozenset[str] = frozenset(get_args(ImageFormat))

_IMAGE_MIME_BY_FORMAT = {fmt: BINARY_DOCUMENT_MIME_BY_FORMAT[fmt] for fmt in IMAGE_FORMATS}
_JSON_STRING_DELIMITER = re.compile(r'["\\]')
_JSON_ASCII_ESCAPE = re.compile(r"\\u00([0-7][0-9a-fA-F])")
_URL_ASCII_ESCAPE = re.compile(r"%[ \t\r\n\f\v]*([0-7])[ \t\r\n\f\v]*([0-9a-fA-F])")
_BASE64_NORMALIZATION = str.maketrans("-_", "+/", " \t\r\n\f\v")
_BASE64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_ENCODING_ESCAPED_WHITESPACE = re.compile(r"\\(?:[tnrfv]|x(?:09|0[a-dA-D]|20))")
_HEX_NORMALIZATION = str.maketrans("ABCDEF", "abcdef", " \t\r\n\f\v:-")
_JSON_BYTE_INTEGER = r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9][0-9]?|0)"
_JSON_BYTE_TOKEN = re.compile(r"(?<=[\[,])[ \t\r\n\f\v]*" + _JSON_BYTE_INTEGER + r"(?=[ \t\r\n\f\v]*(?:,|\]|$))")


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
        # Freeze unconditionally rather than gate on the carrier type: a list
        # of parts becomes the declared tuple, a tuple of parts is kept by
        # identity, and anything else is refused by the exact-type check.
        freeze_fields(self, "content")
        if type(self.content) is not tuple or not self.content:
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


def _image_encoding_probe(data: bytes) -> tuple[str, str]:
    """Match every base64 representation of the image's significant bits.

    Padding carries no image bits. A decoder also ignores the low bits of the
    final symbol when the image does not end at a three-byte boundary. Match
    those equivalent symbols, including when embedded in a larger value, just
    as the raw-byte check refuses any occurrence of the full submitted image.
    Keep the literal prefix and final-symbol set local to this call: compiling
    image-sized regular expressions would retain images in Python's regex cache.
    """
    encoded = base64.b64encode(data).decode("ascii")
    core = encoded.rstrip("=")
    padding = len(encoded) - len(core)
    if not padding:
        return core, ""
    first = _BASE64_ALPHABET.index(core[-1])
    final_symbols = _BASE64_ALPHABET[first : first + (1 << (2 * padding))]
    return core[:-1], final_symbols


def _contains_protected_encoding(text: str, protected: tuple[tuple[str, str], ...]) -> bool:
    for prefix, final_symbols in protected:
        if not final_symbols:
            if prefix in text:
                return True
        # Search complete literals, one temporary at a time. Restarting a long
        # prefix search after every wrong final symbol can revisit overlapping
        # prefixes quadratically; the pad-bit set bounds this to 4 or 16 searches.
        elif any(prefix + symbol in text for symbol in final_symbols):
            return True
    return False


def _contains_image_encoding_text(text: str, protected: tuple[tuple[str, str], ...], images: Sequence[ImagePart]) -> bool:
    # The named encodings use ASCII. Decode its bounded JSON escape form even
    # in malformed diagnostics: an unmatched quote must not hide a later echo.
    # Preserve punctuation so distinct fields cannot become one encoding.
    text = _JSON_ASCII_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), text)
    text = _ENCODING_ESCAPED_WHITESPACE.sub("", text)
    # Compare one ASCII URL-encoding layer as well as the literal text. Keep
    # '+' literal, preserve field punctuation, and never recursively decode.
    # ASCII wrapping may split an escape just as it splits base64 symbols.
    percent_decoded = _URL_ASCII_ESCAPE.sub(lambda match: chr(int(match.group(1) + match.group(2), 16)), text)
    if percent_decoded != text and _contains_image_encoding_view(percent_decoded, protected, images):
        return True
    return _contains_image_encoding_view(text, protected, images)


def _contains_image_encoding_view(text: str, protected: tuple[tuple[str, str], ...], images: Sequence[ImagePart]) -> bool:
    if _contains_protected_encoding(text.translate(_BASE64_NORMALIZATION), protected):
        return True
    compact_hex = text.translate(_HEX_NORMALIZATION)
    # Avoid allocating a full hex copy for images larger than the candidate.
    if any(2 * image.byte_count <= len(compact_hex) and image.data.hex() in compact_hex for image in images):
        return True
    decoded = bytearray()
    previous_end: int | None = None
    for token in _JSON_BYTE_TOKEN.finditer(text):
        # Only JSON byte-integer tokens enter this conversion: no booleans,
        # floats, negatives, or out-of-range values are coerced into image bytes.
        # Diagnostic text may wrap them with ASCII whitespace invalid in JSON.
        # Match one token at a time: repeating the token in one regex retains a
        # backtracking frame per byte and can dwarf the response's memory size.
        if previous_end is not None and text[previous_end : token.start()].strip(" \t\r\n\f\v") != ",":
            if any(image.data in decoded for image in images):
                return True
            decoded.clear()
        decoded.append(int(token.group()))
        previous_end = token.end()
    return any(image.data in decoded for image in images)


def _contains_image_encoding(text: str, protected: tuple[tuple[str, str], ...], images: Sequence[ImagePart]) -> bool:
    """Compare complete base64, hex and byte-array echoes without rewriting output.

    Decode individual JSON strings too: serialization escapes embedded tabs
    and newlines, and whole-document parsing discards duplicate-key values.
    HTTP protocol errors also carry Python bytes-repr whitespace escapes;
    fold those specific spellings without decoding arbitrary nested content.
    Keep JSON syntax intact so separate fields never become one encoding.
    """
    if _contains_image_encoding_text(text, protected, images):
        return True
    literal_start: int | None = None
    escaped_at = -1
    for delimiter in _JSON_STRING_DELIMITER.finditer(text):
        if delimiter.start() == escaped_at:
            continue
        if delimiter.group() == "\\":
            if literal_start is not None:
                escaped_at = delimiter.end()
            continue
        if literal_start is None:
            literal_start = delimiter.start()
            continue
        literal = text[literal_start : delimiter.end()]
        literal_start = None
        try:
            normalized_string = json.loads(literal)
        except json.JSONDecodeError:
            continue
        if _contains_image_encoding_text(normalized_string, protected, images):
            return True
    return False


def image_response_is_audit_safe(messages: Sequence[ChatMessage], response: CallPayload, *, body: bytes | None = None) -> bool:
    """Refuse submitted image bytes echoed anywhere in a candidate call audit.

    Admission never rewrites completion text. A provider echo cannot be retained
    as a parsed body, a header, or exact replay bytes. JSON string normalization
    also catches slash/unicode escapes, ASCII wrapping, equivalent alphabets,
    optional padding, unused pad bits and one ASCII percent-encoding layer,
    including malformed/duplicate-key
    bodies whose diagnostic representation would otherwise retain their raw text.
    Complete hex strings and contiguous JSON byte integers are compared with the
    submitted bytes too; this does not decode arbitrary reversible encodings or
    concatenate separate fields into an image.
    """
    images = [
        part for message in messages if not isinstance(message.content, str) for part in message.content if isinstance(part, ImagePart)
    ]
    if not images:
        return True
    protected = tuple(_image_encoding_probe(image.data) for image in images)
    audit_text = canonical_json(response.to_dict())
    if _contains_image_encoding(audit_text, protected, images):
        return False
    bodies = [body] if body is not None else []
    if body is None and isinstance(response, HTTPCallResponse):
        for evidence in (response.transport, response.decoded_body):
            if evidence is not None:
                bodies.append(base64.b64decode(evidence.body_b64, validate=True))
    for raw_body in bodies:
        if any(image.data in raw_body for image in images):
            return False
        try:
            normalized = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        except RecursionError:
            return False
        else:
            normalized_text = json.dumps(normalized, ensure_ascii=True)
            if _contains_image_encoding(normalized_text, protected, images):
                return False
        # Use the JSON byte decoder's encoding detection for the independent
        # literal scan too; whole-document parsing loses earlier duplicate keys.
        text = raw_body.decode(json.detect_encoding(raw_body), errors="replace")
        if _contains_image_encoding(text, protected, images):
            return False
    return True


def parts_hash(content: tuple[ContentPart, ...]) -> str:
    """Order-sensitive SHA-256 over the audit views of a parts tuple."""
    views: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, TextPart):
            views.append({"type": "text", "sha256": hashlib.sha256(part.text.encode("utf-8")).hexdigest()})
        else:
            views.append(part.audit_view())
    return hashlib.sha256(canonical_json(views).encode("utf-8")).hexdigest()
