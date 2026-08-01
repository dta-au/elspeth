"""Regression tests for the repo-wide permanent multi-rule blanket ratchet."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from elspeth_lints.core.cli import main

_LEGACY_BLANKET = """\
per_file_rules:
  - pattern: "core/*.py"
    rules: [R1, R5, R6]
    reason: "legacy defensive cluster"
    expires: null
    max_hits: 5
"""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_repo(tmp_path: Path, baseline_yaml: str) -> tuple[Path, str]:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "ratchet@example.invalid")
    _git(tmp_path, "config", "user.name", "Ratchet Test")
    allowlist_root = tmp_path / "config" / "cicd"
    target = allowlist_root / "enforce_example" / "allowlist.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(baseline_yaml, encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "baseline")
    return allowlist_root, _git(tmp_path, "rev-parse", "HEAD")


def _check(*, allowlist_root: Path, baseline_ref: str, repo_root: Path):
    try:
        from elspeth_lints.core.per_file_blanket_ratchet import check_per_file_blanket_ratchet
    except ModuleNotFoundError:
        pytest.fail("per-file blanket ratchet is not implemented")
    return check_per_file_blanket_ratchet(
        allowlist_root=allowlist_root,
        baseline_ref=baseline_ref,
        repo_root=repo_root,
    )


def test_new_permanent_multi_rule_blanket_is_rejected(tmp_path: Path) -> None:
    allowlist_root, baseline = _init_repo(tmp_path, "per_file_rules: []\n")
    (allowlist_root / "enforce_example" / "allowlist.yaml").write_text(
        """\
per_file_rules:
  - pattern: "core/*.py"
    rules: [R1, R6]
    reason: "legacy defensive cluster"
    expires: null
""",
        encoding="utf-8",
    )

    report = _check(allowlist_root=allowlist_root, baseline_ref=baseline, repo_root=tmp_path)

    assert report.head_blanket_count == 1
    assert report.grandfathered_count == 0
    assert len(report.violations) == 1
    assert report.violations[0].pattern == "core/*.py"
    assert report.violations[0].reason == "new permanent multi-rule blanket"


def test_cli_reports_policy_failure_without_treating_it_as_a_broken_measurement(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    allowlist_root, baseline = _init_repo(tmp_path, "per_file_rules: []\n")
    (allowlist_root / "enforce_example" / "allowlist.yaml").write_text(
        """\
per_file_rules:
  - pattern: "core/*.py"
    rules: [R1, R6]
    reason: "legacy defensive cluster"
