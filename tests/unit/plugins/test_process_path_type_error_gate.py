"""Whole-tree gate: no bare ``TypeError`` escapes a plugin's ``process`` path.

Why this gate exists (elspeth-5887fb7928)
-----------------------------------------
A transform that finds a wrongly-typed row VALUE must reject it by RETURNING
``TransformResult.error(...)``: the row then leaves through the node's
``on_error``, and at a batch node (aggregation or collector) the whole batch
fails with a recorded, value-free reason. A bare ``TypeError`` does something
else entirely. It matches no conversion clause in the engine, so it escapes
``process`` and aborts the RUN: zero terminal outcomes, a traceback, exit 4
(ADR-008 §"TIER_1 registration is load-bearing", Correction 2026-08-21).

That ``raise TypeError(... "This indicates an upstream validation bug" ...)``
convention spread by copying: two type-enforcement commits seeded it,
every new batch-plugin family copied it, and one plan cited a sibling as its
precedent, until twelve sites in eleven batch plugins aborted runs on one bad
row. Each copy read like doctrine. This gate cuts that loop: a new copy turns
the branch red where the author is looking.

What it flags
-------------
For every class in every module under ``src/elspeth/plugins``, start at the
class's ``process`` method (its own or one inherited from a base class defined
in the same module) and follow every reference to another function in scope:

* ``self.<m>`` / ``cls.<m>`` / ``<ThisClass>.<m>``: methods of the class and its
  same-module bases, a subclass override winning;
* ``super().<m>``: ``<m>`` on every same-module ancestor of the class whose
  method makes the call (every one, not only the next in the MRO, so the walk
  can only over-reach);
* ``<OtherClass>.<m>``: a method of another class defined in the same module
  (a static helper such as ``_Checks.numeric(v)``);
* bare names of module-level functions;
* the name of a module-level dispatch table (a dict, list, tuple or set
  literal assigned at module level): every function or ``<Class>.<m>`` it holds.

A reference counts whether it is called or passed as a callback. A
``raise TypeError(...)``, or a ``raise err`` whose name the same function bound
with ``err = TypeError(...)``, reached this way is reported unless some ``try``
on the path (at the raise itself or at any reference that led to it) has a
handler that catches ``TypeError``, ``Exception``, ``BaseException`` or
everything, and that handler does not re-raise (``raise``, ``raise exc`` and
``raise exc.with_traceback(...)`` of the bound name are re-raises).

At base 74c0ce0db this flagged exactly the twelve original sites and nothing
else. Scoped to ``TypeError`` deliberately: widening to ``ValueError``,
``KeyError``, ``RuntimeError`` and ``NotImplementedError`` adds only lifecycle
and self-consistency invariants whose conditions do not read a row value
(heterogeneous contract modes, heterogeneous output schemas, "called before
on_start"). A second smell, not this one.

What it does NOT see (honest false-negative classes)
----------------------------------------------------
The scan is per module and per class. It does not prove that no ``TypeError``
can escape ``process``; it stops the explicit convention from spreading
through the shapes the twelve original sites used. It is blind to:

* a class whose ``process`` is inherited from a base in ANOTHER module: the
  class is never rooted, so the hooks it overrides are never scanned. On
  2026-09-24 that is 6 of the 38 registered transforms (the two Bedrock
  guardrails, AzureAISearch, AzureContentSafety, AzurePromptShield and
  RAGRetrieval, whose ``process`` lives in ``transforms/rag/core.py`` or
  ``transforms/azure/base.py`` or ``transforms/aws/_guardrail_transform.py``);
* a helper OBJECT the class composes (``self._builder.build(...)``), in the
  same module or another. This is the shape of the RAG query builder
  (``transforms/rag/query.py``), whose two ``raise TypeError`` sites aborted
  runs at 74c0ce0db and were fixed separately; the gate did not see them;
* helper functions and classes imported from another module, and a
  ``super()`` call that resolves to a base in another module;
* dispatch that is not a module-level literal: a table held as a class or
  instance attribute (``self._CHECKS[kind]``), built inside a function, or
  filled at runtime, and name-based dispatch (``getattr(self, name)``);
* implicit raises (``float(x)``, ``x < 1`` on a str, ``row[missing]``);
* raises inside nested functions and lambdas (a new scope);
* a ``TypeError`` subclass raised under its own name;
* a ``TypeError`` built anywhere but a direct ``TypeError(...)`` call bound in
  the raising function (``err = self._make_error()`` then ``raise err``, or a
  name bound in another function);
* a bare ``raise`` that re-raises a caught ``TypeError`` from inside a handler.

The escape hatch
----------------
There is no inline marker and no suppression. A ``raise TypeError`` on a
process path whose condition genuinely does not depend on a row value (for
example an owned-type invariant on ELSPETH's own context object) goes into
``REVIEWED_PROCESS_PATH_TYPE_ERRORS`` below, in the same change, with a
comment saying why the condition is not row data. Prefer raising a Tier-1
exception class instead; that needs no entry. The key names the file, the
function and the exception, never a line number, so an unrelated edit cannot
stale it, and each key admits exactly one site.
"""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.helpers.tree_gate import iter_gate_sources

