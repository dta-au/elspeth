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
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Literal

from elspeth_lints.core.ast_walker import iter_own_scope
from elspeth_lints.core.boundary_aliases import (
    argument_names,
    assignment_target_names,
    evaluate_alias_flow,
    function_local_binding_names,
    import_alias_effect,
    match_pattern_binding_names,
    possibly_bound_names,
)
from elspeth_lints.rules.trust_boundary.shared import extract_keywords, iter_boundary_decorators

SiteKind = Literal["getattr", "hasattr", "getattr_static", "dunder_getattr"]

ALL_KINDS: tuple[SiteKind, ...] = ("getattr", "hasattr", "getattr_static", "dunder_getattr")

MODULE_QUALNAME = "<module>"
_LAMBDA_FRAME = "<lambda>"
_SHADOWED_BINDING = "<shadowed>"
#: A binding target deeper than this many dotted segments can never be a probe
#: identity (``builtins.getattr``, ``inspect.getattr_static``) or the exact
#: ``builtins.AttributeError``; it collapses to the shadowed marker. This is
#: what keeps every fixpoint here finite: ``node = node.next`` in a loop would
#: otherwise grow ``X.next.next...`` for ever.
_MAX_TARGET_DEPTH = 8
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
class _ExecutionContext:
    """Which annotation expressions execute for the statements of one scope.

    The inventory models CPython 3.12/3.13 definition-time semantics: signature
    annotations and module/class variable annotations execute eagerly, a
    function-local variable annotation never executes, and
    ``from __future__ import annotations`` (PEP 563) stringizes every
    annotation so none executes. Python 3.14 (PEP 649) defers annotations
    entirely and rejects a walrus inside one, so the eager model is exactly
    the case where an annotation can rebind a probe alias.
    """

    future_annotations: bool
    function_scope: bool

    @property
    def header_annotations_execute(self) -> bool:
        return not self.future_annotations

    @property
    def variable_annotations_execute(self) -> bool:
        return not self.future_annotations and not self.function_scope


def _uses_future_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(statement, ast.ImportFrom)
        and statement.module == "__future__"
        and any(alias.name == "annotations" for alias in statement.names)
        for statement in tree.body
    )


def _parameter_defaults(arguments: ast.arguments) -> list[tuple[ast.arg, ast.expr]]:
    """Return ``(parameter, default)`` pairs in CPython evaluation order.

    Positional defaults (``posonlyargs`` then ``args``, right-aligned) evaluate
    before keyword-only defaults, each group left to right.
    """
    positional = [*arguments.posonlyargs, *arguments.args]
    positional_with_defaults = positional[-len(arguments.defaults) :] if arguments.defaults else []
    pairs = list(zip(positional_with_defaults, arguments.defaults, strict=True))
    pairs.extend(
        (parameter, keyword_default)
        for parameter, keyword_default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True)
        if keyword_default is not None
    )
    return pairs


def _annotation_expressions(arguments: ast.arguments, returns: ast.expr | None) -> list[ast.expr]:
    """Return signature annotations in CPython 3.12/3.13 evaluation order.

    ``compiler_visit_annotations`` walks ``args`` BEFORE ``posonlyargs`` — the
    source order is not the evaluation order — then vararg, keyword-only
    parameters, kwarg, and finally the return annotation.
    """
    parameters: list[ast.arg] = [*arguments.args, *arguments.posonlyargs]
    if arguments.vararg is not None:
        parameters.append(arguments.vararg)
    parameters.extend(arguments.kwonlyargs)
    if arguments.kwarg is not None:
        parameters.append(arguments.kwarg)
    expressions = [parameter.annotation for parameter in parameters if parameter.annotation is not None]
    if returns is not None:
        expressions.append(returns)
    return expressions


def _class_header_arguments(node: ast.ClassDef) -> list[ast.expr]:
    """Return the class bases (starred included) then keyword values, in evaluation order."""
    return [*node.bases, *(keyword.value for keyword in node.keywords)]


def _type_parameter_names(type_params: Sequence[ast.type_param]) -> frozenset[str]:
    return frozenset(parameter.name for parameter in type_params if isinstance(parameter, (ast.TypeVar, ast.ParamSpec, ast.TypeVarTuple)))


def _lazy_type_parameter_expressions(type_params: Sequence[ast.type_param]) -> list[ast.expr]:
    """Return every bound, constraint tuple, and PEP 696 default among ``type_params``.

    All of them are evaluated lazily, on first access, in their own annotation
    scope. ``default_value`` exists only on Python 3.13+ ASTs; the node's own
    ``_fields`` manifest is the version-neutral way to ask.
    """
    expressions: list[ast.expr] = []
    for parameter in type_params:
        if isinstance(parameter, ast.TypeVar) and parameter.bound is not None:
            expressions.append(parameter.bound)
        if (
            isinstance(parameter, (ast.TypeVar, ast.ParamSpec, ast.TypeVarTuple))
            and "default_value" in parameter._fields
            and parameter.default_value is not None
        ):
            expressions.append(parameter.default_value)
    return expressions