""",
        encoding="utf-8",
    )

    try:
        exit_code = main(
            [
                "check-per-file-blanket-ratchet",
                "--allowlist-root",
                str(allowlist_root),
                "--baseline-ref",
                baseline,
                "--repo-root",
                str(tmp_path),
            ]
        )
    except SystemExit as exc:
        exit_code = exc.code

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "1 permanent multi-rule blanket(s) at HEAD" in output
    assert "core/*.py" in output


@pytest.mark.parametrize(
    "head_yaml, expected_head_count",
    (
        (_LEGACY_BLANKET, 1),
        (_LEGACY_BLANKET.replace("[R1, R5, R6]", "[R1, R5]"), 1),
        (_LEGACY_BLANKET.replace("max_hits: 5", "max_hits: 4"), 1),
        (_LEGACY_BLANKET.replace("expires: null", "expires: 2026-12-31"), 0),
        (_LEGACY_BLANKET.replace("[R1, R5, R6]", "[R1]"), 0),
        ("per_file_rules: []\n", 0),
    ),
)
def test_existing_blanket_may_only_stay_equivalent_or_narrower(
    tmp_path: Path,
    head_yaml: str,
    expected_head_count: int,
) -> None:
    allowlist_root, baseline = _init_repo(tmp_path, _LEGACY_BLANKET)
    (allowlist_root / "enforce_example" / "allowlist.yaml").write_text(head_yaml, encoding="utf-8")

    report = _check(allowlist_root=allowlist_root, baseline_ref=baseline, repo_root=tmp_path)

    assert report.head_blanket_count == expected_head_count
    assert report.violations == ()


@pytest.mark.parametrize(
    "head_yaml",
    (
        _LEGACY_BLANKET.replace("[R1, R5, R6]", "[R1, R2, R5, R6]"),
        _LEGACY_BLANKET.replace("max_hits: 5", "max_hits: 6"),
        _LEGACY_BLANKET.replace("max_hits: 5", "max_hits: null"),
        _LEGACY_BLANKET.replace('pattern: "core/*.py"', 'pattern: "core/config.py"'),
    ),
)
def test_existing_blanket_cannot_broaden_or_change_target(tmp_path: Path, head_yaml: str) -> None:
    allowlist_root, baseline = _init_repo(tmp_path, _LEGACY_BLANKET)
    (allowlist_root / "enforce_example" / "allowlist.yaml").write_text(head_yaml, encoding="utf-8")

    report = _check(allowlist_root=allowlist_root, baseline_ref=baseline, repo_root=tmp_path)

    assert len(report.violations) == 1


def test_one_baseline_blanket_cannot_grandfather_a_duplicate(tmp_path: Path) -> None:
    allowlist_root, baseline = _init_repo(tmp_path, _LEGACY_BLANKET)
    duplicate = _LEGACY_BLANKET.replace(
        "    max_hits: 5\n",
        '    max_hits: 5\n  - pattern: "core/*.py"\n    rules: [R1, R5]\n    reason: "duplicate"\n    max_hits: 4\n',
    )
    (allowlist_root / "enforce_example" / "allowlist.yaml").write_text(duplicate, encoding="utf-8")

    report = _check(allowlist_root=allowlist_root, baseline_ref=baseline, repo_root=tmp_path)

    assert report.grandfathered_count == 1
    assert len(report.violations) == 1


def test_touching_source_covered_by_legacy_blanket_is_rejected(tmp_path: Path) -> None:
    allowlist_root, baseline = _init_repo(tmp_path, _LEGACY_BLANKET)
    source = tmp_path / "src" / "elspeth" / "core" / "service.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    _git(tmp_path, "add", str(source.relative_to(tmp_path)))
    _git(tmp_path, "commit", "-qm", "touch covered source")

    report = _check(allowlist_root=allowlist_root, baseline_ref=baseline, repo_root=tmp_path)

    assert report.grandfathered_count == 1
    assert len(report.violations) == 1
    assert report.violations[0].touched_file == "src/elspeth/core/service.py"
    assert report.violations[0].reason == "touched source file remains under permanent multi-rule blanket"


def test_recursive_scan_catches_non_enforce_allowlist_directories(tmp_path: Path) -> None:
    allowlist_root, baseline = _init_repo(tmp_path, "per_file_rules: []\n")
    nested = allowlist_root / "test_to_source_mapping" / "nested" / "allowlist.yaml"
    nested.parent.mkdir(parents=True)
    nested.write_text(_LEGACY_BLANKET, encoding="utf-8")

    report = _check(allowlist_root=allowlist_root, baseline_ref=baseline, repo_root=tmp_path)

    assert len(report.violations) == 1
    assert report.violations[0].source_file.endswith("test_to_source_mapping/nested/allowlist.yaml")


def test_duplicate_rule_ids_are_a_broken_measurement(tmp_path: Path) -> None:
    from elspeth_lints.core.per_file_blanket_ratchet import PerFileBlanketRatchetError

    allowlist_root, baseline = _init_repo(tmp_path, "per_file_rules: []\n")
    (allowlist_root / "enforce_example" / "allowlist.yaml").write_text(
        _LEGACY_BLANKET.replace("[R1, R5, R6]", "[R1, R1]"),
        encoding="utf-8",
    )

    with pytest.raises(PerFileBlanketRatchetError, match="duplicate rule ids"):
        _check(allowlist_root=allowlist_root, baseline_ref=baseline, repo_root=tmp_path)


def test_judge_coverage_and_blanket_ratchet_share_duplicate_rule_validation() -> None:
    from elspeth_lints.core.allowlist_io import AllowlistIOError
    from elspeth_lints.core.judge_coverage import _parse_per_file_rules_for_coverage

    data = {
        "per_file_rules": [
            {
                "pattern": "core/*.py",
                "rules": ["R1", "R1"],
                "reason": "duplicate should never create two semantic grants",
            }
        ]
    }

    with pytest.raises(AllowlistIOError, match="duplicate rule ids"):
        _parse_per_file_rules_for_coverage(data, source_file="allowlist.yaml")