PLUGINS_ROOT = Path(__file__).resolve().parents[3] / "src" / "elspeth" / "plugins"

BANNED_EXCEPTION = "TypeError"
ROOT_METHOD = "process"

# Handler names that absorb a TypeError: the class itself and its bases.
_ABSORBS_TYPE_ERROR = frozenset({BANNED_EXCEPTION, "Exception", "BaseException"})
_BARE_EXCEPT = frozenset({"Exception", "BaseException"})
_NEW_SCOPE = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

# Reviewed sites a whole-tree scan may report. Empty: the twelve original sites
# now raise BatchRowTypeError from their helpers and ``process`` converts it
# once to a returned error. Each entry is "<path under plugins>::<function>::TypeError"
# with a comment saying why its condition does not read a row value.
REVIEWED_PROCESS_PATH_TYPE_ERRORS: tuple[str, ...] = ()

# Anti-vacuity floor: how many plugin classes the scan roots at a process
# method. Measured 2026-09-24 on the lane tree (36), and 36 again on
# 2026-09-26. A walk that silently stopped seeing the plugin tree would report
# zero findings, which is exactly what a clean tree reports;
# test_a_scan_that_stopped_seeing_the_plugin_tree_fails_the_floor pins it.
MIN_PROCESS_ROOTS = 36


@dataclass(frozen=True, slots=True)
class ProcessPathRaise:
    """One ``raise TypeError`` reachable from a ``process`` method."""

    path: str
    function: str
    line: int

    @property
    def key(self) -> str:
        return f"{self.path}::{self.function}::{BANNED_EXCEPTION}"


@dataclass(frozen=True, slots=True)
class _Function:
    qualname: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    # The class whose body defines it; None for a module-level function.
    owner: str | None = None


@dataclass(frozen=True, slots=True)
class ModuleScan:
    raises: frozenset[ProcessPathRaise]
    process_roots: int


def _exception_name(expr: ast.expr) -> str | None:
    target = expr.func if isinstance(expr, ast.Call) else expr
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _reraises_bound_name(exc: ast.expr, bound: str) -> bool:
    """``raise exc`` or ``raise exc.with_traceback(...)`` for the handler's bound name."""
    if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Attribute) and exc.func.attr == "with_traceback":
        exc = exc.func.value
    return isinstance(exc, ast.Name) and exc.id == bound


def _handler_reraises(handler: ast.ExceptHandler) -> bool:
    """True when the handler body re-raises what it caught (in its own scope)."""
    pending: list[ast.AST] = list(handler.body)
    while pending:
        node = pending.pop()
        if isinstance(node, _NEW_SCOPE):
            continue
        if isinstance(node, ast.Raise):
            if node.exc is None:
                return True
            if handler.name is not None and _reraises_bound_name(node.exc, handler.name):
                return True
        pending.extend(ast.iter_child_nodes(node))
    return False


