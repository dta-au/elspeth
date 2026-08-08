"""Shared site inventory for the ``masquerade.attribute-probes`` gate.

This module is the **single** AST walker that both the rule
(``rule.py``) and the baseline seeder (``seed_baseline.py``) use. Per the
ticket's blocking amendment A1, the rule and the seeder MUST compute
``(path, qualname, kind)`` identically — two independent implementations
that happen to agree on samples is exactly the failure mode that produces
simultaneous missing-entry and stale-entry findings on the same site.
Both consumers therefore call :func:`iter_masquerade_sites` and nothing
else computes a site identity.

Threat model (ADR-032, docs/architecture/adr/032-validate-by-trust-domain.md):
an object that merely *looks* like the right shape should not be able to
cross a trust boundary and acquire the privileges of the thing it
imitates. ``getattr``, ``hasattr``, and ``inspect.getattr_static`` are
banned as *internal*-contract presence probes but are the prescribed
mechanism at *external* boundaries (sentinel-defaulted ``getattr`` +
value assertions + construction of an owned type). The ban is about
*purpose*, not *construct* — see the module docstring of ``rule.py`` for
the full amnesty rationale.

Site identity
--------------
``qualname`` is the dotted path of enclosing ``ClassDef``/``FunctionDef``/
``AsyncFunctionDef`` names, innermost last, with ``"<module>"`` standing in
for a site with no enclosing def (module top-level). It deliberately
excludes line numbers — a moved or reformatted site must keep the same
identity, and a renamed/relocated site must re-fire for re-adjudication
(PLAN.md §A, Correction to "gate first, narrow"). ``Lambda`` contributes a
synthetic ``"<lambda>"`` frame so a site inside a lambda has a stable,
if coarse, qualname.

Multiple textually distinct sites can share one ``(path, qualname, kind)``
triple (e.g. two ``getattr`` calls in the same function). The baseline
records ADJUDICATION OF THE SITE-SHAPE, not a per-call-site count: one
baseline entry covers every current and future call of that kind at that
qualname. This is a deliberate, documented weakening in exchange for a
qualname-keyed (rather than line-keyed) identity — see
``config/cicd/masquerade_baseline.yaml``'s header comment.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from elspeth_lints.rules.trust_boundary.shared import extract_keywords, iter_boundary_decorators

SiteKind = Literal["getattr", "hasattr", "getattr_static", "dunder_getattr"]

ALL_KINDS: tuple[SiteKind, ...] = ("getattr", "hasattr", "getattr_static", "dunder_getattr")

MODULE_QUALNAME = "<module>"
_LAMBDA_FRAME = "<lambda>"


@dataclass(frozen=True, slots=True)
class MasqueradeSite:
    """One candidate attribute-masquerading site.

    ``amnesty`` / ``amnesty_reason`` record whether this exact site is
    structurally recognised as one of the three permanently-green
    idioms (PEP 562 module ``__getattr__`` over a closed table,
    ``assert [not] hasattr(...)`` presence-as-subject in tests, or a
    sentinel 3-arg ``getattr`` on a ``@trust_boundary``/
    ``@observation_boundary`` function's ``source_param``). Amnestied
    sites need no baseline entry and can never regress into requiring
    one merely by existing.
    """

    path: str
    qualname: str
    kind: SiteKind
    line: int
    column: int
    arity: int | None
    literal_name: bool | None
    amnesty: bool
    amnesty_reason: str | None


def compute_qualname(stack: Sequence[str]) -> str:
    """Return the dotted qualname for a class/function nesting stack."""
    return ".".join(stack) if stack else MODULE_QUALNAME


def iter_masquerade_sites(tree: ast.Module, display_path: str) -> list[MasqueradeSite]:
    """Return every candidate masquerade site in ``tree``.

    ``display_path`` is the repository-relative, forward-slash path used
    for both the finding's ``file_path`` and the baseline's ``path`` key
    (e.g. ``"src/elspeth/foo.py"``, ``"tests/unit/test_foo.py"``). The
    ``tests`` bucket (paths starting ``"tests/"``) is the only one where
    the ``assert hasattr(...)`` amnesty applies.
    """
    boundary_source_params = _boundary_source_params(tree)
    module_tables = _module_level_literal_containers(tree)
    in_tests = display_path == "tests" or display_path.startswith("tests/")
    visitor = _MasqueradeVisitor(
        display_path=display_path,
        boundary_source_params=boundary_source_params,
        module_tables=module_tables,
        in_tests=in_tests,
    )
    visitor.visit(tree)
    return visitor.sites


def _boundary_source_params(tree: ast.AST) -> dict[int, str]:
    """Map ``id(function_node) -> source_param`` for decorated boundary functions.

    Reuses ``trust_boundary.shared.iter_boundary_decorators`` (import-alias
    aware, recognises both ``@trust_boundary`` and ``@observation_boundary``)
    rather than a hand-rolled decorator-name match, which would miss
    aliased imports and the ``observation_boundary`` spelling — exactly the
    gap PLAN.md Correction 2 calls out in the scratch inventory script.
    """
    result: dict[int, str] = {}
    for match in iter_boundary_decorators(tree):
        extraction = extract_keywords(match.call)
        if extraction.kwargs is None:
            continue
        source_param = extraction.kwargs.get("source_param")
        if isinstance(source_param, str) and source_param:
            result[id(match.function)] = source_param
    return result


def _module_level_literal_containers(tree: ast.Module) -> frozenset[str]:
    """Return names of module-level assignments that are closed string tables.

    Recognises ``NAME = {...}`` / ``(...)`` / ``[...]`` / ``{...}`` (dict,
    tuple, list, set literals) and ``NAME = frozenset((...))`` /
    ``NAME = set((...))`` wrapping such a literal, where every key/element
    is a ``str`` constant. This is the "closed literal table" a module-level
    ``__getattr__`` may safely gate on (Correction 1 / PLAN.md §B.2).
    """
    names: set[str] = set()
    for stmt in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target, value = stmt.target, stmt.value
        if isinstance(target, ast.Name) and value is not None and _is_literal_str_container(value):
            names.add(target.id)
    return frozenset(names)


def _is_literal_str_container(node: ast.expr) -> bool:
    value_node = node
    if isinstance(node, ast.Call):
        func = node.func
        func_name = func.id if isinstance(func, ast.Name) else None
        if func_name not in {"frozenset", "set"} or len(node.args) != 1 or node.keywords:
            return False
        value_node = node.args[0]
    if not isinstance(value_node, (ast.Dict, ast.Tuple, ast.List, ast.Set)):
        return False
    try:
        value = ast.literal_eval(value_node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False
    if isinstance(value, dict):
        return all(isinstance(key, str) for key in value)
    if isinstance(value, (tuple, list, set)):
        return all(isinstance(item, str) for item in value)
    return False


class _MasqueradeVisitor(ast.NodeVisitor):
    """Single-pass walker computing site identity, kind, and amnesty flags."""

    def __init__(
        self,
        *,
        display_path: str,
        boundary_source_params: dict[int, str],
        module_tables: frozenset[str],
        in_tests: bool,
    ) -> None:
        self._display_path = display_path
        self._boundary_source_params = boundary_source_params
        self._module_tables = module_tables
        self._in_tests = in_tests
        self._qual_stack: list[str] = []
        self._container_stack: list[str] = ["module"]
        self._func_stack: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        self._alias_stack: list[dict[str, bool]] = []
        self._assert_direct_call_ids: set[int] = set()
        self.sites: list[MasqueradeSite] = []

    # -- scope bookkeeping -------------------------------------------------

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._qual_stack.append(node.name)
        self._container_stack.append("class")
        self.generic_visit(node)
        self._container_stack.pop()
        self._qual_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        parent_kind = self._container_stack[-1]
        self._qual_stack.append(node.name)
        self._container_stack.append("function")
        self._func_stack.append(node)
        self._alias_stack.append({})
        if node.name == "__getattr__":
            self._record_dunder_getattr(node, parent_kind)
        self.generic_visit(node)
        self._alias_stack.pop()
        self._func_stack.pop()
        self._container_stack.pop()
        self._qual_stack.pop()

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._qual_stack.append(_LAMBDA_FRAME)
        self._container_stack.append("function")
        self.generic_visit(node)
        self._container_stack.pop()
        self._qual_stack.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        if self._func_stack and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Name):
            source_param = self._boundary_source_params.get(id(self._func_stack[-1]))
            if source_param is not None and node.value.id == source_param:
                self._alias_stack[-1][node.targets[0].id] = True
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        test = node.test
        direct_call: ast.Call | None = None
        if isinstance(test, ast.Call):
            direct_call = test
        elif isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and isinstance(test.operand, ast.Call):
            direct_call = test.operand
        if direct_call is not None:
            self._assert_direct_call_ids.add(id(direct_call))
        self.generic_visit(node)

    # -- call-site detection -------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name: str | None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        else:
            name = None
        if name == "getattr":
            self._record_getattr(node)
        elif name == "hasattr":
            self._record_hasattr(node)
        elif name == "getattr_static":
            self._record_getattr_static(node)
        self.generic_visit(node)

    def _record_getattr(self, node: ast.Call) -> None:
        arity = len(node.args)
        literal_name = len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
        amnesty = False
        amnesty_reason: str | None = None
        if arity == 3 and literal_name and self._receiver_is_boundary_param(node.args[0]):
            amnesty = True
            amnesty_reason = "trust-boundary-sentinel"
        self._append("getattr", node, arity=arity, literal_name=literal_name, amnesty=amnesty, amnesty_reason=amnesty_reason)

    def _record_hasattr(self, node: ast.Call) -> None:
        amnesty = self._in_tests and id(node) in self._assert_direct_call_ids
        self._append(
            "hasattr",
            node,
            arity=len(node.args),
            literal_name=None,
            amnesty=amnesty,
            amnesty_reason="assert-hasattr" if amnesty else None,
        )

    def _record_getattr_static(self, node: ast.Call) -> None:
        # No structural amnesty is recognised for getattr_static: per the
        # gate spec, every use is baseline-required. This is a deliberate
        # simplification — PLAN.md notes two live sites are syntactically
        # recognisable (result consumed by `is`, or feeding an
        # isinstance-then-raise), but "intent, not construct" makes a
        # general recognizer unreliable, and the corpus is small (n=10)
        # so per-site human adjudication is cheap. Unrecognised always
        # falls through to baseline-required, never to silently green.
        self._append("getattr_static", node, arity=len(node.args), literal_name=None, amnesty=False, amnesty_reason=None)

    def _record_dunder_getattr(self, node: ast.FunctionDef | ast.AsyncFunctionDef, parent_kind: str) -> None:
        amnesty = False
        amnesty_reason: str | None = None
        if parent_kind == "module" and _is_closed_table_gated(node, self._module_tables):
            amnesty = True
            amnesty_reason = "module-getattr"
        self._append("dunder_getattr", node, arity=None, literal_name=None, amnesty=amnesty, amnesty_reason=amnesty_reason)

    def _receiver_is_boundary_param(self, receiver: ast.expr) -> bool:
        if not self._func_stack:
            return False
        source_param = self._boundary_source_params.get(id(self._func_stack[-1]))
        if source_param is None:
            return False
        if isinstance(receiver, ast.Name):
            if receiver.id == source_param:
                return True
            return self._alias_stack[-1].get(receiver.id, False)
        return False

    def _append(
        self,
        kind: SiteKind,
        node: ast.stmt | ast.expr,
        *,
        arity: int | None,
        literal_name: bool | None,
        amnesty: bool,
        amnesty_reason: str | None,
    ) -> None:
        self.sites.append(
            MasqueradeSite(
                path=self._display_path,
                qualname=compute_qualname(self._qual_stack),
                kind=kind,
                line=node.lineno,
                column=node.col_offset,
                arity=arity,
                literal_name=literal_name,
                amnesty=amnesty,
                amnesty_reason=amnesty_reason,
            )
        )


def _is_closed_table_gated(func_node: ast.FunctionDef | ast.AsyncFunctionDef, tables: frozenset[str]) -> bool:
    """Return True for the recognised PEP 562 "closed table, else raise" shape.

    Recognised shape: a flat sequence of ``if <literal-name-gate>:`` guards
    (no ``elif``/``else`` — each guard is its own top-level statement) whose
    single positional parameter (``name``) is compared for equality against
    a string literal, or membership in a closed table (a module-level
    literal string container, or an inline tuple/list/set of string
    literals), followed by a final unconditional ``raise AttributeError(...)``.

    This is intentionally narrower than "every path is gated" (PLAN.md
    §B-crux item 2) — a chain using ``elif`` or any shape this recognizer
    does not positively confirm simply falls through to baseline-required,
    never to silently green. Confirmed-clean sites are exactly the
    module-level facades cited in Finding 4 (``contracts/errors.py``,
    ``core/security/__init__.py``, ``engine/__init__.py``,
    ``engine/orchestrator/__init__.py``), all of which use flat sequential
    ``if`` guards, not ``elif`` chains.
    """
    positional = [*func_node.args.posonlyargs, *func_node.args.args]
    if len(positional) != 1:
        return False
    param_name = positional[0].arg
    body = func_node.body
    if not body:
        return False
    *guard_stmts, last = body
    for stmt in guard_stmts:
        if not (isinstance(stmt, ast.If) and not stmt.orelse):
            return False
        if not _is_name_literal_gate(stmt.test, param_name, tables):
            return False
    return _is_attribute_error_raise(last)


def _is_name_literal_gate(test: ast.expr, param_name: str, tables: frozenset[str]) -> bool:
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1 and len(test.comparators) == 1):
        return False
    left = test.left
    if not (isinstance(left, ast.Name) and left.id == param_name):
        return False
    op = test.ops[0]
    comparator = test.comparators[0]
    if isinstance(op, ast.Eq):
        return isinstance(comparator, ast.Constant) and isinstance(comparator.value, str)
    if isinstance(op, ast.In):
        if isinstance(comparator, ast.Name):
            return comparator.id in tables
        if isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
            return all(isinstance(elt, ast.Constant) and isinstance(elt.value, str) for elt in comparator.elts)
    return False


def _is_attribute_error_raise(stmt: ast.stmt) -> bool:
    if not isinstance(stmt, ast.Raise) or stmt.exc is None:
        return False
    exc = stmt.exc
    func = exc.func if isinstance(exc, ast.Call) else exc
    if isinstance(func, ast.Name):
        return func.id == "AttributeError"
    if isinstance(func, ast.Attribute):
        return func.attr == "AttributeError"
    return False
