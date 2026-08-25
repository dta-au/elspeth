"""Minimal PNG encoder for packed 8-bit RGB scanlines (no Pillow).

The worker renders with pypdfium2 ``rev_byteorder=True`` which yields a packed
RGB buffer; this encoder writes filter-type-0 scanlines into one IDAT.
"""

from __future__ import annotations

import struct
import zlib

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_COLOUR_TYPE_RGB = 2
_BYTES_PER_PIXEL = 3


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def encode_rgb_png(buffer: bytes | bytearray | memoryview, *, width: int, height: int, stride: int) -> bytes:
    """Encode a top-to-bottom packed RGB buffer as PNG.

    Raises ``ValueError`` when the declared geometry does not describe the buffer.
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"PNG geometry must be positive, got {width}x{height}")
    row_bytes = width * _BYTES_PER_PIXEL
    if stride < row_bytes:
        raise ValueError(f"stride {stride} is smaller than {row_bytes} bytes per row")
    if len(buffer) != stride * height:
        raise ValueError(f"buffer has {len(buffer)} bytes; expected stride*height = {stride * height}")
    view = memoryview(buffer)
    scanlines = bytearray()
    for row in range(height):
        start = row * stride
        scanlines += b"\x00"
        scanlines += view[start : start + row_bytes]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, _COLOUR_TYPE_RGB, 0, 0, 0)
    return _PNG_MAGIC + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(bytes(scanlines), 6)) + _chunk(b"IEND", b"")
