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
    facts = {name: _collect_facts(node, aliases, functions) for name, node in functions.items()}
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
) -> _FunctionFacts:
    facts = _FunctionFacts()
    for node, guards in _walk_own_scope(function, frozenset()):
        if isinstance(node, ast.Raise) and node.exc is not None:
            name = _raise_exception_name(node.exc, aliases)
            if name in _BANNED:
                facts.raises.append(_RaiseSite(line=node.lineno, name=name, guards=guards))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in functions:
            facts.calls.append((node.func.id, guards))
    return facts


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
