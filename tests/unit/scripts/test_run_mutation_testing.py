"""Regression coverage for the mutation-score gate's external CLI boundary."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from scripts import run_mutation_testing as mutation


def report(total: int, *, other: int = 0) -> str:
    """Use the testcase shape emitted by mutmut 2.x junitxml."""
    cases = [f'<testcase name="Mutant #{i}" file="src/elspeth/core/canonical.py" />' for i in range(1, total + 1)]
    cases.extend(
        f'<testcase name="Mutant #{i}" file="src/elspeth/core/landscape/exporter.py" />' for i in range(total + 1, total + other + 1)
    )
    return '<testsuites><testsuite name="mutmut">' + "".join(cases) + "</testsuite></testsuites>"


def invoke(
    monkeypatch: pytest.MonkeyPatch,
    *,
    killed: int = 19,
    total: int = 20,
    run_code: int = 2,
    xml: str | None = None,
    report_code: int = 0,
    results_code: int = 0,
    killed_output: str | None = None,
    strict: bool = True,
    all_modules: bool = False,
    killed_code: int = 0,
    module: str = "canonical.py",
) -> int:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_mutation_testing.py",
            *([] if strict else ["--no-clean"]),
            "--module",
            module,
            *(["--strict"] if strict else []),
            *(["--all"] if all_modules else []),
        ],
    )

    def run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        command = cmd[3]
        if command == "run":
            return subprocess.CompletedProcess(cmd, run_code)
        if command == "results":
            return subprocess.CompletedProcess(cmd, results_code, "Survived 🙁 (1)\n", "")
        if command == "junitxml":
            return subprocess.CompletedProcess(cmd, report_code, report(total, other=100) if xml is None else xml, "")
        if command == "result-ids":
            assert cmd[4] == "killed"
            return subprocess.CompletedProcess(
                cmd, killed_code, " ".join(map(str, range(1, killed + 1))) if killed_output is None else killed_output, ""
            )
        raise AssertionError(cmd)

    with patch.object(mutation, "clean_cache", autospec=True), patch.object(mutation.subprocess, "run", autospec=True, side_effect=run):
        return mutation.main()


@pytest.mark.parametrize(("killed", "expected"), [(18, 2), (19, 0), (20, 0)])
def test_strict_module_score_threshold(monkeypatch: pytest.MonkeyPatch, killed: int, expected: int) -> None:
    assert invoke(monkeypatch, killed=killed) == expected


@pytest.mark.parametrize("module", ["./canonical.py", "landscape/../canonical.py"])
def test_module_alias_cannot_lower_threshold(monkeypatch: pytest.MonkeyPatch, module: str) -> None:
    assert invoke(monkeypatch, killed=18, module=module) == 2


@pytest.mark.parametrize("run_code", [1, 3, 5, 15, 16, -9])
@pytest.mark.parametrize("strict", [False, True])
def test_run_errors_never_report_success(monkeypatch: pytest.MonkeyPatch, run_code: int, strict: bool) -> None:
    assert invoke(monkeypatch, run_code=run_code, strict=strict) == 1


@pytest.mark.parametrize("run_code", [0, 2, 4, 6, 8, 14])
def test_completed_mutant_statuses_are_scored(monkeypatch: pytest.MonkeyPatch, run_code: int) -> None:
    assert invoke(monkeypatch, run_code=run_code, killed=18) == 2


@pytest.mark.parametrize(
    "xml",
    [
        "",
        "not xml",
        "<testsuites/>",
        "<testsuites><testcase/></testsuites>",
        '<testsuites><testsuite><testcase name="Mutant #1" file="src/elspeth/core/else.py" /></testsuite></testsuites>',
    ],
)
def test_missing_or_invalid_score_fails_closed(monkeypatch: pytest.MonkeyPatch, xml: str) -> None:
    assert invoke(monkeypatch, xml=xml) == 1


@pytest.mark.parametrize("killed_output", ["invalid", "1 1", "0", "-1", "999"])
def test_invalid_killed_ids_fail_closed(monkeypatch: pytest.MonkeyPatch, killed_output: str) -> None:
    assert invoke(monkeypatch, killed_output=killed_output) == 1


def test_reporting_failure_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    assert invoke(monkeypatch, report_code=1) == 1
    assert invoke(monkeypatch, results_code=1) == 1
    assert invoke(monkeypatch, killed_code=1) == 1


def test_all_scores_each_module_and_preserves_earlier_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mutation, "MODULES", {"canonical": "canonical.py", "landscape/exporter": "landscape/exporter.py"})
    # The cache has 90% in canonical and 100% in exporter. An aggregate score
    # would falsely pass canonical; a later pass must not erase its failure.
    killed_ids = " ".join(map(str, [*range(1, 19), *range(21, 121)]))
    assert invoke(monkeypatch, killed_output=killed_ids, all_modules=True) == 2


def test_non_strict_survivors_are_not_an_infrastructure_error(monkeypatch: pytest.MonkeyPatch) -> None:
    assert invoke(monkeypatch, strict=False, run_code=2, killed=0) == 0


def test_zero_mutants_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    assert invoke(monkeypatch, killed=0, total=0) == 1


def test_missing_module_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(mutation, "CORE_PATH", tmp_path)
    assert invoke(monkeypatch) == 1


def test_clean_cache_removes_mutmut_sqlite_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cache = tmp_path / ".mutmut-cache"
    cache.write_bytes(b"cache")
    monkeypatch.setattr(mutation, "CACHE_PATH", cache)
    mutation.clean_cache()
    assert not cache.exists()


def test_strict_rejects_cached_proof_before_cache_or_subprocess(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["mutation", "--strict", "--no-clean"])
    with (
        patch.object(mutation, "clean_cache", autospec=True) as clean,
        patch.object(mutation.subprocess, "run", autospec=True, return_value=subprocess.CompletedProcess([], 0, "", "")) as run,
    ):
        assert mutation.main() == 1
        clean.assert_not_called()
        run.assert_not_called()
    assert "--strict requires fresh results" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("returncode", "ids", "expected"), [(1, "", 1), (0, "invalid", 1), (0, "0", 1), (0, "-1", 1), (0, "1 1", 1), (0, "", 0), (0, "1 2", 0)]
)
def test_survivor_reporting_admits_ids_and_propagates_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], returncode: int, ids: str, expected: int
) -> None:
    monkeypatch.setattr(sys, "argv", ["mutation", "--show-survivors"])
    responses = [subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], returncode, ids, "report error" if returncode else "")]
    with patch.object(mutation.subprocess, "run", autospec=True, side_effect=responses) as run:
        assert mutation.main() == expected
        assert run.call_args.args[0] == [sys.executable, "-m", "mutmut", "result-ids", "survived"]
    output = capsys.readouterr().out
    assert "all mutants were killed" not in output
    if expected == 0 and not ids:
        assert "No surviving mutants recorded" in output
    elif expected == 0:
        assert "1 2" in output
