"""Pins for the trust-tier pre-commit ratchet (``scripts/trust_tier_ratchet.py``).

The tier-model corpus is deliberately non-empty until the operator signs the
package, so the fail-closed CLI cannot be the pre-commit entry: it would refuse
every commit on its trigger paths, including ones that shrink the corpus. The
hook must therefore invoke the ratchet, and the ratchet's comparison must be
line-insensitive and one-directional: new findings block, removed ones do not.
These tests exercise the comparison on synthetic findings; they never spawn the
real lint.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
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

REPO_ROOT = Path(__file__).resolve().parents[3]
PRE_COMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
HOOK_ID = "elspeth-lints-trust-tier"
HOOK_ENTRY = ".venv/bin/python -m scripts.trust_tier_ratchet"
HOOK_TRIGGER = "^(config/cicd/enforce_tier_model/|elspeth-lints/src/elspeth_lints/rules/trust_tier/)"
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"


def _finding(
    path: str = "web/app.py", rule_id: str = "R5", message: str = "isinstance() used: x", *, line: int = 10, severity: str = "error"
) -> FindingRecord:
    return FindingRecord(path=path, rule_id=rule_id, message=message, line=line, column=4, severity=severity)


def _trust_tier_hook() -> dict[str, object]:
    payload = yaml.safe_load(PRE_COMMIT_CONFIG.read_text(encoding="utf-8"))
    hooks = [hook for repo in payload["repos"] for hook in repo.get("hooks", ()) if hook["id"] == HOOK_ID]
    assert len(hooks) == 1, f"expected exactly one {HOOK_ID} hook, found {len(hooks)}"
    hook = hooks[0]
    assert isinstance(hook, dict)
    return hook


def test_pre_commit_trust_tier_hook_invokes_the_ratchet_script() -> None:
    """The hook's entry is the ratchet, not the fail-closed CLI, with its trigger and scope unchanged."""
    hook = _trust_tier_hook()

    assert hook["entry"] == HOOK_ENTRY
    assert hook["language"] == "system"
    assert hook["pass_filenames"] is False
    assert hook["files"] == HOOK_TRIGGER
    assert "types" not in hook
    assert (REPO_ROOT / "scripts" / "trust_tier_ratchet.py").is_file()


def test_ratchet_runs_the_same_rule_and_verify_mode_as_the_previous_entry() -> None:
    """The ratchet reproduces the previous entry's arguments and environment; it adds no key handling."""
    env = lint_environment()

    assert RULE_ID == "trust_tier.tier_model"
    assert SCAN_ROOT == "src/elspeth"
    assert env["PYTHONPATH"] == LINTS_SOURCE_ROOT == "elspeth-lints/src"
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
    assert "already present 1x in HEAD, 2x staged" in report
    assert f"+ {_finding(line=90).render()}" in report


def test_note_severity_findings_never_gate_but_are_counted() -> None:
    """Mirror the CLI: only non-note severities fail, so a new ``@trust_boundary`` suppression note passes."""
    head = [_finding()]
    staged = [*head, _finding(path="core/x.py", rule_id="R_TB_SUPPRESSED", message="@trust_boundary suppressed R5", severity="note")]

    result = compare_corpora(head, staged)

    assert result.exit_code == 0
    assert (result.head_notes, result.staged_notes, result.added_count) == (0, 1, 0)


def test_passing_report_says_ok_and_carries_the_counts() -> None:
    result = compare_corpora([_finding()], [_finding()])

    report = format_report(result, head_sha=HEAD_SHA)

    assert report.startswith(f"trust-tier ratchet: working tree vs HEAD {HEAD_SHA}\n")
    assert "head 1, staged 1, added 0, removed 0" in report
    assert report.rstrip().endswith("OK: the staged tree adds no trust-tier finding relative to HEAD.")


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
        '[{"column": 2, "file_path": "x", "line": 5, "message": "m", "rule_id": "R5"}]',
        '[{"column": 2, "file_path": 3, "line": 5, "message": "m", "rule_id": "R5", "severity": "error"}]',
        '[{"column": "2", "file_path": "x", "line": 5, "message": "m", "rule_id": "R5", "severity": "error"}]',
    ],
)
def test_parse_findings_refuses_malformed_documents(payload: str, tmp_path: Path) -> None:
    with pytest.raises(RatchetError):
        parse_findings(payload, tree_root=tmp_path)
