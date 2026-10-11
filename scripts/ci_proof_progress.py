"""Supervise one Linux CI proof process, including its collection time.

Usage: python scripts/ci_proof_progress.py --nodeid NODE --timeout-seconds N
       --report result.json -- python -m pytest ...

Heartbeats establish liveness, not proof progress. No AST stage is inferred.
The child inherits output and starts a new session. Descendants must remain in
that process group; deliberate session escapes are outside this supervisor's
contract. Linux subreaping lets us reap descendants when their parent exits.
CPU totals come from wait4 for the child and adopted descendants, including
CPU of descendants they themselves waited for; no live CPU estimate is made.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path

PHASE = "child-process-including-collection"
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37


def _positive_seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return seconds


def supervise(args: argparse.Namespace) -> int:
    """Run a standalone supervisor; only our child's process group is signalled."""
    # This guard includes the core's cleanup/restoration finally: any exceptional
    # supervisor exit invalidates terminal evidence, even after atomic publication.
    args.report.unlink(missing_ok=True)
    try:
        return _supervise(args)
    except BaseException:
        args.report.unlink(missing_ok=True)
        raise


def _supervise(args: argparse.Namespace) -> int:
    if sys.platform != "linux":
        raise RuntimeError("proof supervision requires Linux subreaping")
    libc = ctypes.CDLL(None, use_errno=True)
    prior_subreaper = ctypes.c_int()
    if libc.prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(prior_subreaper), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot read subreaper state")
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot enable subreaping")

    cancellation_signal: int | None = None
    publishing_result = False

    def cancelled(signum: int, _frame: object) -> None:
        nonlocal cancellation_signal
        if cancellation_signal is None:
            cancellation_signal = signum
        if publishing_result:
            # Cancellation during final publication cannot leave a green result
            # or return the already-computed successful exit status.
            args.report.unlink(missing_ok=True)
            raise SystemExit(128 + signum)

    handlers = {sig: signal.signal(sig, cancelled) for sig in (signal.SIGTERM, signal.SIGINT)}
    started = time.monotonic()
    child: subprocess.Popen[bytes] | None = None
    child_returncode: int | None = None
    user_cpu = 0.0
    system_cpu = 0.0
    outcome = "running"
    forced_kill = False
    cleanup_complete = False
    try:
        child = subprocess.Popen(args.command, start_new_session=True)
        pgid = child.pid
        next_heartbeat = started
        termination_started: float | None = None
        kill_started: float | None = None
        while True:
            now = time.monotonic()
            # Negative pid restricts waits to our group, including adopted children.
            while True:
                try:
                    pid, status, usage = os.wait4(-pgid, os.WNOHANG)
                except ChildProcessError:
                    break
                if pid == 0:
                    break
                user_cpu += usage.ru_utime
                system_cpu += usage.ru_stime
                if pid == child.pid:
                    child_returncode = os.waitstatus_to_exitcode(status)
                    child.returncode = child_returncode

            try:
                os.killpg(pgid, 0)
                group_alive = True
            except ProcessLookupError:
                group_alive = False

            if now >= next_heartbeat:
                print(f"proof nodeid={args.nodeid} phase={PHASE} elapsed_seconds={now - started:.3f} outcome={outcome}", flush=True)
                next_heartbeat = now + args.heartbeat_seconds

            if cancellation_signal is not None:
                outcome = "cancelled"
            elif outcome == "running" and now - started >= args.timeout_seconds:
                outcome = "timeout"

            if child_returncode is not None and not group_alive:
                cleanup_complete = True
                if outcome == "running":
                    outcome = "success" if child_returncode == 0 else "child-failed"
                break

            if outcome == "running" and child_returncode is not None:
                outcome = "abandoned-descendants"
            if outcome != "running" and termination_started is None:
                termination_started = now
                with suppress(ProcessLookupError):
                    os.killpg(pgid, cancellation_signal or signal.SIGTERM)
            if termination_started is not None and kill_started is None and now - termination_started >= args.grace_seconds:
                forced_kill = True
                kill_started = now
                with suppress(ProcessLookupError):
                    os.killpg(pgid, signal.SIGKILL)
            if kill_started is not None and now - kill_started >= args.grace_seconds:
                # An uninterruptible kernel task cannot be killed by a userspace
                # supervisor. Preserve that failure explicitly, never finalize green.
                outcome = "cleanup-incomplete"
                break
            time.sleep(min(0.05, args.heartbeat_seconds))

        publishing_result = True
        if cancellation_signal is not None:
            outcome = "cancelled"
            exit_status = 128 + cancellation_signal
        elif outcome == "timeout":
            exit_status = 124
        elif outcome in ("abandoned-descendants", "cleanup-incomplete"):
            exit_status = 125
        elif child_returncode is not None:
            exit_status = child_returncode if child_returncode >= 0 else 128 - child_returncode
        else:
            exit_status = 125
        report = {
            "nodeid": args.nodeid,
            "phase": PHASE,
            "outcome": outcome,
            "exit_status": exit_status,
            "child_returncode": child_returncode,
            "elapsed_seconds": time.monotonic() - started,
            "user_cpu_seconds": user_cpu,
            "system_cpu_seconds": system_cpu,
            "cpu_scope": "wait4-reaped-processes",
            "cancellation_signal": cancellation_signal,
            "forced_kill": forced_kill,
            "cleanup_complete": cleanup_complete,
        }
        temporary_report: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=args.report.parent, delete=False) as stream:
                temporary_report = Path(stream.name)
                stream.write(json.dumps(report, indent=2) + "\n")
            os.replace(temporary_report, args.report)
        finally:
            if temporary_report is not None:
                temporary_report.unlink(missing_ok=True)
        print("proof result=" + json.dumps(report, sort_keys=True), flush=True)
        return exit_status
    finally:
        if child is not None and not cleanup_complete:
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            reap_deadline = time.monotonic() + args.grace_seconds
            while time.monotonic() < reap_deadline:
                try:
                    pid, status, _usage = os.wait4(-child.pid, os.WNOHANG)
                except ChildProcessError:
                    break
                if pid == child.pid:
                    child.returncode = os.waitstatus_to_exitcode(status)
                if pid == 0:
                    time.sleep(0.01)
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        if libc.prctl(PR_SET_CHILD_SUBREAPER, prior_subreaper.value, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "cannot restore subreaper state")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodeid", required=True)
    parser.add_argument("--timeout-seconds", required=True, type=_positive_seconds)
    parser.add_argument("--heartbeat-seconds", default=30.0, type=_positive_seconds)
    parser.add_argument("--grace-seconds", default=10.0, type=_positive_seconds)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("a child command is required after --")
    return supervise(args)


if __name__ == "__main__":
    raise SystemExit(main())
