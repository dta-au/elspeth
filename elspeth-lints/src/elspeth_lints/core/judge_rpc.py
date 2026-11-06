"""Bounded data-only judge RPC to an independently installed supervisor.

This client never starts a process or imports a provider. Admission is fixed
operator-owned configuration, OS peer credentials and the installed artifact
closure, rather than a worker's claimed identity or an environment switch.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import socket
import stat
import struct
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from elspeth_lints.core.judge import (
    AgentToolScope,
    JudgeConfigurationError,
    JudgeContractError,
    JudgeRequest,
    JudgeTransportError,
    SimilarAllowlistEntry,
    _TransportResult,
)

MAX_FRAME_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_EVIDENCE_BYTES = 128 * 1024 * 1024
MAX_EVIDENCE_FILES = 12000
MAX_DEPTH = 12
MAX_NODES = 200000
CALL_DEADLINE_SECONDS = 330.0
WORKER_DEADLINE_SECONDS = 300
ADMISSION_PATH = Path("/etc/elspeth/judge-workers.json")
SUPERVISOR_SOCKET = Path("/run/elspeth-judge/supervisor.sock")
SUPERVISOR_IMAGE = Path("/opt/elspeth/judge-supervisor")
SUPERVISOR_UNIT = Path("/etc/systemd/system/elspeth-judge-supervisor.service")
SUPPORTED_PROFILE = "ubuntu-systemd-v1"
TRANSPORT_NAMES = frozenset({"openrouter", "claude_agent_sdk", "codex_cli"})
_REQUEST_FIELDS = frozenset(
    {
        "version",
        "request_id",
        "request_sha256",
        "transport",
        "model_id",
        "max_tokens",
        "request",
        "tools",
        "evidence",
        "excluded",
        "evidence_sha256",
    }
)
_JUDGE_REQUEST_FIELDS = frozenset(
    {
        "file_path",
        "rule_id",
        "symbol",
        "fingerprint",
        "rationale",
        "surrounding_code",
        "rule_definition",
        "rationale_duplicate_count",
        "similar_entries",
    }
)


def object_fields(value: object, fields: frozenset[str]) -> dict[str, Any]:
    """Admit a closed foreign JSON object; never echo its contents on error."""
    if type(value) is not dict or set(value) != fields:
        raise JudgeContractError("judge RPC object has an invalid closed schema")
    return value


def text(value: object, *, empty: bool = False, limit: int = MAX_FILE_BYTES) -> str:
    if type(value) is not str or (not empty and not value.strip()):
        raise JudgeContractError("judge RPC text field has an invalid type or value")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise JudgeContractError("judge RPC text is not valid UTF-8") from exc
    if size > limit or "\x00" in value:
        raise JudgeContractError("judge RPC text exceeds its permitted bound")
    return value


def integer(value: object, *, maximum: int = 2**63 - 1, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise JudgeContractError("judge RPC integer field has an invalid type or value")
    return value


def digest(value: object) -> str:
    result = text(value, limit=64)
    if len(result) != 64 or any(c not in "0123456789abcdef" for c in result):
        raise JudgeContractError("judge RPC digest is invalid")
    return result


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise JudgeContractError("judge RPC JSON contains a duplicate key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise JudgeContractError("judge RPC JSON contains a nonfinite number")


def decode_json(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_FRAME_BYTES:
        raise JudgeContractError("judge RPC frame exceeds its permitted bound")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise JudgeContractError("judge RPC frame is not unambiguous UTF-8 JSON") from exc
    if type(value) is not dict:
        raise JudgeContractError("judge RPC frame must contain an object")
    pending: list[tuple[object, int]] = [(value, 0)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if depth > MAX_DEPTH or count > MAX_NODES:
            raise JudgeContractError("judge RPC JSON exceeds its structural bounds")
        if type(node) is dict:
            for key, child in node.items():
                text(key, limit=256)
                pending.append((child, depth + 1))
        elif type(node) is list:
            pending.extend((child, depth + 1) for child in node)
        elif type(node) is float and not math.isfinite(node):
            raise JudgeContractError("judge RPC JSON contains a nonfinite number")
        elif type(node) is str:
            text(node, empty=True)
    return value


def canonical_json(value: object) -> bytes:
    try:
        raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise JudgeContractError("judge RPC cannot encode the supplied data") from exc
    decode_json(raw)
    return raw


def frame_bytes(value: object) -> bytes:
    raw = canonical_json(value)
    return struct.pack("!I", len(raw)) + raw


def receive_frame(channel: socket.socket, *, deadline_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + deadline_seconds

    def read_exact(size: int) -> bytes:
        parts = bytearray()
        while len(parts) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise JudgeTransportError("judge RPC deadline expired")
            channel.settimeout(remaining)
            try:
                chunk = channel.recv(min(65536, size - len(parts)))
            except TimeoutError as exc:
                raise JudgeTransportError("judge RPC deadline expired") from exc
            except OSError as exc:
                raise JudgeTransportError("judge RPC channel failed") from exc
            if not chunk:
                raise JudgeTransportError("judge RPC channel closed before a complete reply")
            parts.extend(chunk)
        return bytes(parts)

    size = struct.unpack("!I", read_exact(4))[0]
    if not 0 < size <= MAX_FRAME_BYTES:
        raise JudgeContractError("judge RPC frame exceeds its permitted bound")
    return decode_json(read_exact(size))


def _logical_path(value: object) -> str:
    result = text(value, limit=4096)
    path = PurePosixPath(result)
    if path.is_absolute() or any(part in {".", ".."} for part in result.split("/")) or "\\" in result:
        raise JudgeContractError("judge evidence path is not a canonical relative path")
    return result


def _read_regular(root: Path, relative: str) -> bytes:
    """Descriptor-relative no-follow traversal, including every parent."""
    parts = PurePosixPath(_logical_path(relative)).parts
    if not root.is_absolute():
        raise JudgeConfigurationError("judge evidence roots must be canonical absolute paths")
    directory_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in root.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
                raise JudgeConfigurationError("judge evidence contains an unsupported file or oversized file")
            raw = bytearray()
            while len(raw) <= MAX_FILE_BYTES:
                chunk = os.read(fd, min(65536, MAX_FILE_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            after = os.fstat(fd)
            if len(raw) > MAX_FILE_BYTES or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise JudgeConfigurationError("judge evidence changed while being captured")
            return bytes(raw)
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)


def capture_evidence(scope: AgentToolScope) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Freeze tracked readable evidence twice; never copy private/untracked data."""
    from elspeth_lints.core.source_excerpt import scrub_secrets
    from elspeth_lints.core.source_snapshot import tracked_relative_paths

    def capture() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        files: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        total = 0
        for root_index, root in enumerate(scope.allowed_roots):
            if root_index == 0:
                paths = tracked_relative_paths(root)
            else:
                # The only extra capability in the existing scope is the
                # selected allowlist directory, never its enclosing directory.
                # Keep this logical root populated even when it overlaps the
                # checkout: workers receive distinct root capabilities.
                from elspeth_lints.core.allowlist import iter_allowlist_yaml_paths

                paths = tuple(sorted(path.relative_to(root).as_posix() for path in iter_allowlist_yaml_paths(root)))
            for relative in paths:
                path = PurePosixPath(_logical_path(relative))
                if len(files) + len(excluded) >= MAX_EVIDENCE_FILES:
                    raise JudgeConfigurationError("judge evidence file-count bound exceeded")
                if any(part.startswith(".") or part in {"__pycache__", "node_modules"} for part in path.parts):
                    excluded.append({"root": root_index, "path": relative, "reason": "private-path"})
                    continue
                try:
                    raw = _read_regular(root, relative)
                    source = raw.decode("utf-8")
                except UnicodeError:
                    excluded.append({"root": root_index, "path": relative, "reason": "binary"})
                    continue
                except OSError as exc:
                    raise JudgeConfigurationError("judge evidence could not be captured safely") from exc
                if "\x00" in source:
                    excluded.append({"root": root_index, "path": relative, "reason": "binary"})
                    continue
                if scrub_secrets(source, path_hint=relative).redactions:
                    excluded.append({"root": root_index, "path": relative, "reason": "secret-redaction"})
                    continue
                total += len(raw)
                if total > MAX_EVIDENCE_BYTES:
                    raise JudgeConfigurationError("judge evidence byte bound exceeded")
                files.append({"root": root_index, "path": relative, "text": source})
        return files, excluded

    try:
        first = capture()
        second = capture()
    except ValueError as exc:
        raise JudgeConfigurationError("judge evidence cannot be bound to tracked inputs") from exc
    if first != second:
        raise JudgeConfigurationError("judge evidence changed while being captured")
    return second


