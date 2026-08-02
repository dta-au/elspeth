"""From-tree re-verification for ``sign-bundle`` -- the linchpin.

``verify_bundle_against_tree`` is the all-or-nothing gate ([O1] "staging
asserts; firing verifies"): it re-derives every binding from the *current*
source and refuses on any staleness mismatch *before a single write*. It is a
pure read -- no writes, no HMAC key required (diagnosis runs shape-only; the
binding checks still fire).

Every action ``kind`` in the vocabulary has its own from-tree verify rule,
because the ``diagnose_judge_signatures`` index only covers *existing signable*
entries:

* ``drift_repair`` -- compare the action's ``diagnosis_status`` to the live
  diagnosis row (must be present and in ``_SIGNABLE_DIAGNOSIS_STATUSES``);
* ``justify`` (new_judgment) -- the key has no entry yet, so scan the action's
  file via the shared single-file scan helper and confirm the live finding
  still exists at the staged fingerprint and remains uncovered by both exact
  allow-hit prefixes and production ``per_file_rules``;
* ``stale_delete`` -- confirm the tree still reports the key as a non-signable
  orphan; a reappeared live finding is a mismatch (never delete a live entry);
* ``rotation`` -- remove findings already assigned to judge-gated diagnosis,
  re-plan the residual pre-judge population, and confirm the key is still a
  rotation ``old_key``.

The report carries the computed ``diagnosis`` (reused by the ``sign-bundle``
execute phase's drift_repair lane -- one diagnose call per run) and the residual
``rotation_plan`` (built from the same raw census after judge assignments are
removed, then reused by the execute rotation lane so it never re-scans).
The rotation survey always runs so omitted rotations fail the same complete
worklist check as omitted judgments, drift repairs, and stale deletes; the plan
is exposed on the report only when the bundle contains a rotation action.
Ambiguity in the residual population is genuine and fails verification.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from elspeth_lints.core.allowlist import PerFileRule
from elspeth_lints.core.judge_signature_diagnosis import (
    _SIGNABLE_DIAGNOSIS_STATUSES,
    JudgeSignatureDiagnosisReport,
    diagnose_judge_signatures,
)
from elspeth_lints.core.review_bundle import BundleAction, ReviewBundle
from elspeth_lints.core.tier_model_scan import (
    TargetCensus,
    TargetCoverage,
    census_tree_targets,
    plan_judge_cleanup_groups,
    plan_non_judge_rotations,
    scan_single_file_findings,
    scan_tree_findings,
)
from elspeth_lints.rules.trust_tier.tier_model.rotate import RotationPlan, _finding_covered_by_per_file_rule
from elspeth_lints.rules.trust_tier.tier_model.rule import _load_tier_model_allowlist

# A ``stale_delete`` target is safe to remove only while the tree still reports
# it as one of these non-signable orphan statuses (neither is in
# ``_SIGNABLE_DIAGNOSIS_STATUSES``).
_STALE_DELETE_ORPHAN_STATUSES = frozenset({"NO_MATCHING_FINDING", "SOURCE_FILE_MISSING"})


@dataclass(frozen=True, slots=True)
class BundleVerificationReport:
    """Result of re-deriving a bundle's claims from the source tree."""

    mismatches: tuple[str, ...]
    diagnosis: JudgeSignatureDiagnosisReport
    rotation_plan: RotationPlan | None
    target_census: TargetCensus
    operator_derived_actions: tuple[BundleAction, ...]

    @property
    def ok(self) -> bool:
        """True only when no staged claim disagrees with ground truth."""
        return not self.mismatches


