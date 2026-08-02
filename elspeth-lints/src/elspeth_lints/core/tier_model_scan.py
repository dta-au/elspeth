"""Shared single-file tier_model scan helper.

Promoted out of ``core/cli.py`` so every consumer -- ``cli.py`` (``justify`` and
``migrate-judge-scope``), ``bundle_verify.py``, and the ``elspeth-judge`` MCP
server -- runs the *one* scan call site and cannot drift on the scanner set.

This is a pure scan utility with no signing/HMAC state, which is why it is safe
to share; the security-critical signing helpers stay co-located in ``cli.py``.
The canonical scanner pair is bound here (imported from the tier_model
``rule`` module), so no call site supplies its own -- the same
``scan_file``/``scan_layer_imports_file`` family ``scan_for_rotations`` uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from elspeth_lints.core.allowlist import Allowlist, PerFileRule

if TYPE_CHECKING:
    from elspeth_lints.rules.trust_tier.tier_model.rotate import AmbiguousGroup, RotationPlan


@dataclass(frozen=True, slots=True)
class TargetCensus:
    """Disjoint accounting for one raw, empty-allowlist target scan."""

    raw_target_count: int
    exact_covered_count: int
    per_file_covered_count: int
    uncovered_count: int
    diagnosis_assigned_count: int = 0
    resign_assigned_count: int = 0


@dataclass(frozen=True, slots=True)
class TargetCoverage:
    """Canonical keys that already have authority in the target tree.

    ``exact_keys`` are live allowlist keys. ``diagnosis_assigned_keys`` are
    different live keys that the judge-signature diagnosis has paired with a
    drifted allowlist entry. Keeping the two sets separate in the API prevents
    one covered fingerprint from silently covering another finding with the
    same identity prefix.
    """

    exact_keys: frozenset[str]
    diagnosis_assigned_keys: frozenset[str] = frozenset()
    resign_assigned_keys: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class TargetCensusResult:
    """Raw findings plus their coverage classification."""

    census: TargetCensus
    findings: tuple[Any, ...]
    uncovered_findings: tuple[Any, ...]


def scan_single_file_findings(*, target_file: Path, root: Path) -> list[Any]:
    """Re-run both tier_model scanners against a single file.

    Merges the R1-R7 findings from ``scan_file`` with the layer-import
    violations + TC warnings from ``scan_layer_imports_file``. Mirrors the way
    ``scan_for_rotations`` combines them, so a downstream symbol-match pass sees
    the same finding set the CI run would see.
    """
    from elspeth_lints.rules.trust_tier.tier_model.rule import (
        scan_file,
        scan_layer_imports_file,
    )

    findings: list[Any] = list(scan_file(target_file, root))
    layer_violations, layer_tc = scan_layer_imports_file(target_file, root)
    findings.extend(layer_violations)
    findings.extend(layer_tc)
    return findings


def scan_tree_findings(*, root: Path) -> list[Any]:
    """Enumerate every tier-model target under ``root`` without allowlisting.

    File discovery and the scanner pair are shared with staging and signing so
    neither side can silently enumerate a narrower target set.
    """
    from elspeth_lints.rules.trust_tier.tier_model.rule import iter_scannable_python_files

    findings: list[Any] = []
    for target_file in sorted(iter_scannable_python_files(root)):
        findings.extend(scan_single_file_findings(target_file=target_file, root=root))
    return findings


def census_tree_targets(
    *,
    root: Path,
    findings: tuple[Any, ...] | None = None,
    covered_prefixes: frozenset[str] | set[str] | None = None,
    coverage: TargetCoverage | None = None,
    per_file_rules: list[PerFileRule],
) -> TargetCensusResult:
    """Run the raw scan, then classify every target against live coverage.

    New callers should supply ``coverage`` so coverage is exact at the
    canonical-finding-key level. ``covered_prefixes`` remains accepted for
    compatibility with older callers, but retains its legacy prefix-wide
    semantics and must not be used for completeness/security decisions.
    """
    from elspeth_lints.rules.trust_tier.tier_model.rotate import (
        _finding_covered_by_per_file_rule,
        identity_prefix,
    )

    if coverage is not None and covered_prefixes is not None:
        raise ValueError("provide coverage or covered_prefixes, not both")
    if coverage is None and covered_prefixes is None:
        raise ValueError("coverage is required")

    findings = tuple(scan_tree_findings(root=root)) if findings is None else findings
    seen: set[str] = set()
    uncovered: list[Any] = []
    exact_covered_count = 0
    diagnosis_assigned_count = 0
    resign_assigned_count = 0
    per_file_covered_count = 0
    for finding in findings:
        key = _finding_canonical_key(finding)
        if key in seen:
            raise ValueError(f"target census produced duplicate canonical key {key!r}")
        seen.add(key)
        if coverage is not None and key in coverage.exact_keys:
            exact_covered_count += 1
        elif coverage is not None and key in coverage.diagnosis_assigned_keys:
            diagnosis_assigned_count += 1
        elif coverage is not None and key in coverage.resign_assigned_keys:
            resign_assigned_count += 1
        elif covered_prefixes is not None and identity_prefix(key) in covered_prefixes:
            exact_covered_count += 1
        elif _finding_covered_by_per_file_rule(finding, per_file_rules):
            per_file_covered_count += 1
        else:
            uncovered.append(finding)
    return TargetCensusResult(
        census=TargetCensus(
            raw_target_count=len(findings),
            exact_covered_count=exact_covered_count,
            per_file_covered_count=per_file_covered_count,
            uncovered_count=len(uncovered),
            diagnosis_assigned_count=diagnosis_assigned_count,
            resign_assigned_count=resign_assigned_count,
        ),
        findings=findings,
        uncovered_findings=tuple(uncovered),
    )


def plan_non_judge_rotations(
    *,
    findings: tuple[Any, ...],
    allowlist: Allowlist,
    diagnosis_items: tuple[Any, ...],
) -> RotationPlan:
    """Plan rotations on the residual population after judge routing.

    Filtering judge-gated entries alone is insufficient: their live findings
    would remain and can create false ambiguity against pre-judge entries at
    the same identity prefix. Diagnosis supplies the authoritative live repair
    key for drifted signed entries; exact signed entries retain their own key.
    Remove only those assigned findings, then classify the genuinely
    non-judge population.
    """
    from elspeth_lints.rules.trust_tier.tier_model.rotate import plan_rotations

    live_keys = {_finding_canonical_key(finding) for finding in findings}
    diagnosis_by_key = {item.key: item for item in diagnosis_items}
    judge_assigned_finding_keys: set[str] = set()
    for entry in allowlist.entries:
        if entry.judge_verdict is None:
            continue
        diagnosis = diagnosis_by_key.get(entry.key)
        repair_key = None if diagnosis is None else diagnosis.repair_key
        if repair_key in live_keys:
            judge_assigned_finding_keys.add(repair_key)
        elif entry.key in live_keys:
            judge_assigned_finding_keys.add(entry.key)

    residual_findings = [finding for finding in findings if _finding_canonical_key(finding) not in judge_assigned_finding_keys]
    pre_judge_entries = [entry for entry in allowlist.entries if entry.judge_verdict is None]
    return plan_rotations(
        findings=residual_findings,
        allowlist_entries=pre_judge_entries,
        per_file_rules=allowlist.per_file_rules,
    )


def plan_judge_cleanup_groups(
    *,
    findings: tuple[Any, ...],
    allowlist: Allowlist,
    orphan_keys: frozenset[str],
) -> tuple[AmbiguousGroup, ...]:
    """Return exact live keys deferred behind signed-orphan cleanup.

    This is deliberately a cleanup-only grouping pass, not a rotation plan.
    Judge-gated orphan entries must be deleted before their same-prefix live
    findings can receive fresh judgments, so even a symmetric N:N population
    is a two-cycle cleanup group. Consumers assign the group's exact finding
    keys for this cycle; they must never rotate the signed entries or treat the
    identity prefix itself as coverage.
    """
    from collections import defaultdict

    from elspeth_lints.rules.trust_tier.tier_model.rotate import (
        AmbiguousGroup,
        _finding_covered_by_per_file_rule,
        identity_prefix,
    )

    orphan_entries = [entry for entry in allowlist.entries if entry.key in orphan_keys]
    if not orphan_entries:
        return ()

    entries_by_prefix: dict[str, list[Any]] = defaultdict(list)
    for entry in orphan_entries:
        entries_by_prefix[identity_prefix(entry.key)].append(entry)
    findings_by_prefix: dict[str, list[Any]] = defaultdict(list)
    for finding in findings:
        if _finding_covered_by_per_file_rule(finding, allowlist.per_file_rules):
            continue
        key = _finding_canonical_key(finding)
        prefix = identity_prefix(key)
        if prefix in entries_by_prefix:
            findings_by_prefix[prefix].append(finding)

    return tuple(
        AmbiguousGroup(
            prefix=prefix,
            finding_count=len(findings_by_prefix[prefix]),
            entry_count=len(entries),
            entry_keys=tuple(sorted(entry.key for entry in entries)),
            finding_keys=tuple(sorted(_finding_canonical_key(finding) for finding in findings_by_prefix[prefix])),
        )
        for prefix, entries in sorted(entries_by_prefix.items())
        if findings_by_prefix[prefix]
    )


def _finding_canonical_key(finding: Any) -> str:
    key = finding.canonical_key
    if callable(key):
        key = key()
    if not isinstance(key, str):
        raise ValueError(f"finding.canonical_key must be str; got {type(key).__name__}")
    return key