def definition_header_expressions(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef,
    *,
    annotations: bool,
) -> list[ast.expr]:
    """Return every expression a definition header evaluates, in evaluation order.

    This is the single order both the live visitor and the deferred runtime
    projection replay (elspeth-682e0c6581): a class evaluates decorators, then
    bases (starred included), then keyword values; a function or lambda
    evaluates decorators, then defaults, then — when ``annotations`` is set,
    i.e. outside ``from __future__ import annotations`` — its signature
    annotations. PEP 695 type parameters are not header *effects*: their
    bounds, constraints, and defaults are lazy, and a walrus is a syntax error
    inside them, so they never bind anything in the enclosing scope; the
    visitor inventories them separately (:meth:`_MasqueradeVisitor._visit_type_parameters`).
    """
    if isinstance(node, ast.ClassDef):
        return [*node.decorator_list, *_class_header_arguments(node)]
    decorators = node.decorator_list if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else []
    expressions: list[ast.expr] = [*decorators, *(default for _parameter, default in _parameter_defaults(node.args))]
    if annotations and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        expressions.extend(_annotation_expressions(node.args, node.returns))
    return expressions


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
    future_annotations = _uses_future_annotations(tree)
    visitor = _MasqueradeVisitor(
        display_path=display_path,
        boundary_source_params=boundary_source_params,
        module_tables=module_tables,
        runtime_module_bindings=_module_runtime_bindings(
            tree, _ExecutionContext(future_annotations=future_annotations, function_scope=False)
        ),
        in_tests=in_tests,
        future_annotations=future_annotations,
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
    """Return every binding target ``expression`` may evaluate to.

    Modelled shapes — names and dotted names, a walrus, a conditional
    expression, a literal container subscript — resolve exactly. Every other
    shape keeps its evidence instead of discarding it (elspeth-34ac84b4b6):
    an *uncalled* reference inside the expression may be the value that flows
    out (``wrap(getattr)``, ``partial(getattr, obj)``, ``[getattr]``), so
    those targets are retained beside the ``<shadowed>`` marker recording
    that the value is otherwise unknown. A probe that is *called* inside the
    expression contributes nothing: its result is arbitrary, and the call is
    inventoried where it appears. The marker is what keeps the amnesty layer
    honest — a laundered ``AttributeError`` never resolves to the exact
    builtin identity a PEP 562 hook needs.
    """
    if isinstance(expression, ast.NamedExpr):
        return _resolve_binding_expression(expression.value, bindings)
    if isinstance(expression, ast.IfExp):
        return _resolve_binding_expression(expression.body, bindings) | _resolve_binding_expression(expression.orelse, bindings)
    if isinstance(expression, ast.Subscript):
        return _resolve_subscript(expression, bindings)
    parts = _dotted_name(expression)
    if parts is not None:
        return frozenset(_extend_target(root, parts[1:]) for root in bindings.get(parts[0], frozenset()))
    if isinstance(expression, ast.Attribute):
        # ``wrap(inspect).getattr_static``: an attribute of whatever a
        # laundered value may be, keyed exactly like a dotted alias.
        return frozenset(_extend_target(root, (expression.attr,)) for root in _resolve_binding_expression(expression.value, bindings))
    return _laundered_evidence(expression, bindings) | {_SHADOWED_BINDING}


def _extend_target(root: str, attributes: Sequence[str]) -> str:
    """Append attribute segments to a binding target, bounded by :data:`_MAX_TARGET_DEPTH`."""
    if root == _SHADOWED_BINDING or root.count(".") + len(attributes) >= _MAX_TARGET_DEPTH:
        return _SHADOWED_BINDING
    return ".".join((root, *attributes))


def _resolve_subscript(expression: ast.Subscript, bindings: dict[str, BindingTargets]) -> BindingTargets:
    container = expression.value
    if isinstance(container, (ast.Tuple, ast.List)):
        index = expression.slice
        if isinstance(index, ast.Constant) and isinstance(index.value, int):
            try:
                return _resolve_binding_expression(container.elts[index.value], bindings)
            except IndexError:
                return frozenset()
        # A dynamic index selects some element; a slice keeps a subsequence
        # of them. Either way only the elements themselves can flow out.
        return frozenset(target for element in container.elts for target in _resolve_binding_expression(element, bindings))
    if isinstance(container, ast.Dict):
        try:
            requested_key = ast.literal_eval(expression.slice)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            return frozenset(target for value in container.values for target in _resolve_binding_expression(value, bindings))
        for key, value in zip(container.keys, container.values, strict=True):
            if key is None:
                continue
            try:
                candidate_key = ast.literal_eval(key)
            except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                continue
            if candidate_key == requested_key:
                return _resolve_binding_expression(value, bindings)
        # No literal key matched: only a ``**spread`` entry could still supply it.
        return frozenset(
            target
            for key, value in zip(container.keys, container.values, strict=True)
            if key is None
            for target in _resolve_binding_expression(value, bindings)
        )
    # ``probes[0]`` over a name-bound or computed container: whatever evidence
    # the container carries is what an element may be.
    return _resolve_binding_expression(container, bindings)


def _laundered_evidence(expression: ast.AST, bindings: dict[str, BindingTargets]) -> BindingTargets:
    """Union the targets of every uncalled reference inside an unmodelled expression.

    A callee is consumed by its call — a probe reference in call position is
    inventoried as a site where it stands, and the call's *result* is
    arbitrary — so ``func`` sub-expressions are skipped while arguments are
    walked. Names bound inside the expression itself (lambda parameters,
    comprehension targets) shadow the enclosing bindings.
    """
    evidence: set[str] = set()

    def walk(node: ast.AST, shadowed: frozenset[str]) -> None:
        if isinstance(node, (ast.Name, ast.Attribute)):
            parts = _dotted_name(node)
            if parts is not None:
                if parts[0] not in shadowed:
                    evidence.update(_resolve_binding_expression(node, bindings))
                return
        if isinstance(node, ast.Call):
            for argument in node.args:
                walk(argument, shadowed)
            for keyword in node.keywords:
                walk(keyword.value, shadowed)
            return
        if isinstance(node, ast.Lambda):
            for _parameter, default in _parameter_defaults(node.args):
                walk(default, shadowed)
            walk(node.body, shadowed | argument_names(node.args))
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            first, *remaining = node.generators
            walk(first.iter, shadowed)
            inner = shadowed | {name for generator in node.generators for name in assignment_target_names(generator.target)}
            for condition in first.ifs:
                walk(condition, inner)
            for generator in remaining:
                walk(generator.iter, inner)
                for condition in generator.ifs:
                    walk(condition, inner)
            if isinstance(node, ast.DictComp):
                walk(node.key, inner)
                walk(node.value, inner)
            else:
                walk(node.elt, inner)
            return
        for child in ast.iter_child_nodes(node):
            walk(child, shadowed)

    walk(expression, frozenset())
    return frozenset(evidence)


def _bind_runtime_value(target: ast.expr, value: ast.expr, bindings: dict[str, BindingTargets]) -> None:
    if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)) and len(target.elts) == len(value.elts):
        for target_element, value_element in zip(target.elts, value.elts, strict=True):
            _bind_runtime_value(target_element, value_element, bindings)
        return
    targets = _resolve_binding_expression(value, bindings)
    for name in assignment_target_names(target):
        bindings[name] = targets or frozenset({_SHADOWED_BINDING})


