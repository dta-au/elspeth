"""Public canonical entrypoints own the real driver's ordinary process group."""

from __future__ import annotations

import ctypes
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path

import pytest
from scripts import ci_proof_progress as proof

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts/full-suite-gate.sh"
INVENTORY_ENV = "_ELSPETH_GATE_TEST_SIBLING_ROOT"
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux supervisor/group ownership")

PROBE = """import json, os, signal, subprocess, sys, time
from pathlib import Path

def identity():
    return dict(pid=os.getpid(), pgid=os.getpgrp(),
                start=Path('/proc/self/stat').read_text().rpartition(')')[2].split()[19])

def test_probe():
    mode = os.environ['GATE_MODE']
    cache = Path('.pytest_cache')
    if mode == 'sibling':
        (cache / 'sibling-ready').write_text('ready')
        time.sleep(60)
    if mode in ('abandon', 'hang', 'crash'):
        code = ("import json,os,signal,time; from pathlib import Path; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "row=dict(pid=os.getpid(),pgid=os.getpgrp(),start=Path('/proc/self/stat').read_text().rpartition(')')[2].split()[19]); "
                "Path('.pytest_cache/family-child.json').write_text(json.dumps(row)); time.sleep(60)")
        subprocess.Popen([sys.executable, '-c', code])
        (cache / 'family-parent.json').write_text(json.dumps(identity()))
        deadline = time.monotonic() + 5
        while not (cache / 'family-child.json').exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        if mode == 'crash':
            os._exit(7)
        if mode == 'hang':
            time.sleep(60)
    assert mode != 'fail', 'controlled child assertion'
"""

SITE = """import builtins, os, sys, time
from pathlib import Path
if Path(sys.argv[0]).name == 'ci_proof_progress.py':
    original_print = builtins.print
    def controlled_print(*args, **kwargs):
        if args and str(args[0]).startswith('proof result='):
            mode = os.environ.get('GATE_MODE')
            if mode == 'publication-fail':
                raise BrokenPipeError('controlled supervisor final print failure')
            if mode == 'publication-pause':
                release = Path('.pytest_cache/release-publication')
                while not release.exists():
                    time.sleep(0.01)
        return original_print(*args, **kwargs)
    builtins.print = controlled_print
"""

PROVENANCE = """import os, time
from pathlib import Path
if os.environ.get('GATE_CAPACITY_BARRIER') == '1':
    cache = Path('.pytest_cache')
    (cache / 'capacity-ready').write_text('ready')
    deadline = time.monotonic() + 10
    while not (cache / 'capacity-release').exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('capacity fixture barrier timed out')
        time.sleep(0.01)
"""

PROVENANCE_LATE_FAILURE = """import atexit, os, sys

def fail_after_path_output():
    sys.stdout.flush()
    sys.stderr.write('controlled provenance shutdown failure exit=79\\n')
    sys.stderr.flush()
    os._exit(79)

atexit.register(fail_after_path_output)
"""

LINT_CLI = """import json, os, signal, sys
from pathlib import Path
cache = Path('.pytest_cache')
spec = json.loads((cache / 'lint-result.json').read_text())
(cache / 'lint-argv.json').write_text(json.dumps(sys.argv[1:]))
sys.stderr.write('controlled lint stderr\\n')
if spec['payload'] is None:
    Path(os.readlink('/proc/self/fd/1')).unlink()
else:
    sys.stdout.write(spec['payload'])
    sys.stdout.flush()
if spec['status'] < 0:
    os.kill(os.getpid(), -spec['status'])
raise SystemExit(spec['status'])
"""


def _lint_result(root: Path, payload: str | None, status: int) -> None:
    core = root / "elspeth-lints/src/elspeth_lints/core"
    core.mkdir()
    (core / "__init__.py").write_text("")
    (core / "cli.py").write_text(LINT_CLI)
    (root / ".pytest_cache/lint-result.json").write_text(json.dumps({"payload": payload, "status": status}))


def _lint_finding(*, severity: str = "error", rule_id: str = "trust_tier.tier_model") -> str:
    return json.dumps(
        [
            {
                "file_path": "source.py",
                "rule_id": rule_id,
                "message": "controlled finding",
                "line": 1,
                "column": 0,
                "severity": severity,
                "fingerprint": "controlled-fingerprint",
            }
        ]
    )


def _inventory(root: Path) -> Path:
    return root / ".pytest_cache/sibling-inventory"