def verify_bundle_against_tree(
    bundle: ReviewBundle,
    *,
    root: Path,
    allowlist_dir: Path,
    bundle_allowlist_dir: Path | None = None,
) -> BundleVerificationReport:
    """Re-derive every bundle action's binding from the tree (pure read).

    ``allowlist_dir`` is the tree to inspect. During coherent publication the
    authenticated pre-action allowlist has moved to the transaction's
    candidate path, while the bundle remains bound to the public allowlist
    path. ``bundle_allowlist_dir`` keeps those two identities explicit.
    """
    # CLI callers use repository-relative defaults (``src/elspeth`` and
    # ``config/cicd/enforce_tier_model``).  The scanners resolve each target
    # file before comparing it with ``root``; keep both sides in the same
    # absolute coordinate system or every new-judgment action is falsely
    # reported as unscannable.
    root = Path(root).resolve()
    allowlist_dir = Path(allowlist_dir).resolve()
    expected_bundle_allowlist_dir = Path(bundle_allowlist_dir or allowlist_dir).resolve()

    mismatches: list[str] = []
    recorded_root_path = Path(bundle.root)
    if not recorded_root_path.is_absolute():
        mismatches.append(f"bundle root must be absolute; staged value was {bundle.root!r}")
    elif recorded_root_path.resolve() != root:
        mismatches.append(f"bundle root {recorded_root_path.resolve()} does not match signing root {root}")
    recorded_allowlist_path = Path(bundle.allowlist_dir)
    if not recorded_allowlist_path.is_absolute():
        mismatches.append(f"bundle allowlist_dir must be absolute; staged value was {bundle.allowlist_dir!r}")
    elif recorded_allowlist_path.resolve() != expected_bundle_allowlist_dir:
        mismatches.append(
            f"bundle allowlist_dir {recorded_allowlist_path.resolve()} does not match signing allowlist_dir {expected_bundle_allowlist_dir}"
        )

    diagnosis = diagnose_judge_signatures(root=root, allowlist_dir=allowlist_dir)
    diagnosis_items_by_key: dict[str, list[Any]] = {}
    for item in diagnosis.items:
        diagnosis_items_by_key.setdefault(item.key, []).append(item)
    duplicate_diagnosis_keys = {key for key, items in diagnosis_items_by_key.items() if len(items) > 1}
    for key in sorted(duplicate_diagnosis_keys):
        owners = sorted(item.source_file for item in diagnosis_items_by_key[key])
        mismatches.append(f"target diagnosis contains duplicate diagnosis key {key!r} in {owners!r}")
    index: dict[str, Any] = {key: items[0] for key, items in diagnosis_items_by_key.items() if key not in duplicate_diagnosis_keys}
    bundle_action_keys: set[str] = set()
    for action in bundle.actions:
        if action.key in bundle_action_keys:
            mismatches.append(f"review bundle contains duplicate action key {action.key!r}")
        bundle_action_keys.add(action.key)
    allowlist = _load_tier_model_allowlist(allowlist_dir)
    raw_findings = tuple(scan_tree_findings(root=root))
    full_rotation_plan = plan_non_judge_rotations(
        findings=raw_findings,
        allowlist=allowlist,
        diagnosis_items=diagnosis.items,
    )
    cleanup_groups = plan_judge_cleanup_groups(
        findings=raw_findings,
        allowlist=allowlist,
        orphan_keys=frozenset(item.key for item in diagnosis.items if item.status in _STALE_DELETE_ORPHAN_STATUSES),
    )
    resign_assigned_keys = {rotation.new_key for rotation in full_rotation_plan.rotations}
    resign_assigned_keys.update(finding_key for group in cleanup_groups for finding_key in group.finding_keys)
    target_coverage = TargetCoverage(
        exact_keys=frozenset(entry.key for entry in allowlist.entries),
        diagnosis_assigned_keys=frozenset(
            item.repair_key for item in diagnosis.items if item.status in _SIGNABLE_DIAGNOSIS_STATUSES and item.repair_key is not None
        ),
        resign_assigned_keys=frozenset(resign_assigned_keys),
    )
    target_scan = census_tree_targets(
        root=root,
        findings=raw_findings,
        coverage=target_coverage,
        per_file_rules=allowlist.per_file_rules,
    )

    # Survey every non-judge-gated rotation even when the staged bundle omits
    # the lane. Remove findings already assigned to judge-gated diagnosis so a
    # mixed signed/pre-judge identity group is classified on its true residual
    # population rather than producing filtered-plan pollution.
    for group in full_rotation_plan.ambiguous:
        mismatches.append(
            "target census found ambiguous non-judge target group "
            f"{group.prefix!r} ({group.finding_count} finding(s), {group.entry_count} entry/entries)"
        )
    rotations_by_old_key: dict[str, list[Any]] = {}
    for rotation in full_rotation_plan.rotations:
        rotations_by_old_key.setdefault(rotation.old_key, []).append(rotation)
    for old_key, rotations in sorted(rotations_by_old_key.items()):
        if len(rotations) > 1:
            mismatches.append(f"target census rotation old_key {old_key!r} has {len(rotations)} owners; rotation ownership must be unique")
    rotation_keys = {action.key for action in bundle.actions if action.kind == "rotation"}
    for rotation in full_rotation_plan.rotations:
        if rotation.old_key not in rotation_keys:
            mismatches.append(f"target census missing rotation action for {rotation.old_key!r}")
    rotation_plan: RotationPlan | None = full_rotation_plan if rotation_keys else None

    # One source file can account for hundreds of new judgments.  Re-scan it
    # once and share the key-to-finding map across those actions; otherwise
    # verification cost grows with findings-per-file rather than files.
    new_judgment_findings_by_file: dict[str, dict[str, Any] | None] = {}
    justify_keys = {action.key for action in bundle.actions if action.kind == "justify"}
    for finding in target_scan.findings:
        canonical_key = _finding_canonical_key(finding)
        file_findings = new_judgment_findings_by_file.setdefault(finding.file_path, {})
        assert file_findings is not None
        file_findings[canonical_key] = finding
    for finding in target_scan.uncovered_findings:
        canonical_key = _finding_canonical_key(finding)
        if canonical_key not in justify_keys:
            mismatches.append(f"target census missing justify action for uncovered finding {canonical_key!r}")

    action_keys_by_kind = {
        kind: {action.key for action in bundle.actions if action.kind == kind} for kind in ("drift_repair", "stale_delete")
    }
    operator_derived_actions: list[BundleAction] = []
    for item in diagnosis.items:
        if item.key in duplicate_diagnosis_keys:
            continue
        if item.status in _SIGNABLE_DIAGNOSIS_STATUSES and item.key not in action_keys_by_kind["drift_repair"]:
            if item.status == "INVALID_SIGNATURE" and diagnosis.verification_mode == "authoritative":
                # A key-free stage can validate the signed row's shape and
                # binding, but it cannot authenticate the HMAC without
                # violating key custody. The operator-key gate is the first
                # place this status is knowable, so make the repair explicit
                # there and require the CLI to show/fire it as part of the
                # reviewed transaction rather than deadlocking on an
                # impossible key-free re-stage.
                if item.key in bundle_action_keys:
                    mismatches.append(f"operator-derived invalid-signature repair for {item.key!r} conflicts with a staged action")
                else:
                    operator_derived_actions.append(
                        BundleAction(
                            lane="resign",
                            kind="drift_repair",
                            key=item.key,
                            source_file=item.source_file,
                            diagnosis_status=item.status,
                        )
                    )
            else:
                mismatches.append(f"target census missing drift_repair action for {item.key!r} ({item.status})")
        elif item.status in _STALE_DELETE_ORPHAN_STATUSES and item.key not in action_keys_by_kind["stale_delete"]:
            mismatches.append(f"target census missing stale_delete action for orphan {item.key!r} ({item.status})")

    for action in bundle.actions:
        if action.kind == "drift_repair":
            mismatches.extend(_verify_drift_repair(action, index))
        elif action.kind == "justify":
            mismatches.extend(
                _verify_new_judgment(
                    action,
                    root=root,
                    live_findings_by_file=new_judgment_findings_by_file,
                    covered_keys=target_coverage.exact_keys | target_coverage.diagnosis_assigned_keys,
                    per_file_rules=allowlist.per_file_rules,
                )
            )
        elif action.kind == "stale_delete":
            mismatches.extend(_verify_stale_delete(action, index))
        elif action.kind == "rotation":
            mismatches.extend(_verify_rotation(action, rotation_plan))
        else:  # pragma: no cover - BundleAction.__post_init__ rejects unknown kinds
            mismatches.append(f"action {action.key!r}: unknown kind {action.kind!r}")

    return BundleVerificationReport(
        mismatches=tuple(mismatches),
        diagnosis=diagnosis,
        rotation_plan=rotation_plan,
        target_census=target_scan.census,
        operator_derived_actions=tuple(sorted(operator_derived_actions, key=lambda action: (action.kind, action.key))),
    )


