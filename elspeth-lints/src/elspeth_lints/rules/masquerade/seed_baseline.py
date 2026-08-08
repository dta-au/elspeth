"""Seed/refresh ``config/cicd/masquerade_baseline.yaml`` from the live tree.

Run as ``python -m elspeth_lints.rules.masquerade.seed_baseline`` (from the
elspeth-lints venv) or ``--check`` to verify the on-disk baseline matches
what a fresh generation would produce, without writing.

This module calls :func:`elspeth_lints.rules.masquerade.rule.collect_sites`
AND :func:`elspeth_lints.rules.masquerade.rule.group_non_amnestied_sites`
— the exact same enumerator and the exact same per-key grouping/counting
the rule itself uses to decide what needs a baseline entry and how many
occurrences it should record (blocking amendment A1, extended per the
occurrence-count review finding: the seeder has no walk, no dedup loop,
and no counting logic of its own — it is the rule's own grouping,
serialized). Re-running this after the gate is already seeded and clean
produces byte-identical output; the only way its output changes is a real
change to the covered trees.

Classification default is ``unadjudicated`` for every site this script has
not been told about explicitly. A handful of sites verified by hand during
PLAN.md's Correction 1 (module-level PEP 562 facades that fall outside the
rule's flat if/raise recognizer, the one instance-level ``__getattr__``,
and the two ``getattr_static`` identity/MRO introspection sites) get their
real classification and justification here instead. This is deliberately
small: adjudicating the rest of the corpus is the next session's worklist,
not this gate's job.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from elspeth_lints.rules.masquerade.baseline import BASELINE_RELATIVE_PATH, BaselineEntry, render_baseline_yaml
from elspeth_lints.rules.masquerade.rule import collect_sites, group_non_amnestied_sites
from elspeth_lints.rules.trust_boundary.shared import repository_root

# (path, qualname, kind) -> (classification, justification). Hand-verified per
# PLAN.md Correction 1 / Finding 4. Every other site is seeded as
# "unadjudicated" — see _DEFAULT_JUSTIFICATION and the baseline file's header.
_KNOWN_CLASSIFICATIONS: dict[tuple[str, str, str], tuple[str, str]] = {
    ("src/elspeth/contracts/runtime_val_manifest.py", "_typing_special_form_name", "getattr_static"): (
        "approved-introspection",
        "Fetches the canonical typing special form off a module object for an `is` identity "
        "comparison; static introspection of stdlib structure, not a trust decision "
        "(PLAN.md Finding 4, elspeth-02cd60d8cd).",
    ),
    ("src/elspeth/plugins/infrastructure/base.py", "BaseTransform.__init_subclass__", "getattr_static"): (
        "approved-introspection",
        "__init_subclass__ MRO check that a property wins resolution over "
        "BaseTransform.input_schema, raising TypeError at class creation on shadowing "
        "(elspeth-d6eeb3a71d). This site is ITSELF an anti-masquerading guard; a naive "
        "'fix' would delete a real control (PLAN.md Finding 4). Do not rewrite this site.",
    ),
    ("src/elspeth/contracts/schema_contract.py", "PipelineRow.__getattr__", "dunder_getattr"): (
        "data-container-api",
        "Dot-access API over row DATA delegating to self[key] (a Subscript, not attribute "
        "forwarding to a foreign object), with a `_`-prefix recursion guard and "
        "contract-mode-gated field resolution. The only instance-level __getattr__ found in "
        "production; adjudicated explicitly rather than assumed legitimate (PLAN.md Finding 4).",
    ),
    ("src/elspeth/core/security/__init__.py", "__getattr__", "dunder_getattr"): (
        "module-getattr",
        "Module-level PEP 562 lazy-import facade over the closed _LAZY_EXPORTS dict. The gate "
        "is a try/except KeyError rather than an if/raise chain, so it falls outside this "
        "rule's flat sequential-if structural recognizer, but is the same closed-table idiom "
        "as the amnestied facades (PLAN.md Finding 4).",
    ),
    ("src/elspeth/engine/orchestrator/__init__.py", "__getattr__", "dunder_getattr"): (
        "module-getattr",
        "Module-level PEP 562 lazy-import facade gated on three closed export tuples via an "
        "if/elif/elif/else chain. The gate is real but uses elif, which this rule's flat "
        "sequential-if recognizer does not match (PLAN.md Finding 4).",
    ),
}

_DEFAULT_JUSTIFICATION = (
    "Seeded from the elspeth-b9ad1bbee3 gate-creation corpus; not yet individually reviewed. "
    "classification=unadjudicated means NOT YET REVIEWED, not 'confirmed legitimate' — see "
    "the header of this file and docs/architecture/adr/032-validate-by-trust-domain.md."
)


def build_entries(repo_root: Path) -> list[BaselineEntry]:
    """Return the sorted baseline entries for ``repo_root``.

    Every group returned by :func:`group_non_amnestied_sites` becomes
    exactly one entry, with ``occurrences`` set to that group's ``count``
    directly — there is no separate counting here. This is what makes a
    fresh seed and the rule's own live-scan grouping structurally
    incapable of disagreeing.
    """
    groups = group_non_amnestied_sites(collect_sites(repo_root))
    entries: list[BaselineEntry] = []
    for group in groups:
        classification, justification = _KNOWN_CLASSIFICATIONS.get(group.key, ("unadjudicated", _DEFAULT_JUSTIFICATION))
        entries.append(
            BaselineEntry(
                path=group.path,
                qualname=group.qualname,
                kind=group.kind,
                occurrences=group.count,
                classification=classification,
                justification=justification,
            )
        )
    entries.sort(key=lambda entry: entry.key)
    return entries


def _default_repo_root() -> Path:
    # .../elspeth-lints/src/elspeth_lints/rules/masquerade/seed_baseline.py
    # parents[5] is the repository root (5 levels above this file's directory).
    return Path(__file__).resolve().parents[5]


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=None, help="Repository root (default: derived from this file's location).")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if the on-disk baseline differs from a freshly generated one; do not write.",
    )
    args = parser.parse_args(argv)

    repo_root = repository_root(args.repo_root if args.repo_root is not None else _default_repo_root(), None)
    entries = build_entries(repo_root)
    rendered = render_baseline_yaml(entries)
    baseline_path = repo_root / BASELINE_RELATIVE_PATH

    if args.check:
        existing = baseline_path.read_text(encoding="utf-8") if baseline_path.exists() else None
        if existing != rendered:
            sys.stderr.write(f"{baseline_path} is stale relative to the live tree; re-run without --check to regenerate.\n")
            return 1
        sys.stdout.write(f"{baseline_path} is up to date ({len(entries)} entries).\n")
        return 0

    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(rendered, encoding="utf-8")

    by_classification: dict[str, int] = {}
    for entry in entries:
        by_classification[entry.classification] = by_classification.get(entry.classification, 0) + 1
    sys.stdout.write(f"Wrote {len(entries)} entries to {baseline_path}\n")
    for classification in sorted(by_classification):
        sys.stdout.write(f"  {classification}: {by_classification[classification]}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