def _join_binding_states(states: Sequence[dict[str, BindingTargets]]) -> dict[str, BindingTargets]:
    """Return the per-name union of ``states``.

    This is the hot path of the possible-bindings model (one merge per
    statement), so the two-state case copies the first state at C speed and
    only rebuilds the target sets that actually differ.
    """
    if len(states) == 2:
        first, second = states
        joined = dict(first)
        for name, targets in second.items():
            existing = joined.get(name)
            if existing is None:
                joined[name] = targets
            elif not targets <= existing:
                joined[name] = existing | targets
        return joined
    names = {name for state in states for name in state}
    return {name: frozenset(target for state in states for target in state.get(name, ())) for name in names}


def _target_bindings_from_iterable(
    target: ast.expr,
    iterable: ast.expr,
    bindings: dict[str, BindingTargets],
) -> dict[str, BindingTargets]:
    fallback: BindingTargets = frozenset()
    if isinstance(iterable, (ast.Tuple, ast.List, ast.Set)):
        values = iterable.elts
    elif isinstance(iterable, ast.Dict):
        values = [key for key in iterable.keys if key is not None]
    else:
        # ``for probe in probes``: an element of a name-bound or computed
        # container may be any evidence the container itself carries.
        values = []
        fallback = _resolve_binding_expression(iterable, bindings)

    def collect(candidate: ast.expr, possible_values: Sequence[ast.expr]) -> dict[str, BindingTargets]:
        if isinstance(candidate, ast.Name):
            targets = (
                frozenset(target_name for value in possible_values for target_name in _resolve_binding_expression(value, bindings))
                | fallback
            )
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
    *,
    evaluate: Callable[[ast.expr], None],
) -> dict[str, BindingTargets]:
    """Capture what each default binds at its own evaluation point.

    Defaults evaluate in :func:`_parameter_defaults` order and each is captured
    BEFORE ``evaluate`` replays it against ``bindings``, so a walrus in one
    default is visible to the defaults after it and never to the ones before.
    """
    captured: dict[str, BindingTargets] = {}
    for parameter, default in _parameter_defaults(arguments):
        targets = _resolve_binding_expression(default, bindings)
        evaluate(default)
        if targets:
            captured[parameter.arg] = targets
    return captured


def _apply_runtime_expression(expression: ast.AST, bindings: dict[str, BindingTargets]) -> None:
    if isinstance(expression, ast.Lambda):
        # Defaults evaluate in the enclosing scope when the lambda is created;
        # the body is a deferred scope and binds nothing here.
        for _parameter, default in _parameter_defaults(expression.args):
            _apply_runtime_expression(default, bindings)
        return
    if isinstance(expression, ast.NamedExpr):
        _apply_runtime_expression(expression.value, bindings)
        _bind_runtime_value(expression.target, expression.value, bindings)
        return
    for child in ast.iter_child_nodes(expression):
        _apply_runtime_expression(child, bindings)


def _walk_assignment_target(
    target: ast.expr,
    value: ast.expr | None,
    *,
    visit_expression: Callable[[ast.expr], None],
    bind_name: Callable[[ast.Name, ast.expr | None], None],
) -> None:
    if isinstance(target, (ast.Tuple, ast.List)):
        value_elements = value.elts if isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == len(target.elts) else None
        for index, element in enumerate(target.elts):
            element_value = value_elements[index] if value_elements is not None else None
            _walk_assignment_target(element, element_value, visit_expression=visit_expression, bind_name=bind_name)
        return
    if isinstance(target, ast.Starred):
        _walk_assignment_target(target.value, None, visit_expression=visit_expression, bind_name=bind_name)
        return
    if isinstance(target, ast.Name):
        bind_name(target, value)
        return
    _walk_store_target_expressions(target, visit_expression=visit_expression)


def _walk_store_target_expressions(
    target: ast.expr,
    *,
    visit_expression: Callable[[ast.expr], None],
) -> None:
    if isinstance(target, ast.Attribute):
        visit_expression(target.value)
        return
    if isinstance(target, ast.Subscript):
        visit_expression(target.value)
        visit_expression(target.slice)
        return
    if isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            _walk_store_target_expressions(element, visit_expression=visit_expression)
        return
    if isinstance(target, ast.Starred):
        _walk_store_target_expressions(target.value, visit_expression=visit_expression)


def _walk_control_target(
    target: ast.expr,
    *,
    visit_expression: Callable[[ast.expr], None],
    bind_name: Callable[[ast.Name], None],
) -> None:
    if isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            _walk_control_target(element, visit_expression=visit_expression, bind_name=bind_name)
        return
    if isinstance(target, ast.Starred):
        _walk_control_target(target.value, visit_expression=visit_expression, bind_name=bind_name)
        return
    if isinstance(target, ast.Name):
        bind_name(target)
        return
    _walk_store_target_expressions(target, visit_expression=visit_expression)


def _apply_runtime_assignment_target(
    target: ast.expr,
    value: ast.expr | None,
    bindings: dict[str, BindingTargets],
    value_bindings: dict[str, BindingTargets],
) -> None:
    def bind_name(name: ast.Name, assigned_value: ast.expr | None) -> None:
        if assigned_value is None:
            bindings[name.id] = frozenset({_SHADOWED_BINDING})
        else:
            targets = _resolve_binding_expression(assigned_value, value_bindings)
            bindings[name.id] = targets or frozenset({_SHADOWED_BINDING})

    _walk_assignment_target(
        target,
        value,
        visit_expression=lambda expression: _apply_runtime_expression(expression, bindings),
        bind_name=bind_name,
    )


def _apply_runtime_store_target_expressions(target: ast.expr, bindings: dict[str, BindingTargets]) -> None:
    _walk_store_target_expressions(
        target,
        visit_expression=lambda expression: _apply_runtime_expression(expression, bindings),
    )


