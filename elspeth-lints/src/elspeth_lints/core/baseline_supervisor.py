"""Independent Ubuntu maintenance supervisor; never a signer descendant."""

from __future__ import annotations

import hashlib
import os
import selectors
import shutil
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from elspeth_lints.core import baseline_rpc as rpc
from elspeth_lints.core.judge import JudgeConfigurationError, JudgeContractError, JudgeTransportError
from elspeth_lints.core.judge_supervisor import _check_startup, ensure_unit_dead, stop_unit
from elspeth_lints.core.operator_runtime import validate_artifact_manifest, validate_protected_path

SNAPSHOT_DIRECTORY = Path("/run/elspeth-baseline/snapshots")
_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}


def _write(path: Path, raw: bytes, mode: int = 0o444) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        position = 0
        while position < len(raw):
            position += os.write(fd, raw[position:])
        os.fsync(fd)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def remove_snapshot(directory: Path) -> None:
    if directory.parent != SNAPSHOT_DIRECTORY:
        raise JudgeConfigurationError("maintenance cleanup escaped its private storage")
    for parent, _dirs, _files in os.walk(directory, topdown=False, followlinks=False):
        Path(parent).chmod(0o700)
    shutil.rmtree(directory)


def materialize(request: dict[str, Any], admission: rpc.Admission, channel: socket.socket, deadline: float) -> Path:
    rpc.validate_request(request)
    _check_startup(channel, deadline)
    directory: Path = SNAPSHOT_DIRECTORY / request["request_id"]
    directory.mkdir(mode=0o700)
    repo = directory / "repo"
    repo.mkdir(mode=0o700)
    try:
        for record in request["files"]:
            _check_startup(channel, deadline)
            if record["mode"] == "120000":
                continue
            path = repo / record["path"]
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            raw = rpc.file_bytes(record)
            # The old bytes remain bound in the request. Only this private
            # immutable copy receives the independently expected fixture.
            if record["path"] == rpc.BASELINE_PATH:
                raw = rpc.canonical_json(request["expected_baseline"]) + b"\n"
            _write(path, raw, 0o555 if record["mode"] == "100755" else 0o444)
        for record in request["files"]:
            _check_startup(channel, deadline)
            if record["mode"] == "120000":
                path = repo / record["path"]
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                path.symlink_to(rpc.file_bytes(record).decode("utf-8"))
        (repo / ".venv/bin").mkdir(mode=0o700, parents=True)
        (repo / ".venv/bin/python").symlink_to("/usr/bin/python3.13")
        profile = {
            "version": 1,
            "profile": rpc.PROFILE,
            "worker_uid": admission.worker_uid,
            "worker_gid": admission.worker_gid,
            **{key: request[key] for key in ("request_id", "request_sha256", "snapshot_sha256")},
        }
        _write(directory / "profile.json", rpc.canonical_json(profile))
        for parent, _dirs, _files in os.walk(directory, topdown=False, followlinks=False):
            _check_startup(channel, deadline)
            Path(parent).chmod(0o555)
        return directory
    except BaseException:
        remove_snapshot(directory)
        raise


def service_command(request: dict[str, Any], admission: rpc.Admission, snapshot: Path) -> list[str]:
    unit = f"elspeth-baseline-{request['request_id']}.service"
    properties = [
        f"User={admission.worker_uid}",
        f"Group={admission.worker_gid}",
        "SupplementaryGroups=",
        f"RootDirectory={rpc.WORKER_IMAGE}",
        "MountAPIVFS=yes",
        "WorkingDirectory=/snapshot",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "PrivateNetwork=yes",
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
        "RestrictAddressFamilies=AF_UNIX",
        "IPAddressDeny=any",
        "UMask=0077",
        "KillMode=control-group",
        "SendSIGKILL=yes",
        "TimeoutStopSec=3",
        f"RuntimeMaxSec={rpc.MAINTENANCE_SECONDS}",
        "MemoryMax=8G",
        "CPUQuota=200%",
        "TasksMax=128",
        "LimitCORE=0",
        "LimitNOFILE=256",
        "TemporaryFileSystem=/tmp:rw,nosuid,nodev,size=1G /run:rw,nosuid,nodev,size=16M",
        "ReadWritePaths=/tmp /run",
        f"BindReadOnlyPaths={snapshot / 'repo'}:/snapshot",
        f"BindReadOnlyPaths={snapshot / 'profile.json'}:/run/elspeth-baseline-worker.json",
    ]
    command = ["/usr/bin/systemd-run", "--quiet", "--pipe", "--wait", "--collect", "--service-type=exec", f"--unit={unit}"]
    for value in properties:
        command.extend(("--property", value))
    command.extend(
        (
            "--setenv=PATH=/usr/bin:/bin",
            "--setenv=LANG=C.UTF-8",
            "--setenv=HOME=/tmp/baseline-home",
            "--setenv=PYTHONDONTWRITEBYTECODE=1",
            "/usr/bin/python3.13",
            "-I",
            "-B",
            "-m",
            "elspeth_lints.core.baseline_worker",
        )
    )
    return command


