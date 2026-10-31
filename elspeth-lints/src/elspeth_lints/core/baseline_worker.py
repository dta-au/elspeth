"""One isolated maintenance request; mutable capture/tests run only in children."""

from __future__ import annotations

import importlib.util
import os
import selectors
import stat
import struct
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, NoReturn

from elspeth_lints.core import baseline_rpc as rpc
from elspeth_lints.core.judge import JudgeConfigurationError, JudgeContractError, JudgeTransportError
from elspeth_lints.core.judge_rpc import integer, object_fields
from elspeth_lints.core.operator_runtime import validate_protected_path

SNAPSHOT = Path("/snapshot")
PROFILE_PATH = Path("/run/elspeth-baseline-worker.json")
INTERPRETER = "/usr/bin/python3.13"
JUNIT_PATH = Path("/tmp/baseline-confirmation.xml")
_CHILD_ENV = {
    "PATH": "/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "HOME": "/tmp/baseline-home",
    "TMPDIR": "/tmp",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHON_DOTENV_DISABLED": "1",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    "ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE": "shape-only-when-key-missing",
}


def admit_worker(request: dict[str, Any]) -> None:
    if sys.version_info[:2] != (3, 13) or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise JudgeConfigurationError("maintenance requires the isolated canonical Python 3.13 profile")
    validate_protected_path(PROFILE_PATH)
    profile = object_fields(
        rpc.decode_json(PROFILE_PATH.read_bytes()),
        frozenset({"version", "profile", "worker_uid", "worker_gid", "request_id", "request_sha256", "snapshot_sha256"}),
    )
    if type(profile["version"]) is not int or profile["version"] != 1 or profile["profile"] != rpc.PROFILE:
        raise JudgeConfigurationError("maintenance worker profile is unavailable")
    uid = integer(profile["worker_uid"], minimum=1, maximum=2**31 - 1)
    gid = integer(profile["worker_gid"], minimum=1, maximum=2**31 - 1)
    if (os.getuid(), os.geteuid(), os.getgid(), os.getegid()) != (uid, uid, gid, gid):
        raise JudgeConfigurationError("maintenance worker principal is not admitted")
    for field in ("request_id", "request_sha256", "snapshot_sha256"):
        if profile[field] != request[field]:
            raise JudgeContractError("maintenance worker profile does not match its request")
    metadata = SNAPSHOT.stat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o222:
        raise JudgeConfigurationError("maintenance snapshot is not immutable")
    if os.readlink(SNAPSHOT / ".venv/bin/python") != INTERPRETER:
        raise JudgeConfigurationError("maintenance interpreter link does not match its fixed profile")


def run_child(mode: str, *, deadline: float) -> bytes:
    if mode not in {"capture", "confirm"}:
        raise JudgeConfigurationError("unsupported maintenance subprocess action")
    process = subprocess.Popen(
        [INTERPRETER, "-I", "-B", "-m", "elspeth_lints.core.baseline_worker", mode],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=SNAPSHOT,
        env=dict(_CHILD_ENV),
        close_fds=True,
    )
    output = bytearray()
    try:
        if process.stdout is None:
            raise JudgeTransportError("maintenance subprocess output is unavailable")
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise JudgeTransportError("maintenance subprocess deadline expired")
                if selector.select(min(remaining, 0.1)):
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > rpc.MAX_FRAME_BYTES:
                        raise JudgeContractError("maintenance subprocess output exceeds its bound")
        if process.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
            raise JudgeTransportError("maintenance subprocess did not complete successfully")
        return bytes(output)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()


def terminal_result(raw: bytes) -> dict[str, Any]:
    if len(raw) > 1048576 or b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise JudgeContractError("maintenance test report exceeds its safe bounds")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise JudgeContractError("maintenance test report is malformed") from exc
    if root.tag != "testsuites" or len(root) != 1 or root[0].tag != "testsuite":
        raise JudgeContractError("maintenance test report must contain exactly one suite")
    suite = root[0]
    if any(suite.attrib.get(key) != value for key, value in {"tests": "1", "failures": "0", "errors": "0", "skipped": "0"}.items()):
        raise JudgeContractError("maintenance confirmation did not pass exactly one test")
    cases = list(suite.iter("testcase"))
    if (
        len(cases) != 1
        or cases[0].attrib.get("name") != "test_baseline_capture_is_self_consistent"
        or cases[0].attrib.get("classname") != "tests.unit.elspeth_lints.test_allowlist_loader_unification"
    ):
        raise JudgeContractError("maintenance confirmation report names the wrong test")
    if any(node.tag in {"failure", "error", "skipped"} for node in suite.iter()):
        raise JudgeContractError("maintenance confirmation was failed, skipped or xfailed")
    return {"nodeid": rpc.TEST_NODE, "passed": 1, "failed": 0, "errors": 0, "skipped": 0, "xfailed": 0, "xpassed": 0}