def evidence_digest(files: object, excluded: object) -> str:
    return hashlib.sha256(canonical_json({"files": files, "excluded": excluded})).hexdigest()


def make_request(
    request: JudgeRequest, *, model_id: str, max_tokens: int, transport: str, tool_scope: AgentToolScope | None
) -> dict[str, Any]:
    from elspeth_lints.core.source_excerpt import scrub_secrets

    files, excluded = ([], []) if tool_scope is None else capture_evidence(tool_scope)
    payload = asdict(request)
    for key in ("file_path", "rule_id", "symbol", "fingerprint", "rationale", "surrounding_code", "rule_definition"):
        payload[key] = scrub_secrets(payload[key], path_hint=request.file_path).text
    payload["similar_entries"] = [
        {key: scrub_secrets(value, path_hint=request.file_path).text for key, value in asdict(entry).items()}
        for entry in request.similar_entries
    ]
    result: dict[str, Any] = {
        "version": 1,
        "request_id": uuid.uuid4().hex,
        "request_sha256": "",
        "transport": transport,
        "model_id": model_id,
        "max_tokens": max_tokens,
        "request": payload,
        "tools": None if tool_scope is None else {"roots": len(tool_scope.allowed_roots), "max_turns": tool_scope.max_turns},
        "evidence": files,
        "excluded": excluded,
        "evidence_sha256": evidence_digest(files, excluded),
    }
    result["request_sha256"] = hashlib.sha256(canonical_json(result)).hexdigest()
    validate_request(result)
    return result


