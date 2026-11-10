"""Fresh processes prove the actual standalone root and public CLI ownership."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client import stdio

from tests.helpers.composer_stdio_probe import linux_owned_children, probe

ROOT = Path(__file__).resolve().parents[3]
SUPERVISOR = ROOT / "src/elspeth/composer_mcp/portable_supervisor.py"
CHILD = ROOT / "tests/helpers/composer_lifecycle_probe.py"


def test_dedicated_supervisor_releases_actual_transport_ends_while_alive(tmp_path: Path) -> None:
    helper = ROOT / "tests/helpers/composer_stdio_ownership_probe.py"
    result = subprocess.run([sys.executable, str(helper), "probe", str(tmp_path)], capture_output=True, timeout=25)
    assert result.returncode == 0 and result.stdout == b"", result.stderr.decode()
    assert json.loads((tmp_path / "observation.json").read_text()) == {
        "supervisor_alive_before": True,
        "supervisor_alive_after": True,
        "stdout_eof_while_alive": True,
        "stdin_reader_gone_while_alive": True,
        "inherited_echo": "pong",
    }
    assert json.loads((tmp_path / "status.json").read_text()) == {"returncode": 0}


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc measurement controls")
def test_child_inventory_finds_owned_positive_and_empty_negative() -> None:
    process = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"], stdin=subprocess.PIPE)
    try:
        assert process.pid in linux_owned_children(os.getpid())
        assert linux_owned_children(process.pid) == []
        process.communicate(timeout=5)
        assert process.returncode == 0
        assert process.pid not in linux_owned_children(os.getpid())
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "linux", reason="actual Linux controller-death and subreaper custody")
@pytest.mark.timing_sensitive
def test_actual_controller_death_stops_public_owner_and_reaps_runner(tmp_path: Path) -> None:
    helper = ROOT / "tests/helpers/composer_parent_death_probe.py"
    result = subprocess.run([sys.executable, str(helper), "subreaper", str(tmp_path)], capture_output=True, timeout=40)
    assert result.returncode == 0 and result.stdout == b"", result.stderr.decode()
    assert json.loads((tmp_path / "parent-death.json").read_text()) == {
        "controller_returncode": -signal.SIGKILL,
        "supervisor_returncode": 1,
        "supervisor_reaped": True,
        "runner_reaped_by_supervisor": True,
    }


@pytest.mark.skipif(sys.platform != "linux", reason="actual Linux adopted-child custody after supervisor loss")
def test_supervisor_loss_forces_actual_public_runner_and_reaps_it(tmp_path: Path) -> None:
    helper = ROOT / "tests/helpers/composer_parent_death_probe.py"
    result = subprocess.run([sys.executable, str(helper), "subreaper-channel-loss", str(tmp_path)], capture_output=True, timeout=40)
    assert result.returncode == 0 and result.stdout == b"", result.stderr.decode()
    assert json.loads((tmp_path / "channel-loss.json").read_text()) == {
        "supervisor_returncode": -signal.SIGKILL,
        "runner_returncode": 70,
        "runner_reaped_by_subreaper": True,
        "controller_returncode": 0,
    }


@pytest.mark.parametrize("parent_changed", [False, True])
def test_guard_registration_detects_reparenting_without_pid_one(monkeypatch, parent_changed: bool) -> None:
    import ctypes

    from elspeth.composer_mcp import portable_supervisor as production

    calls = []

    class Libc:
        def prctl(self, *args):
            calls.append(args)
            return 0

    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: Libc())
    monkeypatch.setattr(production.os, "getppid", lambda: 902 if parent_changed else 901)
    assert production._install_controller_death_guard(901, linux=True) is parent_changed
    assert calls == [(1, signal.SIGTERM, 0, 0, 0)]


def test_non_linux_guard_branch_does_not_touch_process_state(monkeypatch) -> None:
    import ctypes

    from elspeth.composer_mcp import portable_supervisor as production

    def forbidden(*args, **kwargs):
        raise AssertionError("non-Linux adapter must not load libc")

    monkeypatch.setattr(ctypes, "CDLL", forbidden)
    assert production._install_controller_death_guard(901, linux=False) is False


def test_guard_syscall_failure_is_visible(monkeypatch) -> None:
    import ctypes

    from elspeth.composer_mcp import portable_supervisor as production

    class Libc:
        def prctl(self, *args):
            return -1

    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: Libc())
    monkeypatch.setattr(ctypes, "get_errno", lambda: errno.EPERM)
    with pytest.raises(OSError) as raised:
        production._install_controller_death_guard(901, linux=True)
    assert raised.value.errno == errno.EPERM


@pytest.mark.asyncio
async def test_public_executable_real_preview_paths_and_eof(tmp_path: Path) -> None:
    receipt = await probe(tmp_path)
    assert receipt["returncode"] == 0
    assert receipt["private"]["sources"]["orders"]["options"]["path"] == "input.csv"
    assert receipt["private"]["outputs"][0]["options"]["path"] == "patched.csv"


@pytest.mark.parametrize("arguments,status,text", [(["--help"], 0, "--data-dir"), (["--unknown"], 2, "unrecognized arguments")])
def test_public_help_and_parse_status(arguments, status, text) -> None:
    result = subprocess.run([str(Path(sys.executable).parent / "elspeth-composer"), *arguments], capture_output=True, timeout=12)
    assert result.returncode == status
    assert text in (result.stdout + result.stderr).decode()
    assert "usage: elspeth-composer" in (result.stdout + result.stderr).decode()
    if status:
        assert not result.stdout


def test_private_runner_refuses_before_application_imports(tmp_path: Path) -> None:
    # A fresh interpreter records imports without changing the child runtime.
    result = subprocess.run(
        [sys.executable, "-v", str(ROOT / "src/elspeth/composer_mcp/runner.py")],
        capture_output=True,
        timeout=8,
        env={key: value for key, value in os.environ.items() if not key.startswith("_ELSPETH_COMPOSER_CONTROL_")},
    )
    assert result.returncode != 0 and result.stdout == b""
    assert b"Private Composer bootstrap is required" in result.stderr
    assert b"import 'elspeth.composer_mcp.server'" not in result.stderr
    assert b"import 'elspeth.contracts'" not in result.stderr
    assert b"import 'elspeth.plugins" not in result.stderr
    (tmp_path / "imports.log").write_bytes(result.stderr)


def test_public_console_executes_stdlib_owner_in_original_process(tmp_path: Path) -> None:
    # An audit hook observes the actual installed console script immediately
    # before Popen. It cannot supply ownership or alter the launched child.
    script = str(Path(sys.executable).parent / "elspeth-composer")
    code = """