def _apply_runtime_control_target(
    target: ast.expr,
    target_bindings: dict[str, BindingTargets],
    bindings: dict[str, BindingTargets],
) -> None:
    def bind_name(name: ast.Name) -> None:
        bindings[name.id] = target_bindings.get(name.id, frozenset({_SHADOWED_BINDING}))

    _walk_control_target(
        target,
        visit_expression=lambda expression: _apply_runtime_expression(expression, bindings),
        bind_name=bind_name,
    )


def _runtime_bindings_after(
    nodes: Sequence[ast.stmt],
    incoming: dict[str, BindingTargets],
    context: _ExecutionContext,
) -> dict[str, BindingTargets]:
    """Project the bindings reachable after ``nodes`` execute from ``incoming``.

    This is the deferred-body authority: a function, lambda, or generator body
    runs later, so it resolves names against every binding the enclosing
    scope can reach, not the binding at its definition point. It must agree
    with the live visitor on which expressions execute and in what order —
    definition headers, evaluated annotations, and loop back edges included.
    """
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
            value_bindings = dict(bindings)
            for assignment_target in node.targets:
                _apply_runtime_assignment_target(assignment_target, node.value, bindings, value_bindings)
        elif isinstance(node, ast.AnnAssign):
            if node.value is None:
                _apply_runtime_store_target_expressions(node.target, bindings)
            else:
                _apply_runtime_expression(node.value, bindings)
                _apply_runtime_assignment_target(node.target, node.value, bindings, dict(bindings))
            # CPython 3.12/3.13 evaluates the annotation AFTER the value is
            # stored, only for module/class variables, never under PEP 563.
            if context.variable_annotations_execute:
                _apply_runtime_expression(node.annotation, bindings)
        elif isinstance(node, ast.Delete):
            for deletion_target in node.targets:
                for name in assignment_target_names(deletion_target):
                    bindings[name] = _DEFAULT_PROBE_BINDINGS.get(name, frozenset({_SHADOWED_BINDING}))
        elif isinstance(node, (ast.AugAssign, ast.TypeAlias)):
            bound_target = node.target if isinstance(node, ast.AugAssign) else node.name
            for name in assignment_target_names(bound_target):
                bindings[name] = frozenset({_SHADOWED_BINDING})
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # Definition headers execute in the enclosing scope, in order,
            # before the name is bound; the body is its own (deferred) scope.
            for expression in definition_header_expressions(node, annotations=context.header_annotations_execute):
                _apply_runtime_expression(expression, bindings)
            bindings[node.name] = frozenset({_SHADOWED_BINDING})
        elif isinstance(node, ast.If):
            _apply_runtime_expression(node.test, bindings)
            body = _runtime_bindings_after(node.body, bindings, context)
            orelse = _runtime_bindings_after(node.orelse, bindings, context) if node.orelse else dict(bindings)
            bindings = _join_binding_states((body, orelse))
        elif isinstance(node, ast.Match):
            _apply_runtime_expression(node.subject, bindings)
            states = [dict(bindings)]
            for case in node.cases:
                case_bindings = dict(bindings)
                for name in match_pattern_binding_names(case.pattern):
                    case_bindings[name] = frozenset({_SHADOWED_BINDING})
                case_bindings.update(_pattern_capture_bindings(case.pattern, node.subject, bindings))
                states.append(_runtime_bindings_after(case.body, case_bindings, context))
            bindings = _join_binding_states(states)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            target_bindings = _loop_target_bindings(node, bindings)
            bindings = _loop_states(node, bindings, target_bindings, context).exit
        elif isinstance(node, (ast.Try, ast.TryStar)):
            states = [_runtime_bindings_after(node.body, bindings, context)]
            states.extend(_runtime_bindings_after(handler.body, bindings, context) for handler in node.handlers)
            if node.orelse:
                states.append(_runtime_bindings_after(node.orelse, states[0], context))
            bindings = _join_binding_states(states)
            if node.finalbody:
                bindings = _runtime_bindings_after(node.finalbody, bindings, context)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                _apply_runtime_expression(item.context_expr, bindings)
                if item.optional_vars is not None:
                    _apply_runtime_control_target(item.optional_vars, {}, bindings)
            bindings = _runtime_bindings_after(node.body, bindings, context)
        else:
            _apply_runtime_expression(node, bindings)
    return bindings


def _possible_runtime_bindings(
    nodes: Sequence[ast.stmt],
    incoming: dict[str, BindingTargets],
    context: _ExecutionContext,
) -> dict[str, BindingTargets]:
    """Return every binding target reachable at any point in ``nodes``."""
    return _possible_and_final_runtime_bindings(nodes, incoming, context)[0]


def _possible_and_final_runtime_bindings(
    nodes: Sequence[ast.stmt],
    incoming: dict[str, BindingTargets],
    context: _ExecutionContext,
) -> tuple[dict[str, BindingTargets], dict[str, BindingTargets]]:
    """Return ``(possible, final)``: every reachable state, and the state after ``nodes``.

    ``final`` is exactly :func:`_runtime_bindings_after` — the walk computes it
    on the way — and is returned so a loop fixpoint need not walk the body
    again for its exit state.
    """
    possible = dict(incoming)
    final = dict(incoming)
    for reachable, after in _iter_statement_states(nodes, incoming, context):
        possible = _join_binding_states((possible, reachable))
        final = after
    return possible, final


