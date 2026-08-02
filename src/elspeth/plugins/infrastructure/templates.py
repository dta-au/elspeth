"""Shared Jinja2 template infrastructure for transform plugins.

Provides a sandboxed Jinja2 environment factory and the TemplateError exception.
Used by both LLM prompt templates and RAG query templates.

The sandbox prevents attribute access, method calls, and module imports.
It does NOT limit CPU or memory consumption from template loops — templates
are authored by pipeline architects (trusted config), not end users.
"""

from __future__ import annotations

from collections.abc import Iterable

from jinja2 import StrictUndefined, nodes
from jinja2.meta import find_undeclared_variables
from jinja2.sandbox import ImmutableSandboxedEnvironment


class TemplateError(Exception):
    """Error in template rendering (including sandbox violations)."""


def create_sandboxed_environment() -> ImmutableSandboxedEnvironment:
    """Create an ImmutableSandboxedEnvironment with StrictUndefined.

    Returns:
        A sandboxed Jinja2 environment that:
        - Raises on undefined variables (StrictUndefined)
        - Blocks attribute access and method calls (ImmutableSandboxedEnvironment)
        - Does not HTML-escape output (autoescape=False)
    """
    return ImmutableSandboxedEnvironment(
        undefined=StrictUndefined,
        autoescape=False,
    )


def find_runtime_unbound_variables(ast: nodes.Template) -> frozenset[str]:
    """Return names that may require render context on a reachable path.

    Jinja's ``find_undeclared_variables`` deliberately reports names assigned
    in conditional branches because its code-generation analysis merges all
    branch stores. That is too broad for ELSPETH's StrictUndefined preflight:
    a local assigned in every branch is defined when a later interpolation
    runs. Keep Jinja's conservative candidate set, then remove a candidate only
    when a path- and order-sensitive walk proves it bound at every load.
    """
    candidates = frozenset(find_undeclared_variables(ast))
    analyzer = _DefiniteBindingAnalyzer(candidates)
    analyzer.analyze(ast.body, frozenset())
    return frozenset(analyzer.unbound | (candidates - analyzer.seen))


class _DefiniteBindingAnalyzer:
    """Conservative flow analysis for Jinja locals relevant to candidates."""

    def __init__(self, candidates: frozenset[str]) -> None:
        self._candidates = candidates
        self.unbound: set[str] = set()
        self.seen: set[str] = set()

    def analyze(self, statements: Iterable[nodes.Node], bound: frozenset[str]) -> frozenset[str]:
        current = bound
        for statement in statements:
            current = self._analyze_node(statement, current)
        return current

    def _analyze_node(self, node: nodes.Node, bound: frozenset[str]) -> frozenset[str]:
        if isinstance(node, nodes.Name):
            if node.ctx == "load" and node.name in self._candidates:
                self.seen.add(node.name)
                if node.name not in bound:
                    self.unbound.add(node.name)
            return bound

        if isinstance(node, nodes.Assign):
            self._scan(node.node, bound)
            self._scan_assignment_target(node.target, bound)
            return bound | _stored_names(node.target)

        if isinstance(node, nodes.AssignBlock):
            if node.filter is not None:
                self._scan(node.filter, bound)
            self.analyze(node.body, bound)
            self._scan_assignment_target(node.target, bound)
            return bound | _stored_names(node.target)

        if isinstance(node, nodes.If):
            return self._analyze_if(node, bound)

        if isinstance(node, nodes.For):
            self._scan(node.iter, bound)
            loop_bound = bound | _stored_names(node.target) | {"loop"}
            if node.test is not None:
                self._scan(node.test, loop_bound)
            self.analyze(node.body, loop_bound)
            self.analyze(node.else_, bound)
            return bound

        if isinstance(node, nodes.With):
            for value in node.values:
                self._scan(value, bound)
            local_bound = bound
            for target in node.targets:
                local_bound |= _stored_names(target)
            self.analyze(node.body, local_bound)
            return bound

        if isinstance(node, nodes.Macro):
            for default in node.defaults:
                self._scan(default, bound)
            argument_names = frozenset(argument.name for argument in node.args)
            self.analyze(node.body, bound | argument_names | {"caller", "kwargs", "varargs"})
            return bound | {node.name}

        if isinstance(node, nodes.CallBlock):
            self._scan(node.call, bound)
            for default in node.defaults:
                self._scan(default, bound)
            argument_names = frozenset(argument.name for argument in node.args)
            self.analyze(node.body, bound | argument_names)
            return bound

        if isinstance(node, nodes.Import):
            self._scan(node.template, bound)
            return bound | {node.target}

        if isinstance(node, nodes.FromImport):
            self._scan(node.template, bound)
            imported_names = {item[1] if isinstance(item, tuple) else item for item in node.names}
            return bound | imported_names

        if isinstance(node, nodes.FilterBlock):
            self._scan(node.filter, bound)
            self.analyze(node.body, bound)
            return bound

        if isinstance(node, nodes.OverlayScope):
            self._scan(node.context, bound)
            self.analyze(node.body, bound)
            return bound

        if isinstance(node, nodes.ScopedEvalContextModifier):
            for option in node.options:
                self._scan(option, bound)
            self.analyze(node.body, bound)
            return bound

        if isinstance(node, (nodes.Block, nodes.Scope)):
            self.analyze(node.body, bound)
            return bound

        self._scan_children(node, bound)
        return bound

    def _analyze_if(self, node: nodes.If, bound: frozenset[str]) -> frozenset[str]:
        self._scan(node.test, bound)
        branch_results = [self.analyze(node.body, bound)]
        for elif_node in node.elif_:
            self._scan(elif_node.test, bound)
            branch_results.append(self.analyze(elif_node.body, bound))
        branch_results.append(self.analyze(node.else_, bound) if node.else_ else bound)
        return frozenset.intersection(*branch_results)

    def _scan(self, node: nodes.Node, bound: frozenset[str]) -> None:
        self._analyze_node(node, bound)

    def _scan_children(self, node: nodes.Node, bound: frozenset[str]) -> None:
        for child in node.iter_child_nodes():
            self._scan(child, bound)

    def _scan_assignment_target(self, target: nodes.Node, bound: frozenset[str]) -> None:
        if isinstance(target, nodes.NSRef) and target.name in self._candidates:
            self.seen.add(target.name)
            if target.name not in bound:
                self.unbound.add(target.name)


def _stored_names(target: nodes.Node) -> frozenset[str]:
    if isinstance(target, nodes.Name) and target.ctx in {"param", "store"}:
        return frozenset({target.name})
    return frozenset(child.name for child in target.find_all(nodes.Name) if child.ctx in {"param", "store"})
