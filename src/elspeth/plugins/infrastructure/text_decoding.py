"""One authority for how plugins decode external bytes with a configured codec.

Sources and blob transforms read text whose bytes they do not own (Tier 3).
Two rules must hold identically in every reader, so they live here:

* ``decoding_codec`` -- a configured UTF-8 codec decodes as ``utf-8-sig``, so a
  leading byte-order mark is consumed instead of entering row data (or making
  ``json.loads`` refuse the document). Only a BOM at the very start is
  stripped; a U+FEFF anywhere else is data and is kept.
* ``BYTE_ESCAPE_ERRORS`` -- an error handler that escapes every undecodable
  byte, so a bad code unit stays on the line that carried it for every codec,
  UTF-16/UTF-32 included (``surrogateescape`` cannot carry those and aborts the
  whole read-ahead chunk).
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path


def decoding_codec(encoding: str) -> str:
    """Return the codec to decode external bytes configured as ``encoding``.

    UTF-8 decodes as ``utf-8-sig`` (strips one leading BOM); every other codec
    is returned unchanged (the UTF-16/UTF-32 codecs already consume their BOM).
    """
    return "utf-8-sig" if codecs.lookup(encoding).name == "utf-8" else encoding


BYTE_ESCAPE_ERRORS = "elspeth.source_byte_escape"
"""Codec error-handler name that escapes every undecodable byte (see below)."""


def _escape_undecodable_bytes(exc: UnicodeError) -> tuple[str, int]:
    """Decode error handler: replace each undecodable byte ``b`` with U+DC00+b.

    ``surrogateescape`` only escapes bytes >= 0x80 and re-raises otherwise, so
    for UTF-16/UTF-32 an illegal code unit such as a lone surrogate
    (``b"\\x00\\xd8"``) escapes nothing and TextIOWrapper raises for its whole
    read-ahead chunk, dropping valid lines on both sides of the bad bytes.
    Escaping every byte keeps the decode going, so the bad bytes stay on the
    line that carried them. A strict decode never yields U+DC00..U+DCFF
    (lone surrogates are invalid in every Unicode encoding), so these code
    points mark the escaped bytes unambiguously.
    """
    if not isinstance(exc, UnicodeDecodeError):
        raise exc
    undecodable = exc.object[exc.start : exc.end]
    return "".join(chr(0xDC00 + byte) for byte in undecodable), exc.end


codecs.register_error(BYTE_ESCAPE_ERRORS, _escape_undecodable_bytes)


def contains_escaped_bytes(value: str) -> bool:
    """Return True when value carries bytes escaped by ``BYTE_ESCAPE_ERRORS``."""
    return any(0xDC00 <= ord(char) <= 0xDCFF for char in value)


@dataclass(frozen=True, slots=True)
class LineByteCodec:
    """Fixed byte order and the BOM consumed from a stream's first line."""

    codec: str
    first_line_prefix: bytes


_BOM_ORDERS: dict[str, tuple[tuple[bytes, str], ...]] = {
    "utf-16": ((codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")),
    "utf-32": ((codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be")),
    "utf-8-sig": ((codecs.BOM_UTF8, "utf-8"),),
}
_NO_BOM_FALLBACK = {"utf-16": "utf-16-le", "utf-32": "utf-32-le", "utf-8-sig": "utf-8"}


def line_byte_codec_from_head(head: bytes, read_encoding: str) -> LineByteCodec:
    """Derive a stream's reconstruction codec from only its leading bytes."""
    name = codecs.lookup(read_encoding).name
    if name not in _BOM_ORDERS:
        return LineByteCodec(codec=read_encoding, first_line_prefix=b"")
    for bom, fixed_codec in _BOM_ORDERS[name]:
        if head.startswith(bom):
            return LineByteCodec(codec=fixed_codec, first_line_prefix=bom)
    return LineByteCodec(codec=_NO_BOM_FALLBACK[name], first_line_prefix=b"")


def line_byte_codec(path: Path, read_encoding: str) -> LineByteCodec:
    """Derive a local file's reconstruction codec from its first bytes."""
    with path.open("rb") as raw:
        return line_byte_codec_from_head(raw.read(4), read_encoding)


def escaped_line_to_bytes(value: str, line_codec: LineByteCodec, *, line_number: int) -> bytes:
    """Rebuild the raw bytes of a line decoded with ``BYTE_ESCAPE_ERRORS``.

    Escaped code points become their original byte; every other run is
    re-encoded with the stream's fixed byte order. Only line 1 regains the
    BOM consumed by the decoder.
    """
    encoder = codecs.getincrementalencoder(line_codec.codec)()
    raw = bytearray(line_codec.first_line_prefix if line_number == 1 else b"")
    for escaped, run in groupby(value, key=lambda char: 0xDC00 <= ord(char) <= 0xDCFF):
        if escaped:
            raw.extend(ord(char) - 0xDC00 for char in run)
        else:
            raw.extend(encoder.encode("".join(run)))
    raw.extend(encoder.encode("", final=True))
    return bytes(raw)
