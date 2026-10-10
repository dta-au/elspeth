"""Controls for bounded raw-query IPC and exact successful JSON."""

import json
import struct

import pytest

from elspeth.mcp.query_protocol import (
    HEADER_BYTES,
    RESULT_BYTES,
    FrameKind,
    QueryBudgetExceeded,
    QueryWorkerError,
    decode_header,
    encode_frame,
    encode_json,
)


@pytest.mark.parametrize(
    "value",
    [[], [{"unicode": '雪\n"', "number": 1.25, "null": None, "bool": True}], {"params": {"x": [1, 2]}}, '\\"\n'],
)
def test_successful_json_bytes_are_unchanged(value: object) -> None:
    expected = json.dumps(value, indent=2).encode("utf-8")
    assert encode_json(value, len(expected)) == expected
    with pytest.raises(QueryBudgetExceeded):
        encode_json(value, len(expected) - 1)


@pytest.mark.parametrize("value", ["x" * 1000, ["x" * 20] * 100, {"x" * 1000: 1}, "\0" * 100])
def test_single_scalar_keys_aggregate_and_escaping_are_bounded(value: object) -> None:
    with pytest.raises(QueryBudgetExceeded):
        encode_json(value, 128)


def test_deep_parameters_are_refused_before_recursion_failure() -> None:
    value: object = 1
    for _ in range(70):
        value = [value]
    with pytest.raises(QueryBudgetExceeded, match="nesting"):
        encode_json(value, 10000)


def test_serializer_errors_remain_errors() -> None:
    with pytest.raises(TypeError):
        encode_json(b"not-json", 100)


@pytest.mark.parametrize("kind", list(FrameKind))
def test_frame_roundtrip(kind: FrameKind) -> None:
    payload = b"[]" if kind is FrameKind.SUCCESS else b""
    frame = encode_frame(kind, payload)
    assert decode_header(frame[:HEADER_BYTES]) == (kind, len(payload))
    assert frame[HEADER_BYTES:] == payload


@pytest.mark.parametrize("header", [b"", b"S\0", b"X\0\0\0\0", b"\xff\0\0\0\0", b"M\0\0\0\1"])
def test_malformed_header_is_not_admitted(header: bytes) -> None:
    with pytest.raises(QueryWorkerError):
        decode_header(header)


def test_oversized_length_refused_before_reading_body() -> None:
    with pytest.raises(QueryWorkerError):
        decode_header(b"S" + struct.pack("!I", RESULT_BYTES + 1))
    with pytest.raises(QueryBudgetExceeded):
        encode_frame(FrameKind.SUCCESS, b"x" * (RESULT_BYTES + 1))


def test_error_frame_cannot_exfiltrate_diagnostics() -> None:
    with pytest.raises(QueryWorkerError):
        encode_frame(FrameKind.DATABASE, b"private query or credentials")
