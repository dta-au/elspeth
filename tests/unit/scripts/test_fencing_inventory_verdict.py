"""The fencing inventory tool's exit status gates: red is exit 1 unless --report-only.

The tool used to print violations and exit 0 unconditionally, so a shell that
read its exit status scored a red gate as green (U-46). These tests drive
``main()`` with a faked ``measure`` so they need no whole-tree scan.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "fencing_inventory.py"


@pytest.fixture(scope="module")
def tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fencing_inventory_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _green(tree: str = "/tree") -> dict[str, Any]:
    pin = {"live_count": 2, "live_digest": "d", "pinned_count": 2, "pinned_digest": "d", "rows": []}
    return {
        "tree": tree,
        "pins": {name: dict(pin) for name in ("dml_identities", "production_calls")},
        "write_shapes": {"live": [["runs", "insert"]], "pinned": [["runs", "insert"]]},
        "violations": {
            "escape": {"count": 0, "test": "test_escape", "rows": []},
            "caller_authority": {"count": 0, "test": "test_caller", "rows": []},
        },
    }


def _red_pin_count() -> dict[str, Any]:
    result = _green()
    result["pins"]["production_calls"]["live_count"] = 3
    return result


def _red_pin_digest() -> dict[str, Any]:
    result = _green()
    result["pins"]["dml_identities"]["live_digest"] = "moved"
    return result


def _red_write_shapes() -> dict[str, Any]:
    result = _green()
    result["write_shapes"]["live"] = [["runs", "insert"], ["runs", "delete"]]
    return result


def _red_violation() -> dict[str, Any]:
    result = _green()
    result["violations"]["escape"] = {"count": 33, "test": "test_escape", "rows": ["x"] * 33}
    return result


RED_CASES = [
    pytest.param(_red_pin_count, "pin production_calls", id="pin-count"),
    pytest.param(_red_pin_digest, "pin dml_identities", id="pin-digest"),
    pytest.param(_red_write_shapes, "DML write shapes", id="write-shapes"),
    pytest.param(_red_violation, "violations escape: 33", id="violation-set"),
]


def _run(tool: ModuleType, monkeypatch: pytest.MonkeyPatch, by_tree: dict[str, dict[str, Any]], argv: list[str]) -> int:
    def fake_measure(tree: Path) -> dict[str, Any]:
        return copy.deepcopy(by_tree[tree.name])

    monkeypatch.setattr(tool, "measure", fake_measure)
    exit_code: int = tool.main(argv)
    return exit_code


def test_green_tree_has_no_reasons_and_exits_zero(
    tool: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert tool.gate_verdict(_green()) == []
    assert _run(tool, monkeypatch, {"live": _green()}, ["live"]) == 0
    assert capsys.readouterr().out.rstrip().splitlines()[-1] == "GATE: GREEN"


@pytest.mark.parametrize(("build", "reason"), RED_CASES)
def test_each_failing_check_is_a_reason_and_exits_one(
    tool: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], build: Any, reason: str
) -> None:
    reasons = tool.gate_verdict(build())
    assert len(reasons) == 1 and reasons[0].startswith(reason)
    assert _run(tool, monkeypatch, {"live": build()}, ["live"]) == 1
    out = capsys.readouterr().out
    assert "GATE: RED" in out and reason in out and "REPORT ONLY" not in out


@pytest.mark.parametrize(("build", "reason"), RED_CASES)
def test_report_only_exits_zero_but_still_says_red(
    tool: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], build: Any, reason: str
) -> None:
    assert _run(tool, monkeypatch, {"live": build()}, ["live", "--report-only"]) == 0
    lines = capsys.readouterr().out.rstrip().splitlines()
    assert "GATE: RED" in lines and lines[-1] == "REPORT ONLY: exit status does not reflect the gate"


def test_baseline_tree_is_classified_against_never_gated(tool: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    by_tree = {"live": _green(), "base": _red_violation()}
    assert _run(tool, monkeypatch, by_tree, ["live", "--baseline", "base"]) == 0
    by_tree = {"live": _red_violation(), "base": _green()}
    assert _run(tool, monkeypatch, by_tree, ["live", "--baseline", "base"]) == 1
