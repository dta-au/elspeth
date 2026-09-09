"""Measure every pin and violation set in the Landscape mutation-fencing gate.

The gate in ``tests/unit/architecture/test_web_landscape_mutation_fencing.py``
spends four test ids on ten distinct checks: four frozen inventory pins
(production callers, coordination callers, internal facade edges, subordinate
Connection-helper edges) and five violation sets (caller authority, coordination
caller authority, API authority, transaction order, escapes). Each test asserts
several of them in sequence, so a red id reports only the FIRST check that
fails and hides every check behind it.

That makes the gate expensive to reason about during the ADR-048 token-threading
burn-down: a lane that clears one assertion learns the next one's state only by
paying another full gate run, and a pin that drifted while masked looks like a
regression appearing from nowhere.

This tool runs every check in one pass against an arbitrary tree, so progress can
be scored without guessing which assertion is currently on top. Because the gate
resolves its own repository root from the test file's location
(``Path(__file__).resolve().parents[3]``), a tree is measured by pointing at its
checkout, and a ``git archive`` of any commit measures that commit.

Usage::

    python scripts/fencing_inventory.py <tree> [--json OUT]
    python scripts/fencing_inventory.py <tree> --baseline <other-tree>

The ``--baseline`` form classifies each pinned inventory row as unchanged,
arrived, departed, or fingerprint-moved against another tree, which is the
evidence a re-pin needs: an arrived row is re-derived, a departed row is a code
change to read, and a moved fingerprint is a call whose arguments changed.

Every gate symbol this reads is named directly rather than looked up by string,
so the tool holds no dynamic-attribute probes (ADR-032) and a gate symbol that is
renamed out from under it fails loudly at import instead of silently measuring
nothing.

Read by nothing in ``src/elspeth``; this is maintainer tooling, not runtime code.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

GATE_RELPATH = Path("tests/unit/architecture/test_web_landscape_mutation_fencing.py")


def load_gate(tree: Path) -> ModuleType:
    """Import the fencing gate module from ``tree`` without importing its siblings."""
    gate_path = tree / GATE_RELPATH
    if not gate_path.is_file():
        raise SystemExit(f"no fencing gate at {gate_path}")
    for entry in (tree / "src", tree / "elspeth-lints" / "src", tree):
        if str(entry) not in sys.path:
            sys.path.insert(0, str(entry))
    spec = importlib.util.spec_from_file_location(f"fencing_gate_{abs(hash(tree))}", gate_path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {gate_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def row_fields(row: object) -> dict[str, str]:
    """Return a row's dataclass fields, stringified so two trees compare cleanly."""
    if not dataclasses.is_dataclass(row) or isinstance(row, type):
        return {"row": str(row)}
    return {name: str(value) for name, value in dataclasses.asdict(row).items()}


def identity(fields: dict[str, str]) -> tuple[str, ...]:
    """Key a row by everything except its source line and its call fingerprint.

    Line numbers are churn and the gate's own digest ignores them. The
    fingerprint is held out so that a call whose arguments changed is reported
    as a MOVE rather than as a departure plus an arrival.
    """
    return tuple(value for name, value in sorted(fields.items()) if not name.endswith("line") and name != "call_fingerprint")


def pin(rows: Sequence[object], digest: str, pinned_count: int, pinned_digest: str) -> dict[str, Any]:
    return {
        "live_count": len(rows),
        "live_digest": digest,
        "pinned_count": pinned_count,
        "pinned_digest": pinned_digest,
        "rows": [row_fields(row) for row in rows],
    }


def violation(rows: Sequence[object], owning_test: str) -> dict[str, Any]:
    return {"count": len(rows), "test": owning_test, "rows": [str(row) for row in rows]}