def _iter_statement_states(
    nodes: Sequence[ast.stmt],
    incoming: dict[str, BindingTargets],
    context: _ExecutionContext,
) -> Iterator[tuple[dict[str, BindingTargets], dict[str, BindingTargets]]]:
    """Yield ``(reachable, after)`` per statement: every state reachable while it runs, and the state after it."""
    current = dict(incoming)
    for node in nodes:
        reachable: dict[str, BindingTargets] = {}

        def merge(state: dict[str, BindingTargets]) -> None:
            nonlocal reachable
            reachable = _join_binding_states((reachable, state))

        if isinstance(node, ast.If):
            merge(_possible_runtime_bindings(node.body, current, context))
            merge(_possible_runtime_bindings(node.orelse, current, context))
        elif isinstance(node, ast.Match):
            for case in node.cases:
                merge(_possible_runtime_bindings(case.body, current, context))
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            working = dict(current)
            target_bindings = _loop_target_bindings(node, working)
            states = _loop_states(node, working, target_bindings, context)
            merge(states.body_possible)
            if node.orelse:
                merge(_possible_runtime_bindings(node.orelse, states.normal_exit, context))
            current = states.exit
            merge(current)
            yield reachable, current
            continue
        elif isinstance(node, (ast.Try, ast.TryStar)):
            merge(_possible_runtime_bindings(node.body, current, context))
            for handler in node.handlers:
                merge(_possible_runtime_bindings(handler.body, current, context))
            merge(_possible_runtime_bindings(node.orelse, current, context))
            merge(_possible_runtime_bindings(node.finalbody, current, context))
        current = _runtime_bindings_after((node,), current, context)
        merge(current)
        yield reachable, current


class _ClassBodyCursor:
    """Position within a class body suite while the visitor walks it.

    Answers, for a lazily evaluated annotation-scope expression at the current
    statement, which class-dict states are reachable from here on and which
    names the class dict already holds. Both are derived once per class body
    (a single projection plus suffix joins, prefix binder sets) the first time
    a lazy expression asks, so a class with many generic members stays
    linear in its length rather than re-projecting its tail per member.
    """

    __slots__ = ("_context", "_entry", "_kept_prefix", "_suffix_states", "index", "statements")

    def __init__(self, statements: Sequence[ast.stmt], entry: dict[str, BindingTargets], context: _ExecutionContext) -> None:
        self.statements = statements
        self.index = 0
        self._entry = entry
        self._context = context
        self._suffix_states: list[dict[str, BindingTargets]] | None = None
        self._kept_prefix: list[frozenset[str]] | None = None

    def states_from_current(self) -> dict[str, BindingTargets]:
        """Every class-dict state reachable while or after the current statement runs."""
        if self._suffix_states is None:
            per_statement = [reachable for reachable, _after in _iter_statement_states(self.statements, self._entry, self._context)]
            suffix: list[dict[str, BindingTargets]] = [{} for _ in per_statement]
            accumulated: dict[str, BindingTargets] = {}
            for position in range(len(per_statement) - 1, -1, -1):
                accumulated = _join_binding_states((per_statement[position], accumulated))
                suffix[position] = accumulated
            self._suffix_states = suffix
        return self._suffix_states[self.index] if self.index < len(self._suffix_states) else {}

    def names_bound_before_current(self) -> frozenset[str]:
        """Names already in the class dict at the current statement, minus any the body ever deletes."""
        if self._kept_prefix is None:
            deleted = {
                name
                for statement in self.statements
                for child in iter_own_scope(statement)
                if isinstance(child, ast.Delete)
                for target in child.targets
                for name in assignment_target_names(target)
            }
            prefix: list[frozenset[str]] = []
            bound: set[str] = set()
            for statement in self.statements:
                prefix.append(frozenset(bound - deleted))
                bound |= possibly_bound_names((statement,))[0]
            prefix.append(frozenset(bound - deleted))
            self._kept_prefix = prefix
        return self._kept_prefix[min(self.index, len(self._kept_prefix) - 1)]


@dataclass(frozen=True, slots=True)
class _LoopStates:
    """The binding states of one loop, all over-approximations.

    ``head`` is the state at the loop head on ANY iteration — the incoming
    state joined with everything the body can carry around the back edge
    (``continue`` paths included). ``entry`` is ``head`` after the per
    iteration ``for`` target store or ``while`` test. ``body_possible`` is
    every state reachable inside the body from ``entry``. ``normal_exit`` is
    the state after zero or more iterations, including a mid-body ``break``;
    ``exit`` additionally runs the ``else`` suite.
    """

    head: dict[str, BindingTargets]
    entry: dict[str, BindingTargets]
    body_possible: dict[str, BindingTargets]
    normal_exit: dict[str, BindingTargets]
    exit: dict[str, BindingTargets]


def _loop_target_bindings(node: ast.For | ast.AsyncFor | ast.While, bindings: dict[str, BindingTargets]) -> dict[str, BindingTargets]:
    """Apply the once-evaluated ``for`` iterable and return the target's element bindings."""
    if isinstance(node, ast.While):
        return {}
    _apply_runtime_expression(node.iter, bindings)
    return _target_bindings_from_iterable(node.target, node.iter, bindings)


def _loop_iteration_bindings(
    node: ast.For | ast.AsyncFor | ast.While,
    head: dict[str, BindingTargets],
    target_bindings: dict[str, BindingTargets],
) -> dict[str, BindingTargets]:
    entry = dict(head)
    if isinstance(node, ast.While):
        _apply_runtime_expression(node.test, entry)
    else:
        _apply_runtime_control_target(node.target, target_bindings, entry)
    return entry


def _loop_head_bindings(
    node: ast.For | ast.AsyncFor | ast.While,
    incoming: dict[str, BindingTargets],
    target_bindings: dict[str, BindingTargets],
    context: _ExecutionContext,
) -> tuple[dict[str, BindingTargets], dict[str, BindingTargets], dict[str, BindingTargets], dict[str, BindingTargets]]:
    """Return ``(head, entry, body_possible, body_end)`` at the loop head's fixpoint.

    A loop body is visited once, so without this a probe used before its
    rebind inside the body is invisible on every iteration after the first
    (elspeth-34ac84b4b6). The head is the least fixpoint of joining the
    body's reachable states back into the incoming state; targets form a
    finite set and the join only grows, so it terminates — normally in two
    passes, more only for alias chains that need several iterations to
    reach the probe. This is a local over-approximation on the existing
    possible-bindings model, not a CFG.
    """
    head = dict(incoming)
    while True:
        entry = _loop_iteration_bindings(node, head, target_bindings)
        body_possible, body_end = _possible_and_final_runtime_bindings(node.body, entry, context)
        candidate = _join_binding_states((head, body_possible))
        if candidate == head:
            return head, entry, body_possible, body_end
        head = candidate


