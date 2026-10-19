"""Composer exception-channel rule implementation.

The invariant: a tool handler must not let a bare ``TypeError`` /
``ValueError`` / ``UnicodeError`` reach the compose loop as its channel for
LLM-argument failures — that channel is :class:`ToolArgumentError`, or a
locally caught error turned into a failure result.

The composer tool planes are written in the sanctioned "catch locally" shape:
a private helper (often a ``@trust_boundary`` Tier-3 parser whose
*declared* contract is ``raises ValueError``) raises, and the handler that
calls it catches and converts. A raise-site-only check cannot see that catch,
so it either flags every helper or is scoped so narrowly it checks nothing
(elspeth-24ba2e24fa: the filter named ``tools.py`` for three months after it
became a package). This rule therefore follows each bare raise along the
module-local call graph and reports it only when some path lets it ESCAPE:

* a raise lexically inside a ``try`` whose handlers catch that exception (or
  a base class of it, or everything) is contained;
* a raise in a helper is contained when EVERY module-local call to that helper
  is guarded the same way — transitively, through intermediate helpers;
* a raise in a function with no module-local caller (a public handler, or a
  helper only reached from another module) escapes — the rule cannot see a
  catch it cannot find, so it fails closed;
* ``__post_init__`` is exempt: a frozen-dataclass validator raising
  ``TypeError`` is ADR-032 Tier-1 nominal typing of an owned type, never the
  LLM-argument channel, and per the ``ToolArgumentError`` contract a bug of
  that kind MUST crash.

A direct negative nominal guard may also protect an internal contract in a
normal function. Only the proven raise site is excluded: its subject must be
a closed owned-carrier parameter or a direct result of a typed callable
parameter. Ambiguous bindings and unsupported syntax retain the finding.
"""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from elspeth_lints.core.protocols import Finding, RuleContext, RuleMetadata, RuleScope
from elspeth_lints.rules.composer.exception_channel.metadata import LEGACY_RULE_ID, RULE_ID, RULE_METADATA, SUGGESTION

_BANNED = frozenset({"TypeError", "ValueError", "UnicodeError", "UnicodeDecodeError", "UnicodeEncodeError"})
_CATCH_ALL = frozenset({"Exception", "BaseException"})
# Base classes a handler may name to contain a banned exception.
_CONTAINING_BASES: dict[str, frozenset[str]] = {
    "TypeError": frozenset(),
    "ValueError": frozenset(),
    "UnicodeError": frozenset({"ValueError"}),
    "UnicodeDecodeError": frozenset({"UnicodeError", "ValueError"}),
    "UnicodeEncodeError": frozenset({"UnicodeError", "ValueError"}),
}
# Tier-1 nominal invariants of owned types (ADR-032); never the LLM channel.
_EXEMPT_FUNCTIONS = frozenset({"__post_init__"})
_SCOPE_BOUNDARIES: tuple[type[ast.AST], ...] = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


@dataclass(frozen=True, slots=True)
class ComposerExceptionChannelRule:
    """Detect bare TypeError/ValueError/UnicodeError raises that can escape a composer tool handler."""

    id: str = RULE_ID
    scope: RuleScope = RuleScope.INCREMENTAL
    metadata: RuleMetadata = RULE_METADATA

    def analyze(self, tree: ast.AST, file_path: Path, context: RuleContext) -> list[Finding]:
        """Analyze one composer tool module."""
        return find_exception_channel_findings(tree, display_path(file_path, context.root))


@dataclass(slots=True)
class _RaiseSite:
    line: int
    name: str
    guards: frozenset[str]


@dataclass(slots=True)
class _FunctionFacts:
    raises: list[_RaiseSite] = field(default_factory=list)
    # (callee name, exception names guarded at the call site)
    calls: list[tuple[str, frozenset[str]]] = field(default_factory=list)


def find_exception_channel_findings(tree: ast.AST, file_path: str) -> list[Finding]:
    """Return CEC1 findings for one parsed module: bare raises that can escape it."""
    aliases = _exception_aliases(tree)
    functions = _module_functions(tree)
    nominal_invariants = _nominal_invariant_raises(tree)
    facts = {name: _collect_facts(node, aliases, functions, nominal_invariants) for name, node in functions.items()}
    callers: dict[str, list[tuple[str, frozenset[str]]]] = {}
    for caller, caller_facts in facts.items():
        for callee, guards in caller_facts.calls:
            callers.setdefault(callee, []).append((caller, guards))

    findings: list[Finding] = []
    for name, function_facts in facts.items():
        if name in _EXEMPT_FUNCTIONS:
            continue
        for site in function_facts.raises:
            if _contained(site.name, site.guards):
                continue
            if _escapes(name, site.name, callers, frozenset()):
                findings.append(_finding(file_path=file_path, line=site.line, name=site.name))
    return sorted(findings, key=lambda finding: finding.line)