def validate_request(value: object) -> dict[str, Any]:
    request = object_fields(value, _REQUEST_FIELDS)
    if type(request["version"]) is not int or request["version"] != 1:
        raise JudgeContractError("judge RPC protocol version is unsupported")
    identifier = text(request["request_id"], limit=32)
    if len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
        raise JudgeContractError("judge RPC request identifier is invalid")
    claimed = digest(request["request_sha256"])
    unsigned = dict(request)
    unsigned["request_sha256"] = ""
    if hashlib.sha256(canonical_json(unsigned)).hexdigest() != claimed:
        raise JudgeContractError("judge RPC request digest does not match its data")
    if type(request["transport"]) is not str or request["transport"] not in TRANSPORT_NAMES:
        raise JudgeContractError("judge RPC transport is unsupported")
    text(request["model_id"], limit=256)
    integer(request["max_tokens"], minimum=1, maximum=1000000)
    body = object_fields(request["request"], _JUDGE_REQUEST_FIELDS)
    for key in _JUDGE_REQUEST_FIELDS - {"rationale_duplicate_count", "similar_entries"}:
        text(body[key], empty=key == "rule_definition")
    _logical_path(body["file_path"])
    integer(body["rationale_duplicate_count"])
    similar = body["similar_entries"]
    if type(similar) is not list or len(similar) > 100:
        raise JudgeContractError("judge RPC similar-entry evidence is invalid")
    for entry in similar:
        entry = object_fields(entry, frozenset({"key", "owner", "reason_excerpt"}))
        for field in entry.values():
            text(field, empty=True)
    tools = request["tools"]
    roots = 0
    if tools is not None:
        tools = object_fields(tools, frozenset({"roots", "max_turns"}))
        roots = integer(tools["roots"], minimum=1, maximum=2)
        integer(tools["max_turns"], minimum=1, maximum=128)
        if request["transport"] == "openrouter":
            raise JudgeConfigurationError("OpenRouter does not support read-only judge tools")
    files, excluded = request["evidence"], request["excluded"]
    if type(files) is not list or type(excluded) is not list or len(files) + len(excluded) > MAX_EVIDENCE_FILES:
        raise JudgeContractError("judge RPC evidence census is invalid")
    if tools is None and (files or excluded):
        raise JudgeContractError("blinded judge requests cannot contain evidence")
    seen: set[tuple[int, str]] = set()
    total = 0
    for entries, fields in ((files, frozenset({"root", "path", "text"})), (excluded, frozenset({"root", "path", "reason"}))):
        for entry in entries:
            item = object_fields(entry, fields)
            root = integer(item["root"], maximum=roots - 1)
            path = _logical_path(item["path"])
            if (root, path) in seen:
                raise JudgeContractError("judge RPC evidence has duplicate paths")
            seen.add((root, path))
            if "text" in fields:
                total += len(text(item["text"], empty=True).encode("utf-8"))
            elif type(item["reason"]) is not str or item["reason"] not in {"private-path", "binary", "secret-redaction"}:
                raise JudgeContractError("judge RPC exclusion category is invalid")
    if total > MAX_EVIDENCE_BYTES or evidence_digest(files, excluded) != digest(request["evidence_sha256"]):
        raise JudgeContractError("judge RPC evidence digest or byte bound is invalid")
    return request