import json, os, pathlib, runpy, sys
script, marker = sys.argv[1:3]
sys.argv = [script, '--help']
def observe(event, args):
    if event == 'subprocess.Popen':
        forbidden = ('elspeth.web', 'elspeth.plugins', 'elspeth.contracts', 'mcp', 'sqlalchemy', 'pydantic')
        assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules for prefix in forbidden)
        assert not pathlib.Path(marker).exists(), 'more than one child spawn'
        pathlib.Path(marker).write_text(json.dumps({'pid': os.getpid(), 'command': args[1]}))
sys.addaudithook(observe)
runpy.run_path(script, run_name='__main__')
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, script, str(tmp_path / "before-spawn.json")], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    stdout, stderr = process.communicate(timeout=12)
    assert process.returncode == 0, stderr.decode()
    assert b"--data-dir" in stdout and not stderr
    observed = json.loads((tmp_path / "before-spawn.json").read_text())
    assert observed["pid"] == process.pid
    assert observed["command"] == [sys.executable, str(ROOT / "src/elspeth/composer_mcp/runner.py"), "--help"]


@pytest.mark.parametrize(
    "mode,expected,complete",
    [("normal", 0, True), ("startup-failure", 1, True), ("transport-failure", 1, True), ("cleanup-failure", 70, False)],
)
def test_real_partial_startup_transport_and_cleanup_outcomes(tmp_path: Path, mode, expected, complete) -> None:
    result = subprocess.run(
        [sys.executable, str(SUPERVISOR), sys.executable, str(CHILD), str(tmp_path), mode], input=b"", capture_output=True, timeout=12
    )
    (tmp_path / "stderr.log").write_bytes(result.stderr)
    (tmp_path / "status.json").write_text(json.dumps({"returncode": result.returncode}))
    assert result.returncode == expected, result.stderr.decode()
    assert result.stdout == b""
    authenticated = json.loads((tmp_path / "authenticated.json").read_text())
    assert authenticated["canonical_wire"] and authenticated["bootstrap_consumed"] and authenticated["descendant_bootstrap_absent"]
    if sys.platform == "linux":
        assert not Path(f"/proc/{authenticated['pid']}").exists()
    assert (tmp_path / "completion").exists() is complete
    if mode == "startup-failure":
        assert json.loads((tmp_path / "outcomes.json").read_text()) == ["LookupError"]
    if mode == "transport-failure":
        assert json.loads((tmp_path / "outcomes.json").read_text()) == ["OSError"]
    if mode == "cleanup-failure":
        assert (tmp_path / "workers-joined").exists()
        assert json.loads((tmp_path / "outcomes.json").read_text()) == ["OSError", "ArithmeticError"]


