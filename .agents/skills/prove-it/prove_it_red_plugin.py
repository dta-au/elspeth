"""pytest plugin that prove-it and lane-manager inject with ``-p prove_it_red_plugin``.

RED means the RUNNER reported a failed assertion. Exit codes and output text cannot carry that fact: every runner
reports an uncaught exception as exit 1 too, and a test controls its own output, so a test that prints
``AssertionError`` and then crashes looks identical from outside (found by three rounds of adversarial review).

This plugin wraps ``pytest_runtest_makereport`` and records, for every test whose CALL-phase REPORT has outcome
``failed``, the exception pytest caught and whether it counts as the test's own assertion:

- ``pytest.fail`` / a ``pytest.raises`` that did not raise (pytest's ``Failed`` outcome) — yes;
- an ``AssertionError`` raised in a test file (the item's file, any file matching the session's ``python_files``
  patterns, or a ``conftest.py``) — yes;
- an ``AssertionError`` raised anywhere else (production code that asserts, ADR-032 style) — no: the test asserted
  nothing itself;
- anything else — no.

Deciding from the REPORT's outcome, not from the exception seen in flight, is what keeps xfail out: a marked or
imperative xfail is reported ``skipped``, never ``failed`` (round four found the in-flight ``XFailed`` counted).
Setup and teardown errors are not ``call`` reports and are not recorded: an assertion in a fixture is not the
test's assertion.

Writes one JSON object per failure to the path in ``PROVE_IT_RED_REPORT``; does nothing when the variable is unset.

Scope: this is proof against MISTAKES, not fraud. The test runs in this process and could append a forged record
(the report path is in its environment); a test that does so is a review matter, not a measurement one.
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
    except Exception:  # a frame without a file (exec'd code, C extension): unknown, treated as not a test file
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
    path = os.environ.get("PROVE_IT_RED_REPORT")
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
