"""Join lane rationale sidecars onto live tier-model findings by AST path.

Usage (from the MERGED tree, key-free shell):

    ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \\
    PYTHONPATH=elspeth-lints/src .venv/bin/python \\
      docs/plans/2026-08-30-per-file-blanket-migration-tools/sidecar_join.py \\
      docs/agents/sweeps/tier-burndown/*.rationales.json > annotate_map.json

Sidecar keys come in two shapes (both produced by lanes from ``Finding`` fields,
never hand-typed):

    <file>:<RULE>:<sym>[:<sym>...]:ast=<ast_path>
    <file>:<RULE>:<sym>[:<sym>...]:<ast_path>:fp=<hex>

Keys beginning with ``_`` are sidecar metadata and are skipped. The join axis is
``(file, rule, symbol_context, ast_path)`` — never the ``(file, rule, symbol)``
triple (non-unique in every bucket) and never a lane's cached ``fp=`` (valid only
at the lane's own tip; fingerprints hash the positional ast path). The ``fp=``
emitted here is re-derived from the merged tree, which is what ``stage_annotate``
needs.

Exit 1 with the unbound keys on stderr if any sidecar key fails to bind, or if
two sidecar keys bind to the same live finding.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from elspeth_lints.core.tier_model_scan import scan_tree_findings
from elspeth_lints.rules.trust_tier.tier_model.rule import Finding

_ROOT = Path("src/elspeth")


def _parse_key(key: str) -> tuple[str, str, tuple[str, ...], str]:
    parts = key.split(":")
    if len(parts) < 4:
        raise ValueError(f"sidecar key has too few segments: {key!r}")
    file_path, rule = parts[0], parts[1]
    if parts[-1].startswith("fp="):
        ast_path = parts[-2]
        symbols = tuple(parts[2:-2])
    else:
        ast_idx = next((i for i, p in enumerate(parts) if p.startswith("ast=")), None)
        if ast_idx is None:
            raise ValueError(f"sidecar key carries neither ast= nor fp= segment: {key!r}")
        ast_path = parts[ast_idx][len("ast=") :]
        symbols = tuple(parts[2:ast_idx])
    return file_path, rule, symbols, ast_path


def main(sidecars: list[str]) -> int:
    live: dict[tuple[str, str, tuple[str, ...], str], str] = {}
    for finding in scan_tree_findings(root=_ROOT):
        if not isinstance(finding, Finding):
            continue
        axis = (finding.file_path, finding.rule_id, finding.symbol_context, finding.ast_path)
        if axis in live:
            sys.stderr.write(f"live tree has two findings at one axis: {axis}\n")
            return 1
        live[axis] = finding.canonical_key

    out: dict[str, str] = {}
    bound_from: dict[str, str] = {}
    unbound: list[str] = []
    per_sidecar: dict[str, int] = {}
    for sidecar in sidecars:
        data = json.loads(Path(sidecar).read_text())
        count = 0
        for key, rationale in data.items():
            if key.startswith("_"):
                continue
            if not isinstance(rationale, str) or not rationale.strip():
                sys.stderr.write(f"{sidecar}: non-string or empty rationale at {key}\n")
                return 1
            axis = _parse_key(key)
            fp_key = live.get(axis)
            if fp_key is None:
                unbound.append(f"{sidecar}: {key}")
                continue
            if fp_key in out:
                sys.stderr.write(f"two sidecar keys bind to {fp_key}: {bound_from[fp_key]} and {key}\n")
                return 1
            out[fp_key] = rationale
            bound_from[fp_key] = key
            count += 1
        per_sidecar[Path(sidecar).name] = count

    for name, count in sorted(per_sidecar.items()):
        sys.stderr.write(f"{name}: {count} bound\n")
    sys.stderr.write(f"total bound={len(out)} unbound={len(unbound)}\n")
    if unbound:
        for item in unbound:
            sys.stderr.write(f"UNBOUND {item}\n")
        return 1
    json.dump(out, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