def _module_functions(tree: ast.AST) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every def in the module, keyed by bare name (first definition wins).

    Methods share the namespace with module-level functions: a call ``f(x)``
    is resolved by name only, which is the conservative direction — an
    unresolvable call is simply not a guard for anything.
    """
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.setdefault(node.name, node)
    return functions


def _collect_facts(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    aliases: dict[str, str],
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    nominal_invariants: frozenset[ast.Raise],
) -> _FunctionFacts:
    facts = _FunctionFacts()
    for node, guards in _walk_own_scope(function, frozenset()):
        if isinstance(node, ast.Raise) and node.exc is not None:
            name = _raise_exception_name(node.exc, aliases)
            if name in _BANNED and node not in nominal_invariants:
                facts.raises.append(_RaiseSite(line=node.lineno, name=name, guards=guards))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in functions:
            facts.calls.append((node.func.id, guards))
    return facts


def _scope_bindings(scope: ast.AST) -> dict[str, list[ast.AST]]:
    """Collect syntactic writes in one namespace, without entering child scopes."""
    bindings: dict[str, list[ast.AST]] = {}

    def bind(name: str, node: ast.AST) -> None:
        bindings.setdefault(name, []).append(node)

    def visit(node: ast.AST) -> None:
        if node is not scope and isinstance(node, _SCOPE_BOUNDARIES):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bind(node.name, node)
                for decorator in node.decorator_list:
                    visit(decorator)
            # Definition headers can write into the enclosing namespace. Child
            # parameters and bodies still belong to their separate namespaces.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                for argument in ast.iter_child_nodes(node.args):
                    if isinstance(argument, ast.arg):
                        if argument.annotation is not None:
                            visit(argument.annotation)
                    else:
                        visit(argument)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
                    visit(node.returns)
            elif isinstance(node, ast.ClassDef):
                for expression in (*node.bases, *node.keywords):
                    visit(expression)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bind(node.id, node)
        elif isinstance(node, ast.arg):
            bind(node.arg, node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bind(alias.asname or alias.name.split(".")[0], node)
        elif isinstance(node, ast.ExceptHandler) and node.name is not None:
            bind(node.name, node)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                bind(name, node)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name is not None:
            bind(node.name, node)
        elif isinstance(node, ast.MatchMapping) and node.rest is not None:
            bind(node.rest, node)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(scope)
    return bindings


def _exact_import(bindings: dict[str, list[ast.AST]], name: str, modules: frozenset[str], module: ast.Module) -> bool:
    sites = bindings.get(name, [])
    return (
        len(sites) == 1
        and isinstance(sites[0], ast.ImportFrom)
        and sites[0] in module.body
        and sites[0].level == 0
        and sites[0].module in modules
        and any(alias.name == name and alias.asname is None for alias in sites[0].names)
    )


def _owned_dataclasses(module: ast.Module, bindings: dict[str, list[ast.AST]]) -> frozenset[str]:
    """Prove unique concrete frozen dataclasses, not arbitrary local class names."""
    if not _exact_import(bindings, "dataclass", frozenset({"dataclasses"}), module):
        return frozenset()
    owned: set[str] = set()
    for node in module.body:
        if not isinstance(node, ast.ClassDef) or bindings[node.name] != [node] or node.bases or node.keywords or node.type_params:
            continue
        frozen = False
        dataclass_seen = False
        supported = True
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Name) and decorator.id == "final":
                supported &= _exact_import(bindings, "final", frozenset({"typing"}), module)
            elif (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Name)
                and decorator.func.id == "dataclass"
                and not decorator.args
                and not dataclass_seen
            ):
                dataclass_seen = True
                keywords = {keyword.arg: keyword.value for keyword in decorator.keywords}
                supported &= len(keywords) == len(decorator.keywords) and set(keywords) <= {"frozen", "slots"}
                supported &= all(isinstance(value, ast.Constant) and type(value.value) is bool for value in keywords.values())
                frozen_value = keywords.get("frozen")
                frozen = isinstance(frozen_value, ast.Constant) and frozen_value.value is True
            else:
                supported = False
        if supported and frozen:
            owned.add(node.name)
    return frozenset(owned)


def _closed_owned_annotation(annotation: ast.expr | None, owned: frozenset[str]) -> frozenset[str]:
    if isinstance(annotation, ast.Name) and annotation.id in owned:
        return frozenset({annotation.id})
    if isinstance(annotation, ast.Constant) and annotation.value is None:
        return frozenset({"None"})
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        left = _closed_owned_annotation(annotation.left, owned)
        right = _closed_owned_annotation(annotation.right, owned)
        if left and right:
            return left | right
    return frozenset()


def _negative_nominal_subject(test: ast.expr, owned: frozenset[str]) -> tuple[str, frozenset[str], bool] | None:
    """Recognize only exact rejection forms, with an optional same-name None guard."""
    optional_subject: str | None = None
    if isinstance(test, ast.BoolOp):
        if not isinstance(test.op, ast.And) or len(test.values) != 2:
            return None
        first, test = test.values
        if not (
            isinstance(first, ast.Compare)
            and isinstance(first.left, ast.Name)
            and len(first.ops) == len(first.comparators) == 1
            and isinstance(first.ops[0], ast.IsNot)
            and isinstance(first.comparators[0], ast.Constant)
            and first.comparators[0].value is None
        ):
            return None
        optional_subject = first.left.id
    if not (
        isinstance(test, ast.Compare)
        and len(test.ops) == len(test.comparators) == 1
        and isinstance(test.left, ast.Call)
        and isinstance(test.left.func, ast.Name)
        and test.left.func.id == "type"
        and len(test.left.args) == 1
        and not test.left.keywords
        and isinstance(test.left.args[0], ast.Name)
    ):
        return None
    subject = test.left.args[0].id
    if optional_subject is not None and optional_subject != subject:
        return None
    comparator = test.comparators[0]
    if isinstance(test.ops[0], ast.IsNot) and isinstance(comparator, ast.Name):
        classes = frozenset({comparator.id})
    elif isinstance(test.ops[0], ast.NotIn) and isinstance(comparator, ast.Tuple) and comparator.elts:
        if not all(isinstance(element, ast.Name) for element in comparator.elts):
            return None
        classes = frozenset(element.id for element in comparator.elts if isinstance(element, ast.Name))
        if len(classes) != len(comparator.elts):
            return None
    else:
        return None
    if not classes <= owned:
        return None
    return subject, classes, optional_subject is not None


def _internal_subject_contract(
    subject: str,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    guard: ast.If,
    bindings: dict[str, list[ast.AST]],
    module_bindings: dict[str, list[ast.AST]],
    owned: frozenset[str],
    module: ast.Module,
) -> frozenset[str]:
    """A bounded declaration check, not arbitrary alias/taint or control-flow inference."""
    if len(bindings.get(subject, [])) != 1:
        return frozenset()
    parameters = {arg.arg: arg for arg in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)}
    if subject in parameters:
        return _closed_owned_annotation(parameters[subject].annotation, owned)
    for statement in function.body[: function.body.index(guard)]:
        if not (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id == subject
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name)
        ):
            continue
        resolver = statement.value.func.id
        if resolver not in parameters or len(bindings.get(resolver, [])) != 1:
            return frozenset()
        annotation = parameters[resolver].annotation
        if not (
            isinstance(annotation, ast.Subscript)
            and isinstance(annotation.value, ast.Name)
            and annotation.value.id == "Callable"
            and _exact_import(module_bindings, "Callable", frozenset({"collections.abc", "typing"}), module)
            and isinstance(annotation.slice, ast.Tuple)
            and len(annotation.slice.elts) == 2
            and (
                isinstance(annotation.slice.elts[0], ast.List)
                or (isinstance(annotation.slice.elts[0], ast.Constant) and annotation.slice.elts[0].value is Ellipsis)
            )
        ):
            return frozenset()
        return _closed_owned_annotation(annotation.slice.elts[1], owned)
    return frozenset()


def _nominal_invariant_raises(tree: ast.AST) -> frozenset[ast.Raise]:
    """Exclude only proven direct internal TypeError sites; all uncertainty stays red."""
    if not isinstance(tree, ast.Module):
        return frozenset()
    module_bindings = _scope_bindings(tree)
    globals_written = {name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names}
    protected = {"type", "TypeError", "dataclass", "final", "Callable"}
    if "*" in module_bindings or {"type", "TypeError"} & module_bindings.keys() or protected & globals_written:
        return frozenset()
    owned = _owned_dataclasses(tree, module_bindings) - globals_written
    proven: set[ast.Raise] = set()
    for function in tree.body:
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if module_bindings[function.name] != [function] or function.decorator_list or function.type_params:
            continue
        bindings = _scope_bindings(function)
        if (protected | owned) & bindings.keys():
            continue
        for statement in function.body:
            if not isinstance(statement, ast.If) or len(statement.body) != 1 or statement.orelse:
                continue
            raised = statement.body[0]
            if not (
                isinstance(raised, ast.Raise)
                and raised.cause is None
                and isinstance(raised.exc, ast.Call)
                and isinstance(raised.exc.func, ast.Name)
                and raised.exc.func.id == "TypeError"
            ):
                continue
            guard = _negative_nominal_subject(statement.test, owned)
            if guard is None:
                continue
            subject, classes, optional = guard
            contract = _internal_subject_contract(subject, function, statement, bindings, module_bindings, owned, tree)
            if classes <= contract and (not optional or "None" in contract):
                proven.add(raised)
    return frozenset(proven)


def _walk_own_scope(node: ast.AST, guards: frozenset[str]) -> Iterator[tuple[ast.AST, frozenset[str]]]:
    """Yield (node, exception names guarded by enclosing try blocks) within one function scope."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _SCOPE_BOUNDARIES):
            continue
        if isinstance(child, ast.Try):
            body_guards = guards | _handler_names(child.handlers)
            for statement in child.body:
                yield statement, body_guards
                yield from _walk_own_scope(statement, body_guards)
            outside_body: tuple[ast.AST, ...] = (*child.handlers, *child.orelse, *child.finalbody)
            for node_outside_body in outside_body:
                yield node_outside_body, guards
                yield from _walk_own_scope(node_outside_body, guards)
            continue
        yield child, guards
        yield from _walk_own_scope(child, guards)