def request_type(request: dict[str, Any]) -> JudgeRequest:
    body = dict(request["request"])
    body["similar_entries"] = tuple(SimilarAllowlistEntry(**entry) for entry in body["similar_entries"])
    return JudgeRequest(**body)


def response_envelope(request: dict[str, Any], *, observation: dict[str, Any] | None = None, error: str | None = None) -> dict[str, Any]:
    result = {key: request[key] for key in ("version", "request_id", "request_sha256", "transport")}
    if error is not None:
        result.update(status="error", error=error)
    else:
        result.update(status="ok", observation=observation)
    return result


def parse_observation(value: object, request: dict[str, Any]) -> _TransportResult:
    if type(value) is not dict:
        raise JudgeContractError("judge RPC response must be an object")
    status = value.get("status")
    fields = frozenset({"version", "request_id", "request_sha256", "transport", "status", "error" if status == "error" else "observation"})
    reply = object_fields(value, fields)
    for key in ("version", "request_id", "request_sha256", "transport"):
        if type(reply[key]) is not type(request[key]) or reply[key] != request[key]:
            raise JudgeContractError("judge RPC response does not match the active request")
    if status == "error":
        category = reply["error"]
        errors = {"configuration": JudgeConfigurationError, "transport": JudgeTransportError, "contract": JudgeContractError}
        if type(category) is not str or category not in errors:
            raise JudgeContractError("judge RPC error category is invalid")
        raise errors[category](f"isolated judge worker reported a {category} failure")
    if status != "ok":
        raise JudgeContractError("judge RPC status is invalid")
    observation = object_fields(
        reply["observation"], frozenset({"raw_text", "served_model_id", "prompt_tokens_total", "prompt_tokens_cached"})
    )
    total = integer(observation["prompt_tokens_total"])
    cached = observation["prompt_tokens_cached"]
    if cached is not None:
        cached = integer(cached, maximum=total)
    return _TransportResult(text(observation["raw_text"]), text(observation["served_model_id"], limit=256), total, cached)


@dataclass(frozen=True, slots=True)
class SupervisorAdmission:
    runtime_root: Path
    artifact_id: str
    executable: Path
    executable_sha256: str
    allowed_client_uids: tuple[int, ...]
    worker_artifacts: dict[str, str]
    unit_sha256: str = ""


def parse_admission_data(value: object) -> SupervisorAdmission:
    """Parse fixed-profile configuration; this function grants no authority."""
    data = object_fields(
        value,
        frozenset(
            {
                "version",
                "profile",
                "socket",
                "runtime_root",
                "artifact_id",
                "executable",
                "executable_sha256",
                "allowed_client_uids",
                "worker_artifacts",
                "unit_sha256",
            }
        ),
    )
    if (
        type(data["version"]) is not int
        or data["version"] != 1
        or data["profile"] != SUPPORTED_PROFILE
        or data["socket"] != str(SUPERVISOR_SOCKET)
    ):
        raise JudgeConfigurationError("the supported judge supervisor profile is unavailable")
    runtime_root = Path(text(data["runtime_root"], limit=4096))
    executable = Path(text(data["executable"], limit=4096))
    if runtime_root != SUPERVISOR_IMAGE or executable != runtime_root / "usr/bin/python3":
        raise JudgeConfigurationError("judge supervisor image or executable is outside the fixed profile")
    artifact_id = digest(data["artifact_id"])
    executable_id = digest(data["executable_sha256"])
    uids = data["allowed_client_uids"]
    if type(uids) is not list or not 1 <= len(uids) <= 16:
        raise JudgeConfigurationError("judge supervisor caller policy is invalid")
    allowed = tuple(integer(uid, maximum=2**31 - 1) for uid in uids)
    if len(set(allowed)) != len(allowed):
        raise JudgeConfigurationError("judge supervisor caller policy has duplicate identities")
    artifacts = object_fields(data["worker_artifacts"], TRANSPORT_NAMES)
    return SupervisorAdmission(
        runtime_root,
        artifact_id,
        executable,
        executable_id,
        allowed,
        {key: digest(value) for key, value in artifacts.items()},
        digest(data["unit_sha256"]),
    )


