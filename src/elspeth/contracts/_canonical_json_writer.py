"""Write already normalized JCS values with native string quoting.

Normalization belongs to the caller. Numeric formatting, unusual object keys
and unsupported values remain owned by the public RFC 8785 implementation.
This module deliberately has no dependency on contracts.hashing.
"""

from __future__ import annotations

# Explicit reexports preserve the existing private wrapper's helper bindings.
from io import BytesIO as BytesIO
from json.encoder import encode_basestring as encode_basestring
from typing import Any

import rfc8785 as rfc8785


def _native_string_bytes(value: str) -> bytes | None:
    try:
        return encode_basestring(value).encode("utf-8")
    except UnicodeEncodeError:
        # Refuse through the original serializer OUTSIDE this error handler:
        # its escaped-string cause offsets and exception context must survive.
        return None


def _write(value: Any, sink: BytesIO) -> None:
    if isinstance(value, str):
        encoded = _native_string_bytes(value)
        if encoded is None:
            rfc8785.dump(value, sink)
        else:
            sink.write(encoded)
    elif type(value) is list:
        sink.write(b"[")
        for index, item in enumerate(value):
            if index:
                sink.write(b",")
            _write(item, sink)
        sink.write(b"]")
    elif type(value) is dict:
        if any(type(key) is not str for key in value):
            # Preserve RFC key-subclass behavior and invalid-key error order.
            rfc8785.dump(value, sink)
            return
        ordered = sorted(value.items(), key=lambda item: item[0].encode("utf-16be"))
        sink.write(b"{")
        for index, (key, item) in enumerate(ordered):
            if index:
                sink.write(b",")
            _write(key, sink)
            sink.write(b":")
            _write(item, sink)
        sink.write(b"}")
    else:
        # Includes numbers: do not reproduce JCS integer or float algorithms.
        rfc8785.dump(value, sink)


def _dumps(value: Any) -> bytes:
    """Serialize already normalized values; callers own the one normalization."""
    sink = BytesIO()
    _write(value, sink)
    return sink.getvalue()
