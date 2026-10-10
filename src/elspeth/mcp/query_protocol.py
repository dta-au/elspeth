"""Bounded JSON and fixed framing for the disposable raw-query worker.

The parent never unpickles worker data or reconstructs successful query rows.
Successful payloads are the original ``json.dumps(result, indent=2)`` text.
Error frames contain only an owned category, never SQL, binds or credentials.
"""

from __future__ import annotations

import json
import struct
from enum import StrEnum

REQUEST_BYTES = 1024 * 1024
RESULT_BYTES = 8 * 1024 * 1024
WORKER_MEMORY_BYTES = 1024 * 1024 * 1024
QUERY_SECONDS = 30.0
CLEANUP_SECONDS = 2.0
HEADER_BYTES = 5
MAX_JSON_DEPTH = 64


class QueryBudgetExceeded(RuntimeError):
    """An explicit refusal; never a successful partial or truncated result."""


class QueryWorkerError(RuntimeError):
    """Worker/protocol failure, distinct from a known resource refusal."""


class FrameKind(StrEnum):
    SUCCESS = "S"
    MEMORY = "M"
    OUTPUT = "O"
    VALIDATION = "V"
    INTEGRITY = "I"
    PRIVILEGE = "P"
    DATABASE = "D"
    SERIALIZATION = "J"
    INTERNAL = "E"


def _check_json_size(value: object, remaining: int, depth: int = 0) -> int:
    """Bound parent-side traversal and scalar encoding before making copies.

    This conservative lower bound is followed by an exact encoded-byte check.
    A huge string is refused before JSONEncoder can allocate its escaped copy.
    Depth/node bounds also prevent unbounded traversal of caller parameters.
    """
    if depth > MAX_JSON_DEPTH or remaining < 1:
        raise QueryBudgetExceeded("Raw query JSON exceeds its size or nesting budget")
    remaining -= 1
    if isinstance(value, str):
        remaining -= len(value)
    elif isinstance(value, dict):
        for key, child in value.items():
            remaining = _check_json_size(key, remaining, depth + 1)
            remaining = _check_json_size(child, remaining, depth + 1)
    elif isinstance(value, (list, tuple)):
        for child in value:
            remaining = _check_json_size(child, remaining, depth + 1)
    if remaining < 0:
        raise QueryBudgetExceeded("Raw query JSON exceeds its size budget")
    return remaining


def encode_json(value: object, maximum: int) -> bytes:
    """Return byte-for-byte standard indented JSON or refuse the entire value."""
    _check_json_size(value, maximum)
    result = bytearray()
    for chunk in json.JSONEncoder(indent=2).iterencode(value):
        encoded = chunk.encode("utf-8")
        if len(encoded) > maximum - len(result):
            raise QueryBudgetExceeded("Raw query JSON exceeds its byte budget")
        result.extend(encoded)
    return bytes(result)


def encode_frame(kind: FrameKind, payload: bytes = b"") -> bytes:
    if len(payload) > RESULT_BYTES:
        raise QueryBudgetExceeded("Raw query result exceeds its byte budget")
    if kind is not FrameKind.SUCCESS and payload:
        raise QueryWorkerError("Error frames cannot contain external diagnostics")
    return kind.value.encode("ascii") + struct.pack("!I", len(payload)) + payload


def decode_header(header: bytes) -> tuple[FrameKind, int]:
    """Validate an untrusted worker header before allocating its payload."""
    if len(header) != HEADER_BYTES:
        raise QueryWorkerError("Incomplete raw query worker header")
    try:
        kind = FrameKind(header[:1].decode("ascii"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise QueryWorkerError("Invalid raw query worker frame kind") from exc
    length = struct.unpack("!I", header[1:])[0]
    if length > RESULT_BYTES or (kind is not FrameKind.SUCCESS and length != 0):
        raise QueryWorkerError("Invalid raw query worker payload length")
    return kind, length