def _absorbed_names(handlers: list[ast.ExceptHandler]) -> frozenset[str]:
    names: set[str] = set()
    for handler in handlers:
        if _handler_reraises(handler):
            continue
        if handler.type is None:
            names |= _BARE_EXCEPT
            continue
        elements = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        for element in elements:
            name = _exception_name(element)
            if name is not None:
                names.add(name)
    return frozenset(names)


def _walk_guarded(node: ast.AST, guards: frozenset[str]) -> Iterator[tuple[ast.AST, frozenset[str]]]:
    """Yield every node in ``node``'s own scope with the handler names guarding it."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _NEW_SCOPE):
            continue
        if isinstance(child, (ast.Try, ast.TryStar)):
            body_guards = guards | _absorbed_names(child.handlers)
            for statement in child.body:
                yield statement, body_guards
                yield from _walk_guarded(statement, body_guards)
            for other in (*child.handlers, *child.orelse, *child.finalbody):
                yield other, guards
                yield from _walk_guarded(other, guards)
            continue
        yield child, guards
        yield from _walk_guarded(child, guards)


def _class_methods(
    cls: ast.ClassDef,
    module_classes: Mapping[str, ast.ClassDef],
    seen: frozenset[str] = frozenset(),
) -> dict[str, _Function]:
    """Methods visible on ``cls``: same-module bases first, then its own (overrides win)."""
    methods: dict[str, _Function] = {}
    for base in cls.bases:
        if isinstance(base, ast.Name) and base.id in module_classes and base.id not in seen:
            methods.update(_class_methods(module_classes[base.id], module_classes, seen | {cls.name}))
    for statement in cls.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            methods[statement.name] = _Function(f"{cls.name}.{statement.name}", statement, cls.name)
    return methods


def _class_lineage(cls: ast.ClassDef, module_classes: Mapping[str, ast.ClassDef]) -> frozenset[str]:
    names = {cls.name}
    pending = [cls]
    while pending:
        current = pending.pop()
        for base in current.bases:
            if isinstance(base, ast.Name) and base.id in module_classes and base.id not in names:
                names.add(base.id)
                pending.append(module_classes[base.id])
    return frozenset(names)


@dataclass(frozen=True, slots=True)
class _ModuleScope:
    """What a reference inside one module can resolve to."""

    functions: Mapping[str, _Function]
    classes: Mapping[str, ast.ClassDef]
    # Module-level containers (dispatch tables) naming in-scope functions: a
    # reference to the table reaches every function it holds.
    tables: Mapping[str, tuple[_Function, ...]]


def _table_members(value: ast.expr, functions: Mapping[str, _Function], classes: Mapping[str, ast.ClassDef]) -> tuple[_Function, ...]:
    """In-scope functions a module-level container literal holds (``{"int": _check_int}``, ``(_Checks.numeric,)``)."""
    if not isinstance(value, (ast.Dict, ast.List, ast.Tuple, ast.Set)):
        return ()
    members: list[_Function] = []
    for node in ast.walk(value):
        if isinstance(node, ast.Name) and node.id in functions:
            members.append(functions[node.id])
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in classes:
            helper_methods = _class_methods(classes[node.value.id], classes)
            if node.attr in helper_methods:
                members.append(helper_methods[node.attr])
    return tuple(members)


def _module_scope(tree: ast.Module) -> _ModuleScope:
    functions = {
        statement.name: _Function(statement.name, statement)
        for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    classes = {statement.name: statement for statement in tree.body if isinstance(statement, ast.ClassDef)}
    tables: dict[str, tuple[_Function, ...]] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name):
            target, value = statement.targets[0].id, statement.value
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) and statement.value is not None:
            target, value = statement.target.id, statement.value
        else:
            continue
        members = _table_members(value, functions, classes)
        if members:
            tables[target] = members
    return _ModuleScope(functions, classes, tables)


def _super_methods(owner: str | None, name: str, classes: Mapping[str, ast.ClassDef]) -> Iterator[_Function]:
    """What ``super().<name>`` inside a method of ``owner`` can reach: ``name`` on any same-module ancestor.

    Every ancestor defining it, not only the next one in the MRO: over-reaching
    can only add a finding, never hide one.
    """
    if owner is None or owner not in classes:
        return
    for ancestor in sorted(_class_lineage(classes[owner], classes) - {owner}):
        for statement in classes[ancestor].body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)) and statement.name == name:
                yield _Function(f"{ancestor}.{name}", statement, ancestor)


def _is_super_call(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "super"


def _references(
    function: _Function,
    scope: _ModuleScope,
    methods: Mapping[str, _Function],
    receivers: frozenset[str],
) -> Iterator[tuple[_Function, frozenset[str]]]:
    """Every in-scope function ``function`` refers to (called or passed), with its guards.

    ``self.``/``cls.``/``<ThisClass>.`` methods, ``super().`` methods of a
    same-module ancestor, methods of another same-module class named directly
    (``_Checks.numeric``), module-level functions, and every function a
    module-level dispatch table holds.
    """
    for node, guards in _walk_guarded(function.node, frozenset()):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in scope.functions:
                yield scope.functions[node.id], guards
            for member in scope.tables.get(node.id, ()):
                yield member, guards
        elif isinstance(node, ast.Attribute):
            if _is_super_call(node.value):
                for inherited in _super_methods(function.owner, node.attr, scope.classes):
                    yield inherited, guards
            elif isinstance(node.value, ast.Name) and node.value.id in receivers:
                if node.attr in methods:
                    yield methods[node.attr], guards
            elif isinstance(node.value, ast.Name) and node.value.id in scope.classes:
                helper_methods = _class_methods(scope.classes[node.value.id], scope.classes)
                if node.attr in helper_methods:
                    yield helper_methods[node.attr], guards


def _names_bound_to_a_type_error(function: _Function) -> frozenset[str]:
    """Names this function's own scope binds to a new ``TypeError(...)`` (``err = TypeError(...)``)."""
    bound: set[str] = set()
    for node, _guards in _walk_guarded(function.node, frozenset()):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and _exception_name(node.value) == BANNED_EXCEPTION:
            bound.update(target.id for target in node.targets if isinstance(target, ast.Name))
    return frozenset(bound)


