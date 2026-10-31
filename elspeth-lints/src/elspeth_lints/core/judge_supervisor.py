"""Independent root service for the Ubuntu systemd judge worker profile.

Install/start this service separately, before any signer invocation. It never
loads signer keys and never clones a signer's environment, memory or FDs.
The fixed service image and private provider homes require operator setup;
missing configuration or enforcement refuses rather than running locally.
"""

from __future__ import annotations

import hashlib
import os
import selectors
import shutil
import socket
import stat
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from elspeth_lints.core.judge import JudgeConfigurationError, JudgeContractError, JudgeTransportError
from elspeth_lints.core.judge_rpc import (
    ADMISSION_PATH,
    MAX_FRAME_BYTES,
    SUPERVISOR_SOCKET,
    SUPPORTED_PROFILE,
    TRANSPORT_NAMES,
    WORKER_DEADLINE_SECONDS,
    SupervisorAdmission,
    canonical_json,
    decode_json,
    digest,
    frame_bytes,
    integer,
    object_fields,
    parse_admission_data,
    parse_observation,
    receive_frame,
    response_envelope,
    text,
    validate_request,
)
from elspeth_lints.core.operator_runtime import validate_artifact_manifest, validate_protected_path

SUPERVISOR_CONFIG = Path("/etc/elspeth/judge-supervisor.json")
EVIDENCE_DIRECTORY = Path("/run/elspeth-judge/evidence")
SYSTEMD_RUN = Path("/usr/bin/systemd-run")
SYSTEMCTL = Path("/usr/bin/systemctl")
_SERVICE_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
STARTUP_DEADLINE_SECONDS = 15


@dataclass(frozen=True, slots=True)
class WorkerProfile:
    uid: int
    gid: int
    root_directory: Path
    artifact_id: str
    provider_home: Path
    authentication: str


def _safe_service_path(value: object) -> Path:
    raw = text(value, limit=4096)
    if (
        not raw.startswith("/")
        or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_/ ." for c in raw)
        or " " in raw
    ):
        raise JudgeConfigurationError("worker profile path cannot be encoded safely for systemd")
    result = Path(raw)
    if str(result) != raw or ".." in result.parts:
        raise JudgeConfigurationError("worker profile path is not canonical")
    return result


def load_profiles(admission: SupervisorAdmission) -> dict[str, WorkerProfile]:
    if os.getuid() != 0 or os.geteuid() != 0:
        raise JudgeConfigurationError("judge supervisor requires an independently launched root service")
    validate_protected_path(SUPERVISOR_CONFIG)
    data = object_fields(decode_json(SUPERVISOR_CONFIG.read_bytes()), frozenset({"version", "profile", "workers"}))
    if type(data["version"]) is not int or data["version"] != 1 or data["profile"] != SUPPORTED_PROFILE:
        raise JudgeConfigurationError("supported worker supervision profile is unavailable")
    workers = object_fields(data["workers"], TRANSPORT_NAMES)
    profiles: dict[str, WorkerProfile] = {}
    for transport, value in workers.items():
        entry = object_fields(value, frozenset({"uid", "gid", "root_directory", "artifact_id", "provider_home", "authentication"}))
        uid = integer(entry["uid"], minimum=1, maximum=2**31 - 1)
        gid = integer(entry["gid"], minimum=1, maximum=2**31 - 1)
        if uid in admission.allowed_client_uids:
            raise JudgeConfigurationError("worker principal overlaps an admitted caller")
        root = _safe_service_path(entry["root_directory"])
        if root != Path("/opt/elspeth/judge-workers") / transport:
            raise JudgeConfigurationError("worker image is outside the fixed Ubuntu profile")
        artifact_id = digest(entry["artifact_id"])
        if artifact_id != admission.worker_artifacts[transport] or validate_artifact_manifest(root) != artifact_id:
            raise JudgeConfigurationError("worker image does not match independent admission")
        # These files, the Python closure, provider SDK/CLI and helpers must
        # all occur in the exact image manifest, never under candidate control.
        validate_protected_path(root / "usr/bin/python3")
        home = _safe_service_path(entry["provider_home"])
        if home != Path("/var/lib/elspeth-judge/providers") / transport:
            raise JudgeConfigurationError("provider home is outside the fixed Ubuntu profile")
        validate_protected_path(home.parent, directory=True)
        metadata = home.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != uid or metadata.st_mode & 0o077:
            raise JudgeConfigurationError("worker provider home is not independent and private")
        authentication = text(entry["authentication"], limit=32)
        if authentication not in {"subscription", "api", "bedrock", "vertex", "azure"}:
            raise JudgeConfigurationError("worker credential profile is unsupported")
        profiles[transport] = WorkerProfile(uid, gid, root, artifact_id, home, authentication)
    if len({profile.uid for profile in profiles.values()}) != len(profiles):
        raise JudgeConfigurationError("provider profiles must use distinct worker principals")
    validate_protected_path(SYSTEMD_RUN)
    validate_protected_path(SYSTEMCTL)
    validate_protected_path(EVIDENCE_DIRECTORY, directory=True)
    return profiles


