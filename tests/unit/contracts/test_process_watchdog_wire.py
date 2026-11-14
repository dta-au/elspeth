"""Wire decoding of watchdog frames read from a peer socket (Tier-3 boundary)."""

from __future__ import annotations

import json

import pytest

from elspeth.contracts.process_watchdog_wire import FrameKind, WatchdogFrame, WatchdogProtocolError, decode_frame, encode_frame

_NONCE = "a" * 64


def test_decode_frame_roundtrips_a_valid_frame() -> None:
    frame = WatchdogFrame(FrameKind.HELLO, _NONCE, 1)
    assert decode_frame(encode_frame(frame)[4:]) == frame


@pytest.mark.parametrize("payload", [b"[]", b'"x"', b"1", b"null"])
def test_decode_frame_rejects_a_non_object_payload(payload: bytes) -> None:
    with pytest.raises(WatchdogProtocolError, match="Watchdog frame must be one object"):
        decode_frame(payload)


def test_decode_frame_rejects_unknown_fields() -> None:
    payload = json.dumps({"v": 1, "kind": "SHUTDOWN_REQUEST", "nonce": _NONCE, "target_pid": 1, "extra": 1}).encode()
    with pytest.raises(WatchdogProtocolError):
        decode_frame(payload)