def _raises_a_type_error(exc: ast.expr, bound: frozenset[str]) -> bool:
    if _exception_name(exc) == BANNED_EXCEPTION:
        return True
    return isinstance(exc, ast.Name) and exc.id in bound


def scan_module(tree: ast.Module, relpath: str) -> ModuleScan:
    """Every ``raise TypeError`` reachable, uncaught, from a class's ``process``."""
    scope = _module_scope(tree)
    module_classes = scope.classes
    found: set[ProcessPathRaise] = set()
    roots = 0

    for cls in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
        methods = _class_methods(cls, module_classes)
        if ROOT_METHOD not in methods:
            continue
        roots += 1
        receivers = _class_lineage(cls, module_classes) | {"self", "cls"}
        visited: set[tuple[str, frozenset[str]]] = set()
        pending: list[tuple[_Function, frozenset[str]]] = [(methods[ROOT_METHOD], frozenset())]
        while pending:
            function, path_guards = pending.pop()
            if (function.qualname, path_guards) in visited:
                continue
            visited.add((function.qualname, path_guards))
            bound = _names_bound_to_a_type_error(function)
            for node, guards in _walk_guarded(function.node, frozenset()):
                if (
                    isinstance(node, ast.Raise)
                    and node.exc is not None
                    and _raises_a_type_error(node.exc, bound)
                    and not (guards | path_guards) & _ABSORBS_TYPE_ERROR
                ):
                    found.add(ProcessPathRaise(relpath, function.qualname, node.lineno))
            for callee, guards in _references(function, scope, methods, receivers):
                pending.append((callee, path_guards | guards))

    return ModuleScan(frozenset(found), roots)


def scan_tree(root: Path) -> tuple[list[ProcessPathRaise], int]:
    """Scan every gate-visible module under ``root``; findings sorted by key, then line."""
    found: list[ProcessPathRaise] = []
    roots = 0
    for parsed in iter_gate_sources(root):
        scan = scan_module(parsed.tree, parsed.path.relative_to(root).as_posix())
        found.extend(scan.raises)
        roots += scan.process_roots
    return sorted(found, key=lambda item: (item.key, item.line)), roots


