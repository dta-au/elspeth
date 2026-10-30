"""Behavior tests for the trust-tier ratchet (``scripts/trust_tier_ratchet.py``).

Strict comparison retains its line-insensitive multiplicity policy. The local
report mode measures raw occurrence churn without adjudicating additions.
These tests exercise the comparison on synthetic findings; they never spawn the
real lint.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from scripts import trust_tier_ratchet as ratchet_module
from scripts.trust_tier_ratchet import (
    LINTS_SOURCE_ROOT,
    RULE_ID,
    SCAN_ROOT,
    VERIFY_MODE,
    VERIFY_MODE_ENV,
    FindingRecord,
    RatchetError,
    compare_corpora,
    format_report,
    lint_environment,
    normalise_path,
    parse_findings,
)

HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"


def test_local_report_hook_shows_successful_measurement_output() -> None:
    config = yaml.safe_load((Path(__file__).resolve().parents[3] / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    [hook] = [hook for repo in config["repos"] for hook in repo["hooks"] if hook["id"] == "elspeth-lints-trust-tier"]
    assert hook["entry"] == ".venv/bin/python -m scripts.trust_tier_ratchet --report-only"
    assert hook["verbose"] is True
    assert hook["language"] == "system"
    assert hook["pass_filenames"] is False


def _finding(
    path: str = "web/app.py", rule_id: str = "R5", message: str = "isinstance() used: x", *, line: int = 10, severity: str = "error"
) -> FindingRecord:
    return FindingRecord(path=path, rule_id=rule_id, message=message, line=line, column=4, severity=severity)


def test_ratchet_runs_the_same_rule_and_verify_mode_as_the_previous_entry() -> None:
    """The rule/shape mode stay fixed, with both selected-tree import roots."""
    env = lint_environment()

    assert RULE_ID == "trust_tier.tier_model"
    assert SCAN_ROOT == "src/elspeth"
    assert env["PYTHONPATH"] == "src:elspeth-lints/src"
    assert LINTS_SOURCE_ROOT == "elspeth-lints/src"
    assert env[VERIFY_MODE_ENV] == VERIFY_MODE == "shape-only-when-key-missing"
    assert VERIFY_MODE_ENV == "ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE"


def test_identical_corpora_pass() -> None:
    head = [_finding(), _finding(path="core/dag.py", rule_id="R6", message="Exception swallowed", line=40)]
    staged = list(head)

    result = compare_corpora(head, staged)

    assert result.exit_code == 0
    assert (result.head_count, result.staged_count, result.added_count, result.removed_count) == (2, 2, 0, 0)


def test_removed_only_passes_and_reports_the_removal() -> None:
    head = [_finding(), _finding(path="core/dag.py", rule_id="R6", message="Exception swallowed", line=40)]
    staged = [head[0]]

    result = compare_corpora(head, staged)

    assert result.exit_code == 0
    assert (result.head_count, result.staged_count, result.added_count, result.removed_count) == (2, 1, 0, 1)
    assert result.raw_removed == (head[1],)
    report = format_report(result, head_sha=HEAD_SHA)
    assert f"- {head[1].render()}" in report
    assert head[1].occurrence_json in report


def test_one_added_finding_fails_and_is_printed_verbatim() -> None:
    head = [_finding()]
    new = _finding(path="plugins/new.py", rule_id="R1", message="getattr() with default on owned type", line=7)
    staged = [*head, new]

    result = compare_corpora(head, staged)
    report = format_report(result, head_sha=HEAD_SHA)

    assert result.exit_code == 1
    assert (result.head_count, result.staged_count, result.added_count, result.removed_count) == (1, 2, 1, 0)
    assert f"+ {new.render()}" in report
    assert new.render() == "plugins/new.py:7:4: R1: getattr() with default on owned type"
    assert "FAIL" in report
    assert HEAD_SHA in report


def test_line_number_only_shift_passes() -> None:
    head = [_finding(line=10), _finding(path="core/dag.py", rule_id="R6", message="Exception swallowed", line=40)]
    staged = [_finding(line=13), _finding(path="core/dag.py", rule_id="R6", message="Exception swallowed", line=52)]

    result = compare_corpora(head, staged)

    assert result.exit_code == 0
    assert (result.added_count, result.removed_count) == (0, 0)


def test_a_second_occurrence_of_an_existing_key_counts_as_added() -> None:
    """Keys form a multiset: a duplicate of a standing finding is still a new finding."""
    head = [_finding(line=10)]
    staged = [_finding(line=10), _finding(line=90)]

    result = compare_corpora(head, staged)
    report = format_report(result, head_sha=HEAD_SHA)

    assert result.exit_code == 1
    assert result.added_count == 1
    assert result.added[0].head_multiplicity == 1
    assert result.added[0].staged_multiplicity == 2
    assert "already present 1x in HEAD, 2x working" in report
    assert f"+ {_finding(line=90).render()}" in report


def test_note_severity_findings_never_gate_but_are_counted() -> None:
    """Mirror the CLI: only non-note severities fail, so a new ``@trust_boundary`` suppression note passes."""
    head = [_finding()]
    staged = [*head, _finding(path="core/x.py", rule_id="R_TB_SUPPRESSED", message="@trust_boundary suppressed R5", severity="note")]

    result = compare_corpora(head, staged)

    assert result.exit_code == 0
    assert (result.head_notes, result.staged_notes, result.added_count) == (0, 1, 0)
    assert result.raw_added == (staged[1],)
    assert "REVIEW REQUIRED" in format_report(result, head_sha=HEAD_SHA, report_only=True)


def test_passing_report_says_ok_and_carries_the_counts() -> None:
    result = compare_corpora([_finding()], [_finding()])

    report = format_report(result, head_sha=HEAD_SHA)

    assert report.startswith(f"trust-tier ratchet: working tree vs HEAD {HEAD_SHA}\n")
    assert "head 1, working 1, added 0, removed 0" in report
    assert "OK: strict normalized multiplicities did not grow" in report
    assert "not A4's --rules all comparison" in report


def test_parse_findings_reads_the_cli_json_shape_and_relativises_absolute_paths(tmp_path: Path) -> None:
    tree_root = tmp_path / "tree"
    tree_root.mkdir()
    payload = (
        "["
        '{"column": 2, "file_path": "web/app.py", "fingerprint": "abc", "line": 5, "message": "m", "rule_id": "R5", '
        '"severity": "error", "suggestion": null},'
        f'{{"column": 0, "file_path": "{tree_root}/elspeth-lints/src/rule.py", "fingerprint": "def", "line": 0, '
        '"message": "Stale map entry", "rule_id": "trust_tier.tier_model", "severity": "error", "suggestion": null}'
        "]"
    )

    findings = parse_findings(payload, tree_root=tree_root)

    assert findings == [
        FindingRecord(path="web/app.py", rule_id="R5", message="m", line=5, column=2, severity="error"),
        FindingRecord(
            path="elspeth-lints/src/rule.py", rule_id="trust_tier.tier_model", message="Stale map entry", line=0, column=0, severity="error"
        ),
    ]


def test_absolute_path_outside_the_tree_is_left_alone(tmp_path: Path) -> None:
    assert normalise_path("/somewhere/else/x.py", tree_root=tmp_path) == "/somewhere/else/x.py"
    assert normalise_path("web/app.py", tree_root=tmp_path) == "web/app.py"


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        '{"file_path": "x"}',
        "[1]",
        '[{"column": 2, "file_path": "x", "line": 5, "message": "m", "rule_id": "R5", "fingerprint": "f"}]',
        '[{"column": 2, "file_path": 3, "line": 5, "message": "m", "rule_id": "R5", "severity": "error", "fingerprint": "f"}]',
        '[{"column": "2", "file_path": "x", "line": 5, "message": "m", "rule_id": "R5", "severity": "error", "fingerprint": "f"}]',
        '[{"column": 2, "file_path": "x", "line": 5, "message": "m", "rule_id": "R5", "severity": "error"}]',
        '[{"column": 2, "file_path": "x", "line": -1, "message": "m", "rule_id": "R5", "severity": "error", "fingerprint": "f"}]',
        '[{"column": 2, "file_path": "x", "line": true, "message": "m", "rule_id": "R5", "severity": "error", "fingerprint": "f"}]',
        '[{"column": 2, "file_path": "x", "line": 5, "message": "m", "rule_id": "R5", "severity": "invalid", "fingerprint": "f"}]',
        '[{"column": 2, "file_path": "x", "line": 5, "message": "m", "rule_id": "R5", "severity": "error", "fingerprint": "f", "extra": NaN}]',
        '[{"column": 2, "file_path": "x", "line": 5, "message": "m", "rule_id": "R5", "severity": "error", "fingerprint": "f", "extra": 1e309}]',
    ],
)
def test_parse_findings_refuses_malformed_documents(payload: str, tmp_path: Path) -> None:
    with pytest.raises(RatchetError):
        parse_findings(payload, tree_root=tmp_path)


def test_raw_identity_replacement_cannot_cancel_inside_the_normalized_summary(tmp_path: Path) -> None:
    row = {"file_path": "use.py", "line": 5, "column": 4, "rule_id": "R5", "message": "same", "severity": "error", "fingerprint": "old"}
    replacement = {**row, "fingerprint": "new", "ast_path": "body[2]/value", "scope_fingerprint": "new-scope"}
    before = parse_findings(json.dumps([row]), tree_root=tmp_path)
    after = parse_findings(json.dumps([replacement]), tree_root=tmp_path)
    result = compare_corpora(before, after)
    assert result.exit_code == 0  # The historical strict normalized policy is unchanged.
    assert result.added_count == result.removed_count == 0
    assert result.raw_added[0].occurrence_json == json.dumps(replacement, sort_keys=True)
    assert result.raw_removed[0].occurrence_json == json.dumps(row, sort_keys=True)
    report = format_report(result, head_sha=HEAD_SHA, report_only=True)
    assert "REVIEW REQUIRED" in report
    assert result.raw_added[0].occurrence_json in report and result.raw_removed[0].occurrence_json in report
    assert "not finding clearance or permission to land" in report


def test_raw_duplicate_multiplicity_is_not_reduced_to_a_set() -> None:
    original = _finding()
    result = compare_corpora([original], [original, original])
    assert result.added_count == 1
    assert result.raw_added == (original,)
    assert result.raw_removed == ()


@pytest.mark.parametrize(("arguments", "expected"), [([], 1), (["--strict"], 1), (["--report-only"], 0)])
def test_cli_report_mode_measures_additions_and_strict_mode_still_blocks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path, arguments: list[str], expected: int
) -> None:
    result = compare_corpora([], [_finding()])
    monkeypatch.setattr(ratchet_module, "ratchet", lambda *_args, **_kwargs: (result, HEAD_SHA))
    assert ratchet_module.main(["--repo-root", str(tmp_path), *arguments]) == expected
    output = capsys.readouterr().out
    assert "REVIEW REQUIRED" in output and _finding().occurrence_json in output
    assert ("MEASURED:" in output) is (arguments == ["--report-only"])


def test_report_mode_does_not_convert_a_failed_comparison_to_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise RatchetError("scan evidence unavailable")

    monkeypatch.setattr(ratchet_module, "ratchet", fail)
    assert ratchet_module.main(["--repo-root", str(tmp_path), "--report-only"]) == 2
    captured = capsys.readouterr()
    assert "scan evidence unavailable" in captured.err and "MEASURED:" not in captured.out


def test_keyless_reporter_refuses_operator_key_before_starting_any_comparison(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(ratchet_module.OPERATOR_KEY_ENV, "unit-test-sentinel")
    calls: list[object] = []
    monkeypatch.setattr(ratchet_module, "ratchet", lambda *_args, **_kwargs: calls.append("unexpected"))
    assert ratchet_module.main(["--repo-root", str(tmp_path), "--report-only"]) == 2
    assert calls == []


@pytest.mark.parametrize(
    ("returncode", "payload"),
    [
        (2, "[]"),
        (0, "not json"),
        (1, "[]"),
        (0, '[{"file_path":"use.py","line":1,"column":0,"rule_id":"R5","message":"m","severity":"error","fingerprint":"f"}]'),
        (1, '[{"file_path":"use.py","line":0,"column":0,"rule_id":"parse-error","message":"bad","severity":"error","fingerprint":"f"}]'),
        (1, '[{"file_path":"use.py","line":0,"column":0,"rule_id":"read-error","message":"bad","severity":"error","fingerprint":"f"}]'),
    ],
)
def test_failed_invalid_or_incomplete_scan_is_not_a_reportable_comparison(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, returncode: int, payload: str
) -> None:
    (tmp_path / SCAN_ROOT).mkdir(parents=True)
    monkeypatch.setattr(
        ratchet_module.subprocess, "run", lambda *_args, **_kwargs: subprocess.CompletedProcess([], returncode, payload, "diagnostic")
    )
    with pytest.raises(RatchetError):
        ratchet_module.run_tier_model_check(tmp_path, python="unused")


def test_cli_exit_one_with_valid_findings_is_a_completed_measurement(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / SCAN_ROOT).mkdir(parents=True)
    row = {"file_path": "use.py", "line": 1, "column": 0, "rule_id": "R5", "message": "m", "severity": "error", "fingerprint": "f"}
    monkeypatch.setattr(
        ratchet_module.subprocess, "run", lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, json.dumps([row]), "diagnostic")
    )
    findings, stderr = ratchet_module.run_tier_model_check(tmp_path, python="unused")
    assert len(findings) == 1 and findings[0].occurrence_json == json.dumps(row, sort_keys=True)
    assert stderr == "diagnostic"


@pytest.mark.parametrize("wrong_root", ["none", "source", "lints"])
def test_both_scan_import_roots_must_belong_to_the_selected_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, wrong_root: str) -> None:
    source = tmp_path / "src/elspeth/__init__.py"
    lints = tmp_path / "elspeth-lints/src/elspeth_lints/__init__.py"
    paths = [str(source), str(lints)]
    if wrong_root == "source":
        paths[0] = str(tmp_path.parent / "other/src/elspeth/__init__.py")
    elif wrong_root == "lints":
        paths[1] = str(tmp_path.parent / "other/elspeth-lints/src/elspeth_lints/__init__.py")
    monkeypatch.setattr(
        ratchet_module.subprocess, "run", lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, json.dumps(paths), "")
    )
    if wrong_root == "none":
        assert ratchet_module.verify_lints_import_path(tmp_path, python="unused") == lints
    else:
        with pytest.raises(RatchetError):
            ratchet_module.verify_lints_import_path(tmp_path, python="unused")


def test_missing_scan_root_refuses_before_starting_a_scan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[object] = []
    monkeypatch.setattr(ratchet_module.subprocess, "run", lambda *_args, **_kwargs: calls.append("unexpected"))
    with pytest.raises(RatchetError, match="scan root is missing"):
        ratchet_module.run_tier_model_check(tmp_path, python="unused")
    assert calls == []
