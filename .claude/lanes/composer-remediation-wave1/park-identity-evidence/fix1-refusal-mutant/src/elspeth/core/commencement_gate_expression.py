"""Shared commencement gate expression contract."""

from __future__ import annotations

import ast
from typing import cast

from elspeth.core.expression_parser import ExpressionParser

# No "env": the namespace was never populated (the sole engine caller passed
# nothing) and was the surface of elspeth-83261b699c — "Commencement gate
# non-bool failure can echo env secret values into audit/error text".
COMMENCEMENT_GATE_ALLOWED_NAMES = ("collections", "dependency_runs")
_REDACTED_STRING_LITERAL = "<redacted-string-literal>"


class _CommencementGateLiteralRedactor(ast.NodeTransformer):
    """Redact value literals while retaining the expression's lookup shape."""

    def _visit_lookup_key(self, node: ast.expr) -> ast.expr:
        """Retain a direct string key; redact literals inside composite keys."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node
        return cast(ast.expr, self.visit(node))

    def visit_Constant(self, node: ast.Constant) -> ast.Constant:
        if isinstance(node.value, str):
            return ast.copy_location(ast.Constant(value=_REDACTED_STRING_LITERAL), node)
        return node

    def visit_Subscript(self, node: ast.Subscript) -> ast.Subscript:
        # Retain an honest direct key for structural diagnosis. Composite key
        # expressions still need ordinary traversal so their value literals do
        # not become an audit/error escape hatch.
        node.value = self.visit(node.value)
        node.slice = self._visit_lookup_key(node.slice)
        return node

    def visit_Call(self, node: ast.Call) -> ast.Call:
        # The restricted grammar admits ``collections.get(key)`` and
        # ``dependency_runs.get(key)`` as map lookups. Preserve their key just
        # as visit_Subscript does; ordinary builtin-call arguments remain
        # values and are recursively redacted.
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in COMMENCEMENT_GATE_ALLOWED_NAMES
            and node.func.attr == "get"
        ):
            node.args = [self._visit_lookup_key(arg) for arg in node.args]
            return node
        return cast(ast.Call, self.generic_visit(node))

    def visit_Dict(self, node: ast.Dict) -> ast.Dict:
        # Preserve direct dict keys, but recurse into composite keys so their
        # non-key literals follow the same rule as subscript and .get keys.
        node.keys = [self._visit_lookup_key(key) if key is not None else None for key in node.keys]
        node.values = [self.visit(value) for value in node.values]
        return node


def validate_commencement_gate_condition(condition: str) -> None:
    """Validate a commencement gate condition against the shared context contract."""
    ExpressionParser(condition, allowed_names=list(COMMENCEMENT_GATE_ALLOWED_NAMES))


def redact_commencement_gate_condition(condition: str) -> str:
    """Render a validated gate condition without non-key string literals."""
    validate_commencement_gate_condition(condition)
    tree = ast.parse(condition, mode="eval")
    redacted = _CommencementGateLiteralRedactor().visit(tree)
    return ast.unparse(ast.fix_missing_locations(redacted))