def assert_matches_reviewed(found: Iterable[ProcessPathRaise], reviewed: Iterable[str]) -> None:
    """Every found key is reviewed and every reviewed key still matches a site (both as multisets)."""
    found = list(found)
    found_count = Counter(item.key for item in found)
    reviewed_count = Counter(reviewed)
    unexpected = found_count - reviewed_count
    stale = reviewed_count - found_count
    assert not unexpected, (
        "a bare TypeError can escape a plugin's process path and abort the run:\n"
        + "\n".join(f"  src/elspeth/plugins/{item.path}:{item.line} in {item.function}" for item in found if item.key in unexpected)
        + "\nA wrongly-typed row value is row data: return TransformResult.error(...) naming the field, the expected "
        "and found type and the row index, never the value. From a value-returning helper in a batch plugin, raise "
        "BatchRowTypeError (plugins/transforms/_batch_row_types.py) and convert it once in process(). If the condition "
        "truly reads no row value, raise a Tier-1 exception class, or add a reviewed entry to "
        "REVIEWED_PROCESS_PATH_TYPE_ERRORS saying why."
    )
    assert not stale, f"reviewed entries no longer match a site; remove them: {sorted(stale)}"


def assert_roots_floor(roots: int) -> None:
    """The scan must have rooted at no fewer than ``MIN_PROCESS_ROOTS`` process methods."""
    assert roots >= MIN_PROCESS_ROOTS, (
        f"the scan rooted at only {roots} process methods under {PLUGINS_ROOT} "
        f"(floor {MIN_PROCESS_ROOTS}); it has stopped seeing the plugin tree, so its empty result proves nothing"
    )


def test_no_unreviewed_type_error_on_a_plugin_process_path() -> None:
    found, roots = scan_tree(PLUGINS_ROOT)

    assert_roots_floor(roots)
    assert_matches_reviewed(found, REVIEWED_PROCESS_PATH_TYPE_ERRORS)


def test_a_scan_that_stopped_seeing_the_plugin_tree_fails_the_floor(tmp_path: Path) -> None:
    """A nonexistent or empty root scans as zero roots and zero findings: the floor, not the findings, catches it."""
    found, roots = scan_tree(tmp_path / "no-plugins-here")

    assert (found, roots) == ([], 0)
    with pytest.raises(AssertionError, match="stopped seeing the plugin tree"):
        assert_roots_floor(roots)
    with pytest.raises(AssertionError, match="stopped seeing the plugin tree"):
        assert_roots_floor(MIN_PROCESS_ROOTS - 1)
    assert_roots_floor(MIN_PROCESS_ROOTS)


# ---------------------------------------------------------------------------
# Instrument controls: the analyzer must find the shapes that spread, and must
# not flag the shapes that route. Each case is a whole module.
# ---------------------------------------------------------------------------

