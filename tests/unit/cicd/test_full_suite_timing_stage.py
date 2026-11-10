"""The local timing stage remains serial and produces its own JUnit evidence."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "full-suite-gate.sh"


def _assert_timing_stage_contract(source: str) -> None:
    assert 'STAGES="ruff,pytest";' in source
    assert 'case "$s" in ruff|mypy|contracts|lints|pytest|timing|testcontainer)' in source
    assignments = [line for line in source.splitlines() if line.startswith("CMD_OF[timing]=")]
    assert len(assignments) == 1
    command = assignments[0].removeprefix('CMD_OF[timing]="').removesuffix('"')
    args = shlex.split(command)
    assert args[:4] == ["$PY", "-m", "pytest", "tests/"]
    assert args.count("-m") == 2  # Python module invocation and pytest marker selection.
    assert args[args.index("-m", 2) + 1] == "timing_sensitive"
    assert args.count("-n") == 1
    assert args[args.index("-n") + 1] == "0"
    assert "--junitxml=<run-dir>/timing-junit.xml" in args
    assert "$PYTEST_ARGS" not in args


def test_local_timing_stage_is_serial_and_keeps_ordinary_defaults() -> None:
    _assert_timing_stage_contract(SCRIPT.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ('STAGES="ruff,pytest";', 'STAGES="ruff,pytest,timing";'),
        ("pytest|timing|testcontainer)", "pytest|testcontainer)"),
        ("-m timing_sensitive -n 0", "-m timing_sensitive -n 12"),
        ("-m timing_sensitive -n 0", "-m integration -n 0"),
        ("--junitxml=<run-dir>/timing-junit.xml", ""),
    ],
    ids=["default-stage-drift", "stage-rejected", "parallel", "wrong-selection", "missing-junit"],
)
def test_local_timing_stage_contract_rejects_broken_fixture(original: str, replacement: str) -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    _assert_timing_stage_contract(source)
    assert original in source
    with pytest.raises(AssertionError):
        _assert_timing_stage_contract(source.replace(original, replacement))
