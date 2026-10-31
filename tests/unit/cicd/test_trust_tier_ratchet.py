"""Behavior tests for the trust-tier ratchet (``scripts/trust_tier_ratchet.py``).

Strict comparison retains its line-insensitive multiplicity policy. The local
report mode measures raw occurrence churn without adjudicating additions.
Synthetic findings cover comparison policy. Small isolated Git fixtures also
exercise the actual current lint engine against archived and working source.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
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


def test_ratchet_runs_the_same_rule_and_verify_mode_as_the_previous_entry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Rule/shape mode stay fixed; import roots are absolute engine paths."""
    monkeypatch.chdir(tmp_path)
    env = lint_environment()

    assert RULE_ID == "trust_tier.tier_model"
    assert SCAN_ROOT == "src/elspeth"
    assert env["PYTHONPATH"] == os.pathsep.join((str(tmp_path / "src"), str(tmp_path / LINTS_SOURCE_ROOT)))
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


@pytest.mark.parametrize("wrong_root", ["none", "source", "lints", "cli", "rule", "taxonomy", "policy"])
def test_both_scan_import_roots_must_belong_to_the_selected_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, wrong_root: str) -> None:
    paths = _probe_paths(tmp_path)
    labels = ("source", "lints", "cli", "rule", "taxonomy", "policy")
    if wrong_root != "none":
        index = labels.index(wrong_root)
        paths[index] = str(tmp_path.parent / "other" / Path(paths[index]).relative_to(tmp_path))
    monkeypatch.setattr(
        ratchet_module.subprocess, "run", lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, json.dumps(paths), "")
    )
    if wrong_root == "none":
        assert ratchet_module.verify_lints_import_path(tmp_path, python="unused") == Path(paths[1])
    else:
        with pytest.raises(RatchetError):
            ratchet_module.verify_lints_import_path(tmp_path, python="unused")


def test_missing_scan_root_refuses_before_starting_a_scan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[object] = []
    monkeypatch.setattr(ratchet_module.subprocess, "run", lambda *_args, **_kwargs: calls.append("unexpected"))
    with pytest.raises(RatchetError, match="scan root is missing"):
        ratchet_module.run_tier_model_check(tmp_path, python="unused")
    assert calls == []


def _probe_paths(root: Path) -> list[str]:
    package = root / LINTS_SOURCE_ROOT / "elspeth_lints"
    paths = [
        str(root / "src/elspeth/__init__.py"),
        str(package / "__init__.py"),
        str(package / "core/cli.py"),
        str(package / "rules/trust_tier/tier_model/rule.py"),
        str(package / "rules/trust_tier/tier_model/exception_taxonomy.py"),
        str(root / "config/cicd/enforce_tier_model"),
    ]
    for raw in paths[:-1]:
        path = Path(raw)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Import-probe fixture.\n", encoding="utf-8")
    Path(paths[-1]).mkdir(parents=True, exist_ok=True)
    return paths


@pytest.mark.parametrize("payload", ["not json", "null", "{}", "[]", '["one"]', '["one", 2]'])
def test_import_probe_refuses_malformed_or_incomplete_evidence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: str) -> None:
    monkeypatch.setattr(ratchet_module.subprocess, "run", lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, payload, ""))
    with pytest.raises(RatchetError):
        ratchet_module.verify_lints_import_path(tmp_path, python="unused")


def test_old_two_path_probe_does_not_prove_the_cli_taxonomy_or_data_policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    paths = _probe_paths(tmp_path)
    monkeypatch.setattr(
        ratchet_module.subprocess, "run", lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, json.dumps(paths[:2]), "")
    )
    with pytest.raises(RatchetError):
        ratchet_module.verify_lints_import_path(tmp_path, python="unused")


def test_failed_import_probe_is_not_measurement_evidence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    paths = _probe_paths(tmp_path)
    monkeypatch.setattr(
        ratchet_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 7, json.dumps(paths), "controlled import failure"),
    )
    with pytest.raises(RatchetError, match="controlled import failure"):
        ratchet_module.verify_lints_import_path(tmp_path, python="unused")


