"""Observe actual stdio pipe ownership through the dedicated production owner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def runner(directory: Path) -> None:
    from elspeth.composer_mcp.runner import _authenticate_supervisor

    connection = _authenticate_supervisor()

    from elspeth.composer_mcp.portable_control import PortableProcessWatchdog
    from elspeth.contracts.process_watchdog_wire import BootstrapCompletionWitness

    owner = PortableProcessWatchdog(connection, threading.Event())
    token = b""
    while len(token) < 4:
        chunk = os.read(0, 4 - len(token))
        if not chunk:
            raise RuntimeError("Inherited runner input closed before positive control")
        token += chunk
    assert token == b"ping"
    os.write(1, b"pong")
    os.write(2, b"runner-inherited-stderr\n")
    # These are the runner's own copies. Keep its authenticated connection and
    # process alive after releasing both transport ends.
    sys.stdin.close()
    sys.stdout.close()
    os.close(0)
    os.close(1)
    (directory / "runner-closed.json").write_text(json.dumps({"pid": os.getpid()}))
    deadline = time.monotonic() + 12
    while not (directory / "release").exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("Outer stdio ownership probe did not release runner")
        time.sleep(0.01)
    os.write(2, b"runner-stderr-after-transport-close\n")
    owner.complete_bootstrap(BootstrapCompletionWitness(owner.target))


def probe(directory: Path) -> None:
    command = [
        sys.executable,
        str(ROOT / "src/elspeth/composer_mcp/portable_supervisor.py"),
        sys.executable,
        str(Path(__file__)),
        "runner",
        str(directory),
    ]
    with (directory / "stderr.log").open("wb") as errors:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
        assert process.stdin is not None and process.stdout is not None
        echoed = threading.Event()
        eof = threading.Event()
        output: list[bytes] = []

        def read_output() -> None:
            output.append(process.stdout.read(4))
            echoed.set()
            output.append(process.stdout.read(1))
            eof.set()

        reader = threading.Thread(target=read_output, name="owned-stdio-probe-reader", daemon=True)
        reader.start()
        try:
            process.stdin.write(b"ping")
            process.stdin.flush()
            deadline = time.monotonic() + 10
            while not (directory / "runner-closed.json").exists():
                assert process.poll() is None, "supervisor exited before ownership observation"
                if time.monotonic() >= deadline:
                    raise TimeoutError("Runner did not close inherited transport")
                time.sleep(0.01)
            assert echoed.wait(1) and output[0] == b"pong"
            before = process.poll()
            output_eof = eof.wait(0.5) and output[1] == b""
            try:
                os.write(process.stdin.fileno(), b"reader-probe")
            except BrokenPipeError:
                input_reader_gone = True
            else:
                input_reader_gone = False
            after = process.poll()
            observation = {
                "supervisor_alive_before": before is None,
                "supervisor_alive_after": after is None,
                "stdout_eof_while_alive": output_eof,
                "stdin_reader_gone_while_alive": input_reader_gone,
                "inherited_echo": output[0].decode(),
            }
            (directory / "observation.json").write_text(json.dumps(observation, indent=2))
            assert observation == {
                "supervisor_alive_before": True,
                "supervisor_alive_after": True,
                "stdout_eof_while_alive": True,
                "stdin_reader_gone_while_alive": True,
                "inherited_echo": "pong",
            }, observation
        finally:
            (directory / "release").write_text("settle the exact owned runner")
            # Closing a buffered writer after the successful loss-of-reader
            # observation can itself report the same expected broken pipe.
            with suppress(BrokenPipeError):
                process.stdin.close()
            status = process.wait(timeout=8)
            reader.join(timeout=2)
            assert not reader.is_alive()
            process.stdout.close()
            (directory / "status.json").write_text(json.dumps({"returncode": status}))
            assert status == 0
        assert (directory / "stderr.log").read_bytes() == b"runner-inherited-stderr\nrunner-stderr-after-transport-close\n"


if __name__ == "__main__":
    if sys.argv[1] == "runner":
        runner(Path(sys.argv[2]))
    else:
        probe(Path(sys.argv[2]))
