"""Regression coverage for complete tier-model target census accounting."""

from pathlib import Path

from elspeth_lints.core import tier_model_scan
from elspeth_lints.rules.trust_tier.tier_model.rotate import identity_prefix
from elspeth_lints.rules.trust_tier.tier_model.rule import Finding


def _finding(fingerprint: str) -> Finding:
    return Finding(
        rule_id="R1",
        file_path="plugins/widget.py",
        line=4,
        col=16,
        symbol_context=("Widget", "lookup"),
        fingerprint=fingerprint,
        code_snippet="payload.get('value')",
        message="test finding",
    )


def test_exact_coverage_does_not_cover_a_distinct_same_prefix_finding(
    monkeypatch,
    tmp_path: Path,
) -> None:
    covered = _finding("aaa")
    uncovered = _finding("bbb")
    monkeypatch.setattr(
        tier_model_scan,
        "scan_tree_findings",
        lambda *, root: [covered, uncovered],
    )

    result = tier_model_scan.census_tree_targets(
        root=tmp_path,
        coverage=tier_model_scan.TargetCoverage(
            exact_keys=frozenset({covered.canonical_key}),
        ),
        per_file_rules=[],
    )

    assert result.census.raw_target_count == 2
    assert result.census.exact_covered_count == 1
    assert result.census.diagnosis_assigned_count == 0
    assert result.census.resign_assigned_count == 0
    assert result.census.per_file_covered_count == 0
    assert result.census.uncovered_count == 1
    assert result.uncovered_findings == (uncovered,)


def test_diagnosis_assignment_is_accounted_separately_from_exact_coverage(
    monkeypatch,
    tmp_path: Path,
) -> None:
    exact = _finding("aaa")
    drift_assigned = _finding("bbb")
    monkeypatch.setattr(
        tier_model_scan,
        "scan_tree_findings",
        lambda *, root: [exact, drift_assigned],
    )

    result = tier_model_scan.census_tree_targets(
        root=tmp_path,
        coverage=tier_model_scan.TargetCoverage(
            exact_keys=frozenset({exact.canonical_key}),
            diagnosis_assigned_keys=frozenset({drift_assigned.canonical_key}),
        ),
        per_file_rules=[],
    )

    assert result.census.raw_target_count == 2
    assert result.census.exact_covered_count == 1
    assert result.census.diagnosis_assigned_count == 1
    assert result.census.resign_assigned_count == 0
    assert result.census.per_file_covered_count == 0
    assert result.census.uncovered_count == 0
    assert result.uncovered_findings == ()


def test_resign_assignment_is_exact_and_does_not_cover_same_prefix_peer(
    monkeypatch,
    tmp_path: Path,
) -> None:
    assigned = _finding("aaa")
    uncovered = _finding("bbb")
    monkeypatch.setattr(
        tier_model_scan,
        "scan_tree_findings",
        lambda *, root: [assigned, uncovered],
    )

    result = tier_model_scan.census_tree_targets(
        root=tmp_path,
        coverage=tier_model_scan.TargetCoverage(
            exact_keys=frozenset(),
            resign_assigned_keys=frozenset({assigned.canonical_key}),
        ),
        per_file_rules=[],
    )

    assert result.census.raw_target_count == 2
    assert result.census.resign_assigned_count == 1
    assert result.census.uncovered_count == 1
    assert result.uncovered_findings == (uncovered,)


def test_legacy_prefix_coverage_remains_accepted(monkeypatch, tmp_path: Path) -> None:
    finding = _finding("aaa")
    monkeypatch.setattr(
        tier_model_scan,
        "scan_tree_findings",
        lambda *, root: [finding],
    )

    result = tier_model_scan.census_tree_targets(
        root=tmp_path,
        covered_prefixes={identity_prefix(finding.canonical_key)},
        per_file_rules=[],
    )

    assert result.census.exact_covered_count == 1
    assert result.census.uncovered_count == 0