def _environment(root: Path, mode: str, *, inventory: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "ELSPETH_GATE_LOG_DIR",
        "GATE_CAPACITY_BARRIER",
        "_ELSPETH_GATE_LAUNCHER_IDENTITY",
        INVENTORY_ENV,
    ):
        env.pop(key, None)
    env.update(
        GATE_MODE=mode,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        PYTHONPATH=f"{root}/src:{root}/elspeth-lints/src",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PATH=f"{root}/.pytest_cache/bin:{env['PATH']}",
    )
    env[INVENTORY_ENV] = str(_inventory(root)) if inventory is None else inventory
    return env


def _identity_alive(identity: dict[str, object]) -> bool:
    try:
        stat = Path(f"/proc/{identity['pid']}/stat").read_text()
    except FileNotFoundError:
        return False
    return stat.rpartition(")")[2].split()[19] == identity["start"]


@pytest.fixture
def project() -> Iterator[Path]:
    # Keep all generated source/data inside the project even under a bare
    # pytest invocation. No production source or supervisor is copied.
    cache = REPO_ROOT / ".pytest_cache"
    cache.mkdir(exist_ok=True)
    libc = ctypes.CDLL(None, use_errno=True)
    prior = ctypes.c_int()
    assert libc.prctl(proof.PR_GET_CHILD_SUBREAPER, ctypes.byref(prior), 0, 0, 0) == 0
    assert libc.prctl(proof.PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    try:
        with tempfile.TemporaryDirectory(prefix="gate-launcher-", dir=cache) as directory:
            root = Path(directory)
            for package in ("src/elspeth", "elspeth-lints/src/elspeth_lints"):
                path = root / package
                path.mkdir(parents=True)
                (path / "__init__.py").write_text("")
            (root / "tests").mkdir()
            (root / ".pytest_cache").mkdir()
            _inventory(root).mkdir()
            (root / "src/elspeth/__init__.py").write_text(PROVENANCE)
            (root / "tests/test_probe.py").write_text(PROBE)
            (root / "src/sitecustomize.py").write_text(SITE)
            (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\naddopts = []\n")
            (root / ".gitignore").write_text(".venv\n.pytest_cache\n.claude\n__pycache__\n")
            (root / ".venv").symlink_to(REPO_ROOT / ".venv", target_is_directory=True)
            env = _environment(root, "pass")
            subprocess.run(["git", "init", "--quiet", "--template="], cwd=root, env=env, check=True)
            subprocess.run(["git", "add", "."], cwd=root, env=env, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Launcher control",
                    "-c",
                    "user.email=control@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "fixture",
                ],
                cwd=root,
                env=env,
                check=True,
            )
            try:
                yield root
            finally:
                # Assertion outcomes happen before fallback cleanup, so the
                # fallback cannot make a failed ownership control look green.
                for run in (root / ".claude/lanes/full-suite-gates").glob("*"):
                    pidfile, statfile = run / "supervisor.pid", run / "supervisor.start-stat"
                    if pidfile.exists() and statfile.exists():
                        identity = {"pid": int(pidfile.read_text()), "start": statfile.read_text().rpartition(")")[2].split()[19]}
                        if _identity_alive(identity):
                            with suppress(ProcessLookupError):
                                os.kill(int(identity["pid"]), signal.SIGTERM)
                    ownerfile = run / "launcher.pid"
                    if ownerfile.exists():
                        owner = int(ownerfile.read_text())
                        deadline = time.monotonic() + 15
                        while time.monotonic() < deadline:
                            try:
                                waited, _status = os.waitpid(owner, os.WNOHANG)
                            except ChildProcessError:
                                break
                            if waited:
                                break
                            time.sleep(0.01)
                for path in (root / ".pytest_cache").glob("family-*.json"):
                    identity = json.loads(path.read_text())
                    if _identity_alive(identity):
                        with suppress(ProcessLookupError):
                            os.killpg(identity["pgid"], signal.SIGKILL)
    finally:
        assert libc.prctl(proof.PR_SET_CHILD_SUBREAPER, prior.value, 0, 0, 0) == 0


def _wait_file(path: Path, seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while not path.is_file() or not path.stat().st_size:
        assert time.monotonic() < deadline, f"missing receipt: {path}"
        time.sleep(0.01)


def _start(
    root: Path,
    mode: str,
    *,
    detached: bool = False,
    timeout: str = "30",
    workers: int | None = 0,
    stage: str = "pytest",
    lints_strict: bool = False,
    capacity_barrier: bool = False,
    inventory: str | None = None,
):
    command = [
        str(SCRIPT),
        "--execute",
        "--root",
        str(root),
        "--stages",
        stage,
        "--run-timeout-seconds",
        timeout,
        "--pytest-args",
        f"--timeout=0 --max-worker-restart=0 -p pytest_timeout -p xdist.plugin --basetemp={root}/.pytest_cache/pytest-temp",
    ]
    if workers is not None:
        command.extend(("--workers", str(workers)))
    if detached:
        command.append("--detach")
    if lints_strict:
        command.append("--lints-strict")
    env = _environment(root, mode, inventory=inventory)
    if capacity_barrier:
        env["GATE_CAPACITY_BARRIER"] = "1"
    process = subprocess.Popen(
        command,
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        runs = []
        while not runs:
            runs = list((root / ".claude/lanes/full-suite-gates").glob("*"))
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert len(runs) == 1
        run = runs[0]
        _wait_file(run / "launcher.pid")
        _wait_file(run / "supervisor.pid")
        if detached:
            stdout, _ = process.communicate(timeout=10)
            assert process.returncode == 0, stdout
            assert "launcher.exit" in stdout and "supervisor.exit" in stdout and "supervisor.pid" in stdout
        return process, run
    except BaseException:
        if process.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        raise


def _finish(process: subprocess.Popen[str], run: Path, detached: bool) -> int:
    if not detached:
        stdout, _ = process.communicate(timeout=45)
        (run / "launcher-output.txt").write_text(stdout)
        assert "launcher.exit" in stdout and "supervisor.exit" in stdout and "supervisor.pid" in stdout
        return process.returncode
    owner = int((run / "launcher.pid").read_text())
    deadline = time.monotonic() + 45
    while True:
        waited, status = os.waitpid(owner, os.WNOHANG)
        if waited:
            return os.waitstatus_to_exitcode(status)
        assert time.monotonic() < deadline, (run / "driver.log").read_text()
        time.sleep(0.01)


def _report(run: Path, expected: int) -> dict[str, object]:
    assert int((run / "supervisor.exit").read_text()) == expected
    assert int((run / "launcher.exit").read_text()) == expected
    report = json.loads((run / "supervisor.json").read_text())
    assert report["exit_status"] == expected and report["cleanup_complete"] is True
    pid = int((run / "supervisor.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    return report


def _family_gone(root: Path) -> None:
    identities = [json.loads(path.read_text()) for path in (root / ".pytest_cache").glob("family-*.json")]
    assert len(identities) == 2
    assert all(not _identity_alive(identity) for identity in identities)
    assert len({identity["pgid"] for identity in identities}) == 1
    with pytest.raises(ProcessLookupError):
        os.killpg(identities[0]["pgid"], 0)


@pytest.mark.parametrize("detached", (False, True))
@pytest.mark.parametrize("mode,expected", (("pass", 0), ("fail", 1)))
def test_public_foreground_and_detached_preserve_driver_and_outer_status(project, detached, mode, expected):
    process, run = _start(project, mode, detached=detached)
    assert _finish(process, run, detached) == expected
    report = _report(run, expected)
    assert report["child_returncode"] == expected
    command = json.loads((run / "supervisor.argv.json").read_text())
    assert "--detach" not in command and command.count("--driver-only") == 1
    assert command[command.index("--grace-seconds") + 1] == "10"
    assert command[command.index("--pytest-args") + 1].endswith(f"--basetemp={project}/.pytest_cache/pytest-temp")
    assert (run / ".done").is_file()
    assert f"driver_exit={expected}" in (run / "driver.exit").read_text()
    assert int((run / "pytest-1.exit").read_text()) == expected
    assert "frozen=yes" in (run / "summary.txt").read_text()


@pytest.mark.parametrize("detached", (False, True), ids=("foreground", "detached"))
@pytest.mark.parametrize("native_exit,expected", ((0, 0), (79, 2)), ids=("healthy", "late-failure"))
def test_public_provenance_requires_success_after_actual_path_output(project, detached, native_exit, expected):
    linked_environment = project / ".venv"
    assert linked_environment.is_symlink()
    environment_target = linked_environment.resolve()
    assert environment_target == (REPO_ROOT / ".venv").resolve()
    if native_exit:
        # Change only this disposable package, never the linked interpreter.
        package_init = project / "elspeth-lints/src/elspeth_lints/__init__.py"
        assert not package_init.is_symlink() and package_init.resolve().is_relative_to(project)
        package_init.write_text(PROVENANCE_LATE_FAILURE)
    producer = "import elspeth, elspeth_lints; print(elspeth.__file__); print(elspeth_lints.__file__)"
    measured = subprocess.run(
        [str(linked_environment / "bin/python"), "-B", "-c", producer],
        cwd=project,
        env=_environment(project, "pass"),
        capture_output=True,
        text=True,
        check=False,
    )
    expected_paths = [str(project / "src/elspeth/__init__.py"), str(project / "elspeth-lints/src/elspeth_lints/__init__.py")]
    assert measured.returncode == native_exit
    assert measured.stdout.splitlines() == expected_paths
    if native_exit:
        assert measured.stderr == "controlled provenance shutdown failure exit=79\n"
    else:
        assert measured.stderr == ""

    process, run = _start(project, "pass", detached=detached)
    actual = _finish(process, run, detached)
    report = _report(run, actual)
    assert report["child_returncode"] == actual
    assert linked_environment.is_symlink() and linked_environment.resolve() == environment_target
    assert actual == expected, "valid printed paths cannot qualify a failed provenance interpreter"
    if native_exit:
        output = (run / ("driver.log" if detached else "launcher-output.txt")).read_text()
        assert "preflight REFUSED; nothing run" in output
        assert not (run / "pytest-1.exit").exists()
        assert not (run / "pytest-junit.xml").exists()
        assert not (run / "summary.txt").exists()
        assert not (run / "driver.exit").exists()
        assert not (run / ".done").exists()
        assert not (project / ".pytest_cache/pytest-temp").exists()
    else:
        preflight = (run / "preflight.txt").read_text()
        assert f"provenance  : elspeth -> {expected_paths[0]}" in preflight
        assert f"              elspeth_lints -> {expected_paths[1]}" in preflight
        assert int((run / "pytest-1.exit").read_text()) == 0
        assert "driver_exit=0" in (run / "driver.exit").read_text()
        summary = (run / "summary.txt").read_text()
        assert "frozen=yes" in summary and "RESULT=PASS" in summary
        assert (run / ".done").is_file()


@pytest.mark.parametrize("detached", (False, True))
@pytest.mark.parametrize(
    "payload,status,findings",
    (("[]", 0, 0), (_lint_finding(severity="note"), 0, 1), (_lint_finding(), 1, 1)),
    ids=("empty", "note", "gating-finding"),
)
def test_lints_complete_corpus_remains_advisory(project, detached, payload, status, findings):
    _lint_result(project, payload, status)
    process, run = _start(project, "pass", detached=detached, stage="lints")
    assert _finish(process, run, detached) == 0
    _report(run, 0)
    assert int((run / "lints-1.exit").read_text()) == status
    assert (run / "lints.json").read_text() == payload
    assert "controlled lint stderr" in (run / "lints.log").read_text()
    summary = (run / "summary.txt").read_text()
    assert f"stage=lints exit={status}" in summary
    assert f"lint_status=complete findings={findings}" in summary
    assert "frozen=yes" in summary and "RESULT=PASS" in summary
    assert "driver_exit=0" in (run / "driver.exit").read_text()
    assert json.loads((project / ".pytest_cache/lint-argv.json").read_text())[-2:] == ["--format", "json"]


@pytest.mark.parametrize(
    "payload,status",
    (
        (None, 0),
        ("", 0),
        ('[{"rule_id":', 1),
        ("{}", 0),
        ("[] []", 0),
        ('[{"rule_id":"controlled"}]', 1),
        (_lint_finding(rule_id="parse-error"), 1),
        (_lint_finding(rule_id="read-error"), 1),
        (_lint_finding(), 0),
        ("[]", 1),
        (_lint_finding(severity="note"), 1),
        ("[]", 2),
    ),
    ids=(
        "missing-output",
        "empty-output",
        "truncated-json",
        "wrong-document-shape",
        "multiple-json-documents",
        "incomplete-record",
        "parse-error",
        "read-error",
        "zero-with-gating-finding",
        "one-without-findings",
        "one-with-notes-only",
        "configuration-error-with-json",
    ),
)
def test_lints_invalid_scan_cannot_pass_advisory_gate(project, payload, status):
    _lint_result(project, payload, status)
    process, run = _start(project, "pass", stage="lints")
    assert _finish(process, run, False) == 1
    _report(run, 1)
    assert int((run / "lints-1.exit").read_text()) == status
    summary = (run / "summary.txt").read_text()
    assert f"stage=lints exit={status}" in summary
    assert "lint_status=INCOMPLETE" in summary and "RESULT=INCOMPLETE" in summary
    assert "RESULT=PASS" not in summary and "frozen=yes" in summary
    assert "driver_exit=1" in (run / "driver.exit").read_text()
    assert (run / ".done").is_file()


@pytest.mark.parametrize("detached", (False, True))
def test_lints_native_crash_cannot_pass_with_complete_json(project, detached):
    _lint_result(project, "[]", -signal.SIGTERM)
    process, run = _start(project, "pass", detached=detached, stage="lints")
    assert _finish(process, run, detached) == 1
    _report(run, 1)
    assert int((run / "lints-1.exit").read_text()) == 128 + signal.SIGTERM
    assert (run / "lints.json").read_text() == "[]"
    assert "RESULT=INCOMPLETE" in (run / "summary.txt").read_text()


def test_lints_strict_keeps_complete_gating_findings_fatal(project):
    _lint_result(project, _lint_finding(), 1)
    process, run = _start(project, "pass", stage="lints", lints_strict=True)
    assert _finish(process, run, False) == 1
    _report(run, 1)
    summary = (run / "summary.txt").read_text()
    assert "lint_status=complete findings=1" in summary
    assert "RESULT=FAIL" in summary and "RESULT=INCOMPLETE" not in summary
    assert int((run / "lints-1.exit").read_text()) == 1


@pytest.mark.parametrize("detached", (False, True))
def test_public_deadline_covers_blocked_execution_preflight(project, detached):
    fifo = project / ".pytest_cache/blocked-source"
    os.mkfifo(fifo)
    (project / "untracked-link").symlink_to(fifo)
    wrapper = project / ".pytest_cache/bin/sha256sum"
    wrapper.parent.mkdir()
    wrapper.write_text(
        "#!/bin/sh\nfor argument do\n"
        '  if [ "$argument" = untracked-link ]; then printf entered > .pytest_cache/hash-entered; fi\n'
        'done\nexec /usr/bin/sha256sum "$@"\n'
    )
    wrapper.chmod(0o755)
    process, run = _start(project, "pass", detached=detached, timeout="3")
    assert _finish(process, run, detached) == 124
    assert (project / ".pytest_cache/hash-entered").read_text() == "entered"
    assert _report(run, 124)["outcome"] == "timeout"
    assert not (run / ".done").exists()
    assert not (run / "pytest-1.exit").exists()


def test_public_deadline_covers_watcher_cleanup_after_passing_stage(project):
    fifo = project / ".pytest_cache/blocked-watcher"
    os.mkfifo(fifo)
    real_git = shutil.which("git")
    assert real_git is not None
    wrapper = project / ".pytest_cache/bin/git"
    wrapper.parent.mkdir()
    wrapper.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = --no-optional-locks ] && [ "$2" = status ] && [ "$4" = -uall ]; then\n'
        "  count=0\n"
        "  if [ -f .pytest_cache/status-count ]; then read -r count < .pytest_cache/status-count; fi\n"
        "  count=$((count + 1))\n"
        '  printf "%s\\n" "$count" > .pytest_cache/status-count\n'
        '  if [ "$count" -ge 3 ]; then\n'
        '    printf "%s\\n" "$$" > .pytest_cache/blocked-watcher.pid\n'
        "    exec /usr/bin/cat .pytest_cache/blocked-watcher\n"
        "  fi\n"
        "fi\n"
        f'exec "{real_git}" "$@"\n'
    )
    wrapper.chmod(0o755)
    process, run = _start(project, "pass", timeout="12")
    _wait_file(project / ".pytest_cache/blocked-watcher.pid")
    pid = int((project / ".pytest_cache/blocked-watcher.pid").read_text())
    identity = {"pid": pid, "start": Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[19]}
    assert _finish(process, run, False) == 124
    _report(run, 124)
    assert not _identity_alive(identity)
    assert int((run / "pytest-1.exit").read_text()) == 0
    assert "phase=watcher_cleanup" in (run / "driver.cleanup-pending").read_text()


@pytest.mark.parametrize("mode,workers,driver_status", (("abandon", 0, 0), ("crash", 1, 1)))
def test_public_detached_cannot_qualify_abandoned_stubborn_descendants(project, mode, workers, driver_status):
    process, run = _start(project, mode, detached=True, workers=workers)
    assert _finish(process, run, True) == 125
    report = _report(run, 125)
    assert report["outcome"] == "abandoned-descendants" and report["forced_kill"] is True
    assert report["child_returncode"] == driver_status
    assert (run / ".done").is_file()  # Driver completion does not qualify cleanup.
    _family_gone(project)


@pytest.mark.parametrize("detached,target", ((False, "launcher.pid"), (True, "supervisor.pid")))
def test_public_cancellation_of_recorded_owner_cleans_actual_driver_family(project, detached, target):
    process, run = _start(project, "hang", detached=detached)
    _wait_file(project / ".pytest_cache/family-child.json")
    os.kill(int((run / target).read_text()), signal.SIGTERM)
    assert _finish(process, run, detached) == 143
    report = _report(run, 143)
    assert report["outcome"] == "cancelled" and report["forced_kill"] is True
    _family_gone(project)


def test_detached_terminal_receipt_waits_for_actual_supervisor_exit(project):
    process, run = _start(project, "publication-pause", detached=True)
    _wait_file(run / "supervisor.json")
    assert (run / ".done").is_file()
    assert not (run / "supervisor.exit").exists()
    assert not (run / "launcher.exit").exists()
    os.kill(int((run / "supervisor.pid").read_text()), 0)
    (project / ".pytest_cache/release-publication").touch()
    assert _finish(process, run, True) == 0
    _report(run, 0)


def test_public_terminal_failure_cannot_qualify_driver_pass(project):
    process, run = _start(project, "publication-fail")
    assert _finish(process, run, False) == 1
    assert int((run / "supervisor.exit").read_text()) == 1
    assert int((run / "launcher.exit").read_text()) == 1
    assert not (run / "supervisor.json").exists()
    assert "RESULT=PASS" in (run / "summary.txt").read_text()
    assert (run / ".done").is_file()


def _fake_ruff(root: Path) -> None:
    # Exercise automatic selection without starting eight/twelve pytest workers.
    (root / ".venv").unlink()
    binaries = root / ".venv/bin"
    binaries.mkdir(parents=True)
    python = binaries / "python"
    python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    python.chmod(0o755)
    ruff = binaries / "ruff"
    ruff.write_text("#!/bin/sh\nexit 0\n")
    ruff.chmod(0o755)


def _inventory_row(root: Path, pid: int, argv: list[str]) -> None:
    row = _inventory(root) / str(pid)
    row.mkdir()
    (row / "cmdline").write_bytes(b"\0".join(argument.encode() for argument in argv) + b"\0")
    (row / "cwd").symlink_to(root, target_is_directory=True)


def _inventory_tripwire(root: Path) -> Path:
    # It covers only ordinary private fixture data, never real process paths.
    marker = root / ".pytest_cache/inventory-accessed"
    cat = root / ".pytest_cache/bin/cat"
    cat.parent.mkdir(exist_ok=True)
    cat.write_text(
        '#!/bin/sh\ncase "$1" in\n'
        f"  {shlex.quote(str(_inventory(root)))}/*/cmdline)\n"
        f'    printf "%s\\n" "$1" > {shlex.quote(str(marker))}; exit 99;;\n'
        'esac\nexec /usr/bin/cat "$@"\n'
    )
    cat.chmod(0o755)
    return marker


def _plan(root: Path, *, workers: int | None = 0, inventory: str | None = None, stage: str = "pytest", source: str | None = None):
    arguments = ["--root", str(root), "--stages", stage]
    if workers is not None:
        arguments.extend(("--workers", str(workers)))
    # A calibration mutant is dry-run only and retains the SAME finite-table
    # environment. No copied script, re-entry or host inventory is executed.
    command = [str(SCRIPT), *arguments] if source is None else ["bash", "-c", source, str(SCRIPT), *arguments]
    return subprocess.run(
        command, cwd=root, env=_environment(root, "pass", inventory=inventory), capture_output=True, text=True, timeout=15
    )


def _assert_explicit_capacity(text: str, workers: int, marker: Path) -> None:
    assert not marker.exists(), "sibling inventory was accessed"
    assert f"capacity    : explicit workers={workers}; caller-established capacity" in text
    assert "siblings    : unobserved (explicit workers; no sibling discovery)" in text
    assert "pytest process(es) observed" not in text


@pytest.mark.parametrize("detached", (False, True))
@pytest.mark.parametrize("workers", (0, 1))
def test_explicit_workers_bypass_inventory_in_foreground_and_detached(project, detached, workers):
    _inventory_row(project, os.getpid(), [sys.executable, "-m", "pytest", "would-match"])
    marker = _inventory_tripwire(project)
    process, run = _start(project, "pass", detached=detached, workers=workers)
    assert _finish(process, run, detached) == 0
    _report(run, 0)
    _assert_explicit_capacity((run / "preflight.txt").read_text(), workers, marker)
    command = json.loads((run / "supervisor.argv.json").read_text())
    assert command[command.index("--workers") + 1] == str(workers)
    output = (run / ("driver.log" if detached else "launcher-output.txt")).read_text()
    assert f" -m pytest tests/ -n {workers} " in output
    assert int((run / "pytest-1.exit").read_text()) == 0
    assert "frozen=yes" in (run / "summary.txt").read_text()


def test_explicit_workers_do_not_validate_unused_fixture_input(project):
    unused = str(project / ".pytest_cache/not-created")
    result = _plan(project, workers=0, inventory=unused)
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_explicit_capacity(result.stdout, 0, project / ".pytest_cache/inventory-accessed")
    assert not Path(unused).exists()


def test_explicit_inventory_oracle_rejects_finite_table_branch_mutant(project):
    _inventory_row(project, os.getpid(), [sys.executable, "-m", "pytest", "would-match"])
    marker = _inventory_tripwire(project)
    control = _plan(project)
    assert control.returncode == 0, control.stdout + control.stderr
    _assert_explicit_capacity(control.stdout, 0, marker)
    source = SCRIPT.read_text()
    branch = 'if [ -n "$WORKERS" ]; then\n    pre "capacity    : explicit'
    assert source.count(branch) == 1
    mutant = source.replace(branch, 'if false; then\n    pre "capacity    : explicit', 1)
    result = _plan(project, source=mutant)
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.read_text().strip() == str(_inventory(project) / str(os.getpid()) / "cmdline")
    with pytest.raises(AssertionError, match="sibling inventory was accessed"):
        _assert_explicit_capacity(result.stdout, 0, marker)


@pytest.mark.parametrize("kind", ("empty", "quoted-shell", "non-pytest-python", "missing-cmdline"))
def test_automatic_capacity_uses_only_finite_nonmatching_or_disappearing_data(project, kind):
    if kind == "quoted-shell":
        _inventory_row(project, os.getpid(), ["/bin/bash", "-c", "python -m pytest tests/"])
    elif kind == "non-pytest-python":
        _inventory_row(project, os.getpid(), [sys.executable, "-m", "http.server"])
    elif kind == "missing-cmdline":
        (_inventory(project) / str(os.getpid())).mkdir()
    result = _plan(project, workers=None)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"0 pytest process(es) observed from fixture {_inventory(project)} -> workers=12" in result.stdout
    assert "host /proc" not in result.stdout


@pytest.mark.parametrize("kind", ("empty-value", "missing", "file", "symlink", "noncanonical", "outside-cache"))
def test_invalid_selected_fixture_root_refuses_without_host_fallback(project, kind):
    selected = project / ".pytest_cache/invalid-inventory"
    if kind == "file":
        selected.write_text("ordinary file, not an inventory directory")
    elif kind == "symlink":
        selected.symlink_to(_inventory(project), target_is_directory=True)
    value = "" if kind == "empty-value" else str(selected)
    if kind == "noncanonical":
        value = str(_inventory(project)) + "/../sibling-inventory"
    elif kind == "outside-cache":
        value = str(project / "tests")
    result = _plan(project, workers=None, inventory=value)
    assert result.returncode == 2
    expected = "sibling fixture root must be a canonical directory" if kind == "noncanonical" else "invalid sibling fixture root"
    assert "REFUSED     : " + expected in result.stdout
    assert "pytest process(es) observed" not in result.stdout


@pytest.mark.parametrize("linked", ("entry", "cmdline"))
def test_fixture_inventory_refuses_linked_entries_without_following_them(project, linked):
    ordinary = project / ".pytest_cache/ordinary-inventory-data"
    ordinary.mkdir()
    (ordinary / "cmdline").write_bytes(b"python\0-m\0pytest\0")
    row = _inventory(project) / str(os.getpid())
    if linked == "entry":
        row.symlink_to(ordinary, target_is_directory=True)
    else:
        row.mkdir()
        (row / "cmdline").symlink_to(ordinary / "cmdline")
    result = _plan(project, workers=None)
    assert result.returncode == 2
    assert "sibling fixture entries must be ordinary private data" in result.stdout


@pytest.mark.parametrize("workers", (0, None))
def test_testcontainer_capacity_warning_distinguishes_unobserved_from_fixture_contention(project, workers):
    _inventory_row(project, os.getpid(), [sys.executable, "-m", "pytest", "fixture-sibling"])
    docker = project / ".pytest_cache/bin/docker"
    docker.parent.mkdir(exist_ok=True)
    docker.write_text('#!/bin/sh\n[ "$#" = 1 ] && [ "$1" = info ]\n')
    docker.chmod(0o755)
    result = _plan(project, workers=workers, stage="testcontainer")
    assert result.returncode == 0, result.stdout + result.stderr
    assert " -m testcontainer -n 0 " in result.stdout
    if workers is None:
        assert f"1 pytest process(es) observed from fixture {_inventory(project)} -> workers=8" in result.stdout
        assert "a testcontainer run beside another suite" in result.stdout
    else:
        _assert_explicit_capacity(result.stdout, workers, project / ".pytest_cache/inventory-accessed")
        assert "siblings unobserved; caller must establish serial/exclusive testcontainer capacity" in result.stdout


@pytest.mark.parametrize("real_sibling", (False, True))
def test_default_workers_exclude_only_owned_wrappers_and_keep_real_sibling(project, real_sibling):
    _fake_ruff(project)
    sibling = None
    sibling_identity = None
    try:
        if real_sibling:
            sibling = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests/test_probe.py",
                    "-n",
                    "0",
                    "-p",
                    "xdist.plugin",
                    "--basetemp=.pytest_cache/sibling-temp",
                ],
                cwd=project,
                env=_environment(project, "sibling"),
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            (project / ".pytest_cache/sibling.pid").write_text(str(sibling.pid) + "\n")
            _wait_file(project / ".pytest_cache/sibling-ready")
            sibling_identity = {"pid": sibling.pid, "start": Path(f"/proc/{sibling.pid}/stat").read_text().rpartition(")")[2].split()[19]}
            _inventory_row(project, sibling.pid, sibling.args)
        process, run = _start(project, "pass", workers=None, stage="ruff", capacity_barrier=True)
        _wait_file(project / ".pytest_cache/capacity-ready")
        for role in ("launcher", "supervisor"):
            _wait_file(run / f"{role}.start-stat")
            pid = int((run / f"{role}.pid").read_text())
            identity = {"pid": pid, "start": (run / f"{role}.start-stat").read_text().rpartition(")")[2].split()[19]}
            assert _identity_alive(identity)
            _inventory_row(project, pid, [sys.executable, "-m", "pytest", f"owned-{role}"])
        (project / ".pytest_cache/capacity-release").touch()
        assert _finish(process, run, False) == 0
        _report(run, 0)
        count, workers = (1, 8) if real_sibling else (0, 12)
        preflight = (run / "preflight.txt").read_text()
        assert f"{count} pytest process(es) observed from fixture {_inventory(project)} -> workers={workers}" in preflight
        assert len(list(_inventory(project).iterdir())) == 2 + count
        assert "host /proc" not in preflight
    finally:
        if sibling is not None:
            sibling.terminate()
            try:
                sibling.wait(timeout=5)
            except subprocess.TimeoutExpired:
                sibling.kill()
                sibling.wait(timeout=5)
            assert sibling_identity is not None and not _identity_alive(sibling_identity)


def test_late_launcher_cancellation_preserves_helper_zero_without_qualifying_success(project):
    process, run = _start(project, "publication-pause", detached=True)
    _wait_file(run / "supervisor.json")
    owner = int((run / "launcher.pid").read_text())
    supervisor = int((run / "supervisor.pid").read_text())
    stopped = False
    try:
        os.kill(owner, signal.SIGSTOP)
        stopped = True
        deadline = time.monotonic() + 5
        while Path(f"/proc/{owner}/stat").read_text().rpartition(")")[2].split()[0] != "T":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        (project / ".pytest_cache/release-publication").touch()
        deadline = time.monotonic() + 5
        while Path(f"/proc/{supervisor}/stat").read_text().rpartition(")")[2].split()[0] != "Z":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        os.kill(owner, signal.SIGTERM)
        os.kill(owner, signal.SIGCONT)
        stopped = False
        assert _finish(process, run, True) == 143
        assert int((run / "supervisor.exit").read_text()) == 0
        assert int((run / "launcher.exit").read_text()) == 143
        report = json.loads((run / "supervisor.json").read_text())
        assert report["exit_status"] == report["child_returncode"] == 0
        assert report["cleanup_complete"] is True
    finally:
        if stopped:
            with suppress(ProcessLookupError):
                os.kill(owner, signal.SIGCONT)
            with suppress(ProcessLookupError):
                os.kill(owner, signal.SIGTERM)


@pytest.mark.parametrize("invalid", ("0", "-1", "nan", "inf", "1e999"))
def test_invalid_public_deadline_is_rejected_before_launch(project, invalid):
    result = subprocess.run(
        [str(SCRIPT), "--execute", "--root", str(project), "--workers", "0", "--run-timeout-seconds", invalid],
        cwd=project,
        env=_environment(project, "pass"),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert not (project / ".claude").exists()


def test_public_dry_run_is_read_only_and_uses_project_local_default(project):
    before = {path.relative_to(project): path.lstat().st_mtime_ns for path in project.rglob("*")}
    result = subprocess.run(
        [str(SCRIPT), "--root", str(project), "--workers", "0"],
        cwd=project,
        env=_environment(project, "pass"),
        capture_output=True,
        text=True,
        timeout=10,
    )
    after = {path.relative_to(project): path.lstat().st_mtime_ns for path in project.rglob("*")}
    assert result.returncode == 0, result.stdout + result.stderr
    assert "dry-run: nothing run" in result.stdout
    _assert_explicit_capacity(result.stdout, 0, project / ".pytest_cache/inventory-accessed")
    assert str(project / ".claude/lanes/full-suite-gates") in result.stdout
    assert before == after
