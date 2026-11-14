"""Collect evidence before an owned test subprocess is stopped."""

from __future__ import annotations

import faulthandler
import os
import signal
import sys
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from multiprocessing.process import BaseProcess
from pathlib import Path
from tempfile import TemporaryDirectory


def linux_process_snapshot(pid: int | None) -> str:
    """Best-effort Linux kernel observations; fields are read independently."""
    fields = dict.fromkeys(("state", "sigpnd", "shdpnd", "wchan", "syscall", "threads"), "unreadable")
    if sys.platform != "linux" or pid is None:
        return "proc: " + " ".join(f"{key}={value}" for key, value in fields.items())
    root = Path(f"/proc/{pid}")
    try:
        status = dict(line.split(":", 1) for line in (root / "status").read_text().splitlines() if ":" in line)
        for source, target in (("State", "state"), ("SigPnd", "sigpnd"), ("ShdPnd", "shdpnd")):
            if source in status:
                fields[target] = status[source].strip()
    except (OSError, ValueError):
        pass  # The child can exit, or proc access can be denied, at any read.
    for name in ("wchan", "syscall"):
        try:
            value = (root / name).read_text().strip()
            fields[name] = value.split()[0] if value else "unreadable"
        except (OSError, ValueError):
            pass
    try:
        threads = []
        for task in sorted((root / "task").iterdir()):
            try:
                stat = (task / "stat").read_text()
                prefix, suffix = stat.rsplit(")", 1)
                comm = prefix.split("(", 1)[1]
                threads.append(f"{task.name}:{suffix.split()[0]}:{comm}")
            except (OSError, ValueError, IndexError):
                threads.append(f"{task.name}:unreadable")
        fields["threads"] = "[" + ",".join(threads) + "]" if threads else "unreadable"
    except OSError:
        pass
    return "proc: " + " ".join(f"{key}={value}" for key, value in fields.items())


@contextmanager
def trace_child_process(path: str) -> Iterator[Callable[[str], None]]:
    """Record target entry after spawn imports and enable child-only stack capture."""
    with Path(path).open("a", encoding="utf-8", buffering=1) as trace:
        if os.name == "posix":
            # faulthandler saves this harmless disposition and restores it
            # atomically on unregister. A late request must also be safe on
            # another live child thread during target teardown.
            signal.signal(signal.SIGUSR1, signal.SIG_IGN)
            faulthandler.register(signal.SIGUSR1, file=trace, all_threads=True)
            trace.write("stack signal ready\n")

        def phase(name: str) -> None:
            trace.write(f"phase={name} monotonic={time.monotonic():.6f}\n")

        phase("target entered (spawn imports complete)")
        try:
            yield phase
        except BaseException:
            trace.write(traceback.format_exc())
            raise
        finally:
            if os.name == "posix":
                faulthandler.unregister(signal.SIGUSR1)


class ProcessDiagnostics:
    """Parent-owned trace with a bounded stack snapshot before cleanup."""

    def __init__(self) -> None:
        self._directory = TemporaryDirectory(prefix="elspeth-child-trace-")
        self.path = str(Path(self._directory.name) / "trace.txt")
        self._started_at = time.monotonic()
        Path(self.path).write_text(f"phase=spawn/import pending monotonic={self._started_at:.6f}\n", encoding="utf-8")

    def snapshot(self, process: BaseProcess) -> str:
        alive = process.is_alive()
        trace = Path(self.path).read_text(encoding="utf-8", errors="replace")
        # Never signal a child still importing: SIGUSR1 has a fatal default
        # disposition until this child's context registers faulthandler.
        if alive and os.name == "posix" and "stack signal ready\n" in trace:
            pid = process.pid
            assert pid is not None, "started child has no pid"
            try:
                os.kill(pid, signal.SIGUSR1)
            except ProcessLookupError:
                pass  # The child exited between the observation and signal.
            else:
                # Diagnostic grace does not turn a missed readiness deadline
                # into a pass. Native faulthandler writes a dump in multiple
                # chunks; first growth does not mean the stack is complete.
                # Consume the existing grace, then reread even when the last
                # write lands at the deadline. Capture remains best effort if
                # the child cannot finish its dump within this bound.
                deadline = time.monotonic() + 0.2
                while (remaining := deadline - time.monotonic()) > 0:
                    time.sleep(min(0.01, remaining))
                trace = Path(self.path).read_text(encoding="utf-8", errors="replace")
        return (
            f"pid={process.pid} alive={alive} exitcode={process.exitcode} elapsed={time.monotonic() - self._started_at:.3f}s\n"
            f"{linux_process_snapshot(process.pid)}\n{trace}"
        )

    def close(self) -> None:
        self._directory.cleanup()
