"""Stdlib PNG encoder for packed RGB buffers."""

from __future__ import annotations

import struct
import zlib

import pytest

from elspeth.plugins.infrastructure.rasterize.png import encode_rgb_png

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _chunks(png: bytes) -> list[tuple[bytes, bytes]]:
    assert png[:8] == PNG_MAGIC
    out: list[tuple[bytes, bytes]] = []
    offset = 8
    while offset < len(png):
        (length,) = struct.unpack(">I", png[offset : offset + 4])
        kind = png[offset + 4 : offset + 8]
        data = png[offset + 8 : offset + 8 + length]
        (crc,) = struct.unpack(">I", png[offset + 8 + length : offset + 12 + length])
        assert crc == zlib.crc32(kind + data) & 0xFFFFFFFF
        out.append((kind, data))
        offset += 12 + length
    return out


def test_encodes_2x2_rgb_with_filter_zero_scanlines() -> None:
    buffer = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255])
    png = encode_rgb_png(buffer, width=2, height=2, stride=6)
    chunks = _chunks(png)
    assert [kind for kind, _ in chunks] == [b"IHDR", b"IDAT", b"IEND"]
    ihdr = chunks[0][1]
    assert struct.unpack(">IIBBBBB", ihdr) == (2, 2, 8, 2, 0, 0, 0)
    raw = zlib.decompress(chunks[1][1])
    assert raw == b"\x00" + buffer[:6] + b"\x00" + buffer[6:]


def test_padded_stride_drops_padding_bytes() -> None:
    buffer = bytes([1, 2, 3, 9, 9]) + bytes([4, 5, 6, 9, 9])
    png = encode_rgb_png(buffer, width=1, height=2, stride=5)
    raw = zlib.decompress(_chunks(png)[1][1])
    assert raw == b"\x00\x01\x02\x03\x00\x04\x05\x06"


@pytest.mark.parametrize(
    ("width", "height", "stride", "length"),
    [(2, 2, 6, 11), (2, 2, 5, 10), (0, 1, 0, 0), (1, 0, 3, 0)],
)
def test_rejects_inconsistent_geometry(width: int, height: int, stride: int, length: int) -> None:
    with pytest.raises(ValueError):
        encode_rgb_png(bytes(length), width=width, height=height, stride=stride)