def _write_private_file(path: Path, raw: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        position = 0
        while position < len(raw):
            position += os.write(fd, raw[position:])
        os.fsync(fd)
        os.fchmod(fd, 0o444)
    finally:
        os.close(fd)


def _check_startup(channel: socket.socket, deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise JudgeTransportError("worker startup deadline expired")
    channel.setblocking(False)
    try:
        channel.recv(1, socket.MSG_PEEK)
    except BlockingIOError:
        return
    # EOF or extra data both cancel the single outstanding request.
    raise JudgeTransportError("judge caller closed or extended the startup request")


def materialize_snapshot(request: dict[str, Any], profile: WorkerProfile, channel: socket.socket, deadline: float) -> Path:
    """Create a root-owned immutable copy of exactly the admitted data bytes."""
    validate_request(request)
    _check_startup(channel, deadline)
    directory: Path = EVIDENCE_DIRECTORY / request["request_id"]
    directory.mkdir(mode=0o700)
    try:
        tools = request["tools"]
        if tools is not None:
            for index in range(tools["roots"]):
                (directory / f"root{index}").mkdir(mode=0o700)
            for item in request["evidence"]:
                _check_startup(channel, deadline)
                path = directory / f"root{item['root']}" / item["path"]
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                _write_private_file(path, item["text"].encode("utf-8"))
        mounted_profile = {
            "version": 1,
            "profile": SUPPORTED_PROFILE,
            "worker_uid": profile.uid,
            "worker_gid": profile.gid,
            "transport": request["transport"],
            "request_id": request["request_id"],
            "request_sha256": request["request_sha256"],
            "evidence_sha256": request["evidence_sha256"],
            "authentication": profile.authentication,
        }
        _write_private_file(directory / "profile.json", canonical_json(mounted_profile))
        _write_private_file(directory / "excluded.json", canonical_json({"excluded": request["excluded"]}))
        for parent, _dirs, _files in os.walk(directory, topdown=False):
            _check_startup(channel, deadline)
            Path(parent).chmod(0o555)
        return directory
    except BaseException:
        remove_snapshot(directory)
        raise


def remove_snapshot(directory: Path) -> None:
    # Only the root service owns these generated paths. Never apply this
    # cleanup to candidate paths or a value received as an executable/root.
    if directory.parent != EVIDENCE_DIRECTORY:
        raise JudgeConfigurationError("snapshot cleanup escaped its supervisor directory")
    for parent, _dirs, _files in os.walk(directory, topdown=False):
        Path(parent).chmod(0o700)
    shutil.rmtree(directory)


def service_command(request: dict[str, Any], profile: WorkerProfile, snapshot: Path) -> list[str]:
    """Fixed confinement profile; request data never chooses its enforcement."""
    unit = f"elspeth-judge-{request['request_id']}.service"
    properties = [
        f"User={profile.uid}",
        f"Group={profile.gid}",
        "SupplementaryGroups=",
        f"RootDirectory={profile.root_directory}",
        "MountAPIVFS=yes",
        "WorkingDirectory=/tmp",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "ProtectProc=invisible",
        "ProcSubset=pid",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectKernelLogs=yes",
        "ProtectControlGroups=yes",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "RestrictSUIDSGID=yes",
        "RestrictNamespaces=yes",
        "LockPersonality=yes",
        "RestrictRealtime=yes",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "UMask=0077",
        "KillMode=control-group",
        "SendSIGKILL=yes",
        "TimeoutStopSec=3",
        f"RuntimeMaxSec={WORKER_DEADLINE_SECONDS}",
        "MemoryMax=2G",
        "TasksMax=128",
        "LimitCORE=0",
        "LimitNOFILE=256",
        "TemporaryFileSystem=/tmp:rw,nosuid,nodev /run:rw,nosuid,nodev",
        "ReadWritePaths=/provider /tmp /run",
        f"BindPaths={profile.provider_home}:/provider",
        f"BindReadOnlyPaths={snapshot / 'profile.json'}:/run/elspeth-worker.json",
    ]
    if request["tools"] is not None:
        # This mounts the private copy, never any original candidate path.
        properties.append(f"BindReadOnlyPaths={snapshot}:/evidence")
    command = [str(SYSTEMD_RUN), "--quiet", "--pipe", "--wait", "--collect", "--service-type=exec", f"--unit={unit}"]
    for property_value in properties:
        command.extend(("--property", property_value))
    # All startup values are constants. Provider auth is opened independently
    # by the worker in /provider; no caller environment is copied to systemd.
    command.extend(
        ("--setenv=HOME=/provider", "--setenv=PATH=/usr/bin:/bin", "--setenv=LANG=C.UTF-8", "--setenv=PYTHONDONTWRITEBYTECODE=1")
    )
    command.extend(("/usr/bin/python3", "-I", "-B", "-m", "elspeth_lints.core.judge_worker"))
    return command


def _systemctl(*arguments: str) -> subprocess.CompletedProcess[bytes]:
    command = [str(SYSTEMCTL), *arguments]
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=dict(_SERVICE_ENV),
        cwd="/",
        close_fds=True,
    )
    output = bytearray()
    deadline = time.monotonic() + 5
    try:
        if process.stdout is None:
            raise JudgeTransportError("service manager output is unavailable")
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise JudgeTransportError("service manager deadline expired")
                if selector.select(remaining):
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > 8192:
                        raise JudgeTransportError("service manager output exceeds its bound")
        return subprocess.CompletedProcess(command, process.wait(timeout=max(0.001, deadline - time.monotonic())), bytes(output))
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()


def stop_unit(unit: str) -> None:
    """Ask the independent service manager to kill the complete worker tree."""
    _systemctl("kill", "--kill-whom=all", "--signal=KILL", unit)
    _systemctl("stop", unit)


def ensure_unit_dead(unit: str, *, launcher_reaped: bool) -> None:
    """Require launcher reaping, service-manager death and an empty cgroup."""
    if not launcher_reaped:
        raise JudgeTransportError("worker launcher death could not be verified")
    completed = _systemctl("show", "--property=LoadState,ActiveState,SubState,MainPID,ControlGroup", unit)
    if completed.returncode not in {0, 1} or len(completed.stdout) > 4096:
        raise JudgeTransportError("worker service death could not be verified")
    try:
        fields = dict(line.split("=", 1) for line in completed.stdout.decode("ascii").splitlines())
    except (UnicodeError, ValueError) as exc:
        raise JudgeTransportError("worker service death confirmation is malformed") from exc
    if (
        not set(fields) <= {"LoadState", "ActiveState", "SubState", "MainPID", "ControlGroup"}
        or fields.get("LoadState") not in {"loaded", "not-found"}
        or fields.get("ActiveState") not in {"inactive", "failed"}
    ):
        raise JudgeTransportError("worker service is still active")
    if fields.get("MainPID") != "0" and not (fields["LoadState"] == "not-found" and "MainPID" not in fields):
        raise JudgeTransportError("worker service retains a main process")
    expected = f"/system.slice/{unit}"
    if fields.get("ControlGroup") not in {None, "", expected}:
        raise JudgeTransportError("worker service cgroup identity is invalid")
    cgroup_root = Path("/sys/fs/cgroup")
    if not (cgroup_root / "cgroup.controllers").is_file() or not (cgroup_root / "system.slice").is_dir():
        raise JudgeTransportError("worker cgroup-v2 enforcement is unavailable")
    events = cgroup_root / expected.lstrip("/") / "cgroup.events"
    try:
        populated = events.read_text(encoding="ascii")
    except FileNotFoundError:
        return
    if "populated 0\n" not in populated:
        raise JudgeTransportError("worker service cgroup still contains processes")


def _exchange_process(process: subprocess.Popen[bytes], channel: socket.socket, request: dict[str, Any]) -> bytes:
    """Finite nonblocking I/O, including cancellation during startup/write."""
    if process.stdin is None or process.stdout is None:
        raise JudgeTransportError("worker service pipes are unavailable")
    pending = memoryview(frame_bytes(request))
    output = bytearray()
    deadline = time.monotonic() + WORKER_DEADLINE_SECONDS + 5
    os.set_blocking(process.stdin.fileno(), False)
    os.set_blocking(process.stdout.fileno(), False)
    with selectors.DefaultSelector() as selector:
        selector.register(channel, selectors.EVENT_READ, "cancel")
        selector.register(process.stdin, selectors.EVENT_WRITE, "input")
        selector.register(process.stdout, selectors.EVENT_READ, "output")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise JudgeTransportError("worker service deadline expired")
            for key, _events in selector.select(min(remaining, 0.1)):
                if key.data == "cancel":
                    # Any caller EOF or additional byte cancels this one-call
                    # channel. No persistent session or second request exists.
                    channel.recv(1)
                    raise JudgeTransportError("judge caller closed or extended the active request")
                if key.data == "input":
                    try:
                        written = os.write(process.stdin.fileno(), pending)
                    except BrokenPipeError as exc:
                        raise JudgeTransportError("worker service died before accepting its request") from exc
                    pending = pending[written:]
                    if not pending:
                        selector.unregister(process.stdin)
                        process.stdin.close()
                else:
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        selector.unregister(process.stdout)
                        if process.poll() is not None:
                            return bytes(output)
                    else:
                        output.extend(chunk)
                        if len(output) > MAX_FRAME_BYTES + 4:
                            raise JudgeContractError("worker service exceeded its output bound")
            # Drain through EOF; a descendant retaining stdout is itself
            # bounded by the outer deadline and cgroup termination.
            if process.poll() is not None and not any(key.data == "output" for key in selector.get_map().values()):
                return bytes(output)


def run_worker(request: dict[str, Any], channel: socket.socket, profile: WorkerProfile) -> dict[str, Any]:
    startup_deadline = time.monotonic() + STARTUP_DEADLINE_SECONDS
    snapshot = materialize_snapshot(request, profile, channel, startup_deadline)
    process: subprocess.Popen[bytes] | None = None
    unit = f"elspeth-judge-{request['request_id']}.service"
    response: bytes | None = None
    error: Exception | None = None
    try:
        # Revalidate the protected image at every call, before independent
        # system-manager startup; never rely on a worker identity message.
        if validate_artifact_manifest(profile.root_directory) != profile.artifact_id:
            raise JudgeConfigurationError("worker image changed after admission")
        _check_startup(channel, startup_deadline)
        process = subprocess.Popen(
            service_command(request, profile, snapshot),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd="/",
            env=dict(_SERVICE_ENV),
            close_fds=True,
            start_new_session=True,
        )
        response = _exchange_process(process, channel, request)
    except Exception as exc:
        error = exc
    finally:
        try:
            stop_unit(unit)
        except Exception as exc:
            error = exc
        launcher_reaped = False
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                launcher_reaped = True
            except Exception as exc:
                error = exc
            finally:
                if process.stdin is not None:
                    process.stdin.close()
                if process.stdout is not None:
                    process.stdout.close()
        try:
            ensure_unit_dead(unit, launcher_reaped=launcher_reaped)
        except Exception as exc:
            error = exc
        remove_snapshot(snapshot)
    if error is not None:
        if isinstance(error, JudgeConfigurationError):
            return response_envelope(request, error="configuration")
        if isinstance(error, JudgeContractError):
            return response_envelope(request, error="contract")
        return response_envelope(request, error="transport")
    if process is None or process.returncode != 0 or response is None or len(response) < 4:
        return response_envelope(request, error="transport")
    size = struct.unpack("!I", response[:4])[0]
    if not 0 < size <= MAX_FRAME_BYTES or len(response) != size + 4:
        return response_envelope(request, error="contract")
    try:
        reply = decode_json(response[4:])
        parse_observation(reply, request)
        return reply
    except JudgeConfigurationError:
        return response_envelope(request, error="configuration")
    except JudgeTransportError:
        return response_envelope(request, error="transport")
    except JudgeContractError:
        return response_envelope(request, error="contract")


def serve_connection(channel: socket.socket, admission: SupervisorAdmission, profiles: dict[str, WorkerProfile]) -> None:
    pid, uid, _gid = struct.unpack("3i", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
    if pid <= 0 or uid not in admission.allowed_client_uids:
        return
    request = validate_request(receive_frame(channel, deadline_seconds=5))
    try:
        reply = run_worker(request, channel, profiles[request["transport"]])
    except JudgeConfigurationError:
        reply = response_envelope(request, error="configuration")
    except JudgeContractError:
        reply = response_envelope(request, error="contract")
    except Exception:
        reply = response_envelope(request, error="transport")
    channel.settimeout(5)
    channel.sendall(frame_bytes(reply))


def main() -> int:
    try:
        # The caller independently validates the unmounted complete image and
        # actual kernel process root. Inside this service image only explicit
        # fixed data mounts exist; never recursively self-bind its artifact.
        if os.getuid() != 0 or os.geteuid() != 0 or not sys.flags.isolated or not sys.dont_write_bytecode:
            raise JudgeConfigurationError("supervisor startup does not match the installed image profile")
        validate_protected_path(ADMISSION_PATH)
        admission = parse_admission_data(decode_json(ADMISSION_PATH.read_bytes()))
        validate_protected_path(Path("/runtime-manifest.json"))
        if hashlib.sha256(Path("/runtime-manifest.json").read_bytes()).hexdigest() != admission.artifact_id:
            raise JudgeConfigurationError("supervisor image manifest does not match external admission")
        profiles = load_profiles(admission)
        validate_protected_path(SUPERVISOR_SOCKET.parent, directory=True)
        if SUPERVISOR_SOCKET.exists():
            raise JudgeConfigurationError("judge supervisor socket is already present")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(SUPERVISOR_SOCKET))
            SUPERVISOR_SOCKET.chmod(0o666)
            listener.listen(8)
            try:
                while True:
                    channel, _address = listener.accept()
                    with channel:
                        try:
                            serve_connection(channel, admission, profiles)
                        except Exception:
                            # Never log requests, raw errors or provider output.
                            continue
            finally:
                SUPERVISOR_SOCKET.unlink()
    except Exception:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