def load_admission() -> SupervisorAdmission:
    from elspeth_lints.core.operator_runtime import validate_artifact_manifest, validate_protected_path

    try:
        validate_protected_path(ADMISSION_PATH)
        admission = parse_admission_data(decode_json(ADMISSION_PATH.read_bytes()))
        validate_protected_path(SUPERVISOR_UNIT)
        if hashlib.sha256(SUPERVISOR_UNIT.read_bytes()).hexdigest() != admission.unit_sha256:
            raise JudgeConfigurationError("judge supervisor service unit does not match admission")
        if validate_artifact_manifest(admission.runtime_root) != admission.artifact_id:
            raise JudgeConfigurationError("judge supervisor artifact does not match admission")
        validate_protected_path(admission.executable)
        if hashlib.sha256(admission.executable.read_bytes()).hexdigest() != admission.executable_sha256:
            raise JudgeConfigurationError("judge supervisor interpreter does not match admission")
        return admission
    except (OSError, ValueError, RuntimeError) as exc:
        raise JudgeConfigurationError("protected judge supervisor admission is unavailable or invalid") from exc


def authenticate_supervisor(channel: socket.socket, admission: SupervisorAdmission) -> None:
    """Authenticate root at the protected endpoint, then observable process data.

    Root independently provisions the authenticated image/unit and is outside
    the candidate/worker threat boundary. Nonroot callers need no ptrace
    privilege: Linux can hide root /proc links, while SO_PEERCRED still proves
    the trusted principal. No identity claim in a reply grants admission.
    """
    try:
        pid, uid, _gid = struct.unpack("3i", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        if uid != 0 or pid <= 1:
            raise JudgeConfigurationError("judge supervisor peer is not admitted")
        try:
            executable = Path(os.readlink(f"/proc/{pid}/exe"))
            process_root = Path(os.readlink(f"/proc/{pid}/root"))
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except PermissionError:
            return
        if process_root != admission.runtime_root:
            raise JudgeConfigurationError("judge supervisor does not run inside its admitted immutable image")
        expected = [b"/usr/bin/python3", b"-I", b"-B", b"-m", b"elspeth_lints.core.judge_supervisor", b""]
        if executable != admission.executable or argv != expected:
            raise JudgeConfigurationError("judge supervisor process identity does not match admission")
    except (OSError, ValueError, struct.error) as exc:
        raise JudgeConfigurationError("judge supervisor OS identity is unavailable") from exc


def call_worker(
    request: JudgeRequest, *, model_id: str, max_tokens: int, transport: str, tool_scope: AgentToolScope | None = None
) -> _TransportResult:
    admission = load_admission()
    if os.getuid() not in admission.allowed_client_uids:
        raise JudgeConfigurationError("caller is not admitted to the judge supervisor")
    payload = make_request(request, model_id=model_id, max_tokens=max_tokens, transport=transport, tool_scope=tool_scope)
    from elspeth_lints.core.operator_runtime import validate_protected_path

    try:
        validate_protected_path(SUPERVISOR_SOCKET.parent, directory=True)
        metadata = SUPERVISOR_SOCKET.lstat()
        # Socket write permission permits connecting; the protected parent
        # controls replacement and SO_PEERCRED controls admitted callers.
        if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != 0:
            raise JudgeConfigurationError("judge supervisor endpoint is not protected")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(5.0)
            channel.connect(str(SUPERVISOR_SOCKET))
            authenticate_supervisor(channel, admission)
            started = time.monotonic()
            channel.settimeout(CALL_DEADLINE_SECONDS)
            channel.sendall(frame_bytes(payload))
            reply = receive_frame(channel, deadline_seconds=max(0.001, CALL_DEADLINE_SECONDS - (time.monotonic() - started)))
            # Exactly one terminal frame followed by supervisor EOF. A second
            # frame, lingering writer or ambiguous disconnect is never a verdict.
            channel.settimeout(2.0)
            if channel.recv(1):
                raise JudgeContractError("judge supervisor sent trailing response data")
        return parse_observation(reply, payload)
    except TimeoutError as exc:
        raise JudgeTransportError("judge supervisor deadline expired; request cancelled") from exc
    except OSError as exc:
        raise JudgeConfigurationError("the independently supervised judge service is unavailable") from exc
