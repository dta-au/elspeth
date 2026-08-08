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
keeps that stable group identity while binding its payload to the sorted
multiset of each occurrence's location-free ``probe_shape`` fingerprint.
Formatting and import-alias spelling therefore do not churn adjudication,
while replacing a reviewed literal probe with dynamic reflection does.
"""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from elspeth_lints.core.boundary_aliases import (
    argument_names,
    assignment_target_names,
    evaluate_alias_flow,
    function_local_binding_names,
    import_alias_effect,
    match_pattern_binding_names,
)
from elspeth_lints.rules.trust_boundary.shared import extract_keywords, iter_boundary_decorators

SiteKind = Literal["getattr", "hasattr", "getattr_static", "dunder_getattr"]

ALL_KINDS: tuple[SiteKind, ...] = ("getattr", "hasattr", "getattr_static", "dunder_getattr")

MODULE_QUALNAME = "<module>"
_LAMBDA_FRAME = "<lambda>"
_SHADOWED_BINDING = "<shadowed>"
type BindingTargets = frozenset[str]
_DEFAULT_PROBE_BINDINGS = {
    "getattr": frozenset({"builtins.getattr"}),
    "hasattr": frozenset({"builtins.hasattr"}),
    "AttributeError": frozenset({"builtins.AttributeError"}),
}
_PROBE_TARGETS: dict[str, SiteKind] = {
    "builtins.getattr": "getattr",
    "builtins.hasattr": "hasattr",
    "inspect.getattr_static": "getattr_static",
}


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
    one merely by existing. ``probe_shape`` is a SHA-256 fingerprint of a
    location-free AST shape whose callee is normalized to ``kind``.
    """

    path: str
    qualname: str
    kind: SiteKind
    line: int
    column: int
    arity: int | None
    literal_name: bool | None
    probe_shape: str
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
        runtime_module_bindings=_module_runtime_bindings(tree),
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
        # Decorators apply bottom-up.  Only an outermost boundary marker is
        # guaranteed to be the installed callable; an unknown decorator above
        # it can unwrap or replace the authenticated wrapper and its metadata.
        if not match.function.decorator_list or match.function.decorator_list[0] is not match.call:
            continue
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
    tuple, list, set literals) where every key/element is a ``str`` constant,
    provided every other use is a membership read. Calls to the shadowable
    ``set``/``frozenset`` names and tables that are reassigned, mutated, or
    escape are deliberately not proven closed.
    """
    candidates: dict[str, ast.Name] = {}
    for stmt in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target, value = stmt.target, stmt.value
        if isinstance(target, ast.Name) and value is not None and _is_literal_str_container(value):
            if target.id in candidates:
                candidates.pop(target.id)
            else:
                candidates[target.id] = target

    # A permanent amnesty needs a table that is demonstrably closed for the
    # whole module, not merely one that happened to have a literal assignment
    # at some earlier point.  Permit only the defining store and membership
    # reads; any reassignment, mutation call, or escape revokes the proof.
    parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    closed: set[str] = set()
    for name, defining_target in candidates.items():
        valid = True
        for candidate in ast.walk(tree):
            if not isinstance(candidate, ast.Name) or candidate.id != name:
                continue
            if candidate is defining_target:
                continue
            parent = parents.get(id(candidate))
            membership_read = (
                isinstance(candidate.ctx, ast.Load)
                and isinstance(parent, ast.Compare)
                and candidate in parent.comparators
                and any(isinstance(operator, ast.In) for operator in parent.ops)
            )
            if not membership_read:
                valid = False
                break
        if valid:
            closed.add(name)
    return frozenset(closed)


def _is_literal_str_container(node: ast.expr) -> bool:
    value_node = node
    if isinstance(node, ast.Call):
        # ``set``/``frozenset`` are shadowable names.  Without a semantic
        # binding proof, accepting their call spelling would grant a permanent
        # amnesty to an arbitrary runtime constructor.
        return False
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


def _resolve_binding_expression(expression: ast.expr, bindings: dict[str, BindingTargets]) -> BindingTargets:
    if isinstance(expression, ast.NamedExpr):
        return _resolve_binding_expression(expression.value, bindings)
    if isinstance(expression, ast.IfExp):
        return _resolve_binding_expression(expression.body, bindings) | _resolve_binding_expression(expression.orelse, bindings)
    if isinstance(expression, ast.Subscript) and isinstance(expression.value, (ast.Tuple, ast.List)):
        index = expression.slice
        if isinstance(index, ast.Constant) and isinstance(index.value, int):
            try:
                return _resolve_binding_expression(expression.value.elts[index.value], bindings)
            except IndexError:
                return frozenset()
    if isinstance(expression, ast.Subscript) and isinstance(expression.value, ast.Dict):
        try:
            requested_key = ast.literal_eval(expression.slice)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            return frozenset()
        for key, value in zip(expression.value.keys, expression.value.values, strict=True):
            if key is None:
                continue
            try:
                candidate_key = ast.literal_eval(key)
            except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                continue
            if candidate_key == requested_key:
                return _resolve_binding_expression(value, bindings)
    parts = _dotted_name(expression)
    if parts is None:
        return frozenset()
    return frozenset(".".join((root, *parts[1:])) for root in bindings.get(parts[0], frozenset()) if root != _SHADOWED_BINDING)


def _bind_runtime_value(target: ast.expr, value: ast.expr, bindings: dict[str, BindingTargets]) -> None:
    if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)) and len(target.elts) == len(value.elts):
        for target_element, value_element in zip(target.elts, value.elts, strict=True):
            _bind_runtime_value(target_element, value_element, bindings)
        return
    targets = _resolve_binding_expression(value, bindings)
    for name in assignment_target_names(target):
        bindings[name] = targets or frozenset({_SHADOWED_BINDING})


def _join_binding_states(states: Sequence[dict[str, BindingTargets]]) -> dict[str, BindingTargets]:
    names = {name for state in states for name in state}
    return {name: frozenset(target for state in states for target in state.get(name, ())) for name in names}


def _target_bindings_from_iterable(
    target: ast.expr,
    iterable: ast.expr,
    bindings: dict[str, BindingTargets],
) -> dict[str, BindingTargets]:
    if isinstance(iterable, (ast.Tuple, ast.List, ast.Set)):
        values = iterable.elts
    elif isinstance(iterable, ast.Dict):
        values = [key for key in iterable.keys if key is not None]
    else:
        values = []

    def collect(candidate: ast.expr, possible_values: Sequence[ast.expr]) -> dict[str, BindingTargets]:
        if isinstance(candidate, ast.Name):
            targets = frozenset(target_name for value in possible_values for target_name in _resolve_binding_expression(value, bindings))
            return {candidate.id: targets} if targets else {}
        if isinstance(candidate, (ast.Tuple, ast.List)):
            result: dict[str, BindingTargets] = {}
            structured = [
                value for value in possible_values if isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == len(candidate.elts)
            ]
            for index, element in enumerate(candidate.elts):
                nested = collect(element, [value.elts[index] for value in structured])
                result = _join_binding_states((result, nested))
            return result
        return {}

    return collect(target, values)


def _pattern_capture_bindings(
    pattern: ast.pattern,
    subject: ast.expr,
    bindings: dict[str, BindingTargets],
) -> dict[str, BindingTargets]:
    if isinstance(pattern, ast.MatchAs) and pattern.pattern is None and pattern.name is not None:
        targets = _resolve_binding_expression(subject, bindings)
        return {pattern.name: targets} if targets else {}
    if isinstance(pattern, ast.MatchSequence) and isinstance(subject, (ast.Tuple, ast.List)) and len(pattern.patterns) == len(subject.elts):
        result: dict[str, BindingTargets] = {}
        for nested_pattern, nested_subject in zip(pattern.patterns, subject.elts, strict=True):
            result = _join_binding_states((result, _pattern_capture_bindings(nested_pattern, nested_subject, bindings)))
        return result
    if isinstance(pattern, ast.MatchMapping) and isinstance(subject, ast.Dict):
        subject_by_key: dict[object, ast.expr] = {}
        for key, value in zip(subject.keys, subject.values, strict=True):
            if key is None:
                continue
            try:
                subject_by_key[ast.literal_eval(key)] = value
            except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                continue
        result = {}
        for key, nested_pattern in zip(pattern.keys, pattern.patterns, strict=True):
            try:
                nested_subject = subject_by_key[ast.literal_eval(key)]
            except (KeyError, ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                continue
            result = _join_binding_states((result, _pattern_capture_bindings(nested_pattern, nested_subject, bindings)))
        return result
    return {}


def _default_parameter_bindings(
    arguments: ast.arguments,
    bindings: dict[str, BindingTargets],
) -> dict[str, BindingTargets]:
    captured: dict[str, BindingTargets] = {}
    positional = [*arguments.posonlyargs, *arguments.args]
    positional_with_defaults = positional[-len(arguments.defaults) :] if arguments.defaults else []
    for parameter, default in zip(positional_with_defaults, arguments.defaults, strict=True):
        targets = _resolve_binding_expression(default, bindings)
        if targets:
            captured[parameter.arg] = targets
    for parameter, keyword_default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True):
        if keyword_default is None:
            continue
        targets = _resolve_binding_expression(keyword_default, bindings)
        if targets:
            captured[parameter.arg] = targets
    return captured


def _apply_runtime_expression(expression: ast.AST, bindings: dict[str, BindingTargets]) -> None:
    if isinstance(expression, ast.Lambda):
        return
    if isinstance(expression, ast.NamedExpr):
        _apply_runtime_expression(expression.value, bindings)
        _bind_runtime_value(expression.target, expression.value, bindings)
        return
    for child in ast.iter_child_nodes(expression):
        _apply_runtime_expression(child, bindings)


def _runtime_bindings_after(nodes: Sequence[ast.stmt], incoming: dict[str, BindingTargets]) -> dict[str, BindingTargets]:
    bindings = dict(incoming)
    for node in nodes:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            effect = import_alias_effect(node)
            if effect.clears_all:
                for name, targets in tuple(bindings.items()):
                    bindings[name] = targets | {_SHADOWED_BINDING}
                if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "inspect":
                    bindings["getattr_static"] = frozenset({"inspect.getattr_static"})
            for name in effect.invalidated:
                bindings[name] = frozenset({_SHADOWED_BINDING})
            for name, import_target in effect.proven:
                bindings[name] = frozenset({import_target})
        elif isinstance(node, ast.Assign):
            _apply_runtime_expression(node.value, bindings)
            for assignment_target in node.targets:
                _bind_runtime_value(assignment_target, node.value, bindings)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            _apply_runtime_expression(node.value, bindings)
            _bind_runtime_value(node.target, node.value, bindings)
        elif isinstance(node, ast.Delete):
            for deletion_target in node.targets:
                for name in assignment_target_names(deletion_target):
                    bindings[name] = _DEFAULT_PROBE_BINDINGS.get(name, frozenset({_SHADOWED_BINDING}))
        elif isinstance(node, (ast.AugAssign, ast.TypeAlias)):
            bound_target = node.target if isinstance(node, ast.AugAssign) else node.name
            for name in assignment_target_names(bound_target):
                bindings[name] = frozenset({_SHADOWED_BINDING})
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bindings[node.name] = frozenset({_SHADOWED_BINDING})
        elif isinstance(node, ast.If):
            _apply_runtime_expression(node.test, bindings)
            body = _runtime_bindings_after(node.body, bindings)
            orelse = _runtime_bindings_after(node.orelse, bindings) if node.orelse else dict(bindings)
            bindings = _join_binding_states((body, orelse))
        elif isinstance(node, ast.Match):
            _apply_runtime_expression(node.subject, bindings)
            states = [dict(bindings)]
            for case in node.cases:
                case_bindings = dict(bindings)
                for name in match_pattern_binding_names(case.pattern):
                    case_bindings[name] = frozenset({_SHADOWED_BINDING})
                case_bindings.update(_pattern_capture_bindings(case.pattern, node.subject, bindings))
                states.append(_runtime_bindings_after(case.body, case_bindings))
            bindings = _join_binding_states(states)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            _apply_runtime_expression(node.iter if isinstance(node, (ast.For, ast.AsyncFor)) else node.test, bindings)
            body_entry = dict(bindings)
            if isinstance(node, (ast.For, ast.AsyncFor)):
                for name in assignment_target_names(node.target):
                    body_entry[name] = frozenset({_SHADOWED_BINDING})
                body_entry.update(_target_bindings_from_iterable(node.target, node.iter, bindings))
            body = _runtime_bindings_after(node.body, body_entry)
            joined = _join_binding_states((bindings, body))
            orelse = _runtime_bindings_after(node.orelse, joined) if node.orelse else body
            bindings = _join_binding_states((bindings, body, orelse))
        elif isinstance(node, (ast.Try, ast.TryStar)):
            states = [_runtime_bindings_after(node.body, bindings)]
            states.extend(_runtime_bindings_after(handler.body, bindings) for handler in node.handlers)
            if node.orelse:
                states.append(_runtime_bindings_after(node.orelse, states[0]))
            bindings = _join_binding_states(states)
            if node.finalbody:
                bindings = _runtime_bindings_after(node.finalbody, bindings)
        else:
            _apply_runtime_expression(node, bindings)
    return bindings


def _possible_runtime_bindings(nodes: Sequence[ast.stmt], incoming: dict[str, BindingTargets]) -> dict[str, BindingTargets]:
    """Return every binding target reachable at any point in ``nodes``."""
    possible = dict(incoming)
    current = dict(incoming)

    def merge(state: dict[str, BindingTargets]) -> None:
        nonlocal possible
        possible = _join_binding_states((possible, state))

    for node in nodes:
        if isinstance(node, ast.If):
            merge(_possible_runtime_bindings(node.body, current))
            merge(_possible_runtime_bindings(node.orelse, current))
        elif isinstance(node, ast.Match):
            for case in node.cases:
                merge(_possible_runtime_bindings(case.body, current))
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            merge(_possible_runtime_bindings(node.body, current))
            merge(_possible_runtime_bindings(node.orelse, current))
        elif isinstance(node, (ast.Try, ast.TryStar)):
            merge(_possible_runtime_bindings(node.body, current))
            for handler in node.handlers:
                merge(_possible_runtime_bindings(handler.body, current))
            merge(_possible_runtime_bindings(node.orelse, current))
            merge(_possible_runtime_bindings(node.finalbody, current))
        current = _runtime_bindings_after((node,), current)
        merge(current)
    return possible


def _module_runtime_bindings(tree: ast.Module) -> dict[str, BindingTargets]:
    """Return bindings possible whenever deferred module members may run."""
    return _possible_runtime_bindings(tree.body, dict(_DEFAULT_PROBE_BINDINGS))


class _MasqueradeVisitor(ast.NodeVisitor):
    """Single-pass walker computing site identity, kind, and amnesty flags."""

    def __init__(
        self,
        *,
        display_path: str,
        boundary_source_params: dict[int, str],
        module_tables: frozenset[str],
        runtime_module_bindings: dict[str, BindingTargets],
        in_tests: bool,
    ) -> None:
        self._display_path = display_path
        self._boundary_source_params = boundary_source_params
        self._module_tables = module_tables
        self._in_tests = in_tests
        self._qual_stack: list[str] = []
        self._container_stack: list[str] = ["module"]
        self._binding_stack: list[tuple[str, dict[str, BindingTargets]]] = [("module", dict(_DEFAULT_PROBE_BINDINGS))]
        self._runtime_module_bindings = runtime_module_bindings
        self._source_stack: list[set[str]] = [set()]
        self._deferred_binding_stack: list[dict[str, BindingTargets]] = []
        self._comprehension_mutation_stack: list[set[str]] = []
        self._assert_direct_call_ids: set[int] = set()
        self.sites: list[MasqueradeSite] = []

    # -- scope bookkeeping -------------------------------------------------

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expression in (*node.decorator_list, *node.bases):
            self.visit(expression)
        for keyword in node.keywords:
            self.visit(keyword.value)
        self._qual_stack.append(node.name)
        self._container_stack.append("class")
        self._push_binding_scope("class")
        self._source_stack.append(set(self._source_names))
        try:
            self._visit_statements(node.body)
        finally:
            self._source_stack.pop()
            self._pop_binding_scope()
            self._container_stack.pop()
            self._qual_stack.pop()
        self._bind_unknown((node.name,))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        captured_defaults = _default_parameter_bindings(node.args, self._bindings)
        self._visit_function_header(node)
        parent_kind = self._container_stack[-1]
        self._qual_stack.append(node.name)
        self._container_stack.append("function")
        nested_in_function = "function" in self._container_stack[:-1]
        local_bindings = function_local_binding_names(node)
        self._push_binding_scope("function", local_bindings=local_bindings)
        deferred_bindings = self._deferred_binding_stack[-1] if nested_in_function else self._runtime_module_bindings
        for name, targets in deferred_bindings.items():
            if name not in local_bindings:
                self._bindings[name] = self._bindings.get(name, frozenset()) | targets
        for name, targets in captured_defaults.items():
            if name in local_bindings:
                self._bindings[name] = targets
        source_param = None if nested_in_function else self._boundary_source_params.get(id(node))
        self._source_stack.append({source_param} if source_param is not None else set())
        self._deferred_binding_stack.append(_possible_runtime_bindings(node.body, dict(self._bindings)))
        try:
            if node.name == "__getattr__":
                self._record_dunder_getattr(node, parent_kind)
            self._visit_statements(node.body)
        finally:
            self._deferred_binding_stack.pop()
            self._source_stack.pop()
            self._pop_binding_scope()
            self._container_stack.pop()
            self._qual_stack.pop()
        self._bind_unknown((node.name,))

    def visit_Lambda(self, node: ast.Lambda) -> None:
        captured_defaults = _default_parameter_bindings(node.args, self._bindings)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        self._qual_stack.append(_LAMBDA_FRAME)
        self._container_stack.append("function")
        nested_in_function = "function" in self._container_stack[:-1]
        local_bindings = argument_names(node.args)
        self._push_binding_scope("function", local_bindings=local_bindings)
        deferred_bindings = self._deferred_binding_stack[-1] if nested_in_function else self._runtime_module_bindings
        for name, targets in deferred_bindings.items():
            if name not in local_bindings:
                self._bindings[name] = self._bindings.get(name, frozenset()) | targets
        for name, targets in captured_defaults.items():
            if name in local_bindings:
                self._bindings[name] = targets
        # A lambda is a deferred nested scope, not the decorated boundary
        # itself. Captured cells can be rebound before invocation, so no
        # boundary-source provenance crosses this execution seam.
        self._source_stack.append(set())
        enclosing_comprehension_mutations = self._comprehension_mutation_stack
        self._comprehension_mutation_stack = []
        lambda_runtime_bindings = dict(self._bindings)
        _apply_runtime_expression(node.body, lambda_runtime_bindings)
        self._deferred_binding_stack.append(_join_binding_states((self._bindings, lambda_runtime_bindings)))
        try:
            self.visit(node.body)
        finally:
            self._deferred_binding_stack.pop()
            self._comprehension_mutation_stack = enclosing_comprehension_mutations
            self._source_stack.pop()
            self._pop_binding_scope()
            self._container_stack.pop()
            self._qual_stack.pop()

    def _visit_comprehension_expression(
        self,
        node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp,
    ) -> None:
        """Visit a comprehension with its implicit function scope.

        Python evaluates the outermost iterable in the enclosing scope,
        then binds every generator target inside a hidden comprehension
        scope. A target named like a trusted boundary source therefore
        shadows that source for filters and the produced element without
        revoking the enclosing function's provenance after the
        comprehension completes.
        """
        first, *remaining = node.generators
        self.visit(first.iter)
        first_bindings = _target_bindings_from_iterable(first.target, first.iter, self._bindings)
        local_bindings = {name for generator in node.generators for name in assignment_target_names(generator.target)}
        self._push_binding_scope("function", local_bindings=local_bindings)
        if isinstance(node, ast.GeneratorExp):
            deferred_bindings = self._deferred_binding_stack[-1] if self._deferred_binding_stack else self._runtime_module_bindings
            for name, targets in deferred_bindings.items():
                if name not in local_bindings:
                    self._bindings[name] = self._bindings.get(name, frozenset()) | targets
        # Generator bodies execute later and close over cells whose values may
        # be rebound before iteration.  Eager list/set/dict comprehensions can
        # retain current boundary provenance; a generator cannot.
        inherited_sources = set() if isinstance(node, ast.GeneratorExp) else self._source_names - local_bindings
        self._source_stack.append(inherited_sources)
        outer_mutations: set[str] = set()
        self._comprehension_mutation_stack.append(outer_mutations)
        try:
            self._bind_unknown(assignment_target_names(first.target))
            self._bindings.update(first_bindings)
            for condition in first.ifs:
                self.visit(condition)
            for generator in remaining:
                self.visit(generator.iter)
                generator_bindings = _target_bindings_from_iterable(generator.target, generator.iter, self._bindings)
                self._bind_unknown(assignment_target_names(generator.target))
                self._bindings.update(generator_bindings)
                for condition in generator.ifs:
                    self.visit(condition)
            if isinstance(node, ast.DictComp):
                self.visit(node.key)
                self.visit(node.value)
            else:
                self.visit(node.elt)
        finally:
            popped_mutations = self._comprehension_mutation_stack.pop()
            if popped_mutations is not outer_mutations:
                raise AssertionError("comprehension mutation stack is unbalanced")
            self._source_stack.pop()
            self._pop_binding_scope()
        self._bind_unknown(tuple(sorted(outer_mutations)))

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension_expression(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension_expression(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension_expression(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension_expression(node)

    @property
    def _bindings(self) -> dict[str, BindingTargets]:
        return self._binding_stack[-1][1]

    @property
    def _source_names(self) -> set[str]:
        return self._source_stack[-1]

    def _push_binding_scope(self, kind: str, *, local_bindings: Sequence[str] | set[str] = ()) -> None:
        for frame_kind, bindings in reversed(self._binding_stack):
            if frame_kind != "class":
                scoped = dict(bindings)
                for name in local_bindings:
                    scoped[name] = frozenset({_SHADOWED_BINDING})
                self._binding_stack.append((kind, scoped))
                return
        raise AssertionError("module binding scope is always present")

    def _pop_binding_scope(self) -> None:
        self._binding_stack.pop()

    def _visit_statements(self, statements: Sequence[ast.stmt]) -> None:
        reachable = True
        for statement in statements:
            if reachable:
                self.visit(statement)
                reachable = _suite_can_fall_through((statement,))
                continue
            # Keep inventorying lexically present dead code, but never let its
            # bindings/provenance contaminate a runtime join after an abrupt
            # transfer in the same suite.
            state = self._snapshot_state()
            self.visit(statement)
            self._restore_state(state)

    def _visit_function_header(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            if argument.annotation is not None:
                self.visit(argument.annotation)
        if node.args.vararg is not None and node.args.vararg.annotation is not None:
            self.visit(node.args.vararg.annotation)
        if node.args.kwarg is not None and node.args.kwarg.annotation is not None:
            self.visit(node.args.kwarg.annotation)
        if node.returns is not None:
            self.visit(node.returns)

    def _snapshot_state(self) -> tuple[dict[str, BindingTargets], set[str]]:
        return dict(self._bindings), set(self._source_names)

    def _restore_state(self, state: tuple[dict[str, BindingTargets], set[str]]) -> None:
        bindings, source_names = state
        self._bindings.clear()
        self._bindings.update(bindings)
        self._source_names.clear()
        self._source_names.update(source_names)

    def _join_states(self, states: Sequence[tuple[dict[str, BindingTargets], set[str]]]) -> None:
        if not states:
            self._restore_state(({}, set()))
            return
        all_names = {name for state in states for name in state[0]}
        bindings = {name: frozenset(target for state in states for target in state[0].get(name, ())) for name in all_names}
        source_names = set.intersection(*(set(state[1]) for state in states))
        self._restore_state((bindings, source_names))

    def _bind_unknown(self, names: Sequence[str]) -> None:
        for name in names:
            self._bindings[name] = frozenset({_SHADOWED_BINDING})
            self._source_names.discard(name)

    def _apply_import(self, node: ast.Import | ast.ImportFrom) -> None:
        effect = import_alias_effect(node)
        if effect.clears_all:
            for name, targets in tuple(self._bindings.items()):
                self._bindings[name] = targets | {_SHADOWED_BINDING}
            self._source_names.clear()
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "inspect":
                self._bindings["getattr_static"] = frozenset({"inspect.getattr_static"})
        self._bind_unknown(effect.invalidated)
        for name, target in effect.proven:
            self._bindings[name] = frozenset({target})
            self._source_names.discard(name)

    def visit_Import(self, node: ast.Import) -> None:
        self._apply_import(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._apply_import(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._bind_assignment(target, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.annotation)
        if node.value is None:
            return
        self.visit(node.value)
        self._bind_assignment(node.target, node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.target)
        self.visit(node.value)
        self._bind_unknown(assignment_target_names(node.target))

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        names = assignment_target_names(node.target)
        for mutations in self._comprehension_mutation_stack:
            mutations.update(names)
        self._bind_assignment(node.target, node.value)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self.visit(target)
            for name in assignment_target_names(target):
                if self._binding_stack[-1][0] == "module" and name in _DEFAULT_PROBE_BINDINGS:
                    self._bindings[name] = _DEFAULT_PROBE_BINDINGS[name]
                    self._source_names.discard(name)
                else:
                    self._bind_unknown((name,))

    def visit_TypeAlias(self, node: ast.TypeAlias) -> None:
        self.visit(node.value)
        self._bind_unknown(assignment_target_names(node.name))

    def _bind_assignment(self, target: ast.expr, value: ast.expr) -> None:
        if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)) and len(target.elts) == len(value.elts):
            for target_element, value_element in zip(target.elts, value.elts, strict=True):
                self._bind_assignment(target_element, value_element)
            return
        names = assignment_target_names(target)
        targets = self._resolved_bindings(value)
        source_value = isinstance(value, ast.Name) and value.id in self._source_names
        self._bind_unknown(names)
        if isinstance(target, ast.Name):
            if targets:
                self._bindings[target.id] = targets
            if source_value:
                self._source_names.add(target.id)

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        start = self._snapshot_state()
        self._visit_statements(node.body)
        body_state = self._snapshot_state()
        self._restore_state(start)
        self._visit_statements(node.orelse)
        orelse_state = self._snapshot_state() if node.orelse else start
        normal_states = []
        if _suite_can_fall_through(node.body):
            normal_states.append(body_state)
        if not node.orelse or _suite_can_fall_through(node.orelse):
            normal_states.append(orelse_state)
        self._join_states(normal_states or (start,))

    def _visit_loop(self, node: ast.For | ast.AsyncFor | ast.While) -> None:
        target = node.target if isinstance(node, (ast.For, ast.AsyncFor)) else None
        header = node.iter if isinstance(node, (ast.For, ast.AsyncFor)) else node.test
        self.visit(header)
        start = self._snapshot_state()
        if target is not None:
            target_bindings = _target_bindings_from_iterable(target, header, self._bindings)
            self._bind_unknown(assignment_target_names(target))
            self._bindings.update(target_bindings)
        self._visit_statements(node.body)
        body_state = self._snapshot_state()
        body_transfers = _suite_transfer_kinds(node.body)
        loop_states = [start]
        if body_transfers & {None, "break", "continue"}:
            loop_states.append(body_state)
        self._join_states(loop_states)
        normal_state = self._snapshot_state()
        self._visit_statements(node.orelse)
        orelse_state = self._snapshot_state() if node.orelse else normal_state
        self._join_states((*loop_states, orelse_state))

    def visit_For(self, node: ast.For) -> None:
        self._visit_loop(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._visit_loop(node)

    def visit_While(self, node: ast.While) -> None:
        self._visit_loop(node)

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        start = self._snapshot_state()
        endpoints = [start] if not _match_is_exhaustive(node) else []
        for case in node.cases:
            self._restore_state(start)
            self._bind_unknown(match_pattern_binding_names(case.pattern))
            self._bindings.update(_pattern_capture_bindings(case.pattern, node.subject, self._bindings))
            if case.guard is not None:
                self.visit(case.guard)
            self._visit_statements(case.body)
            if _suite_can_fall_through(case.body):
                endpoints.append(self._snapshot_state())
        self._join_states(tuple(endpoints) or (start,))

    def _visit_try(self, node: ast.Try | ast.TryStar) -> None:
        start = self._snapshot_state()
        self._visit_statements(node.body)
        body_state = self._snapshot_state()
        if node.orelse:
            self._visit_statements(node.orelse)
            body_state = self._snapshot_state()
        all_names = set(start[0]) | set(body_state[0])
        handler_entry_bindings = {name: start[0].get(name, frozenset()) | body_state[0].get(name, frozenset()) for name in all_names}
        handler_entry_sources = start[1] & body_state[1]
        endpoints = [body_state] if _suite_can_fall_through(node.body) else []
        for handler in node.handlers:
            self._restore_state((handler_entry_bindings, handler_entry_sources))
            if handler.type is not None:
                self.visit(handler.type)
            if handler.name is not None:
                self._bind_unknown((handler.name,))
            self._visit_statements(handler.body)
            if _suite_can_fall_through(handler.body):
                endpoints.append(self._snapshot_state())
        self._join_states(tuple(endpoints) or (start,))
        self._visit_statements(node.finalbody)

    def visit_Try(self, node: ast.Try) -> None:
        self._visit_try(node)

    def visit_TryStar(self, node: ast.TryStar) -> None:
        self._visit_try(node)

    def _visit_with(self, node: ast.With | ast.AsyncWith) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._bind_unknown(assignment_target_names(item.optional_vars))
        self._visit_statements(node.body)

    def visit_With(self, node: ast.With) -> None:
        self._visit_with(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._visit_with(node)

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
        kinds = {_PROBE_TARGETS[target] for target in self._resolved_bindings(node.func) if target in _PROBE_TARGETS}
        for kind in sorted(kinds):
            if kind == "getattr":
                self._record_getattr(node)
            elif kind == "hasattr":
                self._record_hasattr(node)
            elif kind == "getattr_static":
                self._record_getattr_static(node)
        self.generic_visit(node)

    def _resolved_bindings(self, expression: ast.expr) -> BindingTargets:
        return _resolve_binding_expression(expression, self._bindings)

    def _record_getattr(self, node: ast.Call) -> None:
        arity = len(node.args)
        literal_name = len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
        amnesty = False
        amnesty_reason: str | None = None
        if (
            arity == 3
            and not any(isinstance(argument, ast.Starred) for argument in node.args)
            and not node.keywords
            and literal_name
            and self._receiver_is_boundary_param(node.args[0])
        ):
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
        if parent_kind == "module" and _is_closed_table_gated(
            node,
            self._module_tables,
            attribute_error_targets=self._terminal_raise_targets(node),
        ):
            amnesty = True
            amnesty_reason = "module-getattr"
        self._append("dunder_getattr", node, arity=None, literal_name=None, amnesty=amnesty, amnesty_reason=amnesty_reason)

    def _terminal_raise_targets(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> BindingTargets:
        if not node.body or not isinstance(node.body[-1], ast.Raise) or node.body[-1].exc is None:
            return frozenset()
        exception = node.body[-1].exc
        constructor = exception.func if isinstance(exception, ast.Call) else exception
        parts = _dotted_name(constructor)
        if parts is None:
            return frozenset()
        return frozenset(".".join((root, *parts[1:])) for root in self._bindings.get(parts[0], frozenset()))

    def _receiver_is_boundary_param(self, receiver: ast.expr) -> bool:
        return isinstance(receiver, ast.Name) and receiver.id in self._source_names

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
                probe_shape=_probe_shape_fingerprint(kind, node),
                amnesty=amnesty,
                amnesty_reason=amnesty_reason,
            )
        )


def _is_closed_table_gated(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    tables: frozenset[str],
    *,
    attribute_error_targets: BindingTargets,
) -> bool:
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
    if func_node.decorator_list or len(positional) != 1:
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
    return _is_attribute_error_raise(last, attribute_error_targets=attribute_error_targets)


def _dotted_name(expression: ast.expr) -> tuple[str, ...] | None:
    if isinstance(expression, ast.Name):
        return (expression.id,)
    if isinstance(expression, ast.Attribute):
        base = _dotted_name(expression.value)
        if base is not None:
            return (*base, expression.attr)
    return None


def _suite_transfer_kinds(statements: Sequence[ast.stmt]) -> set[str | None]:
    """Return reachable transfer kinds from ``statements``.

    Binding identity is tracked by this module's possible-target model; the
    shared evaluator is reused only for its mature control-transfer semantics.
    """
    return {path.transfer for path in evaluate_alias_flow(statements, {})}


def _suite_can_fall_through(statements: Sequence[ast.stmt]) -> bool:
    return None in _suite_transfer_kinds(statements)


def _match_is_exhaustive(node: ast.Match) -> bool:
    return any(case.guard is None and isinstance(case.pattern, ast.MatchAs) and case.pattern.pattern is None for case in node.cases)


def _probe_shape_fingerprint(kind: SiteKind, node: ast.stmt | ast.expr) -> str:
    normalized: ast.AST = node
    if isinstance(node, ast.Call):
        normalized = ast.Call(
            func=ast.Name(id=kind, ctx=ast.Load()),
            args=node.args,
            keywords=node.keywords,
        )
    shape = ast.dump(normalized, annotate_fields=True, include_attributes=False)
    return hashlib.sha256(f"{kind}|{shape}".encode()).hexdigest()


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


def _is_attribute_error_raise(stmt: ast.stmt, *, attribute_error_targets: BindingTargets) -> bool:
    if not isinstance(stmt, ast.Raise) or stmt.exc is None:
        return False
    return attribute_error_targets == frozenset({"builtins.AttributeError"})