def _loop_states(
    node: ast.For | ast.AsyncFor | ast.While,
    incoming: dict[str, BindingTargets],
    target_bindings: dict[str, BindingTargets],
    context: _ExecutionContext,
) -> _LoopStates:
    head, entry, body_possible, body_end = _loop_head_bindings(node, incoming, target_bindings, context)
    # An exhausted ``for`` leaves the target as it was; a ``while`` exits
    # through one more (falsy) test evaluation. ``head`` already carries every
    # mid-body state a ``break`` can leave with.
    zero_or_more = entry if isinstance(node, ast.While) else head
    normal_exit = _join_binding_states((zero_or_more, body_end))
    exit_state = (
        _join_binding_states((normal_exit, _runtime_bindings_after(node.orelse, normal_exit, context))) if node.orelse else normal_exit
    )
    return _LoopStates(head=head, entry=entry, body_possible=body_possible, normal_exit=normal_exit, exit=exit_state)


def _comprehension_elements(node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp) -> tuple[ast.expr, ...]:
    return (node.key, node.value) if isinstance(node, ast.DictComp) else (node.elt,)


def _comprehension_head_bindings(
    node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp,
    incoming: dict[str, BindingTargets],
    first_target_bindings: dict[str, BindingTargets],
) -> dict[str, BindingTargets]:
    """Return the comprehension-scope bindings at the head of ANY iteration.

    The counterpart of :func:`_loop_head_bindings` for the implicit loop of a
    comprehension: one iteration binds each generator target, then evaluates
    filters, inner iterables, and the element, and any walrus among them is
    live on the next pass.
    """
    head = dict(incoming)
    while True:
        iteration = dict(head)
        first, *remaining = node.generators
        _apply_runtime_control_target(first.target, first_target_bindings, iteration)
        for condition in first.ifs:
            _apply_runtime_expression(condition, iteration)
        for generator in remaining:
            _apply_runtime_expression(generator.iter, iteration)
            generator_bindings = _target_bindings_from_iterable(generator.target, generator.iter, iteration)
            _apply_runtime_control_target(generator.target, generator_bindings, iteration)
            for condition in generator.ifs:
                _apply_runtime_expression(condition, iteration)
        for element in _comprehension_elements(node):
            _apply_runtime_expression(element, iteration)
        candidate = _join_binding_states((head, iteration))
        if candidate == head:
            return head
        head = candidate


def _named_expression_targets(expressions: Sequence[ast.expr]) -> set[str]:
    """Return names a walrus in ``expressions`` binds in the enclosing scope."""
    return {
        name
        for expression in expressions
        for child in iter_own_scope(expression)
        if isinstance(child, ast.NamedExpr)
        for name in assignment_target_names(child.target)
    }