def _handler_names(handlers: list[ast.ExceptHandler]) -> frozenset[str]:
    names: set[str] = set()
    for handler in handlers:
        if handler.type is None:
            names |= _CATCH_ALL
            continue
        elements = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        for element in elements:
            dotted = _dotted_name(element)
            if dotted is not None:
                names.add(dotted.rsplit(".", 1)[-1])
    return frozenset(names)


def _contained(exception: str, guards: frozenset[str]) -> bool:
    return bool(guards & (_CATCH_ALL | {exception} | _CONTAINING_BASES[exception]))


def _escapes(
    function: str,
    exception: str,
    callers: dict[str, list[tuple[str, frozenset[str]]]],
    visited: frozenset[str],
) -> bool:
    """Whether ``exception`` raised unguarded inside ``function`` can leave the module."""
    if function in visited:
        return False  # recursion: no new path
    if function in _EXEMPT_FUNCTIONS:
        return False
    call_sites = callers.get(function)
    if not call_sites:
        return True  # public handler, or reached only from outside this module: fail closed
    return any(
        not _contained(exception, guards) and _escapes(caller, exception, callers, visited | {function}) for caller, guards in call_sites
    )


def _raise_exception_name(exc: ast.expr, aliases: dict[str, str]) -> str | None:
    if isinstance(exc, ast.Call):
        return _exception_reference_name(exc.func, aliases)
    return _exception_reference_name(exc, aliases)


