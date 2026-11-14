"""Isolated Linux subreaper observes only this test's retained descendants."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

from composer_stdio_probe import linux_owned_children
from mcp import ClientSession, StdioServerParameters
from mcp.client import stdio


async def controller(directory: Path, *, channel_loss: bool) -> None:
    processes = []
    create = stdio._create_platform_compatible_process

    async def observe(*args, **kwargs):
        process = await create(*args, **kwargs)
        processes.append(process)
        return process

    parameters = StdioServerParameters(
        command=str(Path(sys.executable).parent / "elspeth-composer"),
        args=["--scratch-dir", str(directory / "scratch"), "--data-dir", str(directory / "data")],
        env=dict(os.environ),
    )
    with (directory / "runner-stderr.log").open("w") as errors, patch.object(stdio, "_create_platform_compatible_process", observe):
        async with stdio.stdio_client(parameters, errlog=errors) as streams, ClientSession(*streams) as session:
            await session.initialize()
            supervisor = processes[0].pid
            children = linux_owned_children(supervisor)
            assert len(children) == 1
            (directory / "ready.json").write_text(json.dumps({"supervisor": supervisor, "runner": children[0]}))
            if channel_loss:
                while not (directory / "kill-supervisor").exists():
                    await asyncio.sleep(0.01)
                processes[0].kill()  # Exact SDK-owned process handle.
                assert await processes[0].wait() == -signal.SIGKILL
                (directory / "supervisor-killed").write_text("observed and reaped")
            else:
                await asyncio.Event().wait()


def subreaper(directory: Path, *, channel_loss: bool) -> None:
    # This state affects only this isolated harness. No competing wildcard
    # reaper, parent pytest syscall, or PID-discovered signal target is used.
    libc = ctypes.CDLL(None, use_errno=True)
    assert libc.prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    owned = None
    with (directory / "controller-stderr.log").open("w") as errors:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__)), "controller-loss" if channel_loss else "controller", str(directory)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=errors,
        )
        try:
            deadline = time.monotonic() + 12
            while not (directory / "ready.json").exists():
                assert process.poll() is None, "controller exited before public initialization"
                if time.monotonic() >= deadline:
                    raise TimeoutError("controller startup expired")
                time.sleep(0.01)
            owned = json.loads((directory / "ready.json").read_text())
            if channel_loss:
                (directory / "kill-supervisor").write_text("kill exact controller-owned supervisor")
                deadline = time.monotonic() + 12
                while not (directory / "supervisor-killed").exists():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("controller did not reap its killed supervisor")
                    time.sleep(0.01)
                target = owned["runner"]
            else:
                process.kill()  # Exact retained Popen, deliberately uncatchable.
                assert process.wait(timeout=5) == -signal.SIGKILL
                target = owned["supervisor"]
            deadline = time.monotonic() + 12
            while True:
                # The reaped parent exit transfers this exact descendant to
                # the subreaper. waitpid confirms actual child custody.
                observed, status = os.waitpid(target, os.WNOHANG)
                if observed:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("public supervisor did not settle on controller death")
                time.sleep(0.01)
            if channel_loss:
                assert os.waitstatus_to_exitcode(status) == 70
                assert process.wait(timeout=5) == 0
                (directory / "channel-loss.json").write_text(
                    json.dumps(
                        {
                            "supervisor_returncode": -signal.SIGKILL,
                            "runner_returncode": 70,
                            "runner_reaped_by_subreaper": True,
                            "controller_returncode": 0,
                        }
                    )
                )
            else:
                assert os.waitstatus_to_exitcode(status) == 1
                assert not Path(f"/proc/{owned['runner']}").exists(), "real supervisor must reap its runner"
                (directory / "parent-death.json").write_text(
                    json.dumps(
                        {
                            "controller_returncode": process.returncode,
                            "supervisor_returncode": os.waitstatus_to_exitcode(status),
                            "supervisor_reaped": True,
                            "runner_reaped_by_supervisor": True,
                        }
                    )
                )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if owned is not None:
                for pid in (owned["supervisor"], owned["runner"]):
                    try:
                        observed, _ = os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError:
                        continue
                    if not observed:
                        # A zero wait result proves an unreaped owned child;
                        # only this harness can reap it or signal it now.
                        os.kill(pid, signal.SIGKILL)
                        os.waitpid(pid, 0)


if __name__ == "__main__":
    if sys.argv[1].startswith("controller"):
        asyncio.run(controller(Path(sys.argv[2]), channel_loss=sys.argv[1] == "controller-loss"))
    else:
        subreaper(Path(sys.argv[2]), channel_loss=sys.argv[1] == "subreaper-channel-loss")
