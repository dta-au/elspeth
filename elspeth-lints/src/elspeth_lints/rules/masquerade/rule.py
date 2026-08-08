"""Implementation of the ``masquerade.attribute-probes`` gate.

ADR-032 (``docs/architecture/adr/032-validate-by-trust-domain.md``) makes
the ban on attribute-presence probing a matter of *purpose*, not
*construct*: ``getattr``/``hasattr``/``inspect.getattr_static`` are banned
as internal-contract presence probes, but sentinel-defaulted ``getattr``
plus value assertions is the *prescribed* pattern at an external (vendor /
SDK / network) boundary, and ``assert [not] hasattr(...)`` in a test is a
presence-as-subject declaration, not a probe. A gate keyed on construct
alone fires on correct, deliberately-defended code — including
``plugins/infrastructure/base.py``'s ``inspect.getattr_static`` MRO check,
which is itself an anti-masquerading guard the elspeth-02cd60d8cd campaign
exists to install (PLAN.md Finding 4). This rule therefore recognises
three permanently-green structural amnesties (see
``inventory.py``'s module docstring for the exact predicates) and falls
back, for every other site, to a per-site baseline
(``config/cicd/masquerade_baseline.yaml``) that must name the site and
record a human classification. A brand-new site of a banned kind, in any
of the four covered roots, that is neither amnestied nor already in the
baseline is an ERROR — new masquerade cannot land silently. A baseline
entry whose site has disappeared entirely is also an ERROR
(``stale-baseline-entry``), and a baseline entry whose recorded
``occurrences`` count no longer matches how many non-amnestied probe sites
currently share its ``(path, qualname, kind)`` key is an ERROR
(``occurrence-count-drift``) in EITHER direction — this is what prevents
someone from adding (or, silently, removing) a probe at an
already-baselined high-traffic boundary (e.g.
``tool_batch.py::_admit_tool_batch``) without the gate noticing, which a
key alone would otherwise permit. A normalized per-occurrence
``probe_shapes`` multiset also has to match exactly, so replacing one
reviewed probe with a materially different call at the same key and count
fires ``probe-shape-drift``.

This rule carries NO signature material, NO tier_model coupling, and does
not touch ``BoundaryRule``. It is a third, independent consumer of
``@trust_boundary``/``@observation_boundary`` metadata (Wardline is the
second, per AGENTS.md) — it reads ``source_param`` dataflow and never
suppresses a ``trust_tier.tier_model`` finding.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path

from elspeth_lints.core.ast_walker import PythonFileReadError, PythonSyntaxError, walk_python_files
from elspeth_lints.core.protocols import Finding, RuleContext, RuleMetadata, RuleScope
from elspeth_lints.rules.masquerade.baseline import BASELINE_RELATIVE_PATH, BaselineEntry, MasqueradeBaseline, load_baseline
from elspeth_lints.rules.masquerade.inventory import MasqueradeSite, SiteKind, iter_masquerade_sites
from elspeth_lints.rules.masquerade.metadata import RULE_ID, RULE_METADATA, SCAN_SUBDIRS
from elspeth_lints.rules.trust_boundary.shared import repository_root


@dataclass(frozen=True, slots=True)
class MasqueradeAttributeProbesRule:
    """Flag unadjudicated attribute-masquerading probe sites."""

    id: str = RULE_ID
    scope: RuleScope = RuleScope.WHOLE_REPO
    metadata: RuleMetadata = RULE_METADATA

    def analyze(self, tree: ast.AST, file_path: Path, context: RuleContext) -> list[Finding]:
        """Whole-repository scan; ``tree``/``file_path`` follow the WHOLE_REPO convention.

        The CLI and the fixture harness both invoke WHOLE_REPO rules as
        ``rule.analyze(<empty tree>, <scan root>, context)`` — the tree is
        never inspected directly; ``file_path`` is the scan root.
        """
        del tree
        return scan_root(file_path, repo_root_override=context.repo_root)


def scan_root(
    root: Path,
    *,
    repo_root_override: Path | None = None,
) -> list[Finding]:
    """Walk the four covered roots under the repository root and diff against the baseline."""
    repo_root = repository_root(root, repo_root_override)
    baseline_path = repo_root / BASELINE_RELATIVE_PATH
    baseline = load_baseline(baseline_path)
    diagnostics: list[Finding] = []
    failed_paths: set[str] = set()
    sites = collect_sites(
        repo_root,
        diagnostics=diagnostics,
        failed_paths=failed_paths,
    )
    baseline_display_path = _display_relative(baseline_path, repo_root)
    findings = [
        *diagnostics,
        *_findings_for_sites(
            sites,
            baseline,
            baseline_display_path=baseline_display_path,
            excluded_paths=failed_paths,
        ),
    ]
    findings.sort(key=lambda finding: (finding.file_path, finding.line, finding.column, finding.fingerprint))
    return findings


@dataclass(frozen=True, slots=True)
class SiteGroup:
    """Every current non-amnestied occurrence sharing one ``(path, qualname, kind)`` key.

    This is the SINGLE grouping computation in the whole gate: both the
    live-scan diff (:func:`_findings_for_sites`) and the baseline seeder
    (``seed_baseline.build_entries``) call :func:`group_non_amnestied_sites`
    and consume its ``SiteGroup`` objects directly. Neither has its own
    dedup/count logic. This is deliberately stronger than blocking
    amendment A1's original ask (matching qualname computation) — it
    guarantees the OCCURRENCE COUNT the two consumers see can never
    diverge either, closing the specific hole a from-scratch
    reimplementation of "count sites per key" could reopen.
    """

    path: str
    qualname: str
    kind: SiteKind
    sites: tuple[MasqueradeSite, ...]

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.path, self.qualname, self.kind)

    @property
    def count(self) -> int:
        return len(self.sites)

    @property
    def probe_shapes(self) -> tuple[str, ...]:
        """Return the normalized per-occurrence shape multiset."""
        return tuple(sorted(site.probe_shape for site in self.sites))

    @property
    def representative(self) -> MasqueradeSite:
        """The first-encountered site at this key — used for line/column/detail in messages."""
        return self.sites[0]


def group_non_amnestied_sites(sites: list[MasqueradeSite]) -> list[SiteGroup]:
    """Group non-amnestied sites by ``(path, qualname, kind)``, preserving first-seen order."""
    buckets: dict[tuple[str, str, SiteKind], list[MasqueradeSite]] = {}
    order: list[tuple[str, str, SiteKind]] = []
    for site in sites:
        if site.amnesty:
            continue
        key = (site.path, site.qualname, site.kind)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(site)
    return [SiteGroup(path=key[0], qualname=key[1], kind=key[2], sites=tuple(buckets[key])) for key in order]


#: Under ``elspeth-lints/src`` only, every rule family keeps its curated
#: fixture corpus at ``rules/<name>/fixtures/examples_violation/`` and
#: ``.../examples_clean/`` (see the ``meta_no_new_bespoke_cicd_enforcer``
#: template this package mirrors). Those files are deliberately-crafted
#: synthetic snippets — half of them exist specifically to CONTAIN a
#: banned pattern for the fixture harness to detect — not production,
#: test-suite, or script code subject to real trust-domain adjudication.
#: Scanning them into the same baseline that adjudicates real code would
#: force every future rule author to hand-adjudicate their own violation
#: fixtures, which is nonsensical (there is no honest ADR-032
#: classification for "this getattr call exists so a test can assert the
#: gate fires on it"). This exclusion is scoped to ``elspeth-lints/src``
#: specifically: a ``fixtures/`` directory under ``tests/`` is real
#: test-double code and is NOT exempted.
_LINT_RULE_FIXTURES_SUBDIR = "elspeth-lints/src"


def collect_sites(
    repo_root: Path,
    *,
    diagnostics: list[Finding] | None = None,
    failed_paths: set[str] | None = None,
) -> list[MasqueradeSite]:
    """Return every candidate masquerade site across the four covered roots.

    This is the SAME enumerator (:func:`iter_masquerade_sites`) the
    baseline seeder calls — see ``inventory.py``'s module docstring for
    why the rule and the seeder must never diverge here.
    """
    sites: list[MasqueradeSite] = []
    scanned_item_count = 0
    missing_subdirs = [subdir for subdir in SCAN_SUBDIRS if not (repo_root / subdir).is_dir()]
    layout_error = bool(missing_subdirs) and not _is_rule_fixture_repo(repo_root)
    if layout_error and diagnostics is None:
        raise ValueError(f"missing required scan roots under {repo_root}: {', '.join(missing_subdirs)}")
    for subdir in SCAN_SUBDIRS:
        subroot = repo_root / subdir
        if not subroot.is_dir():
            continue
        for item in walk_python_files(subroot):
            relative = item.path.relative_to(subroot)
            if subdir == _LINT_RULE_FIXTURES_SUBDIR and "fixtures" in relative.parts:
                continue
            scanned_item_count += 1
            display = f"{subdir}/{relative.as_posix()}"
            if isinstance(item, PythonSyntaxError):
                finding = _syntax_error_finding(item, display)
                if diagnostics is None:
                    raise ValueError(f"{display}: {item.message}")
                if failed_paths is not None:
                    failed_paths.add(display)
                diagnostics.append(finding)
                continue
            if isinstance(item, PythonFileReadError):
                finding = _read_error_finding(item, display)
                if diagnostics is None:
                    raise ValueError(f"{display}: {item.error_type}: {item.message}")
                if failed_paths is not None:
                    failed_paths.add(display)
                diagnostics.append(finding)
                continue
            sites.extend(iter_masquerade_sites(item.tree, display))
    if layout_error:
        message = f"missing required scan roots under {repo_root}: {', '.join(missing_subdirs)}"
        assert diagnostics is not None
        diagnostics.append(_inert_scan_finding(repo_root, message))
    elif scanned_item_count == 0:
        message = f"the required scan roots under {repo_root} contain no in-scope Python files"
        if diagnostics is None:
            raise ValueError(message)
        diagnostics.append(_inert_scan_finding(repo_root, message))
    return sites


def _is_rule_fixture_repo(repo_root: Path) -> bool:
    fixture_root = Path(__file__).resolve().parent / "fixtures"
    return repo_root.resolve().is_relative_to(fixture_root)


def _inert_scan_finding(repo_root: Path, message: str) -> Finding:
    return Finding(
        rule_id=RULE_ID,
        file_path=repo_root.as_posix(),
        line=0,
        column=0,
        message=f"inert-scan: {message}",
        fingerprint=_fingerprint(f"{RULE_ID}:inert-scan", repo_root.as_posix()),
        severity=RULE_METADATA.severity,
        suggestion="Pass the ELSPETH repository root via --repo-root; the gate must scan at least one declared root.",
    )


def _syntax_error_finding(item: PythonSyntaxError, display_path: str) -> Finding:
    return Finding(
        rule_id="parse-error",
        file_path=display_path,
        line=item.line,
        column=item.column,
        message=item.message,
        fingerprint=f"syntax:{item.line}:{item.column}",
    )


def _read_error_finding(item: PythonFileReadError, display_path: str) -> Finding:
    return Finding(
        rule_id="read-error",
        file_path=display_path,
        line=0,
        column=0,
        message=f"{item.error_type}: {item.message}",
        fingerprint=f"read:{item.error_type}",
    )


def _findings_for_sites(
    sites: list[MasqueradeSite],
    baseline: MasqueradeBaseline,
    *,
    baseline_display_path: str,
    excluded_paths: set[str] | None = None,
) -> list[Finding]:
    excluded = excluded_paths or set()
    all_keys: set[tuple[str, str, str]] = {(site.path, site.qualname, site.kind) for site in sites}
    groups_by_key = {group.key: group for group in group_non_amnestied_sites(sites)}
    baseline_by_key = {entry.key: entry for entry in baseline.entries}

    findings: list[Finding] = []
    for key in sorted(set(groups_by_key) | set(baseline_by_key)):
        if key[0] in excluded:
            continue
        group = groups_by_key.get(key)
        entry = baseline_by_key.get(key)
        if entry is None:
            # group is guaranteed non-None here: key came from at least one
            # of the two maps, and it's not in baseline_by_key.
            assert group is not None
            findings.append(_unbaselined_finding(group))
            continue
        if key not in all_keys:
            # The site is gone entirely (amnestied occurrences included) —
            # genuine deletion/rename, not just an occurrence-count change.
            findings.append(_stale_finding(entry, baseline_display_path))
            continue
        actual_count = group.count if group is not None else 0
        if actual_count != entry.occurrences:
            findings.append(
                _occurrence_drift_finding(entry, actual_count=actual_count, representative=group.representative if group else None)
            )
            continue
        assert group is not None
        if group.probe_shapes != entry.probe_shapes:
            findings.append(_probe_shape_drift_finding(entry, group=group))

    findings.sort(key=lambda finding: (finding.file_path, finding.line, finding.column, finding.fingerprint))
    return findings


def _unbaselined_finding(group: SiteGroup) -> Finding:
    site = group.representative
    detail = _site_detail(site)
    message = (
        f"{site.kind} site at {site.qualname!r} in {site.path} is not covered by a permanent amnesty "
        f"and has no entry in {BASELINE_RELATIVE_PATH}{detail}. It has {group.count} current occurrence(s). "
        "Per ADR-032, classify it — external-parse (sentinel getattr + value asserts + owned type), "
        "approved-introspection, reflection-owned-table, module-getattr, or data-container-api — then use "
        "the baseline seeder to add the occurrence count and normalized probe_shapes, or rewrite it to direct access / "
        "isinstance-against-a-concrete-owned-class if the shape is actually guaranteed."
    )
    fingerprint = _fingerprint(RULE_ID, site.path, site.qualname, site.kind)
    return Finding(
        rule_id=RULE_ID,
        file_path=site.path,
        line=site.line,
        column=site.column,
        message=message,
        fingerprint=fingerprint,
        severity=RULE_METADATA.severity,
        suggestion=(
            "Run `python -m elspeth_lints.rules.masquerade.seed_baseline`, then adjudicate the new "
            f"entry in {BASELINE_RELATIVE_PATH}; or rewrite the site."
        ),
        symbol_context=(site.qualname,),
        ast_path=f"{site.kind}:{site.qualname}",
    )


def _stale_finding(entry: BaselineEntry, baseline_display_path: str) -> Finding:
    message = (
        f"stale-baseline-entry: {BASELINE_RELATIVE_PATH} adjudicates {entry.kind} at "
        f"{entry.qualname!r} in {entry.path}, but no such site exists in the tree anymore. "
        "Remove the entry (or, if the site moved/was renamed, let it re-fire and re-adjudicate "
        "under its new qualname) so the baseline keeps tracking reality."
    )
    fingerprint = _fingerprint(f"{RULE_ID}:stale", entry.path, entry.qualname, entry.kind)
    return Finding(
        rule_id=RULE_ID,
        file_path=baseline_display_path,
        line=1,
        column=0,
        message=message,
        fingerprint=fingerprint,
        severity=RULE_METADATA.severity,
        suggestion=f"Delete the stale entry for {entry.path}:{entry.qualname}:{entry.kind} from {BASELINE_RELATIVE_PATH}.",
        symbol_context=(entry.qualname,),
        ast_path=f"baseline:{entry.kind}:{entry.qualname}",
    )


def _occurrence_drift_finding(entry: BaselineEntry, *, actual_count: int, representative: MasqueradeSite | None) -> Finding:
    line = representative.line if representative is not None else 1
    column = representative.column if representative is not None else 0
    direction = "a probe was added" if actual_count > entry.occurrences else "a probe was removed, rewritten, or amnestied"
    remediation = (
        "Remove the now-fully-amnestied baseline entry"
        if actual_count == 0
        else "Refresh the entry with the baseline seeder, including its occurrence count and probe_shapes"
    )
    message = (
        f"occurrence-count-drift: {BASELINE_RELATIVE_PATH} records occurrences: {entry.occurrences} for "
        f"{entry.kind} at {entry.qualname!r} in {entry.path}, but the tree currently has {actual_count}. "
        f"Likely cause: {direction}. {remediation} — after re-adjudicating if the count increased; "
        "a decrease should also be reviewed rather than assumed safe."
    )
    fingerprint = _fingerprint(f"{RULE_ID}:occurrence-drift", entry.path, entry.qualname, entry.kind)
    return Finding(
        rule_id=RULE_ID,
        file_path=entry.path,
        line=line,
        column=column,
        message=message,
        fingerprint=fingerprint,
        severity=RULE_METADATA.severity,
        suggestion=(
            f"Delete the entry for {entry.path}:{entry.qualname}:{entry.kind} from {BASELINE_RELATIVE_PATH}."
            if actual_count == 0
            else "Run `python -m elspeth_lints.rules.masquerade.seed_baseline`, then re-adjudicate "
            f"{entry.path}:{entry.qualname}:{entry.kind}."
        ),
        symbol_context=(entry.qualname,),
        ast_path=f"occurrence-drift:{entry.kind}:{entry.qualname}",
    )


def _probe_shape_drift_finding(entry: BaselineEntry, *, group: SiteGroup) -> Finding:
    site = group.representative
    expected = ", ".join(shape[:12] for shape in entry.probe_shapes) or "<legacy-unbound>"
    actual = ", ".join(shape[:12] for shape in group.probe_shapes)
    message = (
        f"probe-shape-drift: {BASELINE_RELATIVE_PATH} records normalized probe shapes [{expected}] for "
        f"{entry.kind} at {entry.qualname!r} in {entry.path}, but the tree currently has [{actual}]. "
        "The key and occurrence count are unchanged, but at least one probe was materially rewritten. "
        "Refresh probe_shapes and re-adjudicate the changed call before preserving its classification."
    )
    fingerprint = _fingerprint(f"{RULE_ID}:probe-shape-drift", entry.path, entry.qualname, entry.kind)
    return Finding(
        rule_id=RULE_ID,
        file_path=entry.path,
        line=site.line,
        column=site.column,
        message=message,
        fingerprint=fingerprint,
        severity=RULE_METADATA.severity,
        suggestion=f"Refresh probe_shapes and re-adjudicate {entry.path}:{entry.qualname}:{entry.kind}.",
        symbol_context=(entry.qualname,),
        ast_path=f"probe-shape-drift:{entry.kind}:{entry.qualname}",
    )


def _site_detail(site: MasqueradeSite) -> str:
    if site.kind == "getattr":
        return f" (arity={site.arity}, literal_name={site.literal_name})"
    return ""


def _fingerprint(*parts: str) -> str:
    payload = "|".join(parts)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _display_relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


RULE = MasqueradeAttributeProbesRule()
