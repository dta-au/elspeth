"""Stdlib-only, bounded private watchdog wire admission."""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from enum import Enum

GRACE_NS = 30_000_000_000
FRAME_LIMIT = 1024
FRAME_COUNT_LIMIT = 8


class WatchdogProtocolError(Exception):
    """A private frame failed the closed protocol."""


class RecoveryReason(Enum):
    REQUIRED_GENERATION_UNRESOLVED = "REQUIRED_GENERATION_UNRESOLVED"
    REQUIRED_WORKER_LOST = "REQUIRED_WORKER_LOST"
    NORMAL_SHUTDOWN = "NORMAL_SHUTDOWN"
    FAILED_STARTUP = "FAILED_STARTUP"
    WATCHDOG_CHANNEL_FAILURE = "WATCHDOG_CHANNEL_FAILURE"


class FrameKind(Enum):
    HELLO = "HELLO"
    WATCHING_ACK = "WATCHING_ACK"
    BEGIN_RECOVERY = "BEGIN_RECOVERY"
    GRACE_ACK = "GRACE_ACK"
    COMPLETE = "COMPLETE"
    DISARM_ACK = "DISARM_ACK"


@dataclass(frozen=True, slots=True)
class WatchdogFrame:
    kind: FrameKind
    nonce: str
    target_pid: int
    reason: RecoveryReason | None = None
    armed_at_ns: int | None = None
    deadline_ns: int | None = None

    def __post_init__(self) -> None:
        if (
            type(self.kind) is not FrameKind
            or type(self.nonce) is not str
            or len(self.nonce) != 64
            or any(c not in "0123456789abcdef" for c in self.nonce)
        ):
            raise WatchdogProtocolError("Invalid watchdog frame identity")
        if type(self.target_pid) is not int or self.target_pid < 1:
            raise WatchdogProtocolError("Invalid watchdog target")
        if (self.kind is FrameKind.BEGIN_RECOVERY) != (type(self.reason) is RecoveryReason):
            raise WatchdogProtocolError("Invalid watchdog reason")
        if self.kind is FrameKind.GRACE_ACK:
            if (
                type(self.armed_at_ns) is not int
                or type(self.deadline_ns) is not int
                or self.armed_at_ns < 1
                or self.deadline_ns - self.armed_at_ns != GRACE_NS
            ):
                raise WatchdogProtocolError("Invalid watchdog clock receipt")
        elif self.armed_at_ns is not None or self.deadline_ns is not None:
            raise WatchdogProtocolError("Unexpected watchdog clock")

    def validate_target(self, *, nonce: str, target_pid: int) -> None:
        if not hmac.compare_digest(self.nonce, nonce) or self.target_pid != target_pid:
            raise WatchdogProtocolError("Watchdog authority mismatch")


def _reject_number(value: str) -> object:
    raise WatchdogProtocolError("Noninteger watchdog value")


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise WatchdogProtocolError("Duplicate watchdog field")
        result[key] = value
    return result


def decode_frame(payload: bytes) -> WatchdogFrame:
    try:
        value: object = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_unique_pairs, parse_float=_reject_number, parse_constant=_reject_number
        )
        if not isinstance(value, dict):
            raise WatchdogProtocolError("Watchdog frame must be one object")
        if type(value["v"]) is not int or value["v"] != 1 or type(value["kind"]) is not str:
            raise WatchdogProtocolError("Unknown watchdog frame version")
        kind = FrameKind(value["kind"])
        expected = {"v", "kind", "nonce", "target_pid"}
        if kind in (FrameKind.HELLO, FrameKind.WATCHING_ACK):
            expected.add("grace_ns")
        elif kind is FrameKind.BEGIN_RECOVERY:
            expected.add("reason")
        elif kind is FrameKind.GRACE_ACK:
            expected.update(("grace_ns", "armed_at_ns", "deadline_ns"))
        else:
            expected.add("completion_seq")
        if set(value) != expected or any(type(field) not in (str, int) for field in value.values()):
            raise WatchdogProtocolError("Watchdog frame fields disagree with schema")
        if "grace_ns" in value and (type(value["grace_ns"]) is not int or value["grace_ns"] != GRACE_NS):
            raise WatchdogProtocolError("Watchdog grace disagreement")
        if "completion_seq" in value and (type(value["completion_seq"]) is not int or value["completion_seq"] != 1):
            raise WatchdogProtocolError("Watchdog completion sequence disagreement")
        return WatchdogFrame(
            kind,
            value["nonce"],
            value["target_pid"],
            RecoveryReason(value["reason"]) if kind is FrameKind.BEGIN_RECOVERY else None,
            value["armed_at_ns"] if kind is FrameKind.GRACE_ACK else None,
            value["deadline_ns"] if kind is FrameKind.GRACE_ACK else None,
        )
    except (KeyError, TypeError, ValueError, UnicodeError) as error:
        raise WatchdogProtocolError("Invalid watchdog frame") from error


def encode_frame(frame: WatchdogFrame) -> bytes:
    fields: dict[str, str | int] = {"v": 1, "kind": frame.kind.value, "nonce": frame.nonce, "target_pid": frame.target_pid}
    if frame.kind in (FrameKind.HELLO, FrameKind.WATCHING_ACK, FrameKind.GRACE_ACK):
        fields["grace_ns"] = GRACE_NS
    if frame.reason is not None:
        fields["reason"] = frame.reason.value
    if frame.kind is FrameKind.GRACE_ACK:
        assert frame.armed_at_ns is not None and frame.deadline_ns is not None
        fields.update(armed_at_ns=frame.armed_at_ns, deadline_ns=frame.deadline_ns)
    if frame.kind in (FrameKind.COMPLETE, FrameKind.DISARM_ACK):
        fields["completion_seq"] = 1
    payload = json.dumps(fields, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if not 1 <= len(payload) <= FRAME_LIMIT:
        raise WatchdogProtocolError("Watchdog payload length refused")
    return len(payload).to_bytes(4, "big") + payload


class FrameReader:
    def __init__(self) -> None:
        self._buffer = bytearray()
        self._count = 0

    def feed(self, data: bytes) -> tuple[WatchdogFrame, ...]:
        if len(self._buffer) + len(data) > FRAME_LIMIT + 8:
            raise WatchdogProtocolError("Watchdog receive buffer exceeded")
        self._buffer.extend(data)
        frames: list[WatchdogFrame] = []
        while len(self._buffer) >= 4:
            length = int.from_bytes(self._buffer[:4], "big")
            if not 1 <= length <= FRAME_LIMIT:
                raise WatchdogProtocolError("Watchdog frame length refused")
            if len(self._buffer) < length + 4:
                break
            self._count += 1
            if self._count > FRAME_COUNT_LIMIT:
                raise WatchdogProtocolError("Watchdog frame count exceeded")
            frames.append(decode_frame(bytes(self._buffer[4 : 4 + length])))
            del self._buffer[: 4 + length]
        return tuple(frames)

    @property
    def read_capacity(self) -> int:
        return FRAME_LIMIT + 4 - len(self._buffer)
