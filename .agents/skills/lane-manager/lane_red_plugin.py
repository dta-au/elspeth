"""Pytest plugin used by lane-manager to distinguish assertion failures from crashes.

RED means the runner reported a failed assertion. Exit codes and output text
cannot carry that fact: every runner reports an uncaught exception as exit 1,
and a test controls its own output.

This plugin wraps ``pytest_runtest_makereport`` and records every failed call
phase in the path named by ``LANE_MANAGER_RED_REPORT``. ``pytest.fail`` and an
``AssertionError`` raised in a test file count as assertions. Production-code
assertions, setup failures, teardown failures, and xfails do not.
"""

from __future__ import annotations

import fnmatch
import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest


def _raised_in(excinfo: pytest.ExceptionInfo[BaseException]) -> Path | None:
    try:
        entry = excinfo.traceback[-1]
        return Path(str(entry.path))
    except Exception:  # a frame without a file is unknown, so it is not treated as a test assertion
        return None


def _is_test_file(item: pytest.Item, path: Path | None) -> bool:
    if path is None:
        return False
    if path == item.path or path.name == "conftest.py":
        return True
    patterns = item.config.getini("python_files") or ["test_*.py", "*_test.py"]
    return any(fnmatch.fnmatch(path.name, pattern) for pattern in patterns)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[object]) -> Iterator[None]:
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or report.outcome != "failed":
        return
    path = os.environ.get("LANE_MANAGER_RED_REPORT")
    if not path:
        return
    excinfo = call.excinfo
    record: dict[str, object] = {"nodeid": item.nodeid, "type": "none", "assertion": False, "raised_in": None}
    if excinfo is not None:
        record["type"] = excinfo.type.__name__
        if excinfo.errisinstance(pytest.fail.Exception):
            record["assertion"] = True
        elif excinfo.errisinstance(AssertionError):
            raised_in = _raised_in(excinfo)
            record["raised_in"] = None if raised_in is None else str(raised_in)
            record["assertion"] = _is_test_file(item, raised_in)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