def measure(tree: Path) -> dict[str, Any]:
    """Run every pin and violation check in the gate against ``tree``, in assertion order."""
    gate = load_gate(tree)
    units = gate._production_units()
    dml = gate.scan_dml_identities(units)

    frozen_set_test = "test_landscape_production_caller_set_is_frozen"
    api_test = "test_every_landscape_mutation_api_requires_current_typed_authority"
    transaction_test = "test_every_landscape_dml_transaction_is_full_token_fenced_first"
    escape_test = "test_no_mutation_alias_wrapper_dynamic_or_raw_write_escape_exists"

    production = gate.scan_production_calls(units)
    coordination = gate.scan_coordination_production_calls(units)
    internal = gate.scan_internal_landscape_wrapper_edges(units)
    subordinate = gate._subordinate_helper_edges(units, dml)

    escapes = (
        *gate._mutation_callable_escapes(units),
        *gate._internal_coordination_authority_violations(units),
        *gate._dml_callable_escape_violations(units),
        *gate._unknown_or_raw_execution_violations(units),
        *gate._raw_write_surface_violations(units),
        *gate._cross_database_violations(units),
    )

    return {
        "tree": str(tree),
        "pins": {
            "production_calls": pin(
                production,
                gate._canonical_digest(production),
                gate._EXPECTED_CALL_COUNT,
                gate._EXPECTED_PRODUCTION_CALLER_SHA256,
            ),
            "coordination_calls": pin(
                coordination,
                gate._canonical_digest(coordination),
                gate._EXPECTED_COORDINATION_CALL_COUNT,
                gate._EXPECTED_COORDINATION_CALL_SHA256,
            ),
            "internal_edges": pin(
                internal,
                gate._canonical_digest(internal),
                gate._EXPECTED_INTERNAL_EDGE_COUNT,
                gate._EXPECTED_INTERNAL_EDGE_SHA256,
            ),
            "subordinate_edges": pin(
                subordinate,
                gate._canonical_digest(subordinate),
                gate._EXPECTED_SUBORDINATE_EDGE_COUNT,
                gate._EXPECTED_SUBORDINATE_EDGE_SHA256,
            ),
        },
        "violations": {
            "caller_authority": violation(gate._caller_authority_violations(units), frozen_set_test),
            "coordination_caller_authority": violation(gate._coordination_caller_authority_violations(units), frozen_set_test),
            "api_authority": violation(gate._api_authority_violations(units), api_test),
            "transaction_order": violation(gate._transaction_order_violations(units, dml), transaction_test),
            "escape": violation(escapes, escape_test),
        },
    }


def classify(live_rows: Sequence[dict[str, str]], base_rows: Sequence[dict[str, str]]) -> dict[str, list[str]]:
    """Split two row sets into unchanged, arrived, departed and fingerprint-moved.

    A row identity can repeat within one inventory when a caller reaches the same
    helper more than once and the gate gives every such call ``ordinal=1``; those
    rows are separated only by their fingerprint. Such an identity is reported as
    AMBIGUOUS rather than guessed at, because a move and a remove-plus-add are
    genuinely indistinguishable there.
    """
    live: dict[tuple[str, ...], list[str]] = {}
    base: dict[tuple[str, ...], list[str]] = {}
    for rows, target in ((live_rows, live), (base_rows, base)):
        for fields in rows:
            target.setdefault(identity(fields), []).append(fields.get("call_fingerprint", ""))

    out: dict[str, list[str]] = {"arrived": [], "departed": [], "moved": [], "ambiguous": [], "unchanged": []}
    for key in sorted(set(live) | set(base)):
        label = " | ".join(key)
        if key not in base:
            out["arrived"].append(label)
        elif key not in live:
            out["departed"].append(label)
        elif len(live[key]) > 1 or len(base[key]) > 1:
            out["ambiguous"].append(f"{label} (live x{len(live[key])}, base x{len(base[key])})")
        elif live[key] != base[key]:
            out["moved"].append(f"{label} ({base[key][0]} -> {live[key][0]})")
        else:
            out["unchanged"].append(label)
    return out


def report(result: dict[str, Any], baseline: dict[str, Any] | None) -> None:
    print(f"tree: {result['tree']}")
    print()
    print("PINNED INVENTORIES")
    for name, entry in result["pins"].items():
        matched = (entry["live_count"], entry["live_digest"]) == (entry["pinned_count"], entry["pinned_digest"])
        flag = "ok " if matched else "PIN"
        print(f"  {flag} {name:24s} live={entry['live_count']:4d}  pinned={entry['pinned_count']:4d}")
        if not matched:
            print(f"      live digest   {entry['live_digest']}")
            print(f"      pinned digest {entry['pinned_digest']}")
    print()
    print("VIOLATION SETS (a non-zero count fails its test and hides every check behind it)")
    for name, entry in result["violations"].items():
        print(f"      {name:30s} {entry['count']:4d}  {entry['test']}")

    if baseline is None:
        return
    print()
    print(f"CLASSIFIED AGAINST {baseline['tree']}")
    kinds = ("arrived", "departed", "moved", "ambiguous")
    for name, entry in result["pins"].items():
        split = classify(entry["rows"], baseline["pins"][name]["rows"])
        counts = {kind: len(split[kind]) for kind in kinds}
        if not any(counts.values()):
            continue
        print(f"  {name}: " + "  ".join(f"{kind}={counts[kind]}" for kind in kinds))
        for kind in kinds:
            for label in split[kind]:
                print(f"      {kind[:4].upper():5s} {label}")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tree", type=Path, help="checkout to measure (a git archive of a commit measures that commit)")
    parser.add_argument("--baseline", type=Path, default=None, help="second checkout to classify inventory rows against")
    parser.add_argument("--json", type=Path, default=None, help="write the full measurement, rows included, here")
    args = parser.parse_args(list(argv) if argv is not None else None)

    result = measure(args.tree.resolve())
    baseline = measure(args.baseline.resolve()) if args.baseline is not None else None
    report(result, baseline)

    if args.json is not None:
        args.json.write_text(json.dumps({"live": result, "baseline": baseline}, indent=1))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