_FLAGGED_CASES = {
    "inline_in_process": (
        """
class Replicate:
    def process(self, rows, ctx):
        for row in rows:
            if type(row["copies"]) is not int:
                raise TypeError("must be int")
""",
        {"Replicate.process"},
    ),
    "self_helper": (
        """
class Stats:
    def process(self, rows, ctx):
        return self._values(rows)

    def _values(self, rows):
        for index, row in enumerate(rows):
            if type(row["v"]) not in (int, float):
                raise TypeError(f"must be numeric in row {index}")
""",
        {"Stats._values"},
    ),
    "staticmethod_via_class_name": (
        """
class Metrics:
    def process(self, rows, ctx):
        return [Metrics._label(row["label"]) for row in rows]

    @staticmethod
    def _label(value):
        if type(value) not in (str, int, bool):
            raise TypeError("must be a scalar label")
        return value
""",
        {"Metrics._label"},
    ),
    "module_function_transitively": (
        """
def _check(value):
    if type(value) is not str:
        raise TypeError("must be a string")

def _render(rows):
    return [_check(row["text"]) for row in rows]

class Report:
    def process(self, rows, ctx):
        return _render(rows)
""",
        {"_check"},
    ),
    "helper_passed_as_callback": (
        """
class TopK:
    def process(self, rows, ctx):
        return sorted(rows, key=self._rank)

    def _rank(self, row):
        if type(row["score"]) is not float:
            raise TypeError("must be float")
        return row["score"]
""",
        {"TopK._rank"},
    ),
    "helper_on_same_module_base": (
        """
class _Base:
    def _numeric(self, value):
        if type(value) not in (int, float):
            raise TypeError("must be numeric")
        return value

class Drift(_Base):
    def process(self, rows, ctx):
        return [self._numeric(row["v"]) for row in rows]
""",
        {"_Base._numeric"},
    ),
    "handler_that_reraises_is_not_a_catch": (
        """
class Wrapped:
    def process(self, rows, ctx):
        try:
            return self._values(rows)
        except TypeError:
            self._count += 1
            raise

    def _values(self, rows):
        raise TypeError("must be numeric")
""",
        {"Wrapped._values"},
    ),
    "caught_on_another_path_only": (
        """
class Mixed:
    def process(self, rows, ctx):
        try:
            self._values(rows)
        except TypeError:
            pass
        return self._values(rows)

    def _values(self, rows):
        raise TypeError("must be numeric")
""",
        {"Mixed._values"},
    ),
    # The same property with the uncaught reference first: a visited set keyed
    # on the function alone would mark _values seen under the guard and never
    # revisit it on the unguarded path, whichever order the walk takes.
    "caught_on_another_path_only_uncaught_first": (
        """
class Mixed:
    def process(self, rows, ctx):
        values = self._values(rows)
        try:
            self._values(rows)
        except TypeError:
            pass
        return values

    def _values(self, rows):
        raise TypeError("must be numeric")
""",
        {"Mixed._values"},
    ),
    # A NEW TypeError raised inside ``except TypeError`` escapes: the handler
    # guards its try body, not itself (the rag/query.py ``_build_regex`` shape).
    "new_type_error_raised_inside_a_type_error_handler": (
        """
class Query:
    def process(self, row, ctx):
        return self._regex(row["q"])

    def _regex(self, value):
        try:
            return self._pattern.search(value)
        except TypeError as exc:
            raise TypeError("query_field expected str") from exc
""",
        {"Query._regex"},
    ),
    "raise_in_try_else": (
        """
class Query:
    def process(self, row, ctx):
        try:
            value = row["q"]
        except TypeError:
            return None
        else:
            if type(value) is not str:
                raise TypeError("must be str")
""",
        {"Query.process"},
    ),
    "raise_in_try_finally": (
        """
class Query:
    def process(self, row, ctx):
        try:
            value = row["q"]
        except TypeError:
            return None
        finally:
            if type(row["q"]) is not str:
                raise TypeError("must be str")
""",
        {"Query.process"},
    ),
    "named_reraise_is_not_a_catch": (
        """
class Wrapped:
    def process(self, rows, ctx):
        try:
            return self._values(rows)
        except TypeError as exc:
            raise exc

    def _values(self, rows):
        raise TypeError("must be numeric")
""",
        {"Wrapped._values"},
    ),
    "reraise_with_traceback_is_not_a_catch": (
        """
class Wrapped:
    def process(self, rows, ctx):
        try:
            return self._values(rows)
        except TypeError as exc:
            raise exc.with_traceback(None)

    def _values(self, rows):
        raise TypeError("must be numeric")
""",
        {"Wrapped._values"},
    ),
    "qualified_builtins_type_error": (
        """
import builtins

class Replicate:
    def process(self, row, ctx):
        if type(row["copies"]) is not int:
            raise builtins.TypeError("must be int")
""",
        {"Replicate.process"},
    ),
    "subclass_override_is_the_one_reached": (
        """
class _Base:
    def process(self, row, ctx):
        return self._hook(row)

    def _hook(self, row):
        return row

class Search(_Base):
    def _hook(self, row):
        if type(row["k"]) is not int:
            raise TypeError("k must be int")
        return row
""",
        {"Search._hook"},
    ),
    # review-R8-propagation-gate-r2 F1: the shapes below escaped the analyzer.
    "super_call_to_a_same_module_base_helper": (
        """
class _Base:
    def _check(self, value):
        if type(value) is not int:
            raise TypeError("must be int")
        return value

class Stats(_Base):
    def process(self, rows, ctx):
        return [self._check(r["v"]) for r in rows]

    def _check(self, value):
        return super()._check(value)
""",
        {"_Base._check"},
    ),
    "super_call_through_an_empty_intermediate_base": (
        """
class _Mixin:
    def _numeric(self, v):
        if type(v) not in (int, float):
            raise TypeError("must be numeric")
        return v

class _Base(_Mixin):
    pass

class Stats(_Base):
    def process(self, row, ctx):
        return super()._numeric(row["v"])
""",
        {"_Mixin._numeric"},
    ),
    "static_call_on_a_same_module_helper_class": (
        """
class _Checks:
    @staticmethod
    def numeric(value):
        if type(value) not in (int, float):
            raise TypeError("must be numeric")
        return value

class Stats:
    def process(self, rows, ctx):
        return [_Checks.numeric(r["v"]) for r in rows]
""",
        {"_Checks.numeric"},
    ),
    "module_function_via_a_dispatch_table": (
        """
def _check_int(v):
    if type(v) is not int:
        raise TypeError("must be int")

_CHECKS = {"int": _check_int}

class Stats:
    def process(self, row, ctx):
        return _CHECKS["int"](row["v"])
""",
        {"_check_int"},
    ),
    "helper_class_method_via_an_annotated_dispatch_table": (
        """
class _Checks:
    @staticmethod
    def label(v):
        if type(v) is not str:
            raise TypeError("must be str")

_CHECKS: tuple = (_Checks.label,)

class Stats:
    def process(self, row, ctx):
        return [check(row["v"]) for check in _CHECKS]
""",
        {"_Checks.label"},
    ),
    "type_error_bound_to_a_name_then_raised": (
        """
class Replicate:
    def process(self, row, ctx):
        if type(row["copies"]) is not int:
            err = TypeError("must be int")
            raise err
""",
        {"Replicate.process"},
    ),
    "nested_class_is_rooted": (
        """
class Outer:
    class Inner:
        def process(self, row, ctx):
            if type(row["v"]) is not int:
                raise TypeError("must be int")
""",
        {"Inner.process"},
    ),
}

