"""Pin the post-verification posture of every durable operation-receipt replay.

The route utility projects a stored response and checks its response hash before
calling ``after_verified``. A replay projection must be side-effect-free: a
write before the hash check could mutate audit-primary state even when the
stored response is corrupt. This inventory pins every call and whether it
declares a post-verification repair hook. Behavioral ordering is exercised by
the fork and state-revert route regressions.

Counts, rather than a set of call-site names, catch duplicate calls within one
route. The companion rebinding gate keeps literal-callee matching honest: an
alias, partial, or dynamic lookup cannot silently evade this inventory.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

from tests.helpers.tree_gate import iter_gate_sources

_ROOT = Path(__file__).resolve().parents[3]
_SOURCE_ROOT = _ROOT / "src" / "elspeth"
_PRIMITIVE_NAME = "reserve_or_replay_operation_receipt"

# (module relative to src/elspeth, enclosing function, after_verified present)
# -> exact call count. State revert repairs interpretation review surfacing only
# after its replayed response hash has been checked; fork has no replay write.
_EXPECTED_SITES: dict[tuple[str, str, bool], int] = {
    ("web/sessions/routes/composer/state.py", "revert_state", True): 2,
    ("web/sessions/routes/sessions.py", "fork_from_message", False): 1,
}


def _callee_identifier(callee: ast.expr) -> str | None:
    if isinstance(callee, ast.Name):
        return callee.id
    if isinstance(callee, ast.Attribute):
        return callee.attr
    return None


class _ReplaySiteVisitor(ast.NodeVisitor):
    def __init__(self, relative: str, sites: Counter[tuple[str, str, bool]], rebindings: list[tuple[str, int, str]]) -> None:
        self._relative = relative
        self._sites = sites
        self._rebindings = rebindings
        self._functions: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._functions.append(node.name)
        self.generic_visit(node)
        self._functions.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._functions.append(node.name)
        self.generic_visit(node)
        self._functions.pop()

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == _PRIMITIVE_NAME and alias.asname not in (None, alias.name):
                self._rebindings.append((self._relative, node.lineno, f"aliased import as {alias.asname!r}"))
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.rsplit(".", 1)[-1] == _PRIMITIVE_NAME and alias.asname not in (None, alias.name):
                self._rebindings.append((self._relative, node.lineno, f"aliased import as {alias.asname!r}"))
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == _PRIMITIVE_NAME:
            self._rebindings.append((self._relative, node.lineno, f"bare reference ({type(node.ctx).__name__})"))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == _PRIMITIVE_NAME:
            self._rebindings.append((self._relative, node.lineno, f"bare attribute reference ({type(node.ctx).__name__})"))
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and node.value == _PRIMITIVE_NAME:
            self._rebindings.append((self._relative, node.lineno, "name as a string constant (dynamic lookup)"))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if _callee_identifier(node.func) == _PRIMITIVE_NAME:
            enclosing = self._functions[-1] if self._functions else "<module>"
            passes_after_verified = any(keyword.arg == "after_verified" for keyword in node.keywords)
            self._sites[(self._relative, enclosing, passes_after_verified)] += 1
            if isinstance(node.func, ast.Attribute):
                self.visit(node.func.value)
        else:
            self.visit(node.func)
        for argument in node.args:
            self.visit(argument)
        for keyword in node.keywords:
            self.visit(keyword)


def _scan_production_tree() -> tuple[Counter[tuple[str, str, bool]], list[tuple[str, int, str]]]:
    sites: Counter[tuple[str, str, bool]] = Counter()
    rebindings: list[tuple[str, int, str]] = []
    for parsed in iter_gate_sources(_SOURCE_ROOT):
        relative = parsed.path.relative_to(_SOURCE_ROOT).as_posix()
        _ReplaySiteVisitor(relative, sites, rebindings).visit(parsed.tree)
    return sites, rebindings


def test_every_operation_receipt_replay_site_declares_its_after_verified_posture() -> None:
    sites, _rebindings = _scan_production_tree()
    assert dict(sites) == _EXPECTED_SITES


def test_the_operation_receipt_replay_primitive_is_never_rebound() -> None:
    sites, rebindings = _scan_production_tree()
    assert sum(sites.values()) == sum(_EXPECTED_SITES.values())
    assert rebindings == []