def test_key_refusal_precedes_every_native_child(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(ratchet_module.OPERATOR_KEY_ENV, "unit-test-sentinel")

    def unexpected_child(*_args: object, **_kwargs: object) -> None:
        pytest.fail("key-bearing reporter started a native child")

    monkeypatch.setattr(ratchet_module.subprocess, "run", unexpected_child)
    assert ratchet_module.main(["--repo-root", str(tmp_path), "--report-only"]) == 2


def _fixture_git(root: Path, *arguments: str) -> str:
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Ratchet test",
            "GIT_AUTHOR_EMAIL": "ratchet@example.invalid",
            "GIT_COMMITTER_NAME": "Ratchet test",
            "GIT_COMMITTER_EMAIL": "ratchet@example.invalid",
        }
    )
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    ).stdout.strip()


def _opposition_source(*, member_write: bool) -> str:
    write = "data['error'].__bases__ = (str,)" if member_write else "data['note'] = context"
    return (
        "class Refusal(Exception): pass\n"
        "def update(context):\n"
        "    data = {'error': Refusal}\n"
        f"    {write}\n"
        "def check(exc): return isinstance(exc, Refusal)\n"
    )


def _native_repo(tmp_path: Path, *, head_member_write: bool = True, head_policy: bool = True) -> tuple[Path, str]:
    """Only tests executing this fixture create or mutate its private Git tree."""
    root = tmp_path / "repo"
    source = root / SCAN_ROOT
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("", encoding="utf-8")
    (source / "subject.py").write_text(_opposition_source(member_write=head_member_write), encoding="utf-8")
    old_package = root / LINTS_SOURCE_ROOT / "elspeth_lints"
    old_package.mkdir(parents=True)
    (old_package / "__init__.py").write_text('raise RuntimeError("ARCHIVED_ENGINE_SELECTED")\n', encoding="utf-8")
    policy = root / "config/cicd/enforce_tier_model"
    if head_policy:
        policy.mkdir(parents=True)
        (policy / "_defaults.yaml").write_text("version: 1\ndefaults: {}\n", encoding="utf-8")
    _fixture_git(root, "init", "--quiet")
    _fixture_git(root, "add", ".")
    _fixture_git(root, "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "archived source and engine sentinel")
    head = _fixture_git(root, "rev-parse", "--verify", "HEAD^{commit}")

    # Disposable native-engine fixture, never a lane source tree or a checkout
    # of the real repository. Curated rule corpora and bytecode are not needed
    # by check mode; every actual module comes from the current source package.
    current_root = Path(__file__).resolve().parents[3]
    shutil.copytree(
        current_root / LINTS_SOURCE_ROOT,
        root / LINTS_SOURCE_ROOT,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("fixtures", "__pycache__"),
    )
    (source / "subject.py").write_text(_opposition_source(member_write=not head_member_write), encoding="utf-8")
    policy.mkdir(parents=True, exist_ok=True)
    (policy / "_defaults.yaml").write_text("version: 1\ndefaults: {}\n", encoding="utf-8")
    return root, head


def _fixture_inputs(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mode)
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".yaml"}
        and not {".git", ".pytest_cache", "__pycache__"}.intersection(path.relative_to(root).parts)
    }


def _native_cli(*, engine_root: Path, data_root: Path) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    assert ratchet_module.OPERATOR_KEY_ENV not in environment
    environment["PYTHONPATH"] = os.pathsep.join((str(engine_root / "src"), str(engine_root / LINTS_SOURCE_ROOT)))
    environment[VERIFY_MODE_ENV] = VERIFY_MODE
    return subprocess.run(
        [sys.executable, "-B", "-m", "elspeth_lints.core.cli", "check", "--rules", RULE_ID, "--root", SCAN_ROOT, "--format", "json"],
        cwd=data_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _r5_rows(completed: subprocess.CompletedProcess[str], data_root: Path) -> list[FindingRecord]:
    findings = ratchet_module.validate_scan_result(completed.stdout, returncode=completed.returncode, tree_root=data_root)
    return [finding for finding in findings if finding.rule_id == "R5"]


@pytest.mark.parametrize("head_member_write", [False, True])
def test_real_archive_uses_current_engine_and_preserves_source_opposition(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], head_member_write: bool
) -> None:
    root, head = _native_repo(tmp_path, head_member_write=head_member_write)
    before = _fixture_inputs(root)
    result = ratchet_module.main(["--repo-root", str(root), "--python", sys.executable, "--report-only"])
    captured = capsys.readouterr()
    assert result == 0, captured.err
    assert "ARCHIVED_ENGINE_SELECTED" not in captured.err
    assert head in captured.out and "MEASURED:" in captured.out
    changes = [line for line in captured.out.splitlines() if line.startswith(("- ", "+ ")) and ": R5:" in line]
    assert len(changes) == 1
    assert changes[0].startswith("- subject.py:" if head_member_write else "+ subject.py:")
    assert _fixture_inputs(root) == before