def exchange_process(process: subprocess.Popen[bytes], channel: socket.socket, request: dict[str, Any]) -> bytes:
    if process.stdin is None or process.stdout is None:
        raise JudgeTransportError("maintenance worker pipes are unavailable")
    pending = memoryview(rpc.frame_bytes(request))
    output = bytearray()
    deadline = time.monotonic() + rpc.MAINTENANCE_SECONDS + 5
    os.set_blocking(process.stdin.fileno(), False)
    os.set_blocking(process.stdout.fileno(), False)
    with selectors.DefaultSelector() as selector:
        selector.register(channel, selectors.EVENT_READ, "cancel")
        selector.register(process.stdin, selectors.EVENT_WRITE, "input")
        selector.register(process.stdout, selectors.EVENT_READ, "output")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise JudgeTransportError("maintenance worker deadline expired")
            for key, _events in selector.select(min(remaining, 0.1)):
                if key.data == "cancel":
                    channel.recv(1)
                    raise JudgeTransportError("maintenance caller closed or extended the request")
                if key.data == "input":
                    try:
                        count = os.write(process.stdin.fileno(), pending)
                    except BrokenPipeError as exc:
                        raise JudgeTransportError("maintenance worker died before accepting input") from exc
                    pending = pending[count:]
                    if not pending:
                        selector.unregister(process.stdin)
                        process.stdin.close()
                else:
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        selector.unregister(process.stdout)
                    else:
                        output.extend(chunk)
                        if len(output) > rpc.MAX_FRAME_BYTES + 4:
                            raise JudgeContractError("maintenance worker output exceeds its bound")
            if process.poll() is not None and not any(key.data == "output" for key in selector.get_map().values()):
                return bytes(output)


def run_worker(request: dict[str, Any], admission: rpc.Admission, channel: socket.socket) -> dict[str, Any]:
    deadline = time.monotonic() + 60
    snapshot = materialize(request, admission, channel, deadline)
    process: subprocess.Popen[bytes] | None = None
    output: bytes | None = None
    error: Exception | None = None
    unit = f"elspeth-baseline-{request['request_id']}.service"
    try:
        if validate_artifact_manifest(rpc.WORKER_IMAGE) != admission.worker_artifact_id:
            raise JudgeConfigurationError("maintenance worker image changed")
        _check_startup(channel, deadline)
        process = subprocess.Popen(
            service_command(request, admission, snapshot),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd="/",
            env=dict(_ENV),
            close_fds=True,
            start_new_session=True,
        )
        output = exchange_process(process, channel, request)
    except Exception as exc:
        error = exc
    finally:
        try:
            stop_unit(unit)
        except Exception as exc:
            error = exc
        reaped = False
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                reaped = True
            except Exception as exc:
                error = exc
            finally:
                if process.stdin is not None:
                    process.stdin.close()
                if process.stdout is not None:
                    process.stdout.close()
        try:
            ensure_unit_dead(unit, launcher_reaped=reaped)
        except Exception as exc:
            error = exc
        remove_snapshot(snapshot)
    if error is not None:
        category = (
            "configuration"
            if isinstance(error, JudgeConfigurationError)
            else "contract"
            if isinstance(error, JudgeContractError)
            else "transport"
        )
        return rpc.response(request, error=category)
    if process is None or process.returncode != 0 or output is None or len(output) < 4:
        return rpc.response(request, error="transport")
    size = struct.unpack("!I", output[:4])[0]
    if not 0 < size <= rpc.MAX_FRAME_BYTES or len(output) != size + 4:
        return rpc.response(request, error="contract")
    try:
        result = rpc.decode_json(output[4:])
        rpc.validate_response(result, request)
        return result
    except JudgeConfigurationError:
        return rpc.response(request, error="configuration")
    except JudgeContractError:
        return rpc.response(request, error="contract")
    except Exception:
        return rpc.response(request, error="transport")


def serve_connection(channel: socket.socket, admission: rpc.Admission) -> None:
    pid, uid, _gid = struct.unpack("3i", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
    if pid <= 0 or uid not in admission.allowed_uids:
        return
    request = rpc.validate_request(rpc.receive_frame(channel, deadline_seconds=15))
    try:
        result = run_worker(request, admission, channel)
    except JudgeConfigurationError:
        result = rpc.response(request, error="configuration")
    except JudgeContractError:
        result = rpc.response(request, error="contract")
    except Exception:
        result = rpc.response(request, error="transport")
    channel.settimeout(5)
    channel.sendall(rpc.frame_bytes(result))


def main() -> int:
    try:
        if (
            os.getuid() != 0
            or os.geteuid() != 0
            or sys.version_info[:2] != (3, 13)
            or not sys.flags.isolated
            or not sys.dont_write_bytecode
        ):
            raise JudgeConfigurationError("maintenance supervisor startup does not match its fixed profile")
        validate_protected_path(rpc.ADMISSION_PATH)
        admission = rpc.parse_admission(rpc.decode_json(rpc.ADMISSION_PATH.read_bytes()))
        validate_protected_path(Path("/runtime-manifest.json"))
        if hashlib.sha256(Path("/runtime-manifest.json").read_bytes()).hexdigest() != admission.artifact_id:
            raise JudgeConfigurationError("maintenance supervisor image manifest changed")
        if validate_artifact_manifest(rpc.WORKER_IMAGE) != admission.worker_artifact_id:
            raise JudgeConfigurationError("maintenance worker image does not match admission")
        for path in (Path("/usr/bin/systemd-run"), Path("/usr/bin/systemctl"), rpc.WORKER_IMAGE / "usr/bin/python3.13"):
            validate_protected_path(path)
        validate_protected_path(SNAPSHOT_DIRECTORY, directory=True)
        validate_protected_path(rpc.SUPERVISOR_SOCKET.parent, directory=True)
        if rpc.SUPERVISOR_SOCKET.exists():
            raise JudgeConfigurationError("maintenance socket is already present")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(rpc.SUPERVISOR_SOCKET))
            rpc.SUPERVISOR_SOCKET.chmod(0o666)
            listener.listen(8)
            try:
                while True:
                    channel, _address = listener.accept()
                    with channel:
                        try:
                            serve_connection(channel, admission)
                        except Exception:
                            continue
            finally:
                rpc.SUPERVISOR_SOCKET.unlink()
    except Exception:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