_CLEAN_CASES = {
    "caught_at_the_call_site_and_converted": """
class BatchRowTypeError(Exception):
    pass

class Stats:
    def process(self, rows, ctx):
        try:
            values = self._values(rows)
        except BatchRowTypeError as exc:
            return {"error": exc}
        return values

    def _values(self, rows):
        for row in rows:
            if type(row["v"]) not in (int, float):
                raise BatchRowTypeError("wrong type")
""",
    "type_error_caught_by_name": """
class Mult:
    def process(self, row, ctx):
        try:
            return self._times(row)
        except TypeError:
            return {"error": "invalid_input"}

    def _times(self, row):
        raise TypeError("x")
""",
    "type_error_caught_in_a_tuple": """
class Mult:
    def process(self, row, ctx):
        try:
            return self._times(row)
        except (ValueError, TypeError):
            return {"error": "invalid_input"}

    def _times(self, row):
        raise TypeError("x")
""",
    "caught_by_exception": """
class Mult:
    def process(self, row, ctx):
        try:
            raise TypeError("x")
        except Exception as exc:
            return {"error": type(exc).__name__}
""",
    "config_time_raise_in_init": """
class Configured:
    def __init__(self, config):
        if type(config["n"]) is not int:
            raise TypeError("n must be int")

    def process(self, row, ctx):
        return row
""",
    "helper_not_reachable_from_process": """
class Probe:
    def process(self, row, ctx):
        return row

    def _unused(self, value):
        raise TypeError("x")
""",
    "class_without_process": """
class Sink:
    def write(self, rows, ctx):
        raise TypeError("x")
""",
    "other_exception_is_not_this_gate": """
class Explode:
    def process(self, row, ctx):
        if row.contract.mode != "fixed":
            raise ValueError("heterogeneous contract modes")
""",
    "caught_by_a_bare_except": """
class Mult:
    def process(self, row, ctx):
        try:
            return self._times(row)
        except:
            return {"error": "invalid_input"}

    def _times(self, row):
        raise TypeError("x")
""",
    "other_exception_bound_to_a_name_then_raised": """
class Replicate:
    def process(self, row, ctx):
        if row.contract.mode != "fixed":
            err = ValueError("heterogeneous contract modes")
            raise err
""",
    "dispatch_table_that_process_never_reads": """
def _check_int(v):
    raise TypeError("must be int")

_CHECKS = {"int": _check_int}

class Stats:
    def process(self, row, ctx):
        return row
""",
    "super_call_into_a_base_from_another_module": """
from elspeth.plugins.infrastructure.base import BaseTransform

class Stats(BaseTransform):
    def process(self, row, ctx):
        return super().process(row, ctx)
""",
    "helper_class_method_caught_at_the_call_site": """
class _Checks:
    @staticmethod
    def numeric(value):
        raise TypeError("must be numeric")

class Stats:
    def process(self, row, ctx):
        try:
            return _Checks.numeric(row["v"])
        except TypeError:
            return {"error": "invalid_input"}
""",
    "overridden_base_helper_is_not_reached": """
class _Base:
    def _hook(self, row):
        raise TypeError("x")

class Search(_Base):
    def process(self, row, ctx):
        return self._hook(row)

    def _hook(self, row):
        return row
""",
}


