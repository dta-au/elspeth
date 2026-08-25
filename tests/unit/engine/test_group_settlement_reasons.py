"""Closed group-settlement reason vocabulary (unified-lineage spec §2/§6.4, ADR-042, WS6).

Reasons are categorical tokens from ONE StrEnum — never free prose, never
hand-written strings at emission sites. ``scope_group_failed`` is the reason
for a member terminated because its group had already FAILED when it
arrived; ``late_arrival_after_merge`` is reserved for arrival after a
SUCCESSFUL merge.

SCOPE (META-9.3): the enum covers coalesce and scope/collector settlement.
row_union's reasons (``row_union_branch_lost``, ``late_arrival_after_release``,
``row_union_group_failed``) are a sibling vocabulary and are deliberately
NOT members — the second test pins that boundary in both directions.
"""

from __future__ import annotations

import ast
from pathlib import Path

from elspeth.contracts.enums import GroupSettlementReason

REPO_ROOT = Path(__file__).resolve().parents[3]
_ENUM_SOURCE = REPO_ROOT / "src" / "elspeth" / "contracts" / "enums.py"


def test_vocabulary_is_exactly_the_specced_four() -> None:
    assert {r.value for r in GroupSettlementReason} == {
        "late_arrival_after_merge",
        "scope_group_failed",
        "empty_expansion",
        "all_members_lost",
    }


def test_row_union_reasons_stay_outside_the_enum() -> None:
    """META-9.3: row_union's closed reasons are a sibling vocabulary, not members."""
    values = {r.value for r in GroupSettlementReason}
    for row_union_reason in ("row_union_branch_lost", "late_arrival_after_release", "row_union_group_failed"):
        assert row_union_reason not in values


def test_no_production_module_hand_writes_a_settlement_reason_literal() -> None:
    """Emission sites reference enum members, never the string (ADR-042 D1).

    Whole-``src`` AST walk over string constants; the enum's own definition
    is the single permitted occurrence. Comments and docstrings are not
    ``ast.Constant`` expression values in a load-bearing position — a
    docstring IS a Constant, so module/class/function docstrings are skipped
    explicitly rather than by luck.
    """
    vocabulary = {r.value for r in GroupSettlementReason}
    hits: list[tuple[str, int, str]] = []
    for path in sorted((REPO_ROOT / "src" / "elspeth").rglob("*.py")):
        if path == _ENUM_SOURCE:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        docstring_nodes: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                    docstring_nodes.add(id(first.value))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstring_nodes
                and node.value in vocabulary
            ):
                hits.append((str(path.relative_to(REPO_ROOT)), node.lineno, node.value))
    assert hits == [], f"settlement reason written as a literal instead of GroupSettlementReason: {hits!r}"