def _module_runtime_bindings(tree: ast.Module, context: _ExecutionContext) -> dict[str, BindingTargets]:
    """Return bindings possible whenever deferred module members may run."""
    return _possible_runtime_bindings(tree.body, dict(_DEFAULT_PROBE_BINDINGS), context)


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
        future_annotations: bool,
    ) -> None:
        self._display_path = display_path
        self._boundary_source_params = boundary_source_params
        self._module_tables = module_tables
        self._in_tests = in_tests
        self._future_annotations = future_annotations
        self._qual_stack: list[str] = []
        self._container_stack: list[str] = ["module"]
        self._binding_stack: list[tuple[str, dict[str, BindingTargets]]] = [("module", dict(_DEFAULT_PROBE_BINDINGS))]
        self._runtime_module_bindings = runtime_module_bindings
        self._source_stack: list[set[str]] = [set()]
        self._deferred_binding_stack: list[dict[str, BindingTargets]] = []
        # The class body being visited and how far into it we are: a lazily
        # evaluated annotation-scope expression immediately inside a class body
        # sees the class dict at access time — every state from its own
        # statement onward, LATER rebindings included — before it falls
        # through to the globals.
        self._class_body_stack: list[_ClassBodyCursor] = []
        # Names of every active PEP 695 type parameter; they shadow enclosing
        # names in the annotation scope and every scope nested inside it.
        self._type_parameter_stack: list[frozenset[str]] = []
        self._comprehension_mutation_stack: list[set[str]] = []
        self._assert_direct_call_ids: set[int] = set()
        self.sites: list[MasqueradeSite] = []

    # -- scope bookkeeping -------------------------------------------------

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        in_class_body = self._container_stack[-1] == "class"
        if node.type_params:
            # Bases and keywords of a generic class evaluate inside the type
            # parameter scope (they can see ``T``); the body cannot see an
            # enclosing class, but the annotation scope can.
            self._push_annotation_scope(node.type_params)
            self._visit_type_parameters(node.type_params, declared_name=node.name, in_class_body=in_class_body)
        for expression in _class_header_arguments(node):
            self.visit(expression)
        self._qual_stack.append(node.name)
        self._container_stack.append("class")
        self._push_binding_scope("class")
        self._source_stack.append(set(self._source_names))
        cursor = _ClassBodyCursor(
            node.body, dict(self._bindings), _ExecutionContext(future_annotations=self._future_annotations, function_scope=False)
        )
        self._class_body_stack.append(cursor)
        try:
            self._visit_statements(node.body, cursor=cursor)
        finally:
            self._class_body_stack.pop()
            self._source_stack.pop()
            self._pop_binding_scope()
            self._container_stack.pop()
            self._qual_stack.pop()
            if node.type_params:
                self._pop_annotation_scope()
        self._bind_unknown((node.name,))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        parent_kind = self._container_stack[-1]
        captured_defaults = self._visit_function_header(node)
        self._qual_stack.append(node.name)
        self._container_stack.append("function")
        nested_in_function = "function" in self._container_stack[:-1]
        # Type parameters are closure cells of the body: never rebound there,
        # never refreshed from the deferred enclosing state.
        local_bindings = function_local_binding_names(node) | _type_parameter_names(node.type_params)
        self._push_binding_scope("function", local_bindings=local_bindings)
        deferred_bindings = self._deferred_binding_stack[-1] if nested_in_function else self._runtime_module_bindings
        self._merge_deferred_bindings(deferred_bindings, excluding=local_bindings)
        for name, targets in captured_defaults.items():
            if name in local_bindings:
                self._bindings[name] = targets
        source_param = None if nested_in_function else self._boundary_source_params.get(id(node))
        self._source_stack.append({source_param} if source_param is not None else set())
        self._deferred_binding_stack.append(_possible_runtime_bindings(node.body, dict(self._bindings), self._context))
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
            if node.type_params:
                self._pop_annotation_scope()
        self._bind_unknown((node.name,))

    def visit_Lambda(self, node: ast.Lambda) -> None:
        captured_defaults = _default_parameter_bindings(node.args, self._bindings, evaluate=self.visit)
        self._qual_stack.append(_LAMBDA_FRAME)
        self._container_stack.append("function")
        nested_in_function = "function" in self._container_stack[:-1]
        local_bindings = argument_names(node.args)
        self._push_binding_scope("function", local_bindings=local_bindings)
        deferred_bindings = self._deferred_binding_stack[-1] if nested_in_function else self._runtime_module_bindings
        self._merge_deferred_bindings(deferred_bindings, excluding=local_bindings)
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
            self._merge_deferred_bindings(deferred_bindings, excluding=local_bindings)
        # Every iteration after the first enters through the back edge: a
        # walrus in a filter or the element rebinds names for the next pass,
        # so the head state is the fixpoint of replaying one iteration.
        self._bindings.update(_comprehension_head_bindings(node, self._bindings, first_bindings))
        rebound = _named_expression_targets(
            (*first.ifs, *(part for generator in remaining for part in (generator.iter, *generator.ifs)), *_comprehension_elements(node))
        )
        # Generator bodies execute later and close over cells whose values may
        # be rebound before iteration.  Eager list/set/dict comprehensions can
        # retain current boundary provenance; a generator cannot.
        inherited_sources = set() if isinstance(node, ast.GeneratorExp) else self._source_names - local_bindings - rebound
        self._source_stack.append(inherited_sources)
        outer_mutations: set[str] = set()
        self._comprehension_mutation_stack.append(outer_mutations)
        try:
            self._visit_control_target(first.target, first_bindings)
            for condition in first.ifs:
                self.visit(condition)
            for generator in remaining:
                self.visit(generator.iter)
                generator_bindings = _target_bindings_from_iterable(generator.target, generator.iter, self._bindings)
                self._visit_control_target(generator.target, generator_bindings)
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
        # A function-like scope sees the nearest enclosing function/module
        # frame, never a class body and never the (class-visible) type
        # parameter frame — but every active type parameter is a closure cell.
        for frame_kind, bindings in reversed(self._binding_stack):
            if frame_kind not in {"class", "annotation"}:
                scoped = dict(bindings)
                for name in (*local_bindings, *self._active_type_parameters):
                    scoped[name] = frozenset({_SHADOWED_BINDING})
                self._binding_stack.append((kind, scoped))
                return
        raise AssertionError("module binding scope is always present")

    def _pop_binding_scope(self) -> None:
        self._binding_stack.pop()

    @property
    def _active_type_parameters(self) -> frozenset[str]:
        return frozenset().union(*self._type_parameter_stack) if self._type_parameter_stack else frozenset()

    def _push_annotation_scope(self, type_params: Sequence[ast.type_param]) -> None:
        """Enter a PEP 695 annotation scope: the current frame — class body included — plus the type parameters."""
        names = _type_parameter_names(type_params)
        scoped = dict(self._bindings)
        for name in names:
            scoped[name] = frozenset({_SHADOWED_BINDING})
        self._binding_stack.append(("annotation", scoped))
        self._source_stack.append(set(self._source_names))
        self._type_parameter_stack.append(names)

    def _pop_annotation_scope(self) -> None:
        self._type_parameter_stack.pop()
        self._source_stack.pop()
        self._binding_stack.pop()

    def _merge_deferred_bindings(
        self, deferred: dict[str, BindingTargets], *, excluding: Sequence[str] | set[str] | frozenset[str] = ()
    ) -> None:
        """Widen the current frame with the bindings a later-running body may see."""
        shadowed = self._active_type_parameters
        for name, targets in deferred.items():
            if name not in excluding and name not in shadowed:
                self._bindings[name] = self._bindings.get(name, frozenset()) | targets

    def _visit_type_parameters(self, type_params: Sequence[ast.type_param], *, declared_name: str, in_class_body: bool) -> None:
        for expression in _lazy_type_parameter_expressions(type_params):
            self._visit_lazy_annotation_expression(expression, declared_name=declared_name, in_class_body=in_class_body)

    def _visit_lazy_annotation_expression(self, expression: ast.expr, *, declared_name: str, in_class_body: bool) -> None:
        """Inventory a bound, constraint, PEP 696 default, or ``type`` alias value.

        These evaluate on first access, after the declaration has completed:
        the declared name is bound by then, enclosing function locals and
        module globals may have been rebound (the deferred bindings), and an
        annotation scope immediately inside a class body sees that class's
        namespace as it stands at access time — including rebindings later in
        the body. Type parameters shadow all of that; boundary provenance
        cannot cross the deferral.
        """
        state = self._snapshot_state()
        outer_deferred = self._deferred_binding_stack[-1] if "function" in self._container_stack else self._runtime_module_bindings
        if in_class_body:
            # The class dict is consulted first, so a name the body bound
            # BEFORE this statement (and never deletes) shadows the enclosing
            # scope outright; everything the body can still do from here on
            # is a reachable class-dict state.
            cursor = self._class_body_stack[-1]
            self._merge_deferred_bindings(outer_deferred, excluding=cursor.names_bound_before_current())
            self._merge_deferred_bindings(cursor.states_from_current())
        else:
            self._merge_deferred_bindings(outer_deferred)
        self._bind_unknown((declared_name,))
        self._source_names.clear()
        self.visit(expression)
        self._restore_state(state)

    def _visit_statements(self, statements: Sequence[ast.stmt], *, cursor: _ClassBodyCursor | None = None) -> None:
        reachable = True
        for index, statement in enumerate(statements):
            if cursor is not None:
                cursor.index = index
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

    @property
    def _context(self) -> _ExecutionContext:
        return _ExecutionContext(future_annotations=self._future_annotations, function_scope=self._container_stack[-1] == "function")

    def _visit_function_header(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, BindingTargets]:
        """Visit the header in :func:`definition_header_expressions` order; return the captured defaults.

        For a generic function the annotation scope is left pushed for the
        body (type parameters are visible there) and popped by the caller.
        Defaults are evaluated OUTSIDE it, in the enclosing scope, and are
        passed in — a type parameter does not shadow them.
        """
        for decorator in node.decorator_list:
            self.visit(decorator)
        captured_defaults = _default_parameter_bindings(node.args, self._bindings, evaluate=self.visit)
        if node.type_params:
            in_class_body = self._container_stack[-1] == "class"
            self._push_annotation_scope(node.type_params)
            self._visit_type_parameters(node.type_params, declared_name=node.name, in_class_body=in_class_body)
        for annotation in _annotation_expressions(node.args, node.returns):
            if self._context.header_annotations_execute:
                self.visit(annotation)
            else:
                self._visit_unexecuted_annotation(annotation, deferred=True)
        return captured_defaults

    def _visit_unexecuted_annotation(self, annotation: ast.expr, *, deferred: bool) -> None:
        """Inventory an annotation that never executes at definition time, without replaying its effects.

        Under ``from __future__ import annotations`` — and for every
        function-local variable annotation — the expression does not run when
        the statement does, so a walrus inside it must not touch the live
        state. The probe sites in it are still lexically present: a
        stringized module/class/signature annotation can be evaluated later
        by ``typing.get_type_hints`` (``deferred``), so it is inventoried
        against the deferred bindings like any other late-running body; a
        local variable annotation is evaluated by nothing and is inventoried
        under the current state like other dead code.
        """
        state = self._snapshot_state()
        if deferred:
            self._merge_deferred_bindings(
                self._deferred_binding_stack[-1] if "function" in self._container_stack else self._runtime_module_bindings
            )
        self.visit(annotation)
        self._restore_state(state)

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
        value_bindings = dict(self._bindings)
        value_sources = set(self._source_names)
        for target in node.targets:
            self._visit_assignment_target(target, node.value, value_bindings, value_sources)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is None:
            self._visit_store_target_expressions(node.target)
        else:
            self.visit(node.value)
            self._visit_assignment_target(node.target, node.value, dict(self._bindings), set(self._source_names))
        # CPython 3.12/3.13 evaluates a variable annotation after the value is
        # stored, only outside function bodies, and never under PEP 563.
        context = self._context
        if context.variable_annotations_execute:
            self.visit(node.annotation)
        else:
            self._visit_unexecuted_annotation(node.annotation, deferred=not context.function_scope)

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
        in_class_body = self._container_stack[-1] == "class"
        declared_name = node.name.id if isinstance(node.name, ast.Name) else ""
        if node.type_params:
            self._push_annotation_scope(node.type_params)
            self._visit_type_parameters(node.type_params, declared_name=declared_name, in_class_body=in_class_body)
        # The alias value is lazy even without type parameters.
        self._visit_lazy_annotation_expression(node.value, declared_name=declared_name, in_class_body=in_class_body)
        if node.type_params:
            self._pop_annotation_scope()
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

    def _visit_assignment_target(
        self,
        target: ast.expr,
        value: ast.expr | None,
        value_bindings: dict[str, BindingTargets],
        value_sources: set[str],
    ) -> None:
        """Evaluate and apply one assignment target in CPython store order."""

        def bind_name(name: ast.Name, assigned_value: ast.expr | None) -> None:
            if assigned_value is None:
                self._bind_unknown((name.id,))
                return
            targets = _resolve_binding_expression(assigned_value, value_bindings)
            source_value = isinstance(assigned_value, ast.Name) and assigned_value.id in value_sources
            self._bind_unknown((name.id,))
            if targets:
                self._bindings[name.id] = targets
            if source_value:
                self._source_names.add(name.id)

        _walk_assignment_target(target, value, visit_expression=self.visit, bind_name=bind_name)

    def _visit_control_target(self, target: ast.expr, bindings: dict[str, BindingTargets]) -> None:
        """Evaluate a for/with target and apply its stores left to right."""

        def bind_name(name: ast.Name) -> None:
            self._bind_unknown((name.id,))
            targets = bindings.get(name.id)
            if targets:
                self._bindings[name.id] = targets

        _walk_control_target(target, visit_expression=self.visit, bind_name=bind_name)

    def _visit_store_target_expressions(self, target: ast.expr) -> None:
        """Visit only expressions Python evaluates while storing ``target``."""
        _walk_store_target_expressions(target, visit_expression=self.visit)

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
        target_bindings: dict[str, BindingTargets] = {}
        rebound = _named_expression_targets((node.test,)) if isinstance(node, ast.While) else set(assignment_target_names(node.target))
        if not isinstance(node, ast.While):
            self.visit(node.iter)
            target_bindings = _target_bindings_from_iterable(node.target, node.iter, self._bindings)
        # Every iteration after the first enters through the back edge, so the
        # body (and a ``while`` test) is visited under the loop-head fixpoint,
        # and boundary provenance is dropped for any name the loop can rebind.
        head, _entry, _body_possible, _body_end = _loop_head_bindings(node, self._bindings, target_bindings, self._context)
        body_rebound, clears_all = possibly_bound_names(node.body)
        self._restore_state((head, set() if clears_all else self._source_names - rebound - body_rebound))
        if isinstance(node, ast.While):
            self.visit(node.test)
        start = self._snapshot_state()
        if not isinstance(node, ast.While):
            self._visit_control_target(node.target, target_bindings)
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
                self._visit_control_target(item.optional_vars, {})
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