@pytest.mark.parametrize(("source", "expected"), list(_FLAGGED_CASES.values()), ids=list(_FLAGGED_CASES))
def test_the_analyzer_flags_a_type_error_that_escapes_process(source: str, expected: set[str]) -> None:
    scan = scan_module(ast.parse(source), "case.py")

    assert {item.function for item in scan.raises} == expected


@pytest.mark.parametrize("source", list(_CLEAN_CASES.values()), ids=list(_CLEAN_CASES))
def test_the_analyzer_passes_a_type_error_that_cannot_escape_process(source: str) -> None:
    scan = scan_module(ast.parse(source), "case.py")

    assert scan.raises == frozenset()


def test_the_key_is_line_independent_and_one_per_site() -> None:
    first = scan_module(ast.parse(_FLAGGED_CASES["self_helper"][0]), "stats.py")
    shifted = scan_module(ast.parse("\n\n\n" + _FLAGGED_CASES["self_helper"][0]), "stats.py")

    assert [item.key for item in first.raises] == ["stats.py::Stats._values::TypeError"]
    assert [item.key for item in shifted.raises] == [item.key for item in first.raises]
    assert [item.line for item in shifted.raises] != [item.line for item in first.raises]


_SITE_B = ProcessPathRaise("b.py", "B._h", 3)


def test_a_reviewed_site_passes() -> None:
    assert_matches_reviewed([_SITE_B], ["b.py::B._h::TypeError"])


def test_an_unreviewed_site_fails() -> None:
    with pytest.raises(AssertionError, match=r"can escape a plugin's process path(?s:.*)b\.py:3 in B\._h"):
        assert_matches_reviewed([_SITE_B], [])


def test_a_reviewed_entry_with_no_matching_site_is_reported_stale() -> None:
    with pytest.raises(AssertionError, match=r"no longer match a site; remove them: \['a\.py::A\.process::TypeError'\]"):
        assert_matches_reviewed([_SITE_B], ["b.py::B._h::TypeError", "a.py::A.process::TypeError"])


def test_each_reviewed_key_admits_exactly_one_site() -> None:
    second_site = ProcessPathRaise("b.py", "B._h", 9)

    with pytest.raises(AssertionError, match="can escape"):
        assert_matches_reviewed([_SITE_B, second_site], ["b.py::B._h::TypeError"])
    with pytest.raises(AssertionError, match="no longer match"):
        assert_matches_reviewed([_SITE_B], ["b.py::B._h::TypeError", "b.py::B._h::TypeError"])
