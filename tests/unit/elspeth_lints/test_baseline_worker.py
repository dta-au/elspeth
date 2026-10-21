"""Small real capture/pytest controls and fake supervisor lifecycle controls."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from elspeth_lints.core import baseline_rpc as rpc
from elspeth_lints.core import baseline_supervisor as supervisor
from elspeth_lints.core import baseline_worker as worker
from elspeth_lints.core.judge import JudgeConfigurationError, JudgeContractError, JudgeTransportError

EXPECTED = {"demo.rule": [{"file_path": "demo.py", "fingerprint": "abc"}]}


def _request() -> dict[str, object]:
    files = [
        {"path": path, "mode": "100644", "sha256": hashlib.sha256(raw).hexdigest(), "content": base64.b64encode(raw).decode()}
        for path, raw in [(rpc.BASELINE_PATH, b"{}"), (rpc.CAPTURE_MODULE, b"# capture\n")]
    ]
    files.sort(key=lambda item: item["path"])
    return rpc.make_request(files, hashlib.sha256(rpc.canonical_json(files)).hexdigest(), EXPECTED)


def _admission() -> rpc.Admission:
    return rpc.Admission("a" * 64, "b" * 64, "c" * 64, (40000,), 40001, 40001, "d" * 64)


def _fixture(root: Path, *, drift: bool = False) -> None:
    for path in ("tests", "tests/unit", "tests/unit/elspeth_lints", "tests/unit/elspeth_lints/fixtures"):
        (root / path).mkdir(parents=True, exist_ok=True)
        if not path.endswith("fixtures"):
            (root / path / "__init__.py").write_text("")
    (root / rpc.BASELINE_PATH).write_text(json.dumps({"demo.rule": []} if drift else EXPECTED))
    source = """import json
from pathlib import Path
import pytest

def capture_all(*, allowlist_dir=None):
    assert allowlist_dir is not None
    assert allowlist_dir.is_dir() and not list(allowlist_dir.iterdir())
    return {"demo.rule": [{"file_path": "demo.py", "fingerprint": "abc"}]}

def test_baseline_capture_is_self_consistent(tmp_path):
    root = Path(__file__).resolve().parents[3]
    old = json.loads((root / "tests/unit/elspeth_lints/fixtures/fingerprint_baseline.json").read_text())
    empty = tmp_path / "empty"
    empty.mkdir()
    if capture_all(allowlist_dir=empty) != old:
        pytest.xfail("fingerprint drift")