def _verify_drift_repair(action: BundleAction, index: dict[str, Any]) -> list[str]:
    item = index.get(action.key)
    if item is None:
        return [f"drift_repair {action.key!r}: no diagnosis row in the tree (staged status {action.diagnosis_status!r}); re-run stage_scan"]
    if item.status not in _SIGNABLE_DIAGNOSIS_STATUSES:
        return [f"drift_repair {action.key!r}: tree status {item.status!r} is not a signable drift (staged {action.diagnosis_status!r})"]
    if item.status != action.diagnosis_status:
        return [f"drift_repair {action.key!r}: staged diagnosis_status {action.diagnosis_status!r} but the tree reports {item.status!r}"]
    return []


def _verify_new_judgment(
    action: BundleAction,
    *,
    root: Path,
    live_findings_by_file: dict[str, dict[str, Any] | None],
    covered_keys: frozenset[str],
    per_file_rules: list[PerFileRule],
) -> list[str]:
    if not action.file_path:  # pragma: no cover - enforced by BundleAction.__post_init__
        return [f"new_judgment {action.key!r}: missing file_path"]
    if action.file_path not in live_findings_by_file:
        target_file = (root / action.file_path).resolve()
        try:
            findings = scan_single_file_findings(target_file=target_file, root=root)
        except (OSError, ValueError):
            live_findings_by_file[action.file_path] = None
        else:
            live_findings_by_file[action.file_path] = {_finding_canonical_key(finding): finding for finding in findings}
    live_findings = live_findings_by_file[action.file_path]
    if live_findings is None:
        return [f"new_judgment {action.key!r}: source {action.file_path} could not be scanned"]
    finding = live_findings.get(action.key)
    if finding is None:
        return [
            f"new_judgment {action.key!r}: no live finding at the staged fingerprint in "
            f"{action.file_path} (vanished or fingerprint-shifted)"
        ]
    if action.key in covered_keys or _finding_covered_by_per_file_rule(finding, per_file_rules):
        return [f"new_judgment {action.key!r}: finding is already covered by the current allowlist; re-run stage_scan"]
    return []