def _exception_aliases(tree: ast.AST) -> dict[str, str]:
    """Return simple aliases to banned builtins exception classes."""
    aliases: dict[str, str] = {"builtins": "builtins"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "builtins":
                    aliases[alias.asname or alias.name] = "builtins"
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "builtins":
            for alias in node.names:
                if alias.name in _BANNED:
                    aliases[alias.asname or alias.name] = alias.name
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if value is None:
                continue
            resolved = _exception_reference_name(value, aliases)
            if resolved not in _BANNED:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            for target in targets:
                if isinstance(target, ast.Name):
                    aliases[target.id] = resolved
    return aliases


def _exception_reference_name(expr: ast.expr, aliases: dict[str, str]) -> str | None:
    if isinstance(expr, ast.Name):
        return _canonical_exception_name(aliases.get(expr.id, expr.id))
    if isinstance(expr, ast.Attribute):
        dotted = _dotted_name(expr)
        if dotted is None:
            return None
        parts = dotted.split(".")
        if parts[0] in aliases:
            dotted = ".".join((aliases[parts[0]], *parts[1:]))
        return _canonical_exception_name(dotted)
    return None


def _dotted_name(expr: ast.expr) -> str | None:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        base = _dotted_name(expr.value)
        if base is None:
            return None
        return f"{base}.{expr.attr}"
    return None


def _canonical_exception_name(name: str) -> str | None:
    if name in _BANNED:
        return name
    if name.startswith("builtins."):
        candidate = name.rsplit(".", 1)[-1]
        if candidate in _BANNED:
            return candidate
    return None


def _finding(*, file_path: str, line: int, name: str) -> Finding:
    fingerprint_payload = f"{LEGACY_RULE_ID}|{file_path}|{line}|{name}"
    return Finding(
        rule_id=LEGACY_RULE_ID,
        file_path=file_path,
        line=line,
        column=0,
        message=f"raise {name}(...) at {file_path}:{line} can escape the tool handler — use ToolArgumentError",
        fingerprint=hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest()[:16],
        severity=RULE_METADATA.severity,
        suggestion=SUGGESTION,
    )


def display_path(file_path: Path, root: Path) -> str:
    """Return a path relative to root when possible."""
    try:
        return file_path.relative_to(root).as_posix()
    except ValueError:
        pass
    try:
        return file_path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return file_path.as_posix()


RULE = ComposerExceptionChannelRule()