"""
    (root / rpc.CAPTURE_MODULE).write_text(source)


def _run_fixture(root: Path, mode: str) -> subprocess.CompletedProcess[bytes]:
    trusted_source = Path(worker.__file__).resolve().parents[2]
    harness = "\n".join(
        [
            "import os,sys",
            "from pathlib import Path",
            f"sys.path.insert(0, {str(trusted_source)!r})",
            "from elspeth_lints.core import baseline_worker as w",
            f"w.SNAPSHOT=Path({str(root)!r})",
            f"w.JUNIT_PATH=Path({str(root / 'confirmation.xml')!r})",
            "os.chdir(w.SNAPSHOT)",
            "raise SystemExit(w._capture_child() if sys.argv[1]=='capture' else w._confirm_child())",
        ]
    )
    # Test-only launch of the trusted harness against synthetic data. There is
    # no admission flag in the production protocol and no real credentials.
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "HOME": str(root),
        "TMPDIR": str(root),
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.run(
        [sys.executable, "-I", "-B", "-c", harness, mode], cwd=root, env=environment, capture_output=True, timeout=30, check=False
    )


def test_real_small_capture_and_consistency_pass_without_git(tmp_path: Path) -> None:
    _fixture(tmp_path)
    assert not (tmp_path / ".git").exists()
    captured = _run_fixture(tmp_path, "capture")
    assert captured.returncode == 0, captured.stderr.decode()
    assert rpc.decode_json(captured.stdout) == EXPECTED
    confirmation = _run_fixture(tmp_path, "confirm")
    assert confirmation.returncode == 0, confirmation.stdout.decode() + confirmation.stderr.decode()
    assert worker.terminal_result((tmp_path / "confirmation.xml").read_bytes())["passed"] == 1


def test_real_xfail_drift_cannot_be_reported_as_success_under_runxfail(tmp_path: Path) -> None:
    _fixture(tmp_path, drift=True)
    captured = _run_fixture(tmp_path, "capture")
    assert captured.returncode == 0
    assert rpc.decode_json(captured.stdout) == EXPECTED
    confirmation = _run_fixture(tmp_path, "confirm")
    assert confirmation.returncode != 0
    with pytest.raises(JudgeContractError):
        worker.terminal_result((tmp_path / "confirmation.xml").read_bytes())


def test_lying_capture_is_rejected_before_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "admit_worker", lambda _request: None)
    monkeypatch.setitem(worker._CHILD_ENV, "HOME", str(tmp_path / "home"))
    run = Mock(return_value=rpc.canonical_json({"demo.rule": []}))
    monkeypatch.setattr(worker, "run_child", run)
    with pytest.raises(JudgeContractError, match="independently"):
        worker.execute(_request())
    assert run.call_count == 1
    assert run.call_args.args[0] == "capture"


def test_default_worker_refuses_without_isolated_profile() -> None:
    with pytest.raises((JudgeConfigurationError, RuntimeError)):
        worker.admit_worker(_request())


@pytest.mark.parametrize("replacement", [b'<skipped type="pytest.xfail"/>', b"<skipped/>", b"<error/>", b"<failure/>"])
def test_terminal_report_rejects_skips_xfails_and_failures(replacement: bytes) -> None:
    raw = (
        b'<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0"><testcase classname="tests.unit.elspeth_lints.test_allowlist_loader_unification" name="test_baseline_capture_is_self_consistent">'
        + replacement
        + b"</testcase></testsuite></testsuites>"
    )
    with pytest.raises(JudgeContractError):
        worker.terminal_result(raw)


def test_materialization_changes_only_private_baseline_and_adds_fixed_interpreter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supervisor, "SNAPSHOT_DIRECTORY", tmp_path)
    monkeypatch.setattr(supervisor, "_check_startup", lambda *_args: None)
    request = _request()
    directory = supervisor.materialize(request, _admission(), Mock(spec=socket.socket), 0)
    try:
        assert json.loads((directory / "repo" / rpc.BASELINE_PATH).read_text()) == EXPECTED
        old_record = next(item for item in request["files"] if item["path"] == rpc.BASELINE_PATH)
        assert rpc.file_bytes(old_record) == b"{}"
        assert os.readlink(directory / "repo/.venv/bin/python") == "/usr/bin/python3.13"
        assert (directory / "repo" / rpc.CAPTURE_MODULE).read_bytes() == b"# capture\n"
        assert (directory / "repo").stat().st_mode & 0o222 == 0
    finally:
        supervisor.remove_snapshot(directory)


def test_maintenance_profile_has_no_network_credentials_or_candidate_mounts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARBITRARY_OLD_KEY", "synthetic-private-value")
    command = supervisor.service_command(_request(), _admission(), Path("/run/elspeth-baseline/snapshots/fixture"))
    joined = " ".join(command)
    assert "PrivateNetwork=yes" in command
    assert "RestrictAddressFamilies=AF_UNIX" in command
    assert "KillMode=control-group" in command
    assert "synthetic-private-value" not in joined
    assert "/provider" not in joined
    assert "ReadWritePaths=/tmp /run" in command
    assert str(rpc.WORKER_IMAGE) in joined


def test_worker_terminal_is_refused_unless_supervisor_confirms_death(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request()
    events: list[str] = []
    process = Mock(spec=subprocess.Popen)
    process.stdin = Mock()
    process.stdout = Mock()
    process.poll.return_value = 0
    process.returncode = 0
    process.wait.side_effect = lambda **_kwargs: events.append("reaped")
    terminal = {"nodeid": rpc.TEST_NODE, "passed": 1, "failed": 0, "errors": 0, "skipped": 0, "xfailed": 0, "xpassed": 0}
    reply = rpc.response(request, baseline=EXPECTED, terminal=terminal)
    monkeypatch.setattr(supervisor, "materialize", lambda *_args: Path("/run/elspeth-baseline/snapshots/fixture"))
    monkeypatch.setattr(supervisor, "_check_startup", lambda *_args: None)
    monkeypatch.setattr(supervisor, "validate_artifact_manifest", lambda _path: "d" * 64)
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(supervisor, "exchange_process", lambda *_args: rpc.frame_bytes(reply))
    monkeypatch.setattr(supervisor, "stop_unit", lambda _unit: events.append("stopped"))
    monkeypatch.setattr(supervisor, "ensure_unit_dead", Mock(side_effect=JudgeTransportError("not dead")))
    monkeypatch.setattr(supervisor, "remove_snapshot", lambda _path: events.append("removed"))
    result = supervisor.run_worker(request, _admission(), Mock(spec=socket.socket))
    assert result["status"] == "error"
    assert events == ["stopped", "reaped", "removed"]
