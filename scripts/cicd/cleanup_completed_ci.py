#!/usr/bin/env python3
"""Run the reviewed host cleanup with an explicit run allowlist and one deleter.

This is a host-administration wrapper, not a new cleanup implementation. The
installed helper retains its listener guardian, process/container/mount checks,
inode checks and quarantine protocol. Dry-run is the default. Run it in the
host mount/PID namespaces as root after verifying the named GitHub runs ended.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import signal
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import FrameType, ModuleType

HELPER = Path("/usr/local/sbin/runner-workspace-cleanup")
REVIEWED_SHA256 = "7d293120f7ec54ce45bc8348f27a38696886f419dbef73b1ae5fa36b092a2a12"


class ReviewedCleanupHelper(ModuleType):
    """The narrow interface supplied by the exact reviewed host-helper bytes."""

    CHECKOUT: re.Pattern[str]
    QUARANTINE: re.Pattern[str]
    ThreadPoolExecutor: Callable[..., ThreadPoolExecutor]
    main: Callable[[], None]


def run_id(value: str) -> str:
    if re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise argparse.ArgumentTypeError("run IDs must be positive decimal integers")
    return value


def load_helper(path: Path) -> ReviewedCleanupHelper:
    source = path.read_bytes()
    if hashlib.sha256(source).hexdigest() != REVIEWED_SHA256:
        raise SystemExit("Installed cleanup helper changed; review it before updating the binding")
    helper = ReviewedCleanupHelper("reviewed_runner_workspace_cleanup")
    exec(compile(source, str(path), "exec"), helper.__dict__)
    return helper


def restrict_runs(helper: ReviewedCleanupHelper, allowed: list[str]) -> None:
    """Narrow both fresh candidates and leftover quarantine entries."""
    identifiers = "|".join(sorted({run_id(value) for value in allowed}))
    if not identifiers:
        raise ValueError("At least one completed run ID is required")
    checkout = rf"(?:ci|bp|mutation)-({identifiers})-[0-9]+-[A-Za-z0-9_.-]+\Z"
    quarantine = rf"(elspeth-nyx-[1-6])-[a-f0-9]{{32}}-((?:ci|bp|mutation)-(?:{identifiers})-[0-9]+-[A-Za-z0-9_.-]+)\Z"
    helper.CHECKOUT = re.compile(checkout)
    helper.QUARANTINE = re.compile(quarantine)


def serial_executor(*, max_workers: int) -> ThreadPoolExecutor:
    """The installed helper requests four; this administrative run permits one."""
    if max_workers != 4:
        raise ValueError("Unexpected installed cleanup executor request")
    return ThreadPoolExecutor(max_workers=1)


def interrupted(signum: int, _frame: FrameType | None) -> None:
    """Preserve the installed helper's normal SIGTERM/SIGHUP cleanup path."""
    raise SystemExit(128 + signum)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", action="append", type=run_id, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    helper = load_helper(HELPER)
    restrict_runs(helper, args.run_id)
    helper.ThreadPoolExecutor = serial_executor
    original_argv = sys.argv
    original_handlers = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        sys.argv = [str(HELPER), "--min-age-hours", "1", "--json"]
        if not args.execute:
            sys.argv.append("--dry-run")
        helper.main()
    finally:
        sys.argv = original_argv
        for sig, handler in original_handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    main()
