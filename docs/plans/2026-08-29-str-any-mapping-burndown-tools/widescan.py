"""Prototype scan behind the 2026-08-29 [str, Any] burn-down plan.

Reports every ``dict|Dict|Mapping|MutableMapping[str, Any]`` subscript in
``src/elspeth`` regardless of syntactic position, and writes the raw hit list
as JSON for ``bucket.py``. Run from the repository root::

    python widescan.py hits.json

This is a sizing tool, not the gate: Task 1 of the plan folds the same walk
into ``scripts/check_contracts.py`` with slot-addressed keys and tests.
"""

from __future__ import annotations

import ast
import collections
import json
import sys
from pathlib import Path

ROOT = Path("src/elspeth")
MAPPING_NAMES = frozenset({"dict", "Dict", "Mapping", "MutableMapping"})


def _is_str(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "str"


def _is_any(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == "Any") or (isinstance(node, ast.Attribute) and node.attr == "Any")


def spelling_of(node: ast.AST) -> str | None:
    """Return the mapping spelling when ``node`` is ``<Mapping>[str, Any]``."""
    if not (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Tuple) and len(node.slice.elts) == 2):
        return None
    base = node.value
    name = base.id if isinstance(base, ast.Name) else base.attr if isinstance(base, ast.Attribute) else None
    if name not in MAPPING_NAMES:
        return None
    key_t, val_t = node.slice.elts
    if _is_str(key_t) and _is_any(val_t):
        return name
    return None


def hits_in_file(path: Path) -> list[tuple[str, int, str, str]]:
    """Every hit as ``(file, line, spelling, form)`` where form is ``ast`` or ``string``."""
    tree = ast.parse(path.read_text())
    hits: list[tuple[str, int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "[str" in node.value and "Any]" in node.value:
            try:
                parsed = ast.parse(node.value, mode="eval").body
            except SyntaxError:
                continue
            hits.extend((str(path), node.lineno, s, "string") for inner in ast.walk(parsed) if (s := spelling_of(inner)))
            continue
        spelling = spelling_of(node)
        if spelling is not None:
            hits.append((str(path), node.lineno, spelling, "ast"))
    return hits


def main(out_path: Path) -> None:
    hits = [h for f in sorted(ROOT.rglob("*.py")) for h in hits_in_file(f)]
    by_file = collections.Counter(h[0] for h in hits)
    by_kind = collections.Counter(h[2] for h in hits)
    string_form = sum(1 for h in hits if h[3] == "string")
    sys.stdout.write(f"TOTAL {len(hits)} files {len(by_file)} kinds {dict(by_kind)} string-form {string_form}\n")
    for file, count in by_file.most_common(40):
        with Path(file).open() as fh:
            loc = sum(1 for _ in fh)
        sys.stdout.write(f"{count:4} {loc:6} {file}\n")
    with out_path.open("w") as fh:
        json.dump(hits, fh)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
