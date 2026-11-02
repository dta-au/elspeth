"""Bounded, read-only directory probes used by work-at-risk.sh.

The default report is a diagnostic, not permission to exhaust the filesystem.
An incomplete walk fails closed; only a completed walk or an observed warning
threshold produces a number. No file contents are read.
"""

from __future__ import annotations

import argparse
import math
import os
import signal
import sys
from collections.abc import Generator
from pathlib import Path
from types import FrameType

PRUNED_NAMES = frozenset({".venv", "node_modules", "__pycache__", ".git"})


class ScanBudgetExceeded(Exception):
    """The probe cannot establish a complete result within its resource budget."""


def _expired(_signum: int, _frame: FrameType | None) -> None:
    raise ScanBudgetExceeded("elapsed-time budget exhausted")


def _top_roots(paths: list[str]) -> list[str]:
    roots = {os.path.abspath(path) for path in paths}
    result = []
    for root in sorted(roots):
        if not any(str(parent) in roots for parent in Path(root).parents):
            result.append(root)
    return result


def _entries(path: str) -> Generator[os.DirEntry[str], None, None]:
    with os.scandir(path) as entries:
        yield from entries


def scan_paths(roots: list[str], *, mode: str, exclude: set[str], max_entries: int, threshold: int) -> int:
    """Visit entries once, returning immediately at the requested threshold.

    scandir normally supplies directory types without an inode read. Source
    probes stat only matching __init__.py entries, not millions of ordinary
    files. Iterators stay bounded by tree depth rather than directory width.
    """
    total = visited = 0
    for root in _top_roots(roots):
        if os.path.islink(root):
            continue
        if mode == "sources" and (root in exclude or Path(root).name in PRUNED_NAMES):
            continue
        stack = [_entries(root)]
        try:
            while stack:
                try:
                    entry = next(stack[-1])
                except StopIteration:
                    stack.pop()
                    continue
                visited += 1
                if max_entries and visited > max_entries:
                    raise ScanBudgetExceeded(f"entry budget exhausted ({max_entries} entries)")
                if entry.is_dir(follow_symlinks=False):
                    if mode == "sources" and (entry.name in PRUNED_NAMES or entry.path in exclude):
                        continue
                    stack.append(_entries(entry.path))
                elif mode == "sources":
                    if (
                        entry.name == "__init__.py"
                        and entry.path.endswith("/src/elspeth/__init__.py")
                        and entry.is_file(follow_symlinks=False)
                    ):
                        total += 1
                        if threshold and total >= threshold:
                            return total
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
                    if total > threshold:
                        return total
        finally:
            for entries in stack:
                entries.close()
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("sources", "lanes"))
    parser.add_argument("roots", nargs="+")
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--max-entries", type=int, default=20_000)
    parser.add_argument("--max-seconds", type=float, default=2.0)
    parser.add_argument("--threshold", type=int, required=True)
    args = parser.parse_args()
    if args.max_entries < 0 or not math.isfinite(args.max_seconds) or args.max_seconds < 0 or args.threshold < 0:
        parser.error("limits must be finite and nonnegative; zero disables the corresponding limit")
    signal.signal(signal.SIGALRM, _expired)
    if args.max_seconds:
        signal.setitimer(signal.ITIMER_REAL, args.max_seconds)
    try:
        result = scan_paths(
            args.roots,
            mode=args.mode,
            exclude={os.path.abspath(path) for path in args.exclude},
            max_entries=args.max_entries,
            threshold=args.threshold,
        )
    except (OSError, ScanBudgetExceeded) as error:
        print(f"{args.mode} scan incomplete: {error}", file=sys.stderr)
        return 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
