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
from jinja2.visitor import NodeVisitor

from elspeth.contracts.trust_boundary import trust_boundary


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


class _DefiniteBindingAnalyzer(NodeVisitor):
    """Conservative flow analysis for Jinja locals relevant to candidates.

    Dispatch runs through jinja2's own ``NodeVisitor`` (one ``visit_<Class>``
    per concrete node class). Every visitor takes the set of names definitely
    bound before the node and returns the set definitely bound after it;
    unhandled nodes scan their children without binding anything.
    """

    def __init__(self, candidates: frozenset[str]) -> None:
        self._candidates = candidates
        self.unbound: set[str] = set()
        self.seen: set[str] = set()

    def analyze(self, statements: Iterable[nodes.Node], bound: frozenset[str]) -> frozenset[str]:
        current = bound
        for statement in statements:
            current = self.visit(statement, current)
        return current

    def generic_visit(self, node: nodes.Node, bound: frozenset[str]) -> frozenset[str]:
        self._scan_children(node, bound)
        return bound

    def visit_Name(self, node: nodes.Name, bound: frozenset[str]) -> frozenset[str]:
        if node.ctx == "load" and node.name in self._candidates:
            self.seen.add(node.name)
            if node.name not in bound:
                self.unbound.add(node.name)
        return bound

    def visit_Assign(self, node: nodes.Assign, bound: frozenset[str]) -> frozenset[str]:
        self._scan(node.node, bound)
        self._scan_assignment_target(node.target, bound)
        return bound | _stored_names(node.target)

    def visit_AssignBlock(self, node: nodes.AssignBlock, bound: frozenset[str]) -> frozenset[str]:
        if node.filter is not None:
            self._scan(node.filter, bound)
        self.analyze(node.body, bound)
        self._scan_assignment_target(node.target, bound)
        return bound | _stored_names(node.target)

    def visit_If(self, node: nodes.If, bound: frozenset[str]) -> frozenset[str]:
        self._scan(node.test, bound)
        branch_results = [self.analyze(node.body, bound)]
        for elif_node in node.elif_:
            self._scan(elif_node.test, bound)
            branch_results.append(self.analyze(elif_node.body, bound))
        branch_results.append(self.analyze(node.else_, bound) if node.else_ else bound)
        return frozenset.intersection(*branch_results)

    def visit_For(self, node: nodes.For, bound: frozenset[str]) -> frozenset[str]:
        self._scan(node.iter, bound)
        loop_bound = bound | _stored_names(node.target) | {"loop"}
        if node.test is not None:
            self._scan(node.test, loop_bound)
        self.analyze(node.body, loop_bound)
        self.analyze(node.else_, bound)
        return bound

    def visit_With(self, node: nodes.With, bound: frozenset[str]) -> frozenset[str]:
        for value in node.values:
            self._scan(value, bound)
        local_bound = bound
        for target in node.targets:
            local_bound |= _stored_names(target)
        self.analyze(node.body, local_bound)
        return bound

    def visit_Macro(self, node: nodes.Macro, bound: frozenset[str]) -> frozenset[str]:
        for default in node.defaults:
            self._scan(default, bound)
        argument_names = frozenset(argument.name for argument in node.args)
        self.analyze(node.body, bound | argument_names | {"caller", "kwargs", "varargs"})
        return bound | {node.name}

    def visit_CallBlock(self, node: nodes.CallBlock, bound: frozenset[str]) -> frozenset[str]:
        self._scan(node.call, bound)
        for default in node.defaults:
            self._scan(default, bound)
        argument_names = frozenset(argument.name for argument in node.args)
        self.analyze(node.body, bound | argument_names)
        return bound

    def visit_Import(self, node: nodes.Import, bound: frozenset[str]) -> frozenset[str]:
        self._scan(node.template, bound)
        return bound | {node.target}

    @trust_boundary(
        tier=3,
        source=(
            "a jinja2 FromImport AST node produced by the sandboxed template compiler "
            "from operator-authored template text — jinja2, not ELSPETH, owns its shape"
        ),
        source_param="node",
        suppresses=("R5",),
        invariant=(
            "admits only str entries and (name, alias) 2-tuples with a str alias from "
            "node.names; any other shape raises TemplateError instead of silently "
            "mis-computing the definitely-bound name set"
        ),
        test_ref="tests/unit/plugins/infrastructure/test_templates.py::test_from_import_binding_rejects_malformed_names",
        test_fingerprint="8d52b5b569a25d169595912e4e57b424b6f07c34ec5245e9b098d79e670c04e7",
    )
    def visit_FromImport(self, node: nodes.FromImport, bound: frozenset[str]) -> frozenset[str]:
        self._scan(node.template, bound)
        imported_names: set[str] = set()
        for item in node.names:
            if isinstance(item, str):
                imported_names.add(item)
            elif isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], str):
                imported_names.add(item[1])
            else:
                raise TemplateError(f"jinja2 FromImport name entry has an unsupported shape: {item!r}")
        return bound | imported_names

    def visit_FilterBlock(self, node: nodes.FilterBlock, bound: frozenset[str]) -> frozenset[str]:
        self._scan(node.filter, bound)
        self.analyze(node.body, bound)
        return bound

    def visit_OverlayScope(self, node: nodes.OverlayScope, bound: frozenset[str]) -> frozenset[str]:
        self._scan(node.context, bound)
        self.analyze(node.body, bound)
        return bound

    def visit_ScopedEvalContextModifier(self, node: nodes.ScopedEvalContextModifier, bound: frozenset[str]) -> frozenset[str]:
        for option in node.options:
            self._scan(option, bound)
        self.analyze(node.body, bound)
        return bound

    def visit_Block(self, node: nodes.Block, bound: frozenset[str]) -> frozenset[str]:
        self.analyze(node.body, bound)
        return bound

    def visit_Scope(self, node: nodes.Scope, bound: frozenset[str]) -> frozenset[str]:
        self.analyze(node.body, bound)
        return bound

    def _scan(self, node: nodes.Node, bound: frozenset[str]) -> None:
        self.visit(node, bound)

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
