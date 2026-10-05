"""Collect evidence before an owned test subprocess is stopped."""

from __future__ import annotations

import faulthandler
import os
import signal
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from multiprocessing.process import BaseProcess
from pathlib import Path
from tempfile import TemporaryDirectory


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
                # into a pass. Keep it bounded even when the child is stuck.
                deadline = time.monotonic() + 0.2
                while time.monotonic() < deadline:
                    latest = Path(self.path).read_text(encoding="utf-8", errors="replace")
                    if len(latest) > len(trace):
                        trace = latest
                        break
                    time.sleep(0.01)
        return f"pid={process.pid} alive={alive} exitcode={process.exitcode} elapsed={time.monotonic() - self._started_at:.3f}s\n{trace}"

    def close(self) -> None:
        self._directory.cleanup()
