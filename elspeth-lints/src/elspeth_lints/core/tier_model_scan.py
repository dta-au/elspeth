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
from elspeth_lints.core.ast_walker import PythonFileReadError, PythonSyntaxError, parse_python_file

if TYPE_CHECKING:
    from elspeth_lints.rules.trust_tier.tier_model.rotate import RotationPlan


@dataclass(frozen=True, slots=True)
class TargetCensus:
    """Disjoint accounting for one raw, empty-allowlist target scan."""

    raw_target_count: int
    exact_covered_count: int
    per_file_covered_count: int
    uncovered_count: int


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

    _require_scannable_python_file(target_file=target_file, root=root)
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
    covered_keys: frozenset[str] | set[str],
    per_file_rules: list[PerFileRule],
) -> TargetCensusResult:
    """Run the raw scan, then classify every target against live coverage."""
    from elspeth_lints.rules.trust_tier.tier_model.rotate import _finding_covered_by_per_file_rule

    findings = tuple(scan_tree_findings(root=root))
    seen: set[str] = set()
    uncovered: list[Any] = []
    exact_covered_count = 0
    per_file_covered_count = 0
    for finding in findings:
        key = _finding_canonical_key(finding)
        if key in seen:
            raise ValueError(f"target census produced duplicate canonical key {key!r}")
        seen.add(key)
        if key in covered_keys:
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
        ),
        findings=findings,
        uncovered_findings=tuple(uncovered),
    )


def diagnosis_deferred_prefixes(diagnosis_items: tuple[Any, ...]) -> frozenset[str]:
    """Return identity groups reserved for an outstanding diagnosis action.

    Exact allowlist matching remains fingerprint-specific. This separate
    routing set prevents a drift repair or stale-delete cycle from also
    creating a fresh judgment for the same identity in one bundle.
    """
    from elspeth_lints.rules.trust_tier.tier_model.rotate import identity_prefix

    deferred: set[str] = set()
    for item in diagnosis_items:
        if not item.requires_action:
            continue
        try:
            deferred.add(identity_prefix(item.key))
        except ValueError:
            continue
    return frozenset(deferred)


def routable_new_judgment_findings(
    *,
    uncovered_findings: tuple[Any, ...],
    diagnosis_items: tuple[Any, ...],
    rotation_plan: RotationPlan,
) -> tuple[Any, ...]:
    """Select uncovered findings not reserved for another authority lane."""
    from elspeth_lints.rules.trust_tier.tier_model.rotate import identity_prefix

    deferred_prefixes = diagnosis_deferred_prefixes(diagnosis_items)
    rotation_keys = {rotation.new_key for rotation in rotation_plan.rotations}
    return tuple(
        finding
        for finding in uncovered_findings
        if _finding_canonical_key(finding) not in rotation_keys
        and identity_prefix(_finding_canonical_key(finding)) not in deferred_prefixes
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


def _finding_canonical_key(finding: Any) -> str:
    key = finding.canonical_key
    if callable(key):
        key = key()
    if not isinstance(key, str):
        raise ValueError(f"finding.canonical_key must be str; got {type(key).__name__}")
    return key


def _require_scannable_python_file(*, target_file: Path, root: Path) -> None:
    """Fail closed when an authority-bearing scan cannot parse one target."""
    parsed = parse_python_file(target_file)
    try:
        display_path = target_file.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        display_path = str(target_file)
    if isinstance(parsed, PythonSyntaxError):
        raise ValueError(f"tier-model scan failed for {display_path}: syntax error at {parsed.line}:{parsed.column}: {parsed.message}")
    if isinstance(parsed, PythonFileReadError):
        raise ValueError(f"tier-model scan failed for {display_path}: read error ({parsed.error_type}): {parsed.message}")
