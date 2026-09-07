"""Measure whether a candidate branch in the fencing gate's receiver classifier is INERT.

Written to justify deleting the ``{"session_service", "trail"}`` name exclusion from
``_looks_like_landscape_receiver`` (elspeth-90b13c325d follow-up).  Kept because the
deletion's justification is a measurement, and a measurement nobody can re-run is an
assertion.  Point it at any single branch you suspect is dead.

**It diffs the four ROW SETS, not the four counts.**  That distinction is the whole
point.  A row that moves between categories — say from ``unknown mutation receiver``
to ``callable escape`` — can leave every total unchanged while changing what the gate
says.  Identical SETS exclude reclassification by construction; identical COUNTS do
not.  Deleting a classifier branch on the strength of matching totals alone would be
the weaker measurement dressed as the stronger one.

The gate file is never modified: two scratch copies are written under a temporary root
laid out as ``<root>/tests/unit/architecture/`` with ``<root>/src`` symlinked at the
tree, because ``_repo_root()`` is ``Path(__file__).resolve().parents[3]``.  A copy left
anywhere else scans the wrong tree and reports zeros that read like a clean result.

Usage:
    python scripts/probe_receiver_marker_inertness.py <tree> <scratchdir> <branch-source>

``branch-source`` is the exact text of the branch to remove and must match the gate
file verbatim; a miss is an assertion failure, never a silent no-op.  Exit status is 0
when the branch is INERT (all four row sets identical) and 1 when it is load-bearing,
so the probe can gate a deletion in a pre-merge check.

The measurement this tool was written for, kept as the deletion's justification:

    branch: '    if {"session_service", "trail"} & segments:\\n        return False\\n'
    tree:   release/0.8.0 @ 6623010fd (the slot-45 merged tree)
    result: INERT.  esc 26/26, callers 192/192, api 68/68, tx 79/79, and every one of
            the four ROW SETS byte-identical with and without the branch.  No gate test
            referenced it either — it was the single grep hit in the file.

That measurement was taken TWICE, once on ``af03b56d8`` before the receiver-precision
rule landed (esc 33) and once on ``6623010fd`` after it (esc 26).  Re-taking it mattered:
slot 45 changed the classification rule those rows flow through, so the earlier run was
a measurement of a different function and could not be carried forward.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_GATE_RELATIVE = "tests/unit/architecture/test_web_landscape_mutation_fencing.py"


def _load(name: str, source: str, scratch: Path, tree: Path):
    home = scratch / "tests" / "unit" / "architecture"
    home.mkdir(parents=True, exist_ok=True)
    src_link = scratch / "src"
    if not src_link.exists():
        src_link.symlink_to(tree / "src")
    path = home / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load gate copy {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _row_sets(gate) -> dict[str, set[str]]:
    units = gate._production_units()
    dml = gate.scan_dml_identities(units)
    escapes = (
        *gate._mutation_callable_escapes(units),
        *gate._internal_coordination_authority_violations(units),
        *gate._dml_callable_escape_violations(units),
        *gate._unknown_or_raw_execution_violations(units),
        *gate._raw_write_surface_violations(units),
        *gate._cross_database_violations(units),
    )
    return {
        "esc": set(escapes),
        "callers": set(gate._caller_authority_violations(units)),
        "api": set(gate._api_authority_violations(units)),
        "tx": set(gate._transaction_order_violations(units, dml)),
    }


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print(__doc__)
        return 2
    tree = Path(argv[1]).resolve()
    scratch = Path(argv[2]).resolve()
    branch_source = argv[3]

    sys.path[:0] = [str(tree), str(tree / "src"), str(tree / "elspeth-lints" / "src")]
    original = (tree / _GATE_RELATIVE).read_text(encoding="utf-8")
    if branch_source not in original:
        raise AssertionError(f"branch source not found verbatim in {tree / _GATE_RELATIVE}")

    with_branch = _row_sets(_load("gate_with_branch", original, scratch, tree))
    without_branch = _row_sets(_load("gate_without_branch", original.replace(branch_source, "", 1), scratch, tree))

    inert = True
    for key in ("esc", "callers", "api", "tx"):
        added = sorted(without_branch[key] - with_branch[key])
        removed = sorted(with_branch[key] - without_branch[key])
        print(f"## {key}: with_branch={len(with_branch[key])} without_branch={len(without_branch[key])}")
        for row in removed:
            print(f"  -only-with-branch: {row}")
        for row in added:
            print(f"  +appears-without:  {row}")
        if added or removed:
            inert = False

    print("## VERDICT: " + ("INERT — all four row sets identical" if inert else "LOAD-BEARING — a row set moved"))
    return 0 if inert else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