def _verify_stale_delete(action: BundleAction, index: dict[str, Any]) -> list[str]:
    item = index.get(action.key)
    if item is None:
        return [f"stale_delete {action.key!r}: no diagnosis row in the tree; cannot confirm the entry is an orphan"]
    if item.status not in _STALE_DELETE_ORPHAN_STATUSES:
        return [f"stale_delete {action.key!r}: tree reports {item.status!r}, not an orphan; the covered finding reappeared"]
    if action.source_file != item.source_file:
        return [
            f"stale_delete {action.key!r}: staged source_file {action.source_file!r} does not match "
            f"the fresh diagnosis owning YAML {item.source_file!r}"
        ]
    return []


def _verify_rotation(action: BundleAction, rotation_plan: RotationPlan | None) -> list[str]:
    if rotation_plan is None:  # pragma: no cover - a rotation action guarantees a computed plan
        return [f"rotation {action.key!r}: no filtered rotation plan was computed"]
    rotations = [candidate for candidate in rotation_plan.rotations if candidate.old_key == action.key]
    if not rotations:
        return [f"rotation {action.key!r}: no longer an applicable rotation old_key in a fresh non-judge-gated scan"]
    if len(rotations) != 1:
        return [f"rotation {action.key!r}: fresh rotation old_key has {len(rotations)} owners; expected exactly one"]
    rotation = rotations[0]
    if action.source_file != rotation.entry_source_file:
        return [
            f"rotation {action.key!r}: staged source_file {action.source_file!r} does not match "
            f"the fresh rotation owning YAML {rotation.entry_source_file!r}"
        ]
    return []


def _finding_canonical_key(finding: Any) -> str:
    key = finding.canonical_key
    if callable(key):
        key = key()
    if not isinstance(key, str):
        raise ValueError(f"finding.canonical_key must be str; got {type(key).__name__}")
    return key
