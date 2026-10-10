"""Actual failed counter custody requires the existing external watchdog."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path


def test_failed_actual_counter_after_early_flag_keeps_watchdog_pending():
    child_source = Path(__file__).resolve().parents[2] / "helpers" / "finalizer_failed_counter_child.py"
    child = subprocess.Popen(
        [sys.executable, str(child_source)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = child.communicate(timeout=45)
        assert child.returncode == -signal.SIGKILL, stderr.decode()
        assert b"ACTUAL_NORMAL_SHUTDOWN_RECOVERY_ACK\n" in stdout
        assert b"ACTUAL_JOINED_GENERATION_FAILED_COUNTER_STAYS_UNKNOWN\n" in stdout
        assert b"FORBIDDEN_WATCHDOG_COMPLETE" not in stdout
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.communicate(timeout=5)
