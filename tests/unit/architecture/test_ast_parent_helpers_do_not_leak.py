"""The architecture scans' parent-link helpers must not write onto CPython's shared AST context nodes.

``ast.parse`` reuses one ``Load``/``Store``/``Del`` instance across every tree it builds. A parent link
written onto one of them points every later tree in the process at the scanned tree, so a later
``copy.deepcopy`` of an unrelated tree follows it and can exhaust the recursion limit.
"""

from __future__ import annotations

import ast
from collections.abc import Callable

import pytest
from tests.unit.architecture.test_session_db_mutation_authority import _attach_parents as attach_inventory_parents
from tests.unit.architecture.test_web_landscape_mutation_fencing import _attach_parents as attach_landscape_parents

_HELPERS: dict[str, tuple[Callable[[ast.AST], None], str]] = {
    "landscape": (attach_landscape_parents, "_landscape_parent"),
    "inventory": (attach_inventory_parents, "_inventory_parent"),
}


def _shared_contexts() -> tuple[ast.expr_context, ...]:
    tree = ast.parse("x = y\ndel z")
    assign = tree.body[0]
    delete = tree.body[1]
    assert isinstance(assign, ast.Assign) and isinstance(delete, ast.Delete)
    target, value, deleted = assign.targets[0], assign.value, delete.targets[0]
    assert isinstance(target, ast.Name) and isinstance(value, ast.Name) and isinstance(deleted, ast.Name)
    return (value.ctx, target.ctx, deleted.ctx)


@pytest.mark.parametrize("helper", sorted(_HELPERS))
def test_parent_links_stay_inside_the_scanned_tree(helper: str) -> None:
    attach, attribute = _HELPERS[helper]
    scanned = ast.parse("value = source.field\nother = value\ndel other")
    try:
        attach(scanned)
        fresh = ast.parse("x").body[0]
        assert isinstance(fresh, ast.Expr) and isinstance(fresh.value, ast.Name)
        assert attribute not in vars(fresh.value.ctx)
        for shared in _shared_contexts():
            assert attribute not in vars(shared)
        # Control: the helper still links the scanned tree's own nodes.
        field = next(node for node in ast.walk(scanned) if isinstance(node, ast.Attribute))
        assert isinstance(vars(field)[attribute], ast.Assign)
    finally:
        # Keep a failing run from contaminating the rest of the worker.
        for shared in _shared_contexts():
            vars(shared).pop(attribute, None)
