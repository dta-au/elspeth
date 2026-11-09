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

Refresh preserves classification and justification from an existing entry
only when its key, count, and normalized probe-shape multiset still match.
A changed shape is a changed review subject and returns to
``unadjudicated``. Genuinely new keys also default to ``unadjudicated``;
review metadata lives in the ledger it adjudicates, not in a second table
that cannot bind the reviewed call shapes. This keeps human adjudications
durable without letting them float onto materially rewritten calls.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from elspeth_lints.rules.masquerade.baseline import (
    BASELINE_RELATIVE_PATH,
    BaselineEntry,
    load_baseline,
    render_baseline_yaml,
)
from elspeth_lints.rules.masquerade.rule import collect_sites, group_non_amnestied_sites
from elspeth_lints.rules.trust_boundary.shared import repository_root

_DEFAULT_JUSTIFICATION = (
    "Seeded from the elspeth-b9ad1bbee3 gate-creation corpus; not yet individually reviewed. "
    "classification=unadjudicated means NOT YET REVIEWED, not 'confirmed legitimate' — see "
    "the header of this file and docs/architecture/adr/032-validate-by-trust-domain.md."
)


def build_entries(repo_root: Path) -> list[BaselineEntry]:
    """Return the sorted baseline entries for ``repo_root``.

    Every group returned by :func:`group_non_amnestied_sites` becomes
    exactly one entry, with ``occurrences`` and ``probe_shapes`` copied
    directly from that group — there is no separate counting or shape
    computation here. Existing review metadata survives only when those
    live bindings still match.
    """
    existing_path = repo_root / BASELINE_RELATIVE_PATH
    existing_by_key = {entry.key: entry for entry in load_baseline(existing_path).entries}
    groups = group_non_amnestied_sites(collect_sites(repo_root))
    entries: list[BaselineEntry] = []
    for group in groups:
        existing = existing_by_key.get(group.key)
        unchanged_existing = existing is not None and existing.occurrences == group.count and existing.probe_shapes == group.probe_shapes
        if unchanged_existing:
            assert existing is not None
            classification, justification = existing.classification, existing.justification
        else:
            classification, justification = "unadjudicated", _DEFAULT_JUSTIFICATION
        entries.append(
            BaselineEntry(
                path=group.path,
                qualname=group.qualname,
                kind=group.kind,
                occurrences=group.count,
                probe_shapes=group.probe_shapes,
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