async def _file(path: Path, seconds: float = 8.0) -> None:
    async with asyncio.timeout(seconds):
        while not path.exists():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,release",
    [("blocked", True), ("blocked", False), ("quarantine", True)],
    ids=["release-joins", "real-deadline-kills", "real-generation-expiry"],
)
@pytest.mark.timing_sensitive
async def test_blocked_actual_preflight_retains_admission_until_join_or_deadline(
    tmp_path: Path, monkeypatch, mode: str, release: bool
) -> None:
    processes = []
    create = stdio._create_platform_compatible_process

    async def observe(*args, **kwargs):
        process = await create(*args, **kwargs)
        processes.append(process)
        return process

    # Keep the SDK's own fallback kill outside the production 30-second grace.
    monkeypatch.setattr(stdio, "PROCESS_TERMINATION_TIMEOUT", 40.0)
    parameters = StdioServerParameters(
        command=sys.executable, args=[str(SUPERVISOR), sys.executable, str(CHILD), str(tmp_path), mode], env=dict(os.environ)
    )
    with (tmp_path / "stderr.log").open("w") as errors, patch.object(stdio, "_create_platform_compatible_process", observe):
        async with stdio.stdio_client(parameters, errlog=errors) as streams, ClientSession(*streams) as session:
            await session.initialize()
            configured = await session.call_tool(
                "set_pipeline",
                {
                    "source": {
                        "plugin": "null",
                        "on_success": "result",
                        "options": {"schema": {"mode": "observed"}},
                        "on_validation_failure": "discard",
                    },
                    "nodes": [],
                    "edges": [],
                    "outputs": [
                        {
                            "sink_name": "result",
                            "plugin": "csv",
                            "options": {
                                "path": "output.csv",
                                "schema": {"mode": "observed"},
                                "mode": "write",
                                "collision_policy": "auto_increment",
                            },
                            "on_write_failure": "discard",
                        }
                    ],
                },
            )
            assert not configured.isError and json.loads(configured.content[0].text)["success"]
            pending = asyncio.create_task(session.call_tool("preview_pipeline", {}))
            await _file(tmp_path / "worker")
            caller_timeout_observed = mode == "blocked" and release
            if caller_timeout_observed:
                timed_out = await asyncio.wait_for(pending, timeout=8)
                assert timed_out.isError and timed_out.content[0].text == "Tool error: TimeoutError"
                pending = asyncio.create_task(session.call_tool("preview_pipeline", {}))
                await asyncio.sleep(0.05)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            # A second cancelled caller must join the same admitted preflight.
            pending = asyncio.create_task(session.call_tool("preview_pipeline", {}))
            await asyncio.sleep(0.05)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            process = processes[0]
            assert process.stdin is not None
            await process.stdin.aclose()
            await _file(tmp_path / "draining.json")
            assert json.loads((tmp_path / "draining.json").read_text())["admissions"] == 1
            if os.name != "nt":
                process.send_signal(signal.SIGTERM)
                process.send_signal(signal.SIGTERM)
            await asyncio.sleep(0.15)
            assert process.returncode is None
            assert not (tmp_path / "completion").exists()
            if mode == "quarantine":
                await _file(tmp_path / "expired", seconds=12)
                assert process.returncode is None and not (tmp_path / "completion").exists()
            started = time.monotonic()
            if release:
                (tmp_path / "release").write_text("release actual worker")
            async with asyncio.timeout(35):
                status = await process.wait()
            elapsed = time.monotonic() - started
            (tmp_path / "status.json").write_text(
                json.dumps({"returncode": status, "wait_seconds": elapsed, "caller_timeout_observed": caller_timeout_observed})
            )
            assert (tmp_path / "completion").exists() is (release and mode != "quarantine")
            if mode == "quarantine":
                assert status == 70
                outcomes = json.loads((tmp_path / "outcomes.json").read_text())
                assert "RequiredGenerationUnavailable" in outcomes
            elif release:
                assert status in (0, 1), (status, (tmp_path / "stderr.log").read_text())
            elif os.name != "nt":
                assert status == -signal.SIGKILL and elapsed >= 28
            else:
                assert status != 0 and elapsed >= 28
            child_pid = json.loads((tmp_path / "authenticated.json").read_text())["pid"]
            if sys.platform == "linux":
                assert not Path(f"/proc/{child_pid}").exists()


