"""Scoped JCS writer using native string quoting and public RFC primitives.

This private entrypoint shares the existing contracts normalizer and serves
explicit continuation document projections. Public canonical_json remains
unchanged. Numeric formatting, unusual object keys and unsupported values
remain RFC-owned.
"""

from __future__ import annotations

from io import BytesIO
from json.encoder import encode_basestring
from typing import Any

import rfc8785

from elspeth.contracts.hashing import _normalize_frozen_and_reject_non_finite


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


def canonical_json_native_strings(value: object) -> str:
    """Return the existing canonical wire format with native string quoting."""
    normalized = _normalize_frozen_and_reject_non_finite(value)
    sink = BytesIO()
    _write(normalized, sink)
    return sink.getvalue().decode("utf-8")
