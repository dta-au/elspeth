"""Data-only maintenance RPC; signed admission remains in the operator.

The worker's capture and test observations are compared with an independently
derived baseline. They never assert signed validity or authorize publication.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import socket
import stat
import struct
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from elspeth_lints.core.judge import JudgeConfigurationError, JudgeContractError, JudgeTransportError
from elspeth_lints.core.judge_rpc import (
    MAX_DEPTH,
    MAX_EVIDENCE_BYTES,
    MAX_EVIDENCE_FILES,
    MAX_FILE_BYTES,
    MAX_NODES,
    digest,
    integer,
    object_fields,
    text,
)
from elspeth_lints.core.judge_rpc import MAX_FRAME_BYTES as MAX_FRAME_BYTES
from elspeth_lints.core.operator_runtime import validate_artifact_manifest, validate_protected_path

ADMISSION_PATH = Path("/etc/elspeth/baseline-workers.json")
SUPERVISOR_SOCKET = Path("/run/elspeth-baseline/supervisor.sock")
SUPERVISOR_IMAGE = Path("/opt/elspeth/baseline-supervisor")
SUPERVISOR_UNIT = Path("/etc/systemd/system/elspeth-baseline-supervisor.service")
WORKER_IMAGE = Path("/opt/elspeth/baseline-worker")
PROFILE = "ubuntu-systemd-python313-v1"
BASELINE_PATH = "tests/unit/elspeth_lints/fixtures/fingerprint_baseline.json"
CAPTURE_MODULE = "tests/unit/elspeth_lints/test_allowlist_loader_unification.py"
TEST_NODE = CAPTURE_MODULE + "::test_baseline_capture_is_self_consistent"
MAINTENANCE_SECONDS = 1800
CALL_SECONDS = MAINTENANCE_SECONDS + 90
MAX_BASE64_BYTES = ((MAX_FILE_BYTES + 2) // 3) * 4
_REQUEST_FIELDS = frozenset({"version", "action", "request_id", "request_sha256", "snapshot_sha256", "files", "expected_baseline"})


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise JudgeContractError("maintenance JSON contains duplicate fields")
        result[key] = value
    return result


def _nonfinite(_value: str) -> None:
    raise JudgeContractError("maintenance JSON contains nonfinite data")


def decode_json(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_FRAME_BYTES:
        raise JudgeContractError("maintenance frame exceeds its bound")
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_nonfinite)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise JudgeContractError("maintenance frame is not unambiguous UTF-8 JSON") from exc
    if type(result) is not dict:
        raise JudgeContractError("maintenance frame must contain an object")
    pending: list[tuple[object, int]] = [(result, 0)]
    count = 0
    while pending:
        value, depth = pending.pop()
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise JudgeContractError("maintenance JSON exceeds structural bounds")
        if type(value) is dict:
            for key, child in value.items():
                text(key, limit=4096)
                pending.append((child, depth + 1))
        elif type(value) is list:
            pending.extend((child, depth + 1) for child in value)
        elif type(value) is str:
            try:
                if len(value.encode("utf-8")) > MAX_BASE64_BYTES or "\x00" in value:
                    raise JudgeContractError("maintenance string exceeds its bound")
            except UnicodeError as exc:
                raise JudgeContractError("maintenance JSON contains invalid Unicode") from exc
        elif type(value) is float and not math.isfinite(value):
            raise JudgeContractError("maintenance JSON contains nonfinite data")
    return result


def canonical_json(value: object) -> bytes:
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise JudgeContractError("maintenance data cannot be encoded") from exc
    # Arrays are needed for the snapshot digest; use an object only for the
    # structural check, while returning precisely the canonical array bytes.
    decode_json(raw if type(value) is dict else b'{"value":' + raw + b"}")
    return raw


def frame_bytes(value: object) -> bytes:
    raw = canonical_json(value)
    if len(raw) > MAX_FRAME_BYTES:
        raise JudgeContractError("maintenance frame exceeds its bound")
    return struct.pack("!I", len(raw)) + raw


def receive_frame(channel: socket.socket, *, deadline_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + deadline_seconds

    def read_exact(size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise JudgeTransportError("maintenance RPC deadline expired")
            channel.settimeout(remaining)
            try:
                chunk = channel.recv(min(65536, size - len(result)))
            except TimeoutError as exc:
                raise JudgeTransportError("maintenance RPC deadline expired") from exc
            except OSError as exc:
                raise JudgeTransportError("maintenance RPC channel failed") from exc
            if not chunk:
                raise JudgeTransportError("maintenance RPC closed before a terminal response")
            result.extend(chunk)
        return bytes(result)

    size = struct.unpack("!I", read_exact(4))[0]
    if not 0 < size <= MAX_FRAME_BYTES:
        raise JudgeContractError("maintenance frame exceeds its bound")
    return decode_json(read_exact(size))


def logical_path(value: object) -> str:
    value = text(value, limit=4096)
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(part in {".", "..", ".git", ".venv"} for part in path.parts) or "\\" in value:
        raise JudgeContractError("maintenance file path is unsafe")
    return value


def file_bytes(record: dict[str, str]) -> bytes:
    value = record["content"]
    if type(value) is not str or len(value) > MAX_BASE64_BYTES:
        raise JudgeContractError("maintenance file content exceeds its bound")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise JudgeContractError("maintenance content is not canonical base64") from exc
    if (
        len(raw) > MAX_FILE_BYTES
        or base64.b64encode(raw).decode("ascii") != value
        or hashlib.sha256(raw).hexdigest() != digest(record["sha256"])
    ):
        raise JudgeContractError("maintenance file digest or canonical base64 is invalid")
    return raw


def validate_files(value: object) -> list[dict[str, str]]:
    if type(value) is not list or not 1 <= len(value) <= MAX_EVIDENCE_FILES:
        raise JudgeContractError("maintenance file census exceeds its bound")
    files: list[dict[str, str]] = []
    by_path: dict[str, dict[str, str]] = {}
    directories: set[str] = {"."}
    links: dict[str, str] = {}
    total = 0
    for raw_record in value:
        record = object_fields(raw_record, frozenset({"path", "mode", "sha256", "content"}))
        path = logical_path(record["path"])
        mode = text(record["mode"], limit=6)
        if mode not in {"100644", "100755", "120000"} or path in by_path:
            raise JudgeContractError("maintenance file mode or duplicate path is invalid")
        raw = file_bytes(record)
        total += len(raw)
        if total > MAX_EVIDENCE_BYTES:
            raise JudgeContractError("maintenance snapshot byte bound exceeded")
        by_path[path] = record
        files.append(record)
        directories.update(str(parent) for parent in PurePosixPath(path).parents)
        if mode == "120000":
            try:
                target = raw.decode("utf-8")
            except UnicodeError as exc:
                raise JudgeContractError("maintenance symlink target is invalid") from exc
            if not target or target.startswith("/") or "\\" in target or "\x00" in target:
                raise JudgeContractError("maintenance symlink target is unsafe")
            links[path] = target
    if set(by_path) & directories:
        raise JudgeContractError("maintenance file conflicts with a parent directory")

    def resolved(link: str) -> str:
        # Resolve as the filesystem does: walk from the link's parent and follow
        # a census link as soon as a prefix names one, so a later ".." climbs from
        # the followed target. Lexical normalisation would collapse "d/.." first
        # and miss an escape through a link to a shallower directory.
        follows = 0

        def walk(origin: str) -> list[str]:
            nonlocal follows
            follows += 1
            if follows > 40:
                raise JudgeContractError("maintenance symlink cycle is unsupported")
            stack = list(PurePosixPath(origin).parent.parts)
            for part in links[origin].split("/"):
                if part in {"", "."}:
                    continue
                if stack and "/".join(stack) not in directories:
                    raise JudgeContractError("maintenance symlink has an unavailable target")
                if part == "..":
                    if not stack:
                        raise JudgeContractError("maintenance symlink escapes or cycles its snapshot")
                    stack.pop()
                    continue
                stack.append(part)
                if "/".join(stack) in links:
                    stack = walk("/".join(stack))
            return stack

        final = walk(link)
        if not final:
            raise JudgeContractError("maintenance symlink escapes or cycles its snapshot")
        return logical_path("/".join(final))

    for path in links:
        final = resolved(path)
        if final not in by_path and final not in directories:
            raise JudgeContractError("maintenance symlink has an unavailable target")
        if path.startswith(final + "/"):
            raise JudgeContractError("maintenance directory symlink creates a traversal cycle")
    if [record["path"] for record in files] != sorted(by_path):
        raise JudgeContractError("maintenance census must use canonical path order")
    for required in (BASELINE_PATH, CAPTURE_MODULE):
        if required not in by_path or by_path[required]["mode"] not in {"100644", "100755"}:
            raise JudgeContractError("maintenance snapshot lacks a required regular input")
    return files


def validate_baseline(value: object) -> dict[str, list[dict[str, str]]]:
    if type(value) is not dict or not 1 <= len(value) <= 128:
        raise JudgeContractError("maintenance baseline rule census is invalid")
    total = 0
    for rule, rows in value.items():
        text(rule, limit=256)
        if type(rows) is not list:
            raise JudgeContractError("maintenance baseline findings must be an array")
        total += len(rows)
        if total > 50000:
            raise JudgeContractError("maintenance baseline finding bound exceeded")
        for row in rows:
            row = object_fields(row, frozenset({"file_path", "fingerprint"}))
            logical_path(row["file_path"])
            text(row["fingerprint"], limit=256)
    return value


def make_request(files: list[dict[str, str]], snapshot_sha256: str, expected_baseline: dict[str, list[dict[str, str]]]) -> dict[str, Any]:
    request: dict[str, Any] = {
        "version": 1,
        "action": "baseline-capture",
        "request_id": uuid.uuid4().hex,
        "request_sha256": "",
        "snapshot_sha256": snapshot_sha256,
        "files": files,
        "expected_baseline": expected_baseline,
    }
    request["request_sha256"] = hashlib.sha256(canonical_json(request)).hexdigest()
    return validate_request(request)


def validate_request(value: object) -> dict[str, Any]:
    request = object_fields(value, _REQUEST_FIELDS)
    if type(request["version"]) is not int or request["version"] != 1 or request["action"] != "baseline-capture":
        raise JudgeContractError("maintenance protocol or action is unsupported")
    identifier = text(request["request_id"], limit=32)
    if len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
        raise JudgeContractError("maintenance request identifier is invalid")
    claimed = digest(request["request_sha256"])
    unsigned = dict(request)
    unsigned["request_sha256"] = ""
    if hashlib.sha256(canonical_json(unsigned)).hexdigest() != claimed:
        raise JudgeContractError("maintenance request correlation digest is invalid")
    files = validate_files(request["files"])
    if hashlib.sha256(canonical_json(files)).hexdigest() != digest(request["snapshot_sha256"]):
        raise JudgeContractError("maintenance snapshot digest is invalid")
    validate_baseline(request["expected_baseline"])
    return request


def response(request: dict[str, Any], *, baseline: object = None, terminal: object = None, error: str | None = None) -> dict[str, Any]:
    result = {key: request[key] for key in ("version", "action", "request_id", "request_sha256", "snapshot_sha256")}
    if error is not None:
        result.update(status="error", error=error)
    else:
        result.update(status="ok", baseline=baseline, terminal=terminal, python_version=[3, 13])
    return result


def validate_response(value: object, request: dict[str, Any]) -> None:
    if type(value) is not dict:
        raise JudgeContractError("maintenance response must be an object")
    status = value.get("status")
    fields = {"version", "action", "request_id", "request_sha256", "snapshot_sha256", "status"}
    fields.update({"error"} if status == "error" else {"baseline", "terminal", "python_version"})
    reply = object_fields(value, frozenset(fields))
    for field in ("version", "action", "request_id", "request_sha256", "snapshot_sha256"):
        if type(reply[field]) is not type(request[field]) or reply[field] != request[field]:
            raise JudgeContractError("maintenance response does not match its request")
    if status == "error":
        category = reply["error"]
        errors = {"configuration": JudgeConfigurationError, "transport": JudgeTransportError, "contract": JudgeContractError}
        if type(category) is not str or category not in errors:
            raise JudgeContractError("maintenance error category is invalid")
        raise errors[category](f"isolated maintenance worker reported a {category} failure")
    if (
        status != "ok"
        or type(reply["python_version"]) is not list
        or len(reply["python_version"]) != 2
        or any(type(n) is not int for n in reply["python_version"])
        or reply["python_version"] != [3, 13]
    ):
        raise JudgeContractError("maintenance requires canonical Python 3.13")
    if validate_baseline(reply["baseline"]) != request["expected_baseline"]:
        raise JudgeContractError("maintenance capture differs from the independently derived baseline")
    terminal = object_fields(reply["terminal"], frozenset({"nodeid", "passed", "failed", "errors", "skipped", "xfailed", "xpassed"}))
    if terminal["nodeid"] != TEST_NODE:
        raise JudgeContractError("maintenance confirmation used the wrong test")
    for key in ("passed", "failed", "errors", "skipped", "xfailed", "xpassed"):
        integer(terminal[key], maximum=1)
    if terminal["passed"] != 1 or any(terminal[key] != 0 for key in ("failed", "errors", "skipped", "xfailed", "xpassed")):
        raise JudgeContractError("maintenance confirmation requires exactly one passing test without skip or xfail")


@dataclass(frozen=True, slots=True)
class Admission:
    artifact_id: str
    interpreter_id: str
    unit_id: str
    allowed_uids: tuple[int, ...]
    worker_uid: int
    worker_gid: int
    worker_artifact_id: str


def parse_admission(value: object) -> Admission:
    data = object_fields(
        value,
        frozenset(
            {
                "version",
                "profile",
                "artifact_id",
                "interpreter_sha256",
                "unit_sha256",
                "allowed_client_uids",
                "worker_uid",
                "worker_gid",
                "worker_artifact_id",
            }
        ),
    )
    if type(data["version"]) is not int or data["version"] != 1 or data["profile"] != PROFILE:
        raise JudgeConfigurationError("maintenance profile is unavailable")
    uids = data["allowed_client_uids"]
    if type(uids) is not list or not 1 <= len(uids) <= 16:
        raise JudgeContractError("maintenance caller policy is invalid")
    allowed = tuple(integer(uid, maximum=2**31 - 1) for uid in uids)
    uid = integer(data["worker_uid"], minimum=1, maximum=2**31 - 1)
    gid = integer(data["worker_gid"], minimum=1, maximum=2**31 - 1)
    if len(set(allowed)) != len(allowed) or uid in allowed:
        raise JudgeConfigurationError("maintenance principal separation is unavailable")
    return Admission(
        digest(data["artifact_id"]),
        digest(data["interpreter_sha256"]),
        digest(data["unit_sha256"]),
        allowed,
        uid,
        gid,
        digest(data["worker_artifact_id"]),
    )


def load_admission() -> Admission:
    try:
        validate_protected_path(ADMISSION_PATH)
        admission = parse_admission(decode_json(ADMISSION_PATH.read_bytes()))
        validate_protected_path(SUPERVISOR_UNIT)
        if (
            hashlib.sha256(SUPERVISOR_UNIT.read_bytes()).hexdigest() != admission.unit_id
            or validate_artifact_manifest(SUPERVISOR_IMAGE) != admission.artifact_id
        ):
            raise JudgeConfigurationError("maintenance supervisor artifact or service unit changed")
        interpreter = SUPERVISOR_IMAGE / "usr/bin/python3.13"
        validate_protected_path(interpreter)
        if hashlib.sha256(interpreter.read_bytes()).hexdigest() != admission.interpreter_id:
            raise JudgeConfigurationError("maintenance supervisor interpreter changed")
        return admission
    except (OSError, ValueError, RuntimeError) as exc:
        raise JudgeConfigurationError("protected maintenance profile is unavailable") from exc


def authenticate_peer(channel: socket.socket) -> None:
    try:
        pid, uid, _gid = struct.unpack("3i", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        if uid != 0 or pid <= 1:
            raise JudgeConfigurationError("maintenance supervisor peer is not admitted")
        try:
            executable = Path(os.readlink(f"/proc/{pid}/exe"))
            root = Path(os.readlink(f"/proc/{pid}/root"))
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except PermissionError:
            # Root at the protected endpoint is the trusted principal;
            # admitted nonroot callers need no ptrace privilege.
            return
        if (
            executable != SUPERVISOR_IMAGE / "usr/bin/python3.13"
            or root != SUPERVISOR_IMAGE
            or argv != [b"/usr/bin/python3.13", b"-I", b"-B", b"-m", b"elspeth_lints.core.baseline_supervisor", b""]
        ):
            raise JudgeConfigurationError("maintenance supervisor process does not match its fixed profile")
    except (OSError, ValueError, struct.error) as exc:
        raise JudgeConfigurationError("maintenance supervisor identity is unavailable") from exc


def run_capture(files: list[dict[str, str]], snapshot_sha256: str, expected_baseline: dict[str, list[dict[str, str]]]) -> None:
    request = make_request(files, snapshot_sha256, expected_baseline)
    admission = load_admission()
    if os.getuid() not in admission.allowed_uids:
        raise JudgeConfigurationError("caller is not admitted to baseline maintenance")
    try:
        validate_protected_path(SUPERVISOR_SOCKET.parent, directory=True)
        metadata = SUPERVISOR_SOCKET.lstat()
        if metadata.st_uid != 0 or not stat.S_ISSOCK(metadata.st_mode):
            raise JudgeConfigurationError("maintenance endpoint is not protected")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(5)
            channel.connect(str(SUPERVISOR_SOCKET))
            authenticate_peer(channel)
            started = time.monotonic()
            channel.settimeout(CALL_SECONDS)
            channel.sendall(frame_bytes(request))
            reply = receive_frame(channel, deadline_seconds=max(0.001, CALL_SECONDS - (time.monotonic() - started)))
            channel.settimeout(2)
            if channel.recv(1):
                raise JudgeContractError("maintenance response contains trailing data")
        validate_response(reply, request)
    except TimeoutError as exc:
        raise JudgeTransportError("maintenance RPC deadline expired; operation cancelled") from exc
    except OSError as exc:
        raise JudgeConfigurationError("independent maintenance service is unavailable") from exc