@pytest.mark.asyncio
async def test_embedded_construction_preserves_live_host_owner_and_coordinator(tmp_path: Path) -> None:
    from elspeth.composer_mcp.server import _build_stdio_server
    from elspeth.web import async_workers
    from elspeth.web.dependencies import create_catalog_service
    from elspeth.web.execution.runtime_preflight import RuntimePreflightCoordinator
    from elspeth.web.required_executor import RequiredGenerationUnavailable

    owner = async_workers._RECOVERY_CALLBACK
    draining = async_workers._INSTANCE_DRAINING
    coordinator = RuntimePreflightCoordinator()
    _build_stdio_server(create_catalog_service(), tmp_path / "scratch", tmp_path / "data", runtime_preflight_coordinator=coordinator)
    assert async_workers._RECOVERY_CALLBACK is owner
    assert async_workers._INSTANCE_DRAINING is draining and not draining.is_set()
    with pytest.raises(RequiredGenerationUnavailable, match="unowned process"):
        async_workers.configure_standalone_executor_recovery(
            drain_seconds=10,
            instance_draining=draining,
            generation_unavailable=async_workers._GENERATION_UNAVAILABLE,
            recovery_callback=owner,
        )
    assert async_workers._RECOVERY_CALLBACK is owner
    assert await async_workers.run_sync_in_worker(lambda: 7) == 7
    await coordinator.seal_and_join()
    await async_workers.shutdown_async_workers()


@pytest.mark.timing_sensitive
@pytest.mark.parametrize("owned", [False, True])
def test_fresh_embedded_real_host_owner_and_missing_owner(tmp_path: Path, owned: bool) -> None:
    command = (
        [sys.executable, str(SUPERVISOR), sys.executable, str(CHILD), str(tmp_path), "embedded-owned"]
        if owned
        else [sys.executable, str(CHILD), str(tmp_path), "embedded-unowned"]
    )
    result = subprocess.run(command, input=b"", capture_output=True, timeout=12)
    assert result.returncode == 0 and result.stdout == b"", result.stderr.decode()
    evidence = json.loads((tmp_path / "embedded.json").read_text())
    if owned:
        assert evidence == {"preview_valid": True, "host_owner_preserved": True, "host_coordinator_open": True}
        assert (tmp_path / "completion").exists()
    else:
        assert evidence == {"transport_error": "Tool error: AuditIntegrityError"}
        assert not (tmp_path / "authenticated.json").exists()