def test_real_archived_engine_and_wrong_data_controls_discriminate(tmp_path: Path) -> None:
    root, head = _native_repo(tmp_path)
    archived = tmp_path / "archive"
    archived.mkdir()
    assert ratchet_module.export_head(root, archived) == head
    before = _fixture_inputs(root), _fixture_inputs(archived)

    wrong_engine = _native_cli(engine_root=archived, data_root=archived)
    assert wrong_engine.returncode != 0
    assert "ARCHIVED_ENGINE_SELECTED" in wrong_engine.stderr
    archived_r5 = _r5_rows(_native_cli(engine_root=root, data_root=archived), archived)
    wrong_data_r5 = _r5_rows(_native_cli(engine_root=root, data_root=root), root)
    assert len(archived_r5) == 1
    assert wrong_data_r5 == []
    assert (archived / SCAN_ROOT / "subject.py").read_text(encoding="utf-8") == _opposition_source(member_write=True)
    assert _fixture_inputs(root) == before[0]
    assert _fixture_inputs(archived) == before[1]


def test_archived_policy_is_used_instead_of_current_policy(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, _head = _native_repo(tmp_path)
    (root / SCAN_ROOT / "subject.py").write_text(_opposition_source(member_write=True), encoding="utf-8")
    (root / "config/cicd/enforce_tier_model/current.yaml").write_text(
        "version: 1\nper_file_rules:\n  - pattern: subject.py\n    rules: [R5]\n"
        "    reason: Current fixture policy covers only the current source\n",
        encoding="utf-8",
    )
    before = _fixture_inputs(root)
    result = ratchet_module.main(["--repo-root", str(root), "--python", sys.executable, "--report-only"])
    captured = capsys.readouterr()
    assert result == 0, captured.err
    assert "- subject.py:" in captured.out and ": R5:" in captured.out
    assert _fixture_inputs(root) == before


def test_missing_archived_policy_cannot_fall_back_to_current_policy(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, _head = _native_repo(tmp_path, head_policy=False)
    before = _fixture_inputs(root)
    result = ratchet_module.main(["--repo-root", str(root), "--python", sys.executable, "--report-only"])
    captured = capsys.readouterr()
    assert result == 2
    assert "outside data tree" in captured.err
    assert "MEASURED:" not in captured.out
    assert _fixture_inputs(root) == before


def test_fixed_engine_source_change_between_scans_refuses_measurement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _head = _native_repo(tmp_path)
    taxonomy = root / LINTS_SOURCE_ROOT / "elspeth_lints/rules/trust_tier/tier_model/exception_taxonomy.py"
    original = taxonomy.read_bytes()
    native_run = subprocess.run
    mutated = False

    def mutate_after_native_scan(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal mutated
        completed = native_run(arguments, **kwargs)
        if "elspeth_lints.core.cli" in arguments and not mutated:
            taxonomy.write_bytes(original + b"\n# Fixture engine changed after the first native scan.\n")
            mutated = True
        return completed

    monkeypatch.setattr(ratchet_module.subprocess, "run", mutate_after_native_scan)
    result = ratchet_module.main(["--repo-root", str(root), "--python", sys.executable, "--report-only"])
    captured = capsys.readouterr()
    assert mutated is True
    assert result == 2
    assert "engine source changed" in captured.err
    assert "MEASURED:" not in captured.out


def test_export_archives_the_resolved_object_instead_of_a_moving_head(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    archive_bytes = io.BytesIO()
    payload = b"resolved object\n"
    with tarfile.open(fileobj=archive_bytes, mode="w") as archive:
        info = tarfile.TarInfo("object.txt")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    selected: list[str] = []

    def observe_command(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        if "rev-parse" in arguments:
            return subprocess.CompletedProcess(arguments, 0, HEAD_SHA + "\n", "")
        assert "archive" in arguments
        selected.append(arguments[-1])
        return subprocess.CompletedProcess(arguments, 0, archive_bytes.getvalue(), b"")

    monkeypatch.setattr(ratchet_module.subprocess, "run", observe_command)
    destination = tmp_path / "archive"
    destination.mkdir()
    assert ratchet_module.export_head(tmp_path, destination) == HEAD_SHA
    assert selected == [HEAD_SHA]
    assert (destination / "object.txt").read_bytes() == payload