def execute(request: dict[str, Any]) -> dict[str, Any]:
    admit_worker(request)
    Path(_CHILD_ENV["HOME"]).mkdir(mode=0o700, exist_ok=True)
    deadline = time.monotonic() + rpc.MAINTENANCE_SECONDS - 15
    captured = rpc.validate_baseline(rpc.decode_json(run_child("capture", deadline=deadline)))
    if captured != request["expected_baseline"]:
        raise JudgeContractError("maintenance capture disagrees with the independently derived baseline")
    run_child("confirm", deadline=deadline)
    fd = os.open(JUNIT_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1048576:
            raise JudgeContractError("maintenance test report is not a bounded regular file")
        raw = os.read(fd, 1048577)
    finally:
        os.close(fd)
    terminal = terminal_result(raw)
    result = rpc.response(request, baseline=captured, terminal=terminal)
    rpc.validate_response(result, request)
    return result


def _capture_child() -> int:
    if sys.version_info[:2] != (3, 13) or Path.cwd() != SNAPSHOT:
        return 2
    spec = importlib.util.spec_from_file_location("elspeth_frozen_capture", SNAPSHOT / rpc.CAPTURE_MODULE)
    if spec is None or spec.loader is None:
        return 2
    module = importlib.util.module_from_spec(spec)
    sys.modules["elspeth_frozen_capture"] = module
    spec.loader.exec_module(module)
    # Deliberately mutable code, exclusively inside this restricted worker.
    with tempfile.TemporaryDirectory(prefix="elspeth-baseline-empty-allowlist-") as empty_allowlist:
        captured = module.capture_all(allowlist_dir=Path(empty_allowlist))
    sys.stdout.buffer.write(rpc.canonical_json(rpc.validate_baseline(captured)))
    return 0


def _confirm_child() -> int:
    if sys.version_info[:2] != (3, 13) or Path.cwd() != SNAPSHOT:
        return 2
    import pytest
    from _pytest.outcomes import XFailed

    class RejectXfail:
        Exception = XFailed

        def __call__(self, reason: str = "") -> NoReturn:
            raise AssertionError("maintenance confirmation attempted xfail")

    class ConfirmationPlugin:
        @pytest.hookimpl(trylast=True)
        def pytest_configure(self, config: pytest.Config) -> None:
            # --runxfail normally replaces direct pytest.xfail with a no-op.
            # Existing baseline drift uses that direct call, so reject it.
            pytest.MonkeyPatch().setattr(pytest, "xfail", RejectXfail())

    sys.path[:0] = [str(SNAPSHOT / "src"), str(SNAPSHOT / "elspeth-lints/src"), str(SNAPSHOT)]
    return int(
        pytest.main(
            [
                "-p",
                "no:dotenv",
                "--runxfail",
                "-o",
                "addopts=",
                "-o",
                "cache_dir=" + str(JUNIT_PATH.parent / "baseline-pytest-cache"),
                "--rootdir=" + str(SNAPSHOT),
                "--confcutdir=" + str(SNAPSHOT),
                "--junitxml=" + str(JUNIT_PATH),
                "-q",
                rpc.TEST_NODE,
            ],
            plugins=[ConfirmationPlugin()],
        )
    )


def main() -> int:
    if sys.argv[1:] == ["capture"]:
        return _capture_child()
    if sys.argv[1:] == ["confirm"]:
        return _confirm_child()
    if sys.argv[1:]:
        return 2
    request: dict[str, Any] | None = None
    try:
        header = sys.stdin.buffer.read(4)
        if len(header) != 4:
            return 2
        size = struct.unpack("!I", header)[0]
        if not 0 < size <= rpc.MAX_FRAME_BYTES:
            return 2
        raw = sys.stdin.buffer.read(size)
        if len(raw) != size or sys.stdin.buffer.read(1):
            return 2
        request = rpc.validate_request(rpc.decode_json(raw))
        result = execute(request)
    except JudgeConfigurationError:
        if request is None:
            return 2
        result = rpc.response(request, error="configuration")
    except JudgeContractError:
        if request is None:
            return 2
        result = rpc.response(request, error="contract")
    except Exception:
        if request is None:
            return 2
        result = rpc.response(request, error="transport")
    sys.stdout.buffer.write(rpc.frame_bytes(result))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
