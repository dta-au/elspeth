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
from itertools import groupby


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


def escaped_line_to_bytes(value: str, encoding: str) -> bytes:
    """Rebuild the raw bytes of a line decoded with ``BYTE_ESCAPE_ERRORS``.

    Escaped code points become their original byte; every other run is
    re-encoded with ``encoding`` (it decoded from that codec, so it encodes
    back). A BOM-writing codec (``utf-16``, ``utf-32``) prefixes its BOM, as
    ``str.encode`` would.
    """
    encoder = codecs.getincrementalencoder(encoding)()
    raw = bytearray()
    for escaped, run in groupby(value, key=lambda char: 0xDC00 <= ord(char) <= 0xDCFF):
        if escaped:
            raw.extend(ord(char) - 0xDC00 for char in run)
        else:
            raw.extend(encoder.encode("".join(run)))
    raw.extend(encoder.encode("", final=True))
    return bytes(raw)
